# Student Test Suite Guide

This test suite (`test_student_scenarios.py`) provides comprehensive coverage of the Exchange Simulator's core functionality. These tests are designed to help you understand how the exchange works and verify your understanding.

## Overview

The test suite contains **24 tests** organized into **5 categories**:

1. **Order Book Integrity** (5 tests) - Verifying correct book state
2. **Message Ordering** (4 tests) - Ensuring proper message sequencing
3. **Concurrent Access** (4 tests) - Testing thread safety
4. **Error Handling** (6 tests) - Validating rejection of invalid operations
5. **State Machine Transitions** (5 tests) - Testing order lifecycle

## Running the Tests

### Run all tests:
```bash
pytest tests/unittests/test_student_scenarios.py -v
```

### Run a specific category:
```bash
pytest tests/unittests/test_student_scenarios.py::TestOrderBookIntegrity -v
```

### Run a specific test:
```bash
pytest tests/unittests/test_student_scenarios.py::TestOrderBookIntegrity::test_book_state_after_single_fill -v
```

### Run with detailed output:
```bash
pytest tests/unittests/test_student_scenarios.py -v -s
```

## Test Categories Explained

### 1. Order Book Integrity Tests

These tests verify that the order book maintains correct state after various operations.

**Key Learning Points:**
- Filled orders are immediately removed from the book
- Partial fills update the book to show remaining quantity
- Price-time priority is strictly enforced (best price first, then FIFO)
- Cancelled orders are completely removed
- Complex sequences maintain consistency

**Tests:**
- `test_book_state_after_single_fill` - Full fill removes order
- `test_book_state_after_partial_fill` - Partial fill updates size
- `test_price_time_priority_maintained` - Best price always fills first
- `test_book_state_after_cancel` - Cancel removes from book
- `test_book_integrity_after_multiple_trades` - Complex sequences

### 2. Message Ordering Tests

These tests verify that messages arrive in the correct order, which is critical for maintaining consistent client state.

**Key Learning Points:**
- ACK messages contain order IDs for tracking
- Fill messages arrive in price-level order
- Passive fills arrive promptly after trades
- Multiple partial fills show correct remainder at each step

**Tests:**
- `test_ack_before_fills` - Order ID in first message
- `test_partial_fill_ordering` - Fills ordered by price
- `test_passive_fill_notification_timing` - Timely notifications
- `test_multiple_partial_fills_sequence` - Sequential remainder tracking

### 3. Concurrent Access Tests

These tests verify thread safety and correct behavior under load.

**Key Learning Points:**
- The exchange is thread-safe (multiple clients can submit simultaneously)
- Order IDs are unique across all clients
- Rapid submit/cancel sequences work correctly
- High-frequency submission maintains correctness
- Concurrent modification of the same price level is handled atomically

**Tests:**
- `test_multiple_clients_simultaneous_submission` - Parallel submission
- `test_rapid_order_cancel_sequence` - Fast cancel after submit
- `test_high_frequency_submission_stress` - 100 orders rapid-fire
- `test_concurrent_modification_same_price_level` - Race conditions

### 4. Error Handling Tests

These tests verify proper rejection of invalid operations.

**Key Learning Points:**
- Invalid message formats are rejected with clear reasons
- Cannot cancel non-existent orders
- Cannot cancel another user's order (ownership enforced)
- Cannot cancel already-filled orders
- Market orders with no liquidity are rejected
- Server handles client disconnections gracefully

**Tests:**
- `test_invalid_order_format_rejected` - Malformed messages
- `test_cancel_nonexistent_order_rejected` - Invalid order ID
- `test_cancel_wrong_user_rejected` - Ownership violation
- `test_cancel_already_filled_order_rejected` - Terminal state
- `test_market_order_no_liquidity_rejected` - No liquidity
- `test_connection_drop_during_order` - Server resilience

### 5. State Machine Transition Tests

These tests verify correct order state transitions through the lifecycle.

**Key Learning Points:**
- Orders go through defined states: PENDING → ACCEPTED → FILLED/CANCELLED
- Partially filled orders have an intermediate state
- Rejected orders never reach ACCEPTED
- Terminal states (FILLED, CANCELLED, REJECTED) cannot transition further
- Invalid state transitions are prevented

