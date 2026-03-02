# Complex Trading Scenarios Integration Tests

## Overview

The `test_complex_trading_scenarios.py` file contains comprehensive integration tests for the exchange simulator that cover complex multi-client trading scenarios. These tests use the full `OrderClientWithFSM` to track order state transitions and verify both synchronous responses and asynchronous passive fill notifications.

## Test Structure

### TrackingClient Helper Class

The tests use a `TrackingClient` wrapper that:
- Wraps `OrderClientWithFSM` for full FSM tracking
- Records all fills (both active and passive)
- Tracks all exchange messages
- Provides utilities to query fill history
- Makes assertions cleaner and more focused

### Fixture: exchange_server

Each test uses a pytest fixture that:
- Finds free ports dynamically (no hardcoded ports)
- Starts a fresh exchange server instance
- Yields server and port information
- Automatically tears down after the test

## Test Scenarios

### 1. TestMultiLevelSweeps (2 tests)

Tests orders that sweep through multiple price levels in the order book.

**test_market_order_sweeps_multiple_levels:**
- Client 1 posts orders at 3 different price levels (asks)
- Client 2 sends large market buy that sweeps all levels
- Verifies correct fills at each price level
- Verifies passive fill notifications to Client 1

**test_limit_order_sweeps_partial_levels:**
- Client 1 posts 3 ask levels
- Client 2 sends limit buy that sweeps 2 levels fully and partially fills 3rd
- Verifies correct fill sizes and remainder tracking

### 2. TestPartialFillScenarios (2 tests)

Tests partial fill tracking and remainder calculations.

**test_large_resting_order_multiple_small_fills:**
- Large resting order (1000 shares) filled incrementally
- Three separate fills: 200, 300, 500 shares
- Verifies remainder after each fill: 800 → 500 → 0
- Checks state transitions: ACCEPTED → PARTIALLY_FILLED → FILLED

**test_partial_fill_tracking_across_price_levels:**
- Tests order that sweeps multiple levels with remainder
- Verifies remainder is posted in book at limit price

### 3. TestMarketOrderEdgeCases (3 tests)

Tests market order behavior in edge cases.

**test_market_order_empty_book_rejection:**
- Market order against empty book
- Verifies rejection with "No liquidity" message

**test_market_order_partial_fill_insufficient_liquidity:**
- Market order for 200 shares with only 100 available
- Verifies partial fill and remainder reporting

**test_market_order_sweeps_to_last_share:**
- Market order that exactly consumes all available liquidity
- Verifies no remainder when fully filled

### 4. TestCrossClientPassiveFills (2 tests)

Tests passive fill notifications between different clients.

**test_simple_cross_client_passive_fill:**
- Client 1 posts passive order
- Client 2 aggresses
- Verifies both clients receive fills (passive and active)
- Checks fill sizes and prices match

**test_multiple_passive_fills_same_order:**
- Single passive order filled by multiple aggressors
- Three separate aggressor orders: 300, 500, 200 shares
- Verifies passive client receives all fill notifications
- Checks remainder tracking: 700 → 200 → 0

### 5. TestSelfTrade (2 tests)

Tests scenarios where same client trades with itself.

**test_self_trade_buy_then_sell:**
- Client posts bid, then posts crossing sell
- Verifies client receives both active (sell) and passive (bid) fills
- Checks both order IDs appear in fill records

**test_self_trade_partial_match:**
- Large bid (500 shares) hit by smaller sell (200 shares)
- Verifies sell fully filled, bid partially filled
- Tests self-trade with remainder

### 6. TestRapidFireSequences (2 tests)

Tests rapid-fire order submission and fill handling.

**test_rapid_sequential_orders:**
- 10 orders posted quickly by maker
- 10 orders taking all quickly by taker
- Verifies all fills reported correctly to both clients
- Stress tests message handling and ordering

**test_rapid_alternating_sides:**
- Rapidly alternates posting bids and crossing asks
- 5 pairs of self-trading orders
- Verifies all trades execute correctly

### 7. TestCancelFillRaceConditions (3 tests)

Tests race conditions between cancels and fills.

**test_cancel_after_partial_fill:**
- Order placed for 1000 shares
- Partially filled (300 shares)
- Remainder cancelled (700 shares)
- Verifies correct fill and cancel sizes

**test_multiple_partial_fills_then_cancel:**
- Multiple partial fills followed by cancel
- Fills: 200 → 300 → cancel 500 remainder
- Tracks remainder through sequence: 800 → 500 → cancelled

**test_cancel_then_try_to_fill:**
- Order posted and immediately cancelled
- Attempt to fill cancelled order
- Verifies order not in book, cannot be filled

### 8. TestComplexMultiClientScenarios (2 tests)

Tests complex scenarios with 3+ clients.

**test_three_way_trade:**
- Client A posts bid
- Client B posts offer
- Client C sweeps only the offer
- Verifies correct fills and non-fills

**test_market_maker_scenario:**
- Realistic market maker providing two-sided liquidity
- Trader A lifts offer
- MM requotes
- Trader B hits bid
- Verifies MM receives fills and can successfully requote

## Test Coverage Summary

The test suite covers:

✓ **Multi-level sweeps** - Orders crossing multiple price levels
✓ **Partial fills** - Remainder tracking with multiple incremental fills
✓ **Market orders** - Edge cases including empty book, partial fills
✓ **Passive fills** - Async notifications to standing orders
✓ **Self-trades** - Same client on both sides
✓ **Rapid sequences** - Stress testing with quick submissions
✓ **Cancel races** - Interactions between fills and cancellations
✓ **Multi-client** - Complex 3-way scenarios

## Running the Tests

Run all tests:
```bash
pytest tests/integrationtests/test_complex_trading_scenarios.py -v
```

Run specific test class:
```bash
pytest tests/integrationtests/test_complex_trading_scenarios.py::TestMultiLevelSweeps -v
```

Run single test:
```bash
pytest tests/integrationtests/test_complex_trading_scenarios.py::TestSelfTrade::test_self_trade_buy_then_sell -v
```

## Key Testing Patterns

1. **Fresh Server Per Test**: Each test gets a clean exchange server with free ports
2. **Async Message Tracking**: TrackingClient captures all messages for verification
3. **State Verification**: Tests verify both FSM state transitions and message contents
4. **Timing Considerations**: Strategic `time.sleep()` calls allow async notifications to arrive
5. **Comprehensive Assertions**: Tests check fills, remainders, state transitions, and message counts

## Integration with CI/CD

These tests are designed to:
- Run independently (no shared state)
- Clean up resources properly
- Provide clear failure messages
- Complete in reasonable time (~44 seconds for full suite)
- Work with pytest's standard test discovery

## Future Enhancements

Potential additions:
- Time-to-live (TTL) expiration scenarios
- Order priority testing (price-time priority verification)
- Extreme stress tests (1000+ orders)
- Network failure simulation
- Concurrent client connection/disconnection
- Book depth verification after complex sequences
