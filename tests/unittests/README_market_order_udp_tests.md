# Market Order UDP Broadcast Tests

## Overview

This test suite (`test_market_order_udp_broadcast.py`) verifies the bug fix for market order UDP broadcasts. The bug was that `_process_market_order()` was not broadcasting EXECUTE messages over UDP market data when market orders filled resting limit orders, while `_process_limit_order()` correctly broadcast executions.

## Bug Description

**Before the fix:**
- When a market order filled resting limit orders, the exchange would:
  - Send TCP responses to the aggressor
  - Send passive fill notifications to resting order owners
  - Broadcast STP messages over the TCP feed
  - **NOT** broadcast EXECUTE messages over UDP market data

**After the fix:**
- Market orders now broadcast EXECUTE messages (type=4) over UDP for each trade, just like limit orders do

## Test Coverage

### TestMarketOrderUDPBroadcast

1. **test_market_buy_fills_resting_ask_broadcasts_execute**
   - Verifies market buy orders broadcast EXECUTE when filling asks
   - Checks order ID, size, price, and direction (should be -1 for sell side)

2. **test_market_sell_fills_resting_bid_broadcasts_execute**
   - Verifies market sell orders broadcast EXECUTE when filling bids
   - Checks order ID, size, price, and direction (should be 1 for buy side)

3. **test_market_order_fills_multiple_price_levels_broadcasts_all_executes**
   - Market order walks the book filling orders at 3 different price levels
   - Verifies EXECUTE message broadcast for each filled order
   - Tests size 50 @ $5000.00, 50 @ $5010.00, 20 @ $5020.00

4. **test_book_builder_syncs_with_exchange_after_market_orders**
   - Critical integration test: verifies BookBuilder stays in sync with exchange
   - Tests complex scenario with multiple limit orders and market orders
   - Confirms book builder's reconstructed book matches exchange's internal book

5. **test_market_order_partial_fill_broadcasts_correct_size**
   - Resting order of 100 shares, market order for 25 shares
   - Verifies EXECUTE size is 25 (actual executed), not 100 (full resting size)

6. **test_market_order_execute_direction_matches_resting_order_side**
   - Verifies EXECUTE direction field matches the resting order's side
   - Market buy fills ask → direction -1 (Sell)
   - Market sell fills bid → direction 1 (Buy)

### TestMarketOrderUDPComparisonWithLimitOrder

7. **test_market_and_limit_produce_same_execute_format**
   - Compares EXECUTE messages from market vs. limit order crossing
   - Ensures both produce identical message format
   - Validates consistency between order types

## LOBSTER Message Format

```
Time,Type,OrderID,Size,Price,Direction
```

- **Time**: Seconds after midnight (float)
- **Type**: 1=INSERT, 2=CANCEL, 3=DELETE, 4=EXECUTE, 5=HIDDEN
- **OrderID**: Unique order identifier
- **Size**: Number of shares
- **Price**: Dollar price * 10000 (e.g., $5000.00 = 50000000)
- **Direction**: 1=Buy, -1=Sell (refers to the resting order side)

## Running the Tests

```bash
# Run all market order UDP tests
pytest tests/unittests/test_market_order_udp_broadcast.py -v

# Run a specific test
pytest tests/unittests/test_market_order_udp_broadcast.py::TestMarketOrderUDPBroadcast::test_market_buy_fills_resting_ask_broadcasts_execute -v

# Run with output capture disabled (see all print statements)
pytest tests/unittests/test_market_order_udp_broadcast.py -v -s
```

## Test Strategy

These tests use:
- **Real UDP sockets**: Subscribe to market data feed just like a real client
- **Threading**: Run UDP listener in background while sending orders
- **BookBuilder**: Use the actual book builder to verify synchronization
- **Message parsing**: Parse LOBSTER format messages to verify correctness

## Key Assertions

1. EXECUTE messages (type=4) are broadcast for market order fills
2. Order ID in EXECUTE matches the resting order (not the aggressor)
3. Size reflects actual executed amount (handles partial fills correctly)
4. Direction reflects the resting order's side, not the aggressor's side
5. BookBuilder's reconstructed book matches the exchange's internal state
6. Multiple price levels result in multiple EXECUTE messages

## Related Code

- **exchange_server.py**: `_process_market_order()` (line ~285)
  - Bug fix: Added `broadcast_execute` calls inside trades loop (lines 332-340)
- **udp_market_data.py**: `UDPMarketDataServer.broadcast_execute()`
- **udp_book_builder.py**: `BookBuilder.process_message()`
- **order_book.py**: `OrderBook` maintains internal state

## Success Criteria

All 7 tests pass, confirming:
- Market orders now broadcast EXECUTE messages just like limit orders
- BookBuilder stays synchronized with exchange state
- Message format is correct and consistent
