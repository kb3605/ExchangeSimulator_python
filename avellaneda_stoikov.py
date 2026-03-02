#!/usr/bin/env python3
"""
Avellaneda-Stoikov Market Maker

Implements the optimal market-making strategy from Avellaneda & Stoikov (2008).
Continuously quotes bid and ask prices adjusted for inventory risk, trading
against the liquidity provider's synthetic order flow.

The model computes (in dollar units):
  - Reservation price:  r = s - q * gamma * sigma^2 * (T - t)
  - Optimal spread:     delta = gamma * sigma^2 * (T - t) + (2/gamma) * ln(1 + gamma/k)
  - Quotes:             bid = r - delta/2,  ask = r + delta/2

Fills are tracked via the EMS (message callback on the order connection).
On Ctrl+C, plots inventory, PnL, and bid/ask prices to PDF.

Usage:
    Terminal 1: python exchange_server.py
    Terminal 2: python liquidity_provider.py --mid-price 100.00 --throttle 50000
    Terminal 3: python avellaneda_stoikov.py --gamma 0.01 --order-size 100 -v
"""

import argparse
import math
import signal
import sys
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from typing import List, Optional

from order_client_with_fsm_with_md import OrderClientWithFSMAndMarketData, BBO
from order_client_with_fsm import ManagedOrder, ExchangeMessage
from order_fsm_final import OrderEvent
from messages import DEFAULT_TTL_SECONDS

TICK_SIZE = 100       # $0.01 in LOBSTER format
PRICE_SCALE = 10000   # LOBSTER units per dollar


# ---------------------------------------------------------------------------
# Alpha signals
# ---------------------------------------------------------------------------

class Signal(ABC):
    """Base class for directional alpha adjustments (dollars)."""

    @abstractmethod
    def update(self, bbo: BBO, now: float) -> None:
        """Called on each BBO change with the new best-bid/offer and timestamp."""

    @abstractmethod
    def alpha(self) -> float:
        """Return current alpha adjustment in dollars."""


class OFISignal(Signal):
    """
    Two-regime book-imbalance signal derived from markout regression.

    Computes imbalance I = (bid_size - ask_size) / (bid_size + ask_size).
    When I crosses into the top or bottom quintile, a power-law alpha
    a * delta_ms^b is applied (converted from mils to dollars).
    """

    def __init__(self, top_params=(17.42, 0.468), bot_params=(-35.64, 0.457),
                 top_edge=0.6, bot_edge=-0.333, max_delta_ms=1.0):
        self._a_top, self._b_top = top_params
        self._a_bot, self._b_bot = bot_params
        self._top_edge = top_edge
        self._bot_edge = bot_edge
        self._max_delta_ms = max_delta_ms

        self._regime = 0          # -1 = bottom, 0 = neutral, +1 = top
        self._regime_start = 0.0  # time.time() when regime entered
        self._now = 0.0           # latest timestamp

    def update(self, bbo: BBO, now: float) -> None:
        self._now = now
        if bbo.bid_size is None or bbo.ask_size is None:
            return
        total = bbo.bid_size + bbo.ask_size
        if total == 0:
            return
        imb = (bbo.bid_size - bbo.ask_size) / total

        if imb >= self._top_edge:
            new_regime = 1
        elif imb <= self._bot_edge:
            new_regime = -1
        else:
            new_regime = 0

        if new_regime != self._regime:
            self._regime = new_regime
            self._regime_start = now

    def alpha(self) -> float:
        if self._regime == 0:
            return 0.0
        delta_ms = (self._now - self._regime_start) * 1000.0
        if delta_ms >= self._max_delta_ms:
            return 0.0   # signal fully decayed
        delta_ms = max(delta_ms, 0.0)
        # Remaining alpha = cumulative move still ahead: F(max) - F(now)
        # where F(h) = a * h^b is the markout-regression cumulative price move.
        if self._regime == 1:
            remaining = (self._max_delta_ms ** self._b_top
                         - delta_ms ** self._b_top)
            return self._a_top * remaining * 1e-4
        else:
            remaining = (self._max_delta_ms ** self._b_bot
                         - delta_ms ** self._b_bot)
            return self._a_bot * remaining * 1e-4

    def __repr__(self):
        return (f"OFISignal(top=({self._a_top}, {self._b_top}), "
                f"bot=({self._a_bot}, {self._b_bot}), "
                f"edges=[{self._bot_edge}, {self._top_edge}], "
                f"max_delta_ms={self._max_delta_ms})")


# ---------------------------------------------------------------------------
# Time-series data
# ---------------------------------------------------------------------------

@dataclass
class Snapshot:
    t: float          # seconds since start
    mid: float        # dollars
    bid: float        # market best bid, dollars
    ask: float        # market best ask, dollars
    our_bid: float    # our quote bid, dollars (0 if not quoting)
    our_ask: float    # our quote ask, dollars (0 if not quoting)
    inventory: int
    cash: float       # dollars
    pnl: float        # mark-to-market, dollars
    alpha: float = 0.0  # signal alpha adjustment, dollars


@dataclass
class FillRecord:
    t: float
    side: str         # 'B' or 'S'
    size: int
    price: float      # dollars
    inventory_after: int


# ---------------------------------------------------------------------------
# Market maker
# ---------------------------------------------------------------------------

