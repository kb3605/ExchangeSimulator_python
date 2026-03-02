"""
Tests for market order UDP broadcast bug fix.

This test suite verifies that _process_market_order() properly broadcasts
EXECUTE messages (type=4) over UDP market data when market orders fill
resting limit orders. The bug was that market orders were not broadcasting
executions, while limit orders (_process_limit_order) were.

Test Coverage:
1. Market buy order fills a resting ask - verifies EXECUTE broadcast
2. Market sell order fills a resting bid - verifies EXECUTE broadcast
3. Market order fills multiple resting orders - verifies EXECUTE for each fill
4. BookBuilder synchronization - verifies book builder stays in sync with exchange
"""

import pytest
import sys
import os
import time
import socket
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exchange_server import ExchangeServer
from order_client_with_fsm import OrderClient
from udp_book_builder import BookBuilder


def find_free_port():
    """Find a free port to use for testing."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]


class TestMarketOrderUDPBroadcast:
    """Tests for market order EXECUTE message UDP broadcasts."""

    @pytest.fixture
    def server_with_udp(self):
        """Create and start a test server with UDP market data enabled."""
        order_port = find_free_port()
        feed_port = find_free_port()
        market_data_port = find_free_port()
        server = ExchangeServer(order_port, feed_port, market_data_port)
        assert server.start()
        time.sleep(0.2)  # Allow server components to fully start
        yield server, order_port, feed_port, market_data_port
        server.stop()

    def test_market_buy_fills_resting_ask_broadcasts_execute(self, server_with_udp):
        """
        Test that when a market buy order fills a resting ask, an EXECUTE message
        (type=4) is broadcast on UDP market data with correct order ID, size,
        price, and direction.
        """
        server_obj, order_port, feed_port, market_data_port = server_with_udp

        # Set up UDP listener to capture market data messages
        received_messages = []
        stop_listener = threading.Event()

        def udp_listener():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.5)
            server_addr = ('127.0.0.1', market_data_port)
            sock.sendto(b"subscribe", server_addr)

            while not stop_listener.is_set():
                try:
                    data, _ = sock.recvfrom(1024)
                    msg = data.decode('utf-8').strip()
                    received_messages.append(msg)
                except socket.timeout:
                    continue
                except Exception:
                    break

            sock.close()

        listener_thread = threading.Thread(target=udp_listener, daemon=True)
        listener_thread.start()
        time.sleep(0.2)  # Allow subscription to complete

        # Post a resting ask (limit sell order)
        seller = OrderClient('localhost', order_port)
        assert seller.connect()
        response = seller.send_order("limit,100,50000000,S,seller")
        assert "ACK,1000" in response  # Order ID 1000
        seller.disconnect()

        time.sleep(0.3)  # Wait for INSERT message

        # Execute a market buy order
        buyer = OrderClient('localhost', order_port)
        assert buyer.connect()
        response = buyer.send_order("market,50,0,B,buyer")
        assert "FILL" in response
        assert "50000000" in response  # Price
        buyer.disconnect()

        time.sleep(0.3)  # Wait for EXECUTE message

        # Stop listener and collect messages
        stop_listener.set()
        listener_thread.join(timeout=2.0)

        # Find the EXECUTE message for order_id 1000
        execute_messages = []
        for msg in received_messages:
            parts = msg.split(',')
            if len(parts) == 6:
                event_type = int(parts[1])
                order_id = int(parts[2])
                if event_type == 4 and order_id == 1000:  # EXECUTE type
                    execute_messages.append(msg)

        # Verify EXECUTE message was broadcast
        assert len(execute_messages) >= 1, \
            f"Expected at least one EXECUTE message for order 1000, " \
            f"but found {len(execute_messages)}. All messages: {received_messages}"

        # Verify EXECUTE message contents
        execute_msg = execute_messages[0]
        parts = execute_msg.split(',')
        assert int(parts[1]) == 4, "Event type should be EXECUTE (4)"
        assert int(parts[2]) == 1000, "Order ID should be 1000"
        assert int(parts[3]) == 50, "Executed size should be 50"
        assert int(parts[4]) == 50000000, "Price should be 50000000"
        assert int(parts[5]) == -1, "Direction should be -1 (Sell side executed)"

    def test_market_sell_fills_resting_bid_broadcasts_execute(self, server_with_udp):
        """
        Test that when a market sell order fills a resting bid, an EXECUTE message
        is broadcast on UDP market data.
        """
        server_obj, order_port, feed_port, market_data_port = server_with_udp

        # Set up UDP listener
        received_messages = []
        stop_listener = threading.Event()

        def udp_listener():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.5)
            server_addr = ('127.0.0.1', market_data_port)
            sock.sendto(b"subscribe", server_addr)

            while not stop_listener.is_set():
                try:
                    data, _ = sock.recvfrom(1024)
                    msg = data.decode('utf-8').strip()
                    received_messages.append(msg)
                except socket.timeout:
                    continue
                except Exception:
                    break

            sock.close()

        listener_thread = threading.Thread(target=udp_listener, daemon=True)
        listener_thread.start()
        time.sleep(0.2)

        # Post a resting bid (limit buy order)
        buyer = OrderClient('localhost', order_port)
        assert buyer.connect()
        response = buyer.send_order("limit,100,50000000,B,buyer")
        assert "ACK,1000" in response  # Order ID 1000
        buyer.disconnect()

        time.sleep(0.3)

        # Execute a market sell order
        seller = OrderClient('localhost', order_port)
        assert seller.connect()
        response = seller.send_order("market,75,0,S,seller")
        assert "FILL" in response
        assert "50000000" in response
        seller.disconnect()

        time.sleep(0.3)

        # Stop listener
        stop_listener.set()
        listener_thread.join(timeout=2.0)

        # Find EXECUTE messages for order_id 1000
        execute_messages = []
        for msg in received_messages:
            parts = msg.split(',')
            if len(parts) == 6:
                event_type = int(parts[1])
                order_id = int(parts[2])
                if event_type == 4 and order_id == 1000:
                    execute_messages.append(msg)

        # Verify EXECUTE message was broadcast
        assert len(execute_messages) >= 1, \
            f"Expected EXECUTE message for market sell filling bid 1000, " \
            f"but found {len(execute_messages)}. All messages: {received_messages}"

        # Verify EXECUTE message contents
        execute_msg = execute_messages[0]
        parts = execute_msg.split(',')
        assert int(parts[1]) == 4, "Event type should be EXECUTE (4)"
        assert int(parts[2]) == 1000, "Order ID should be 1000"
        assert int(parts[3]) == 75, "Executed size should be 75"
        assert int(parts[4]) == 50000000, "Price should be 50000000"
        assert int(parts[5]) == 1, "Direction should be 1 (Buy side executed)"

    def test_market_order_fills_multiple_price_levels_broadcasts_all_executes(
            self, server_with_udp):
        """
        Test that when a market order walks the book and fills multiple resting
        orders at different price levels, EXECUTE messages are broadcast for
        each fill.
        """
        server_obj, order_port, feed_port, market_data_port = server_with_udp

        # Set up UDP listener
        received_messages = []
        stop_listener = threading.Event()

        def udp_listener():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.5)
            server_addr = ('127.0.0.1', market_data_port)
            sock.sendto(b"subscribe", server_addr)

            while not stop_listener.is_set():
                try:
                    data, _ = sock.recvfrom(1024)
                    msg = data.decode('utf-8').strip()
                    received_messages.append(msg)
                except socket.timeout:
                    continue
                except Exception:
                    break

            sock.close()

        listener_thread = threading.Thread(target=udp_listener, daemon=True)
        listener_thread.start()
        time.sleep(0.2)

        # Post multiple resting asks at different price levels
        seller = OrderClient('localhost', order_port)
        assert seller.connect()

        # Ask 1: 50 @ $5000.00 (order_id 1000)
        r1 = seller.send_order("limit,50,50000000,S,seller1")
        assert "ACK,1000" in r1

        # Ask 2: 50 @ $5010.00 (order_id 1001)
        r2 = seller.send_order("limit,50,50100000,S,seller2")
        assert "ACK,1001" in r2

        # Ask 3: 50 @ $5020.00 (order_id 1002)
        r3 = seller.send_order("limit,50,50200000,S,seller3")
        assert "ACK,1002" in r3

        seller.disconnect()
        time.sleep(0.3)

        # Execute a market buy for 120 shares (will walk through all three levels)
        buyer = OrderClient('localhost', order_port)
        assert buyer.connect()
        response = buyer.send_order("market,120,0,B,buyer")

        # Should have multiple FILL messages at different prices
        assert "FILL" in response
        lines = response.strip().split('\n')
        # Market orders can have multiple fill responses
        assert len(lines) >= 2, "Should have multiple fill responses"

        buyer.disconnect()
        time.sleep(0.3)

        # Stop listener
        stop_listener.set()
        listener_thread.join(timeout=2.0)

        # Find all EXECUTE messages
        execute_messages = []
        for msg in received_messages:
            parts = msg.split(',')
            if len(parts) == 6:
                event_type = int(parts[1])
                if event_type == 4:  # EXECUTE
                    order_id = int(parts[2])
                    size = int(parts[3])
                    price = int(parts[4])
                    execute_messages.append({
                        'order_id': order_id,
                        'size': size,
                        'price': price,
                        'raw': msg
                    })

        # Verify we got EXECUTE messages for multiple orders
        assert len(execute_messages) >= 3, \
            f"Expected at least 3 EXECUTE messages for 3 filled orders, " \
            f"but found {len(execute_messages)}. Messages: {execute_messages}"

        # Verify executions at different price levels
        # Order 1000: 50 @ 50000000
        order_1000_executes = [e for e in execute_messages if e['order_id'] == 1000]
        assert len(order_1000_executes) >= 1, "Should have EXECUTE for order 1000"
        assert order_1000_executes[0]['size'] == 50
        assert order_1000_executes[0]['price'] == 50000000

        # Order 1001: 50 @ 50100000
        order_1001_executes = [e for e in execute_messages if e['order_id'] == 1001]
        assert len(order_1001_executes) >= 1, "Should have EXECUTE for order 1001"
        assert order_1001_executes[0]['size'] == 50
        assert order_1001_executes[0]['price'] == 50100000

        # Order 1002: 20 @ 50200000 (partial fill)
        order_1002_executes = [e for e in execute_messages if e['order_id'] == 1002]
        assert len(order_1002_executes) >= 1, "Should have EXECUTE for order 1002"
        assert order_1002_executes[0]['size'] == 20
        assert order_1002_executes[0]['price'] == 50200000

    def test_book_builder_syncs_with_exchange_after_market_orders(
            self, server_with_udp):
        """
        Test that the BookBuilder's view of the order book stays in sync with
        the exchange's internal book state after processing limit and market orders.
        This verifies that all necessary UDP broadcasts (INSERT, EXECUTE) are sent.
        """
        server_obj, order_port, feed_port, market_data_port = server_with_udp

        # Set up BookBuilder to process market data
        builder = BookBuilder(record_prices=False)
        stop_listener = threading.Event()

        def udp_listener_with_builder():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.5)
            server_addr = ('127.0.0.1', market_data_port)
            sock.sendto(b"subscribe", server_addr)

            while not stop_listener.is_set():
                try:
                    data, _ = sock.recvfrom(1024)
                    msg = data.decode('utf-8').strip()
                    builder.process_message(msg)
                except socket.timeout:
                    continue
                except Exception:
                    break

            sock.close()

        listener_thread = threading.Thread(target=udp_listener_with_builder, daemon=True)
        listener_thread.start()
        time.sleep(0.2)

        # Build a book with multiple orders
        client = OrderClient('localhost', order_port)
        assert client.connect()

        # Post bids
        client.send_order("limit,100,50000000,B,buyer1")  # Order 1000
        client.send_order("limit,75,49000000,B,buyer2")   # Order 1001

        # Post asks
        client.send_order("limit,50,51000000,S,seller1")  # Order 1002
        client.send_order("limit,60,52000000,S,seller2")  # Order 1003

        time.sleep(0.5)  # Wait for INSERT messages

        # Verify book builder state matches exchange state (before market orders)
        exchange_bids, exchange_asks = server_obj._order_book.get_snapshot()
        builder_bids, builder_asks = builder.book.get_snapshot()

        assert len(exchange_bids) == len(builder_bids), \
            f"Bid count mismatch: exchange={len(exchange_bids)}, builder={len(builder_bids)}"
        assert len(exchange_asks) == len(builder_asks), \
            f"Ask count mismatch: exchange={len(exchange_asks)}, builder={len(builder_asks)}"

        # Verify best bid
        assert exchange_bids[0].price == builder_bids[0].price == 50000000
        assert exchange_bids[0].size == builder_bids[0].size == 100

        # Verify best ask
        assert exchange_asks[0].price == builder_asks[0].price == 51000000
        assert exchange_asks[0].size == builder_asks[0].size == 50

        # Execute a market sell that partially fills the best bid
        response = client.send_order("market,30,0,S,market_seller")
        assert "FILL" in response

        time.sleep(0.5)  # Wait for EXECUTE message

        # Verify book builder still matches exchange after market order
        exchange_bids, exchange_asks = server_obj._order_book.get_snapshot()
        builder_bids, builder_asks = builder.book.get_snapshot()

        # Best bid should be partially filled: was 100, now 70
        assert exchange_bids[0].price == builder_bids[0].price == 50000000, \
            f"Best bid price mismatch: exchange={exchange_bids[0].price}, " \
            f"builder={builder_bids[0].price}"
        assert exchange_bids[0].size == builder_bids[0].size == 70, \
            f"Best bid size mismatch after market order: " \
            f"exchange={exchange_bids[0].size}, builder={builder_bids[0].size}"

        # Execute a market buy that fully fills best ask and part of next
        response = client.send_order("market,80,0,B,market_buyer")
        assert "FILL" in response

        time.sleep(0.5)

        # Verify synchronization after second market order
        exchange_bids, exchange_asks = server_obj._order_book.get_snapshot()
        builder_bids, builder_asks = builder.book.get_snapshot()

        # Best ask was 50 @ 51000000, next was 60 @ 52000000
        # Market buy of 80 should consume all of first ask (50) and 30 of second
        # So best ask should now be 30 @ 52000000
        assert exchange_asks[0].price == builder_asks[0].price == 52000000, \
            f"Best ask price mismatch: exchange={exchange_asks[0].price}, " \
            f"builder={builder_asks[0].price}"
        assert exchange_asks[0].size == builder_asks[0].size == 30, \
            f"Best ask size mismatch after market order: " \
            f"exchange={exchange_asks[0].size}, builder={builder_asks[0].size}"

        stop_listener.set()
        listener_thread.join(timeout=2.0)
        client.disconnect()

    def test_market_order_partial_fill_broadcasts_correct_size(self, server_with_udp):
        """
        Test that when a market order partially fills a resting order, the EXECUTE
        message contains the correct executed size (not the resting order's full size).
        """
        server_obj, order_port, feed_port, market_data_port = server_with_udp

        # Set up UDP listener
        received_messages = []
        stop_listener = threading.Event()

        def udp_listener():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.5)
            server_addr = ('127.0.0.1', market_data_port)
            sock.sendto(b"subscribe", server_addr)

            while not stop_listener.is_set():
                try:
                    data, _ = sock.recvfrom(1024)
                    msg = data.decode('utf-8').strip()
                    received_messages.append(msg)
                except socket.timeout:
                    continue
                except Exception:
                    break

            sock.close()

        listener_thread = threading.Thread(target=udp_listener, daemon=True)
        listener_thread.start()
        time.sleep(0.2)

        # Post a resting ask for 100 shares
        seller = OrderClient('localhost', order_port)
        assert seller.connect()
        response = seller.send_order("limit,100,50000000,S,seller")
        assert "ACK,1000" in response
        seller.disconnect()

        time.sleep(0.3)

        # Market buy for only 25 shares (partial fill)
        buyer = OrderClient('localhost', order_port)
        assert buyer.connect()
        response = buyer.send_order("market,25,0,B,buyer")
        assert "FILL" in response
        assert "25" in response
        buyer.disconnect()

        time.sleep(0.3)

        # Stop listener
        stop_listener.set()
        listener_thread.join(timeout=2.0)

        # Find EXECUTE message for order 1000
        execute_messages = []
        for msg in received_messages:
            parts = msg.split(',')
            if len(parts) == 6:
                event_type = int(parts[1])
                order_id = int(parts[2])
                if event_type == 4 and order_id == 1000:
                    execute_messages.append(msg)

        assert len(execute_messages) >= 1, \
            f"Expected EXECUTE message for partial fill, got: {received_messages}"

        # Verify the EXECUTE size is 25, not 100
        execute_msg = execute_messages[0]
        parts = execute_msg.split(',')
        assert int(parts[3]) == 25, \
            f"EXECUTE size should be 25 (partial fill), but got {parts[3]}"

    def test_market_order_execute_direction_matches_resting_order_side(
            self, server_with_udp):
        """
        Test that the direction in the EXECUTE message corresponds to the side
        of the resting order being executed, not the aggressor.

        LOBSTER format: Direction 1=Buy, -1=Sell
        - Market buy fills resting ask -> EXECUTE direction should be -1 (Sell)
        - Market sell fills resting bid -> EXECUTE direction should be 1 (Buy)
        """
        server_obj, order_port, feed_port, market_data_port = server_with_udp

        # Set up UDP listener
        received_messages = []
        stop_listener = threading.Event()

        def udp_listener():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.5)
            server_addr = ('127.0.0.1', market_data_port)
            sock.sendto(b"subscribe", server_addr)

            while not stop_listener.is_set():
                try:
                    data, _ = sock.recvfrom(1024)
                    msg = data.decode('utf-8').strip()
                    received_messages.append(msg)
                except socket.timeout:
                    continue
                except Exception:
                    break

            sock.close()

        listener_thread = threading.Thread(target=udp_listener, daemon=True)
        listener_thread.start()
        time.sleep(0.2)

        # Test 1: Market buy fills resting ask
        seller = OrderClient('localhost', order_port)
        assert seller.connect()
        seller.send_order("limit,50,50000000,S,seller")  # Resting ask (order 1000)
        seller.disconnect()

        time.sleep(0.3)

        buyer = OrderClient('localhost', order_port)
        assert buyer.connect()
        buyer.send_order("market,50,0,B,buyer")  # Market buy
        buyer.disconnect()

        time.sleep(0.3)

        # Find EXECUTE for order 1000 (resting ask)
        execute_1000 = None
        for msg in received_messages:
            parts = msg.split(',')
            if len(parts) == 6 and int(parts[1]) == 4 and int(parts[2]) == 1000:
                execute_1000 = msg
                break

        assert execute_1000 is not None, "Should have EXECUTE for order 1000"
        parts = execute_1000.split(',')
        assert int(parts[5]) == -1, \
            "Direction should be -1 (Sell) when resting ask is executed"

        # Test 2: Market sell fills resting bid
        buyer2 = OrderClient('localhost', order_port)
        assert buyer2.connect()
        buyer2.send_order("limit,50,49000000,B,buyer2")  # Resting bid (order 1001)
        buyer2.disconnect()

        time.sleep(0.3)

        seller2 = OrderClient('localhost', order_port)
        assert seller2.connect()
        seller2.send_order("market,50,0,S,seller2")  # Market sell
        seller2.disconnect()

        time.sleep(0.3)

        # Stop listener
        stop_listener.set()
        listener_thread.join(timeout=2.0)

        # Find EXECUTE for order 1001 (resting bid)
        execute_1001 = None
        for msg in received_messages:
            parts = msg.split(',')
            if len(parts) == 6 and int(parts[1]) == 4 and int(parts[2]) == 1001:
                execute_1001 = msg
                break

        assert execute_1001 is not None, "Should have EXECUTE for order 1001"
        parts = execute_1001.split(',')
        assert int(parts[5]) == 1, \
            "Direction should be 1 (Buy) when resting bid is executed"


class TestMarketOrderUDPComparisonWithLimitOrder:
    """
    Comparison tests to verify market order broadcasts match limit order broadcasts.
    Both should produce identical EXECUTE messages when they cross.
    """

    @pytest.fixture
    def server_with_udp(self):
        """Create and start a test server with UDP market data enabled."""
        order_port = find_free_port()
        feed_port = find_free_port()
        market_data_port = find_free_port()
        server = ExchangeServer(order_port, feed_port, market_data_port)
        assert server.start()
        time.sleep(0.2)
        yield server, order_port, feed_port, market_data_port
        server.stop()

    def test_market_and_limit_produce_same_execute_format(self, server_with_udp):
        """
        Verify that a market order and a crossing limit order produce EXECUTE
        messages in the same format for the resting order.
        """
        server_obj, order_port, feed_port, market_data_port = server_with_udp

        # Set up UDP listener
        received_messages = []
        stop_listener = threading.Event()

        def udp_listener():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.5)
            server_addr = ('127.0.0.1', market_data_port)
            sock.sendto(b"subscribe", server_addr)

            while not stop_listener.is_set():
                try:
                    data, _ = sock.recvfrom(1024)
                    msg = data.decode('utf-8').strip()
                    received_messages.append(msg)
                except socket.timeout:
                    continue
                except Exception:
                    break

            sock.close()

        listener_thread = threading.Thread(target=udp_listener, daemon=True)
        listener_thread.start()
        time.sleep(0.2)

        # Test 1: Limit order crossing
        client = OrderClient('localhost', order_port)
        assert client.connect()

        client.send_order("limit,50,50000000,S,seller1")  # Resting ask (order 1000)
        time.sleep(0.3)

        client.send_order("limit,50,50000000,B,buyer1")   # Crossing limit buy
        time.sleep(0.3)

        # Find EXECUTE for order 1000 from limit order
        limit_execute = None
        for msg in received_messages:
            parts = msg.split(',')
            if len(parts) == 6 and int(parts[1]) == 4 and int(parts[2]) == 1000:
                limit_execute = msg
                break

        assert limit_execute is not None, "Should have EXECUTE from limit order crossing"
        limit_parts = limit_execute.split(',')

        # Test 2: Market order crossing (same scenario)
        received_messages.clear()  # Clear previous messages

        client.send_order("limit,50,51000000,S,seller2")  # Resting ask (order 1002)
        time.sleep(0.3)

        client.send_order("market,50,0,B,buyer2")         # Market buy
        time.sleep(0.3)

        # Find EXECUTE for order 1002 from market order
        market_execute = None
        for msg in received_messages:
            parts = msg.split(',')
            if len(parts) == 6 and int(parts[1]) == 4 and int(parts[2]) == 1002:
                market_execute = msg
                break

        assert market_execute is not None, "Should have EXECUTE from market order"
        market_parts = market_execute.split(',')

        # Compare message formats (excluding order_id and timestamp)
        # Format: Time,Type,OrderID,Size,Price,Direction
        assert limit_parts[1] == market_parts[1] == "4", "Both should be EXECUTE (type 4)"
        assert limit_parts[3] == market_parts[3] == "50", "Both should execute same size"
        assert limit_parts[5] == market_parts[5] == "-1", \
            "Both should have direction -1 (Sell side executed)"

        stop_listener.set()
        listener_thread.join(timeout=2.0)
        client.disconnect()
