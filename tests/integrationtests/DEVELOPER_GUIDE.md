# Developer Guide: Complex Trading Scenarios Tests

## Quick Start

```bash
# Run all tests
pytest tests/integrationtests/test_complex_trading_scenarios.py -v

# Run specific scenario
pytest tests/integrationtests/test_complex_trading_scenarios.py::TestSelfTrade -v

# Run with output
pytest tests/integrationtests/test_complex_trading_scenarios.py -v -s
```

## Adding New Tests

### Template for New Test

```python
def test_your_scenario(self, exchange_server):
    """
    Brief description of what this test validates.

    Scenario:
    1. Step 1 description
    2. Step 2 description
    3. Step 3 description
    """
    server, order_port, feed_port = exchange_server

    # Setup clients
    client1 = TrackingClient("Client1", "localhost", order_port)
    client2 = TrackingClient("Client2", "localhost", order_port)

    assert client1.connect()
    assert client2.connect()

    client1.start_async_receive()
    client2.start_async_receive()

    # Execute scenario
    order1 = client1.create_order("limit", 100, 500000, "B")
    assert client1.submit_order_sync(order1)

    time.sleep(0.2)  # Let order reach book

    order2 = client2.create_order("limit", 100, 500000, "S")
    assert client2.submit_order_sync(order2)

    time.sleep(0.5)  # Let async fills arrive

    # Assertions
    assert len(client1.fills) == 1
    assert order1.state_name == "FILLED"
    assert order2.state_name == "FILLED"

    # Cleanup
    client1.disconnect()
    client2.disconnect()
```

### Common Patterns

#### Creating Orders

```python
# Limit buy
order = client.create_order("limit", size=100, price=500000, side="B", ttl=3600)

# Limit sell
order = client.create_order("limit", size=100, price=500000, side="S", ttl=3600)

# Market buy
order = client.create_order("market", size=100, price=0, side="B")

# Market sell
order = client.create_order("market", size=100, price=0, side="S")
```

#### Submitting Orders

```python
# Synchronous (wait for initial response)
assert client.submit_order_sync(order, timeout=5.0)

# Asynchronous (no wait)
assert client.submit_order(order)
```

#### Canceling Orders

```python
assert client.cancel_order(order, user="client")
time.sleep(0.3)  # Let cancel process
assert order.state_name == "CANCELLED"
```

#### Checking Fills

```python
# Number of fills
assert len(client.fills) == 3

# Total filled size
total = sum(f.size for f in client.fills)
assert total == 300

# Fill at specific order
fills = [f for f in client.fills if f.order_id == order.exchange_order_id]
assert len(fills) == 2

# Fill prices
prices = [f.price for f in client.fills]
assert 500000 in prices
```

#### State Assertions

```python
# Check order state
assert order.state_name == "ACCEPTED"
assert order.state_name == "FILLED"
assert order.state_name == "PARTIALLY_FILLED"
assert order.state_name == "CANCELLED"
assert order.state_name == "REJECTED"

# Check if terminal
assert order.is_terminal

# Check if live
assert order.is_live
```

## TrackingClient API

### Connection Management

```python
client = TrackingClient("Name", "localhost", port)
client.connect()                    # Connect to exchange
client.start_async_receive()        # Start async message loop
client.disconnect()                 # Disconnect and cleanup
```

### Order Management

```python
order = client.create_order(...)    # Create managed order
client.submit_order(order)          # Submit without waiting
client.submit_order_sync(order)     # Submit and wait for response
client.cancel_order(order)          # Cancel order
```

### Fill Tracking

```python
client.fills                         # List of FillRecord objects
client.clear_fills()                 # Clear fill history
client.get_total_filled_size(oid)   # Total size filled for order
client.get_fill_prices(oid)         # List of fill prices
client.get_fill_count(oid)          # Number of fills
```

### Message Tracking

```python
client.all_messages                  # List of (order, message) tuples
```

## FillRecord Structure

```python
@dataclass
class FillRecord:
    order_id: int           # Exchange order ID
    size: int              # Fill size
    price: int             # Fill price (in 10000ths)
    remainder: Optional[int]  # Remaining size (if partial)
    is_partial: bool       # True if PARTIAL_FILL
```

## Timing Considerations

### When to use time.sleep()

```python
# After order submission (let it reach book)
client.submit_order_sync(order)
time.sleep(0.2)

# After aggressive order (let passive fills arrive)
client2.submit_order_sync(aggressive_order)
time.sleep(0.5)  # Passive fills are async

# After cancel (let cancel process)
client.cancel_order(order)
time.sleep(0.3)
```