**Tests:**
- `test_order_lifecycle_posted_to_filled` - PENDING → ACCEPTED → FILLED
- `test_order_lifecycle_posted_to_cancelled` - PENDING → ACCEPTED → CANCELLED
- `test_order_lifecycle_partial_fills` - Multiple PARTIALLY_FILLED states
- `test_order_rejection_immediate` - PENDING → REJECTED
- `test_invalid_state_transition_prevented` - Invalid transitions blocked

## Understanding the Test Code

Each test follows this pattern:

```python
def test_something(self, exchange):
    """
    Test: What is being tested

    Learning objective: Why this matters

    Scenario:
    1. Step 1 description
    2. Step 2 description
    3. Verification
    """
    server, order_port, feed_port = exchange

    # Test implementation...
```

### Common Patterns

**Creating a client:**
```python
client = OrderClient('localhost', order_port)
assert client.connect()
```

**Submitting an order:**
```python
# Format: "limit,size,price,side,user"
response = client.send_order("limit,100,500000,B,trader")
```

**Price encoding:**
Prices are in LOBSTER format: `price * 10000`
- $50.00 = 500000
- $49.50 = 495000
- $51.25 = 512500

**Checking the book:**
```python
bids, asks = server._order_book.get_snapshot()
assert len(bids) == 1
assert bids[0].price == 500000
assert bids[0].size == 100
```

**Async message handling:**
```python
messages = []
def handler(msg):
    messages.append(msg)

client.start_async_receive(handler)
# ... operations that trigger async messages ...
time.sleep(0.3)  # Wait for messages
# Check messages list
```

## Common Pitfalls

### 1. Passive Fill Notifications
When your resting order is filled, you receive an **asynchronous** notification on the same connection. Be careful not to mix these with synchronous responses:

```python
# GOOD: Separate connections
maker.send_order("limit,100,500000,S,maker")
maker.disconnect()  # Avoid receiving passive fill

taker.send_order("limit,100,500000,B,taker")
```

### 2. Race Conditions in Tests
Always add small delays when testing async behavior:

```python
taker.send_order("limit,100,500000,B,taker")
time.sleep(0.3)  # Give time for passive fill to arrive
```

### 3. Order ID Extraction
Extract order IDs carefully from responses:

```python
response = "ACK,1000,100,500000"
order_id = int(response.split(',')[1])  # 1000
```

## Extending the Tests

Feel free to add your own tests! Here's a template:

```python
def test_your_scenario(self, exchange):
    """
    Test: Brief description

    Learning objective: What this teaches

    Scenario:
    1. Setup
    2. Action
    3. Verification
    """
    server, order_port, feed_port = exchange

    # Your test code here
    client = OrderClient('localhost', order_port)
    assert client.connect()

    # ... test implementation ...

    client.disconnect()
```

## Test Statistics

- **Total tests:** 24
- **Test coverage:**
  - Order book operations: 5 tests
  - Message protocol: 4 tests
  - Concurrency: 4 tests
  - Error conditions: 6 tests
  - State transitions: 5 tests
- **Execution time:** ~45-50 seconds for full suite
- **Thread safety:** All tests are isolated and can run in parallel

## Troubleshooting

### Tests hang or timeout
- Check that the exchange server is not already running on the test ports
- Ensure no firewall is blocking localhost connections

### Random failures
- Usually caused by timing issues
- Try increasing sleep durations in your local copy
- Check for leftover connections from previous test runs

### Connection refused errors
- The exchange fixture should handle server startup
- If you see this, the server failed to start - check logs

## Learning Path

Recommended order for studying the tests:

1. Start with **Order Book Integrity** - understand the core data structure
2. Move to **Error Handling** - learn what inputs are valid/invalid
3. Study **Message Ordering** - understand the protocol
4. Examine **State Machine Transitions** - understand order lifecycle
5. Finally **Concurrent Access** - understand thread safety

## Additional Resources

- Main test file: `test_student_scenarios.py`
- Existing integration tests: `test_complex_trading_scenarios.py`
- Exchange implementation: `exchange_server.py`
- Order book implementation: `order_book.py`
- Client implementation: `order_client_with_fsm.py`

## Getting Help

If a test fails:
1. Read the test documentation (docstring)
2. Check the assertion message - it explains what went wrong
3. Run with `-v -s` for detailed output
4. Add print statements to understand the flow
5. Use the `print_book_state(server)` helper function to see book state

Happy testing!