class AvellanedaStoikov(OrderClientWithFSMAndMarketData):
    """
    Avellaneda-Stoikov optimal market maker.

    All AS model math is done in dollar units.  Sigma is estimated from
    mid-price returns sampled at fixed intervals (not every BBO tick) to
    avoid microstructure noise.
    """

    def __init__(self, host='localhost', order_port=10000, md_port=10002,
                 gamma=0.01, k=20.0, k_auto=False, k_min=5.0, horizon=300.0,
                 tau_fixed: Optional[float] = None,
                 sigma_window=100,
                 max_inventory=500, order_size=100, min_requote=0.1,
                 warmup=5.0,
                 verbose=False, output='as_market_maker.pdf',
                 plot_alpha: bool = False, price_margin: float = 0.05,
                 signals: Optional[List[Signal]] = None):
        super().__init__(host, order_port, md_port)

        self._signals = signals or []
        self._gamma = gamma
        self._k = k
        self._k_auto = k_auto
        self._horizon = horizon
        self._tau_fixed = tau_fixed  # if set, τ is pinned to this value (stationary mode)
        self._sigma_window = sigma_window
        self._max_inventory = max_inventory
        self._order_size = order_size
        self._min_requote = min_requote
        self._warmup = warmup
        self._verbose = verbose
        self._output = output
        self._plot_alpha = plot_alpha
        self._price_margin = price_margin

        # State — protected by _state_lock for cross-thread access
        self._state_lock = threading.Lock()
        self._inventory = 0
        self._cash = 0.0          # dollars
        self._bid_order: Optional[ManagedOrder] = None
        self._ask_order: Optional[ManagedOrder] = None
        self._sigma = 0.01        # dollars, conservative default
        self._t0 = 0.0
        self._session_start = 0.0
        self._last_requote_time = 0.0

        # Prevent concurrent _update_quotes from MD thread and fill callback
        self._quote_lock = threading.Lock()

        # Sigma estimation: sample mid at fixed intervals, not every tick
        self._sigma_sample_interval = 1.0   # seconds between samples
        self._last_sigma_sample_time = 0.0
        self._sigma_samples: deque = deque(maxlen=sigma_window)  # dollars

        # Dynamic k estimation: count executions per second from market data
        self._k_trade_count = 0             # trades in current window
        self._k_window_start = 0.0          # start of current counting window
        self._k_window_sec = 5.0            # window length in seconds
        self._k_ema_alpha = 0.3             # EMA smoothing factor
        self._k_min = k_min                 # floor to avoid divide-by-zero spreads

        # Time-series
        self._snapshots: List[Snapshot] = []
        self._fills: List[FillRecord] = []
        self._data_lock = threading.Lock()

        # Exchange time tracking (from LOBSTER message timestamps)
        self._exchange_time: float = 0.0   # latest exchange timestamp (seconds since midnight)
        self._exchange_t0: float = 0.0     # first exchange timestamp seen

        # Stats
        self._n_fills = 0
        self._n_requotes = 0

    # ------------------------------------------------------------------
    # BBO hook
    # ------------------------------------------------------------------

    def on_bbo_change(self, old_bbo: BBO, new_bbo: BBO) -> None:
        if new_bbo.mid_price is None:
            return

        mid_d = new_bbo.mid_price / PRICE_SCALE
        now = time.time()

        # Quote protection: if the market has moved so that our standing quote
        # now crosses the opposite side, cancel immediately rather than waiting
        # for the next requote cycle.  This is the primary defence against fills
        # at worse-than-opposite-side prices.  cancel_order() is fire-and-forget
        # so it is safe to call here from the MD callback thread.
        if (new_bbo.ask_price is not None
                and self._bid_order is not None and self._bid_order.is_live
                and not self._bid_order.pending_cancel
                and not self._bid_order.pending_modify
                and self._bid_order.price >= new_bbo.ask_price):
            ltime = self._exchange_time if self._exchange_time != 0.0 else None
            if self._verbose:
                print(f"[PROTECT] bid ${self._bid_order.price/PRICE_SCALE:.2f}"
                      f" >= ask ${new_bbo.ask_price/PRICE_SCALE:.2f}"
                      f" — cancel eid={self._bid_order.exchange_order_id}")
            self.cancel_order(self._bid_order, logical_time=ltime)

        if (new_bbo.bid_price is not None
                and self._ask_order is not None and self._ask_order.is_live
                and not self._ask_order.pending_cancel
                and not self._ask_order.pending_modify
                and self._ask_order.price <= new_bbo.bid_price):
            if self._verbose:
                print(f"[PROTECT] ask ${self._ask_order.price/PRICE_SCALE:.2f}"
                      f" <= bid ${new_bbo.bid_price/PRICE_SCALE:.2f} — cancelling")
            ltime = self._exchange_time if self._exchange_time != 0.0 else None
            self.cancel_order(self._ask_order, logical_time=ltime)

        # Lone-quote protection: if the BBO just moved to our resting price and
        # we appear to be the only size there, cancel immediately.  The tell is
        # that the old BBO had a better price on that side (we were behind the
        # market) and the new BBO is at our price with size <= our order size
        # (all other liquidity at that level has gone).  We filter out our own
        # size by checking ask_size <= our size rather than ask_size == 0.
        ltime = self._exchange_time if self._exchange_time != 0.0 else None
        if (self._ask_order is not None and self._ask_order.is_live
                and not self._ask_order.pending_modify
                and not self._ask_order.pending_cancel
                and new_bbo.ask_price is not None and new_bbo.ask_size is not None
                and new_bbo.ask_price == self._ask_order.price
                and new_bbo.ask_size <= self._ask_order.size
                and old_bbo.ask_price is not None
                and old_bbo.ask_price < self._ask_order.price):
            if self._verbose:
                print(f"[PULL] ask ${self._ask_order.price/PRICE_SCALE:.2f} exposed alone"
                      f" (book={new_bbo.ask_size} <= ours={self._ask_order.size})")
            self.cancel_order(self._ask_order, logical_time=ltime)
            # Do NOT clear _ask_order — wait for CANCEL_ACK to drive is_live=False,
            # then _update_quotes_locked will resubmit naturally.

        if (self._bid_order is not None and self._bid_order.is_live
                and not self._bid_order.pending_modify
                and not self._bid_order.pending_cancel
                and new_bbo.bid_price is not None and new_bbo.bid_size is not None
                and new_bbo.bid_price == self._bid_order.price
                and new_bbo.bid_size <= self._bid_order.size
                and old_bbo.bid_price is not None
                and old_bbo.bid_price > self._bid_order.price):
            if self._verbose:
                print(f"[PULL] bid ${self._bid_order.price/PRICE_SCALE:.2f} exposed alone"
                      f" (book={new_bbo.bid_size} <= ours={self._bid_order.size})")
            self.cancel_order(self._bid_order, logical_time=ltime)

        # Sample mid for sigma at fixed intervals to filter microstructure noise
        if now - self._last_sigma_sample_time >= self._sigma_sample_interval:
            self._last_sigma_sample_time = now
            self._update_sigma(mid_d)

        for sig in self._signals:
            sig.update(new_bbo, now)

        self._record_snapshot(new_bbo)

        # Don't quote during warmup — the book may not reflect the real
        # market yet (e.g. historical replay still building initial depth)
        if now - self._t0 < self._warmup:
            return

        if now - self._last_requote_time >= self._min_requote:
            self._update_quotes(new_bbo)

    # ------------------------------------------------------------------
    # Dynamic k estimation (trade arrival rate from market data)
    # ------------------------------------------------------------------

    def on_market_data(self, lobster_line: str) -> None:
        """Hook called for every raw LOBSTER line from the market-data feed.

        Tracks exchange time from LOBSTER timestamps; estimates k from total
        message rate when --k-auto is enabled.
        """
        # Extract exchange timestamp from the first LOBSTER field
        try:
            ts = float(lobster_line.split(',')[0])
            if self._exchange_t0 == 0.0:
                self._exchange_t0 = ts
            self._exchange_time = ts
        except (ValueError, IndexError):
            pass

        if not self._k_auto:
            return

        now = time.time()
        if self._k_window_start == 0.0:
            self._k_window_start = now

        self._k_trade_count += 1

        elapsed = now - self._k_window_start
        if elapsed >= self._k_window_sec:
            rate = self._k_trade_count / elapsed
            with self._state_lock:
                self._k = max(self._k_min,
                              self._k_ema_alpha * rate +
                              (1 - self._k_ema_alpha) * self._k)
            if self._verbose:
                print(f"[K-AUTO] msgs/s={rate:.1f}  k={self._k:.1f}")
            self._k_trade_count = 0
            self._k_window_start = now

    # ------------------------------------------------------------------
    # Volatility estimation (dollar units, sampled at fixed intervals)
    # ------------------------------------------------------------------

    def _update_sigma(self, mid_d: float) -> None:
        self._sigma_samples.append(mid_d)

        if len(self._sigma_samples) < 5:
            return  # keep default sigma until we have enough data

        # Simple rolling std of returns
        prices = list(self._sigma_samples)
        returns = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
        n = len(returns)
        mean_ret = sum(returns) / n
        var = sum((r - mean_ret) ** 2 for r in returns) / n

        # Convert to per-second volatility (samples are sigma_sample_interval apart)
        var_per_sec = var / self._sigma_sample_interval

        raw_sigma = math.sqrt(var_per_sec) if var_per_sec > 0 else 0.001

        # Cap sigma at 0.1% of mid price — prevents explosion from book noise
        mid_approx = prices[-1]
        sigma_cap = mid_approx * 0.001  # $0.10 for a $100 stock

        with self._state_lock:
            self._sigma = max(min(raw_sigma, sigma_cap), 0.001)

    # ------------------------------------------------------------------
    # AS quote computation (all in dollars)
    # ------------------------------------------------------------------

    def _compute_quotes(self, bbo: BBO):
        mid = bbo.mid_price
        if mid is None:
            return None

        s = mid / PRICE_SCALE  # to dollars

        now = time.time()
        if self._tau_fixed is not None:
            tau = self._tau_fixed
        else:
            elapsed = now - self._session_start
            tau = max(self._horizon - elapsed, 1.0)
            if elapsed >= self._horizon:
                self._session_start = now
                tau = self._horizon

        with self._state_lock:
            q = self._inventory
            sigma = self._sigma
            k = self._k

        gamma = self._gamma

        # Signal alpha (dollars)
        alpha_total = sum(sig.alpha() for sig in self._signals)

        # Reservation price (dollars)
        inv_adjust = q * gamma * sigma * sigma * tau
        r = s + alpha_total - inv_adjust

        # Optimal spread (dollars)
        # As gamma -> 0, the spread formula reduces to 2/k
        if gamma > 0:
            spread = gamma * sigma * sigma * tau + (2.0 / gamma) * math.log(1 + gamma / k)
        else:
            spread = 2.0 / k

        # Floor spread at 2 ticks ($0.02), cap at 2% of mid price
        tick_d = TICK_SIZE / PRICE_SCALE
        min_spread = 2 * tick_d
        max_spread = s * 0.02
        spread = max(min_spread, min(spread, max_spread))

        bid_d = r - spread / 2.0
        ask_d = r + spread / 2.0

        # Clamp quotes to the current BBO: never improve on the best bid or ask.
        # Using tick-aligned integers from the book avoids banker's-rounding
        # artifacts that occur when clamping to the half-tick mid price.
        mkt_bid = (bbo.bid_price / PRICE_SCALE) if bbo.bid_price else s - tick_d
        mkt_ask = (bbo.ask_price / PRICE_SCALE) if bbo.ask_price else s + tick_d

        # Clamp to the current BBO: never place inside the spread.
        # Using mkt_bid/mkt_ask (not mid) ensures we are always passive —
        # our bid joins or trails the best bid, our ask joins or trails the
        # best ask.  Clamping to mid instead lets us quote inside the spread,
        # making us the first target for aggressive orders and dramatically
        # increasing adverse selection.
        bid_d = min(bid_d, mkt_bid)
        ask_d = max(ask_d, mkt_ask)

        # Convert to LOBSTER and snap to tick grid.
        # Use directed rounding: floor bids, ceiling asks.  This is critical
        # when mid_price is a half-tick (e.g. 100.005): Python's banker's
        # round(10000.5) = 10000 would produce ask_price = market_bid, creating
        # a crossing order that fills immediately at below-mid price.
        # A small epsilon absorbs float-representation noise for on-tick values.
        _eps = 1e-9
        bid_price = int(bid_d * PRICE_SCALE / TICK_SIZE + _eps) * TICK_SIZE
        ask_price = math.ceil(ask_d * PRICE_SCALE / TICK_SIZE - _eps) * TICK_SIZE

        # Final sanity: bid and ask must be positive and bid < ask
        if bid_price <= 0:
            bid_price = TICK_SIZE
        if ask_price <= bid_price:
            ask_price = bid_price + TICK_SIZE

        return bid_price, ask_price

    # ------------------------------------------------------------------
    # Quote management
    # ------------------------------------------------------------------

    def _update_quotes(self, bbo: BBO) -> None:
        if not self._quote_lock.acquire(blocking=False):
            return  # another thread is already requoting

        try:
            self._update_quotes_locked(bbo)
        finally:
            self._quote_lock.release()

    def _update_quotes_locked(self, bbo: BBO) -> None:
        result = self._compute_quotes(bbo)
        if result is None:
            return

        bid_price, ask_price = result
        self._last_requote_time = time.time()
        self._n_requotes += 1

        # Safety clamp: maintain a two-tick buffer between our quotes and the
        # opposite BBO.  A one-tick buffer (bid < ask) is insufficient: after
        # PROTECT cancels our bid at ask-price, the next requote fires when
        # ask has ticked back up by one tick.  With a one-tick guard the
        # strategy MODIFYs the bid right back to mkt_bid (= ask - 1 tick),
        # and PROTECT fires again the next time the ask ticks down — an
        # infinite loop.  A two-tick buffer keeps bid ≤ ask - 2 ticks, so a
        # single-tick ask move toward us does NOT trigger PROTECT.
        if bbo.ask_price is not None and bid_price >= bbo.ask_price - TICK_SIZE:
            if self._verbose:
                print(f"[GUARD] computed bid ${bid_price/PRICE_SCALE:.2f}"
                      f" within 1 tick of bbo ask ${bbo.ask_price/PRICE_SCALE:.2f} — clamping")
            bid_price = bbo.ask_price - 2 * TICK_SIZE  # two-tick clearance from ask
        if bbo.bid_price is not None and ask_price <= bbo.bid_price + TICK_SIZE:
            if self._verbose:
                print(f"[GUARD] computed ask ${ask_price/PRICE_SCALE:.2f}"
                      f" within 1 tick of bbo bid ${bbo.bid_price/PRICE_SCALE:.2f} — clamping")
            ask_price = bbo.bid_price + 2 * TICK_SIZE  # two-tick clearance from bid

        with self._state_lock:
            inv = self._inventory

        quote_bid = True
        quote_ask = True

        # Hard stop at absolute inventory limits only.  The AS model's
        # reservation-price skewing (r = s - q*gamma*sigma²*tau) already
        # discourages fills on the high-inventory side by pushing quotes away
        # from the market.  An earlier half-max cutoff caused the bid to
        # disappear entirely from the book once inventory crossed 50% of the
        # limit — the strategy appeared to "update" the quote (via [QUOTE]
        # log) while nothing was actually in the exchange.
        if inv >= self._max_inventory:
            quote_bid = False       # don't buy more at absolute max inventory
        if inv <= -self._max_inventory:
            quote_ask = False       # don't sell more at absolute min inventory

        # Scale order size down as inventory grows (linear taper)
        inv_ratio = abs(inv) / max(self._max_inventory, 1)
        size_scale = max(0.1, 1.0 - inv_ratio)
        bid_size = max(10, int(self._order_size * (size_scale if inv >= 0 else 1.0)))
        ask_size = max(10, int(self._order_size * (1.0 if inv >= 0 else size_scale)))

        ltime = self._exchange_time if self._exchange_time != 0.0 else None

        # --- Stale-modify detection and recovery ---
        # When a MODIFY is sent the exchange must respond with MODIFY_ACK or
        # REJECT within 2 seconds.  If it does not, pending_modify's property
        # auto-clears silently, the next _update_quotes_locked sees the price
        # is still wrong, and re-sends the SAME MODIFY to the SAME (stale)
        # order ID — which again gets no response.  This loops forever: the
        # bid price in the exchange never changes while the log shows
        # continuous [SEND] modify activity.
        #
        # Fix: detect the timeout BEFORE the property clears the raw flag,
        # cancel the unresponsive order, and let the normal CANCEL_ACK path
        # resubmit a fresh quote at the correct price.
        for _stale_order, _stale_tag in ((self._bid_order, 'BID'), (self._ask_order, 'ASK')):
            if (_stale_order is not None
                    and _stale_order.is_live
                    and _stale_order._pending_modify       # raw flag before property clears it
                    and time.time() - _stale_order._pending_modify_time
                        > ManagedOrder._PENDING_TIMEOUT):
                _elapsed = time.time() - _stale_order._pending_modify_time
                print(f"[STALE] {_stale_tag} MODIFY not ACK'd in {_elapsed:.1f}s"
                      f" (eid={_stale_order.exchange_order_id})"
                      f" — cancelling to recover stuck quote")
                # Clear the stale modify flag (cancel_order only checks
                # is_live, not pending_modify, so this is safe).  Keep
                # _bid/_ask_order pointing at the order so the pending_cancel
                # guard below prevents a double-submit this cycle.  The
                # CANCEL_ACK (or REJECT) will arrive via _on_fill, which
                # sets _bid/_ask_order = None and kicks off a fresh requote.
                _stale_order._pending_modify = False
                if self._pending_modify_order is _stale_order:
                    self._pending_modify_order = None
                self.cancel_order(_stale_order, logical_time=ltime)

        # --- Orphan detection ---
        # If an order shows is_live=True but its exchange_order_id is no longer
        # registered in the client's order table, the client and exchange have
        # become desynchronised.  This can happen when:
        #   - A MODIFY_ACK was lost / never arrived (pending_modify timed out),
        #     but the exchange already replaced the order with a new ID.
        #   - A CANCEL_ACK was lost, and the order expired on the exchange.
        #   - A REJECT cleared pending_modify but the order was already gone.
        # In any of these cases _bid_order.exchange_order_id is stale.  Any
        # subsequent cancel or modify using that ID will be rejected, making the
        # stuck quote permanent.  Detect this here and force the FSM into
        # CANCELLED so the normal resubmit path takes over.
        for _ref_attr in ('_bid_order', '_ask_order'):
            _o = getattr(self, _ref_attr)
            if _o is not None and _o.is_live and not _o.pending_modify and not _o.pending_cancel:
                _oid = _o.exchange_order_id
                if _oid is None or self.get_order(_oid) is not _o:
                    # Order is live in FSM but not tracked in _orders — orphaned.
                    if self._verbose:
                        _side = "bid" if _ref_attr == '_bid_order' else "ask"
                        print(f"[ORPHAN] {_side} order {_oid} not in order table "
                              f"(is_live=True) — forcing CANCELLED to recover")
                    try:
                        _o._fsm.process_event(OrderEvent.CANCEL_ACK)
                    except Exception:
                        pass
                    setattr(self, _ref_attr, None)

        # --- Bid side ---
        if self._bid_order is not None and self._bid_order.is_live:
            if not quote_bid:
                if self._verbose:
                    print(f"[SEND] cancel bid eid={self._bid_order.exchange_order_id}"
                          f" (inv limit)")
                self.cancel_order(self._bid_order, logical_time=ltime)
                self._bid_order = None
            elif self._bid_order.pending_cancel:
                if self._verbose:
                    print(f"[SKIP] bid eid={self._bid_order.exchange_order_id}"
                          f" pending_cancel — waiting for CANCEL_ACK")
                quote_bid = False
            elif self._bid_order.pending_modify:
                if self._verbose:
                    print(f"[SKIP] bid eid={self._bid_order.exchange_order_id}"
                          f" pending_modify — waiting for MODIFY_ACK")
                quote_bid = False
            elif self._bid_order.price != bid_price or self._bid_order.size != bid_size:
                if self._verbose:
                    print(f"[SEND] modify bid eid={self._bid_order.exchange_order_id}"
                          f" ${self._bid_order.price/PRICE_SCALE:.2f}"
                          f" → ${bid_price/PRICE_SCALE:.2f}")
                if not self.modify_order(self._bid_order, size=bid_size, price=bid_price,
                                         user="client", logical_time=ltime):
                    if self._verbose:
                        print(f"[FAIL] modify_order returned False for bid"
                              f" eid={self._bid_order.exchange_order_id}")
                    self._bid_order = None
                else:
                    quote_bid = False
            else:
                quote_bid = False   # already correct
        if quote_bid and (self._bid_order is None or not self._bid_order.is_live):
            order = self.create_order("limit", bid_size, bid_price, "B",
                                      DEFAULT_TTL_SECONDS)
            order.logical_time = ltime
            if self._verbose:
                print(f"[SEND] new bid ${bid_price/PRICE_SCALE:.2f} size={bid_size}")
            if self.submit_order_sync(order, timeout=0.5):
                if self._verbose:
                    print(f"[ACK]  new bid eid={order.exchange_order_id}"
                          f" ${bid_price/PRICE_SCALE:.2f}")
                self._bid_order = order
            else:
                if self._verbose:
                    print(f"[TIMEOUT] new bid ${bid_price/PRICE_SCALE:.2f}"
                          f" — no ACK within 0.5s")
                self._bid_order = None

        # --- Ask side ---
        if self._ask_order is not None and self._ask_order.is_live:
            if not quote_ask:
                self.cancel_order(self._ask_order, logical_time=ltime)
                self._ask_order = None
            elif self._ask_order.pending_cancel:
                quote_ask = False   # cancel in-flight; wait for CANCEL_ACK
            elif self._ask_order.pending_modify:
                quote_ask = False   # modify in-flight; wait for MODIFY_ACK before sending another
            elif self._ask_order.price != ask_price or self._ask_order.size != ask_size:
                # Atomic modify — no window for stale fills
                if not self.modify_order(self._ask_order, size=ask_size, price=ask_price,
                                         user="client", logical_time=ltime):
                    self._ask_order = None  # modify failed; re-submit below
                else:
                    quote_ask = False   # modify sent; order still live
            else:
                quote_ask = False
        if quote_ask and (self._ask_order is None or not self._ask_order.is_live):
            order = self.create_order("limit", ask_size, ask_price, "S",
                                      DEFAULT_TTL_SECONDS)
            order.logical_time = ltime
            if self.submit_order_sync(order, timeout=0.5):
                self._ask_order = order
            else:
                self._ask_order = None

        if self._verbose:
            mid_d = bbo.mid_price / PRICE_SCALE if bbo.mid_price else 0
            with self._state_lock:
                sigma = self._sigma
                inv = self._inventory
            alpha_total = sum(sig.alpha() for sig in self._signals)
            alpha_str = f"  alpha=${alpha_total:.6f}" if self._signals else ""
            print(f"[QUOTE] mid=${mid_d:.2f}  "
                  f"bid=${bid_price / PRICE_SCALE:.2f}  "
                  f"ask=${ask_price / PRICE_SCALE:.2f}  "
                  f"spread=${(ask_price - bid_price) / PRICE_SCALE:.4f}  "
                  f"inv={inv}  sigma=${sigma:.4f}{alpha_str}")

    # ------------------------------------------------------------------
    # Fill callback (EMS)
    # ------------------------------------------------------------------

    def _on_fill(self, order: ManagedOrder, msg: ExchangeMessage) -> None:
        if self._verbose:
            side = "BID" if order.side == 'B' else "ASK"
            eid = order.exchange_order_id
            price = order.price / PRICE_SCALE if order.price else 0
            is_bid = (order is self._bid_order)
            is_ask = (order is self._ask_order)
            tracked = "bid_order" if is_bid else ("ask_order" if is_ask else "untracked")
            print(f"[RECV] {msg.msg_type} for {side} eid={eid} price=${price:.2f} "
                  f"({tracked}) is_live={order.is_live}")
        if msg.msg_type in ("CANCEL_ACK", "EXPIRED"):
            # Order removed from book; bypass rate limiter.
            self._last_requote_time = 0
            # For CANCEL_ACK: the exchange broadcasts the UDP DELETE *before*
            # sending the TCP CANCEL_ACK, so the BBO-change event has already
            # fired (with pending_cancel=True) and was correctly skipped.
            # For EXPIRED: the exchange broadcasts the UDP DELETE via the TTL
            # checker thread, so the BBO-change event may also have already
            # fired.
            # In both cases, without a subsequent market-data event there is
            # nothing to trigger the resubmission, leaving the strategy with
            # no live quote.  Fix: kick off a requote from a new thread.  We
            # must not call _update_quotes directly here (receive thread)
            # because submit_order_sync would deadlock waiting on _pending_event.
            if time.time() - self._t0 >= self._warmup:
                def _do_requote():
                    self._update_quotes(self.get_bbo())
                threading.Thread(
                    target=_do_requote,
                    daemon=True, name="requote-after-cancel",
                ).start()
            return

        if msg.msg_type == "REJECT":
            # A REJECT was attributed to this order's in-flight modify or cancel
            # by _process_message (which already cleared pending_modify /
            # pending_cancel on the order).  We still need to decide what to do:
            #
            # - Modify REJECT: the exchange rejected the cancel-replace because
            #   the order ID was not found.  Two sub-cases:
            #   (a) The order was already cancelled/filled on the exchange before
            #       our modify arrived.  The FSM still shows is_live=True but the
            #       order is dead.  Forcibly mark it as dead client-side so the
            #       strategy can resubmit.
            #   (b) The order genuinely exists but we sent the wrong ID (bug).
            #       Treat the same way — mark dead and resubmit.
            #
            # - Cancel REJECT: the order is still live on the exchange (cancel
            #   was rejected).  No special action needed; the strategy will
            #   retry on the next BBO change.
            #
            # We distinguish the two cases via order.is_live: if is_live is
            # still True after the REJECT is processed, the FSM thinks the order
            # is alive.  Whether the reject was for a modify or cancel we check
            # by looking at which pending flag was cleared.  The _pending_modify
            # flag was already cleared by _process_message before calling here;
            # _pending_cancel likewise.
            #
            # Strategy: if the order has is_live=True after a REJECT, check
            # whether its exchange_order_id is still registered in self._orders.
            # If not, the order has been orphaned — mark it dead and kick a
            # requote thread.
            if order.is_live:
                oid = order.exchange_order_id
                order_still_tracked = (oid is not None and self.get_order(oid) is order)
                if not order_still_tracked:
                    # The order is no longer tracked (already removed from
                    # _orders, e.g. filled or cancelled via another path).
                    # Force the FSM into CANCELLED so is_live becomes False
                    # and the strategy stops trying to modify/cancel it.
                    try:
                        order._fsm.process_event(OrderEvent.CANCEL_ACK)
                    except Exception:
                        pass
                    if self._verbose:
                        side_str = "bid" if order.side == 'B' else "ask"
                        print(f"[REJECT-RECOVER] {side_str} order {oid} orphaned "
                              f"after REJECT — forcing CANCELLED, will resubmit")
                    if time.time() - self._t0 >= self._warmup:
                        def _do_requote_reject():
                            self._update_quotes(self.get_bbo())
                        threading.Thread(
                            target=_do_requote_reject,
                            daemon=True, name="requote-after-reject",
                        ).start()
                else:
                    # Order is still tracked and alive; just kick a fresh
                    # requote so PROTECT / normal requote logic re-evaluates.
                    self._last_requote_time = 0
            return

        if msg.msg_type not in ("FILL", "PARTIAL_FILL"):
            return
        if msg.size is None or msg.price is None:
            return

        fill_size = msg.size
        fill_price_d = msg.price / PRICE_SCALE

        # Keep order._size current so the lone-quote BBO check stays accurate
        if msg.msg_type == "PARTIAL_FILL" and msg.remainder_size is not None:
            order._size = msg.remainder_size

        with self._state_lock:
            if order.side == 'B':
                self._inventory += fill_size
                self._cash -= fill_size * fill_price_d
            else:
                self._inventory -= fill_size
                self._cash += fill_size * fill_price_d
            inv = self._inventory
            cash = self._cash

        self._n_fills += 1

        with self._data_lock:
            self._fills.append(FillRecord(
                t=time.time() - self._t0,
                side=order.side,
                size=fill_size,
                price=fill_price_d,
                inventory_after=inv,
            ))

        if self._verbose:
            side_str = "BUY" if order.side == 'B' else "SELL"
            print(f"[FILL] {side_str} {fill_size} @ ${fill_price_d:.2f}  "
                  f"inv={inv}  cash=${cash:.2f}")

        # Clear fully-filled order reference
        if msg.msg_type == "FILL":
            if self._bid_order is not None and order.exchange_order_id == self._bid_order.exchange_order_id:
                self._bid_order = None
            if self._ask_order is not None and order.exchange_order_id == self._ask_order.exchange_order_id:
                self._ask_order = None

        # Force re-quote on next BBO change.  We must NOT call _update_quotes
        # here because we are on the async receive thread — submit_order_sync
        # would deadlock (it waits on _pending_event which only this thread
        # can set).  Setting _last_requote_time = 0 ensures the next
        # on_bbo_change (from the MD thread) triggers a re-quote immediately.
        self._last_requote_time = 0

    # ------------------------------------------------------------------
    # Time-series recording
    # ------------------------------------------------------------------

    def _record_snapshot(self, bbo: BBO) -> None:
        mid = bbo.mid_price
        if mid is None:
            return

        mid_d = mid / PRICE_SCALE

        our_bid = 0.0
        our_ask = 0.0
        if self._bid_order and self._bid_order.is_live:
            our_bid = self._bid_order.price / PRICE_SCALE
        if self._ask_order and self._ask_order.is_live:
            our_ask = self._ask_order.price / PRICE_SCALE

        with self._state_lock:
            inv = self._inventory
            cash = self._cash
        pnl = cash + inv * mid_d

        alpha_total = sum(sig.alpha() for sig in self._signals)

        snap = Snapshot(
            t=time.time() - self._t0,
            mid=mid_d,
            bid=(bbo.bid_price / PRICE_SCALE) if bbo.bid_price is not None else mid_d,
            ask=(bbo.ask_price / PRICE_SCALE) if bbo.ask_price is not None else mid_d,
            our_bid=our_bid,
            our_ask=our_ask,
            inventory=inv,
            cash=cash,
            pnl=pnl,
            alpha=alpha_total,
        )

        with self._data_lock:
            self._snapshots.append(snap)

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(self) -> None:
        print(f"Avellaneda-Stoikov Market Maker")
        k_str = (f"k={self._k} (auto, min={self._k_min})" if self._k_auto
                 else f"k={self._k}")
        print(f"  gamma={self._gamma}  {k_str}  horizon={self._horizon}s")
        print(f"  order_size={self._order_size}  max_inventory={self._max_inventory}")
        print(f"  sigma_window={self._sigma_window}  min_requote={self._min_requote}s  warmup={self._warmup}s")
        if self._signals:
            for sig in self._signals:
                print(f"  signal: {sig}")
        print(f"Press Ctrl+C to stop.\n")

        if not self.connect():
            sys.exit(1)

        self.set_message_callback(self._on_fill)
        self.start()
        self._t0 = time.time()
        self._session_start = self._t0

        try:
            status_interval = 5.0
            last_status = time.time()

            while True:
                time.sleep(0.5)

                now = time.time()
                if now - last_status >= status_interval:
                    last_status = now
                    bbo = self.get_bbo()
                    mid = bbo.mid_price
                    if mid is not None:
                        mid_d = mid / PRICE_SCALE
                        with self._state_lock:
                            inv = self._inventory
                            cash = self._cash
                            sigma = self._sigma
                            k_now = self._k
                        pnl = cash + inv * mid_d
                        if self._tau_fixed is not None:
                            tau = self._tau_fixed
                        else:
                            elapsed = now - self._session_start
                            tau = max(self._horizon - elapsed, 0)
                        alpha_total = sum(sig.alpha() for sig in self._signals)
                        alpha_str = f"  alpha=${alpha_total:.6f}" if self._signals else ""
                        k_str = f"  k={k_now:.1f}" if self._k_auto else ""
                        print(f"[STATUS] mid=${mid_d:.2f}  inv={inv}  "
                              f"pnl=${pnl:.2f}  sigma=${sigma:.4f}  "
                              f"tau={tau:.0f}s  fills={self._n_fills}  "
                              f"requotes={self._n_requotes}{alpha_str}{k_str}")

        except KeyboardInterrupt:
            print(f"\nShutting down...")

            # Cancel outstanding orders to prevent fill flood during shutdown
            self.set_message_callback(None)
            ltime = self._exchange_time if self._exchange_time != 0.0 else None
            if self._bid_order and self._bid_order.is_live:
                self.cancel_order(self._bid_order, logical_time=ltime)
                self._bid_order = None
            if self._ask_order and self._ask_order.is_live:
                self.cancel_order(self._ask_order, logical_time=ltime)
                self._ask_order = None
            time.sleep(0.2)  # let cancels process

            bbo = self.get_bbo()
            mid = bbo.mid_price
            if mid is not None:
                mid_d = mid / PRICE_SCALE
                with self._state_lock:
                    inv = self._inventory
                    cash = self._cash
                pnl = cash + inv * mid_d
                print(f"Final PnL: ${pnl:.2f}  Inventory: {inv}  "
                      f"Fills: {self._n_fills}  Requotes: {self._n_requotes}")
        finally:
            # Ignore further Ctrl+C while shutting down / plotting
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            self.stop()

            # Plot
            with self._data_lock:
                snapshots = list(self._snapshots)
                fills = list(self._fills)
            if snapshots:
                plot_results(snapshots, fills, self._output,
                             self._plot_alpha, self._price_margin)
            else:
                print("No data recorded — nothing to plot.")