### Recommended sleep durations

- After order post: 0.1-0.2 seconds
- After trade (for passive fills): 0.3-0.5 seconds
- After cancel: 0.2-0.3 seconds
- Before final assertions: 0.3-0.5 seconds

## Price Format

Prices are in units of 1/10000 of a dollar:

```python
$50.00  = 500000
$50.50  = 505000
$51.00  = 510000
$49.95  = 499500
```

## Common Assertions

### Basic Fill Verification

```python
# Check fill occurred
assert len(client.fills) > 0

# Check fill details
assert client.fills[0].size == 100
assert client.fills[0].price == 500000
assert client.fills[0].order_id == order.exchange_order_id
```

### Partial Fill Verification

```python
# Check partial fill
assert client.fills[0].is_partial
assert client.fills[0].remainder == 700

# Check state
assert order.state_name == "PARTIALLY_FILLED"
```

### Multi-Fill Verification

```python
# Multiple fills for same order
fills = [f for f in client.fills if f.order_id == order_id]
assert len(fills) == 3

# Total filled
total = sum(f.size for f in fills)
assert total == 450
```

### Cross-Client Verification

```python
# Both clients got fills
assert len(client1.fills) == 1
assert len(client2.fills) == 1

# Same size
assert client1.fills[0].size == client2.fills[0].size

# Same price
assert client1.fills[0].price == client2.fills[0].price
```

## Debugging Tips

### Enable verbose output

```bash
pytest test_complex_trading_scenarios.py -v -s
```

### Add debug prints

```python
print(f"Order state: {order.state_name}")
print(f"Fills: {len(client.fills)}")
print(f"Fill details: {client.fills}")
print(f"All messages: {len(client.all_messages)}")
```

### Check order book state

```python
bids, asks = server._order_book.get_snapshot()
print(f"Bids: {[(l.price, l.size) for l in bids]}")
print(f"Asks: {[(l.price, l.size) for l in asks]}")
```

### Verify exchange order ID

```python
print(f"Exchange order ID: {order.exchange_order_id}")
assert order.exchange_order_id is not None
```

### Check message history

```python
for order, msg in client.all_messages:
    print(f"Order {order.exchange_order_id}: {msg.msg_type}")
```

## Common Pitfalls

### 1. Not waiting for async messages

```python
# WRONG - fills may not arrive yet
client2.submit_order_sync(aggressive)
assert len(client1.fills) == 1  # May fail

# RIGHT - wait for async fills
client2.submit_order_sync(aggressive)
time.sleep(0.5)
assert len(client1.fills) == 1  # Reliable
```

### 2. Forgetting to start async receive

```python
# WRONG - won't receive passive fills
client1.connect()
# missing: client1.start_async_receive()

# RIGHT
client1.connect()
client1.start_async_receive()
```

### 3. Not clearing fills between phases

```python
# If testing multiple trades
client1.clear_fills()  # Reset for next phase
```

### 4. Incorrect price format

```python
# WRONG - price too small
order = client.create_order("limit", 100, 50, "B")

# RIGHT - price in 1/10000 units
order = client.create_order("limit", 100, 500000, "B")  # $50.00
```

### 5. Not disconnecting clients

```python
# Always disconnect
try:
    # test code
finally:
    client1.disconnect()
    client2.disconnect()
```

## Test Organization

### Group related tests in classes

```python
class TestYourFeature:
    """Test description"""

    def test_scenario1(self, exchange_server):
        pass

    def test_scenario2(self, exchange_server):
        pass
```

### Use descriptive test names

```python
# GOOD
def test_market_order_sweeps_multiple_levels(self, exchange_server):

# BAD
def test_scenario1(self, exchange_server):
```

### Document test scenarios

```python
def test_complex_scenario(self, exchange_server):
    """
    Test description here.

    Scenario:
    1. Step 1
    2. Step 2
    3. Expected result
    """
```

## Performance Considerations

- Each test starts a fresh server (~0.1s overhead)
- Network operations add latency
- Async waits add time
- Total suite runtime: ~44 seconds for 18 tests
- Average per test: ~2.5 seconds

## Questions?

See:
- README_complex_scenarios.md - Detailed test documentation
- TEST_SUMMARY.md - Test results and coverage
- test_complex_trading_scenarios.py - Source code with examples