# ---------------------------------------------------------------------------
# Post-run plotter
# ---------------------------------------------------------------------------

def _downsample(snapshots: List[Snapshot], bin_sec: float = 0.25) -> List[Snapshot]:
    """Keep only the last snapshot in each time bin to filter transient BBO states."""
    if not snapshots or bin_sec <= 0:
        return snapshots
    result: List[Snapshot] = []
    current_bin = -1.0
    for s in snapshots:
        b = int(s.t / bin_sec)
        if b != current_bin:
            if result:
                # keep the previous bin's last snapshot
                pass
            current_bin = b
        # Always overwrite — we want the last snapshot in each bin
        if not result or int(result[-1].t / bin_sec) == b:
            if result and int(result[-1].t / bin_sec) == b:
                result[-1] = s
            else:
                result.append(s)
        else:
            result.append(s)
    return result


def plot_results(snapshots: List[Snapshot], fills: List[FillRecord],
                 out_file: str = 'as_market_maker.pdf',
                 plot_alpha: bool = False,
                 price_margin: float = 0.05) -> None:
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed — skipping plot.", file=sys.stderr)
        return

    # Downsample to remove transient BBO spikes from multi-step order processing
    sampled = _downsample(snapshots)

    ts = [s.t for s in sampled]
    mid = [s.mid for s in sampled]
    bid = [s.bid for s in sampled]
    ask = [s.ask for s in sampled]
    our_bid = [s.our_bid if s.our_bid else None for s in sampled]
    our_ask = [s.our_ask if s.our_ask else None for s in sampled]
    inv = [s.inventory for s in sampled]
    pnl = [s.pnl for s in sampled]

    fig, axes = plt.subplots(3, 1, figsize=(14, 10), sharex=True)

    # --- Panel 1: Prices ---
    ax = axes[0]
    ax.fill_between(ts, bid, ask, alpha=0.15, color='C0', label='Market spread')
    ax.plot(ts, mid, linewidth=0.8, color='C0', label='Mid price')

    ob_t = [t for t, p in zip(ts, our_bid) if p is not None]
    ob_p = [p for p in our_bid if p is not None]
    oa_t = [t for t, p in zip(ts, our_ask) if p is not None]
    oa_p = [p for p in our_ask if p is not None]
    if ob_t:
        ax.plot(ob_t, ob_p, '.', markersize=1.5, color='green', alpha=0.5, label='Our bid')
    if oa_t:
        ax.plot(oa_t, oa_p, '.', markersize=1.5, color='red', alpha=0.5, label='Our ask')

    buy_fills = [f for f in fills if f.side == 'B']
    sell_fills = [f for f in fills if f.side == 'S']
    if buy_fills:
        ax.plot([f.t for f in buy_fills], [f.price for f in buy_fills],
                'x', markersize=10, color='green', markeredgewidth=2.5,
                zorder=5, label='Buy fill')
    if sell_fills:
        ax.plot([f.t for f in sell_fills], [f.price for f in sell_fills],
                'x', markersize=10, color='red', markeredgewidth=2.5,
                zorder=5, label='Sell fill')

    ax.set_ylabel('Price ($)')
    ax.set_title('Avellaneda-Stoikov Market Maker')
    ax.legend(loc='upper left', fontsize=7, ncol=3)
    ax.grid(True, alpha=0.3)

    # Clamp y-axis to BBO range + margin so deep quotes don't compress the view
    bbo_lo = min(bid)
    bbo_hi = max(ask)
    ax.set_ylim(bbo_lo - price_margin, bbo_hi + price_margin)

    # Alpha signal overlay (secondary y-axis) — opt-in via plot_alpha
    if plot_alpha:
        alpha_vals = [s.alpha for s in sampled]
        if any(a != 0.0 for a in alpha_vals):
            ax2 = ax.twinx()
            ax2.plot(ts, alpha_vals, linewidth=0.6, color='purple', alpha=0.6, label='Alpha ($)')
            ax2.set_ylabel('Alpha ($)', color='purple')
            ax2.tick_params(axis='y', labelcolor='purple')
            ax2.legend(loc='upper right', fontsize=7)

    # --- Panel 2: Inventory ---
    ax = axes[1]
    ax.plot(ts, inv, linewidth=0.8, color='C1')
    ax.axhline(0, color='gray', linewidth=0.5, linestyle='--')
    ax.set_ylabel('Inventory')
    ax.grid(True, alpha=0.3)

    # --- Panel 3: PnL ---
    ax = axes[2]
    ax.plot(ts, pnl, linewidth=0.8, color='C2')
    ax.axhline(0, color='gray', linewidth=0.5, linestyle='--')
    ax.set_ylabel('PnL ($)')
    ax.set_xlabel('Time (s)')
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(out_file, dpi=150)
    print(f"Plot saved to {out_file}")
    plt.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Avellaneda-Stoikov Market Maker'
    )
    parser.add_argument('--host', default='localhost')
    parser.add_argument('--order-port', type=int, default=10000)
    parser.add_argument('--md-port', type=int, default=10002)
    parser.add_argument('--gamma', type=float, default=0.01,
                        help='Risk aversion parameter (default: 0.01)')
    parser.add_argument('--k-fixed', type=float, default=20.0,
                        help='Order arrival intensity κ.  The liquidity term of the '
                             'spread is ≈2/κ, so κ directly sets your minimum quote '
                             'width.  Illiquid instruments need κ~20; highly liquid '
                             'ones (e.g. MSFT, AMZN) need κ~200-500. (default: 20.0)')
    parser.add_argument('--k-auto', action='store_true',
                        help='Dynamically estimate k from observed message rate '
                             '(recommended for liquid instruments). '
                             '--k becomes the initial estimate; --k-min sets the floor.')
    parser.add_argument('--k-min', type=float, default=5.0,
                        help='Minimum k when using --k-auto (default: 5.0). '
                             'Set to ~100 for liquid stocks to prevent the spread '
                             'from widening excessively before k-auto converges.')
    parser.add_argument('--horizon', type=float, default=300.0,
                        help='Time horizon T in seconds (default: 300)')
    parser.add_argument('--tau-fixed', type=float, default=None, metavar='SECONDS',
                        help='Pin τ=(T-t) to a fixed value instead of letting it decay. '
                             'Makes the model stationary: spreads and inventory adjustments '
                             'are constant through time. --horizon is ignored when set.')
    parser.add_argument('--sigma-window', type=int, default=100,
                        help='Mid-price observations for volatility (default: 100)')
    parser.add_argument('--max-inventory', type=int, default=500,
                        help='Maximum absolute position (default: 500)')
    parser.add_argument('--order-size', type=int, default=100,
                        help='Quote size per side (default: 100)')
    parser.add_argument('--min-requote', type=float, default=0.1,
                        help='Minimum seconds between re-quotes (default: 0.1)')
    parser.add_argument('--warmup', type=float, default=5.0,
                        help='Seconds to observe before quoting (default: 5.0)')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Print quote updates and fills')
    parser.add_argument('-o', '--output', type=str, default='as_market_maker.pdf',
                        help='Output plot filename (default: as_market_maker.pdf)')
    parser.add_argument('--plot-alpha', action='store_true', default=False,
                        help='Overlay alpha signal on price panel (default: off)')
    parser.add_argument('--price-margin', type=float, default=0.05, metavar='DOLLARS',
                        help='Y-axis margin above/below market BBO in price panel '
                             '(default: 0.05). Clips far-off quotes to zoom in on '
                             'quoting action near the spread.')

    # Signal arguments
    parser.add_argument('--signal', type=str, default=None, choices=['ofi'],
                        help='Enable alpha signal (default: none)')
    parser.add_argument('--ofi-top-params', type=float, nargs=2, default=[17.42, 0.468],
                        metavar=('A', 'B'),
                        help='Power-law coefficients for top quintile (default: 17.42 0.468)')
    parser.add_argument('--ofi-bot-params', type=float, nargs=2, default=[-35.64, 0.457],
                        metavar=('A', 'B'),
                        help='Power-law coefficients for bottom quintile (default: -35.64 0.457)')
    parser.add_argument('--ofi-top-edge', type=float, default=0.6,
                        help='Imbalance threshold for bullish regime (default: 0.6)')
    parser.add_argument('--ofi-bot-edge', type=float, default=-0.333,
                        help='Imbalance threshold for bearish regime (default: -0.333)')
    parser.add_argument('--ofi-max-delta', type=float, default=1.0,
                        help='Max delta in ms for extrapolation cap (default: 1.0)')

    args = parser.parse_args()

    signals = []
    if args.signal == 'ofi':
        signals.append(OFISignal(
            top_params=tuple(args.ofi_top_params),
            bot_params=tuple(args.ofi_bot_params),
            top_edge=args.ofi_top_edge,
            bot_edge=args.ofi_bot_edge,
            max_delta_ms=args.ofi_max_delta,
        ))
    mm = AvellanedaStoikov(
        host=args.host,
        order_port=args.order_port,
        md_port=args.md_port,
        gamma=args.gamma,
        k=args.k_fixed,
        k_auto=args.k_auto,
        k_min=args.k_min,
        horizon=args.horizon,
        tau_fixed=args.tau_fixed,
        sigma_window=args.sigma_window,
        max_inventory=args.max_inventory,
        order_size=args.order_size,
        min_requote=args.min_requote,
        warmup=args.warmup,
        verbose=args.verbose,
        output=args.output,
        plot_alpha=args.plot_alpha,
        price_margin=args.price_margin,
        signals=signals,
    )

    mm.run()


if __name__ == '__main__':
    main()
