"""
Tests for TTL order expiry UDP market data broadcast.

This test suite verifies the bug fix where _expire_order() was not broadcasting
a DELETE message over UDP market data when an order expired via TTL.
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


class TestTTLExpiryUDPBroadcast:
    """Tests for TTL expiry UDP market data broadcast."""

    @pytest.fixture
    def server_with_udp(self):
        """Create and start a test server with UDP market data enabled."""
        order_port = find_free_port()
        feed_port = find_free_port()
        market_data_port = find_free_port()
        server = ExchangeServer(order_port, feed_port, market_data_port)
        assert server.start()

        # Start the main loop in a background thread to enable TTL expiry checks
        run_thread = threading.Thread(target=server.run, daemon=True)
        run_thread.start()

        time.sleep(0.2)  # Allow server components to fully start
        yield server, order_port, feed_port, market_data_port
        server.stop()

    def test_expired_order_broadcasts_delete_message(self, server_with_udp):
        """
        Test that when a limit order with a short TTL expires, a DELETE message
        is broadcast on the UDP market data feed (type=3 DELETE event).
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
                except Exception as e:
                    break

            sock.close()

        listener_thread = threading.Thread(target=udp_listener, daemon=True)
        listener_thread.start()
        time.sleep(0.2)  # Allow subscription to complete

        # Place an order with very short TTL (2 seconds)
        client = OrderClient('localhost', order_port)
        assert client.connect()
        response = client.send_order("limit,100,50000000,B,trader1,2")
        assert "ACK" in response
        assert "1000" in response  # First order ID

        # Wait for initial INSERT message
        time.sleep(0.5)
        initial_count = len(received_messages)
        assert initial_count >= 1  # Should have received INSERT

        # Wait for order to expire (2 second TTL + margin)
        time.sleep(2.5)

        # Stop listener and collect messages
        stop_listener.set()
        listener_thread.join(timeout=2.0)
        client.disconnect()

        # Find the DELETE message for order_id 1000
        delete_messages = []
        for msg in received_messages:
            parts = msg.split(',')
            if len(parts) == 6:
                event_type = int(parts[1])
                order_id = int(parts[2])
                if event_type == 3 and order_id == 1000:  # DELETE type
                    delete_messages.append(msg)

        # Verify DELETE message was broadcast
        assert len(delete_messages) >= 1, \
            f"Expected at least one DELETE message for expired order 1000, " \
            f"but found {len(delete_messages)}. Messages: {received_messages}"

        # Verify DELETE message contents
        delete_msg = delete_messages[0]
        parts = delete_msg.split(',')
        assert int(parts[1]) == 3  # Event type = DELETE
        assert int(parts[2]) == 1000  # Order ID
        assert int(parts[3]) == 100  # Size
        assert int(parts[4]) == 50000000  # Price
        assert int(parts[5]) == 1  # Direction (1=Buy)

    def test_book_builder_removes_expired_order(self, server_with_udp):
        """
        Test that the BookBuilder properly removes an expired order from
        the book when it receives the DELETE message.
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
        time.sleep(0.2)  # Allow subscription to complete

        # Place a bid with short TTL (2 seconds)
        client = OrderClient('localhost', order_port)
        assert client.connect()
        response = client.send_order("limit,100,50000000,B,trader1,2")
        assert "ACK,1000" in response

        # Wait for INSERT to be processed
        time.sleep(0.5)

        # Verify order is in the book
        bids, asks = builder.book.get_snapshot()
        assert len(bids) == 1, "Bid should be in the book after INSERT"
        assert bids[0].price == 50000000
        assert bids[0].size == 100

        # Wait for order to expire
        time.sleep(2.5)

        # Verify order has been removed from the book
        bids, asks = builder.book.get_snapshot()
        assert len(bids) == 0, \
            f"Bid should have been removed from book after expiry, " \
            f"but found {len(bids)} bids: {bids}"

        stop_listener.set()
        listener_thread.join(timeout=2.0)
        client.disconnect()

    def test_expired_order_removed_from_exchange_book(self, server_with_udp):
        """
        Test that an expired order is removed from the exchange's internal
        order book (not just the UDP feed).
        """
        server_obj, order_port, feed_port, market_data_port = server_with_udp

        # Place an order with short TTL
        client = OrderClient('localhost', order_port)
        assert client.connect()
        client.send_order("limit,100,50000000,B,trader1,2")
        time.sleep(0.3)

        # Verify order is in the exchange book
        bids, asks = server_obj._order_book.get_snapshot()
        assert len(bids) == 1

        # Wait for expiry
        time.sleep(2.5)

        # Verify order removed from exchange book
        bids, asks = server_obj._order_book.get_snapshot()
        assert len(bids) == 0, "Order should be removed from exchange book after TTL expiry"

        client.disconnect()

    def test_multiple_orders_at_same_price_aggregated(self, server_with_udp):
        """
        Test that multiple orders at the same price level are properly
        aggregated in the OrderBook's get_snapshot (basic sanity test).
        """
        server_obj, order_port, feed_port, market_data_port = server_with_udp

        # Set up BookBuilder
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

        # Place multiple orders at the same price level
        client = OrderClient('localhost', order_port)
        assert client.connect()

        # Three bids at 5000.00
        client.send_order("limit,100,50000000,B,trader1")
        client.send_order("limit,50,50000000,B,trader2")
        client.send_order("limit,75,50000000,B,trader3")

        # Two asks at 5100.00
        client.send_order("limit,60,51000000,S,trader1")
        client.send_order("limit,40,51000000,S,trader2")

        time.sleep(0.5)  # Allow messages to be processed

        # Check aggregation in BookBuilder
        bids, asks = builder.book.get_snapshot()

        # Should have aggregated bids at 50000000
        assert len(bids) >= 1
        bid_at_5000 = [b for b in bids if b.price == 50000000]
        assert len(bid_at_5000) == 1, "Should have one aggregated bid level at 5000.00"
        assert bid_at_5000[0].size == 225, \
            f"Bid size should be 100+50+75=225, but got {bid_at_5000[0].size}"
        assert bid_at_5000[0].order_count == 3, \
            f"Bid order_count should be 3 orders, but got {bid_at_5000[0].order_count}"

        # Should have aggregated asks at 51000000
        assert len(asks) >= 1
        ask_at_5100 = [a for a in asks if a.price == 51000000]
        assert len(ask_at_5100) == 1, "Should have one aggregated ask level at 5100.00"
        assert ask_at_5100[0].size == 100, \
            f"Ask size should be 60+40=100, but got {ask_at_5100[0].size}"
        assert ask_at_5100[0].order_count == 2, \
            f"Ask order_count should be 2 orders, but got {ask_at_5100[0].order_count}"

        stop_listener.set()
        listener_thread.join(timeout=2.0)
        client.disconnect()

    def test_expired_order_maintains_aggregation(self, server_with_udp):
        """
        Test that when one order expires from a price level with multiple orders,
        the remaining orders are still properly aggregated.
        """
        server_obj, order_port, feed_port, market_data_port = server_with_udp

        # Set up BookBuilder
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

        # Place multiple orders at same price, one with short TTL
        client = OrderClient('localhost', order_port)
        assert client.connect()

        # Two orders at 5000.00: one normal, one with 2 second TTL
        client.send_order("limit,100,50000000,B,trader1")  # No TTL (default 3600s)
        client.send_order("limit,50,50000000,B,trader2,2")  # 2 second TTL

        time.sleep(0.5)

        # Verify initial aggregation
        bids, asks = builder.book.get_snapshot()
        bid_at_5000 = [b for b in bids if b.price == 50000000]
        assert len(bid_at_5000) == 1
        assert bid_at_5000[0].size == 150, "Initial size should be 100+50=150"
        assert bid_at_5000[0].order_count == 2, "Should have 2 orders initially"

        # Wait for one order to expire
        time.sleep(2.5)

        # Verify remaining order is still properly displayed
        bids, asks = builder.book.get_snapshot()
        bid_at_5000 = [b for b in bids if b.price == 50000000]
        assert len(bid_at_5000) == 1, "Should still have bid level at 5000.00"
        assert bid_at_5000[0].size == 100, \
            f"After expiry, size should be 100, but got {bid_at_5000[0].size}"
        assert bid_at_5000[0].order_count == 1, \
            f"After expiry, should have 1 order, but got {bid_at_5000[0].order_count}"

        stop_listener.set()
        listener_thread.join(timeout=2.0)
        client.disconnect()

    def test_expired_order_client_receives_notification(self, server_with_udp):
        """
        Test that the client receives an EXPIRED notification when their order
        expires via TTL.
        """
        server_obj, order_port, feed_port, market_data_port = server_with_udp

        # Client with async message handling
        client_messages = []

        def message_callback(msg):
            client_messages.append(msg)

        client = OrderClient('localhost', order_port)
        assert client.connect()
        client.start_async_receive(message_callback)
        time.sleep(0.1)

        # Place order with short TTL
        client.send_order_async("limit,100,50000000,B,trader1,2")
        time.sleep(0.3)  # Wait for ACK

        # Wait for expiry
        time.sleep(2.5)

        # Verify EXPIRED notification received
        expired_msgs = [m for m in client_messages if "EXPIRED" in m]
        assert len(expired_msgs) >= 1, \
            f"Client should receive EXPIRED notification, " \
            f"but got messages: {client_messages}"

        # Verify EXPIRED message format: EXPIRED,order_id,size
        expired_msg = expired_msgs[0]
        parts = expired_msg.strip().split(',')
        assert parts[0] == "EXPIRED"
        assert int(parts[1]) == 1000  # Order ID
        assert int(parts[2]) == 100  # Size

        client.disconnect()

    def test_no_delete_broadcast_for_cancelled_order(self, server_with_udp):
        """
        Test that if an order is cancelled before it expires, the TTL expiry
        mechanism doesn't try to broadcast a DELETE for an already-removed order.
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

        # Place order with short TTL
        client = OrderClient('localhost', order_port)
        assert client.connect()
        response = client.send_order("limit,100,50000000,B,trader1,3")
        assert "ACK,1000" in response

        time.sleep(0.3)

        # Cancel the order before it expires
        cancel_response = client.send_order("cancel,1000,trader1")
        assert "CANCEL_ACK" in cancel_response

        # Wait past the TTL expiry time
        time.sleep(3.5)

        # Count DELETE messages for order 1000
        delete_count = 0
        for msg in received_messages:
            parts = msg.split(',')
            if len(parts) == 6:
                event_type = int(parts[1])
                order_id = int(parts[2])
                if event_type == 3 and order_id == 1000:
                    delete_count += 1

        # Should only have one DELETE (from the cancel), not from TTL expiry
        assert delete_count == 1, \
            f"Should have exactly one DELETE (from cancel), but got {delete_count}"

        stop_listener.set()
        listener_thread.join(timeout=2.0)
        client.disconnect()


class TestOrderBookAggregation:
    """Sanity tests for OrderBook aggregation logic."""

    @pytest.fixture
    def server(self):
        """Create a basic server for aggregation tests."""
        order_port = find_free_port()
        feed_port = find_free_port()
        server = ExchangeServer(order_port, feed_port)
        assert server.start()
        time.sleep(0.1)
        yield server, order_port, feed_port
        server.stop()

    def test_bid_aggregation_at_single_price(self, server):
        """Test that multiple bids at the same price are aggregated correctly."""
        server_obj, order_port, feed_port = server

        client = OrderClient('localhost', order_port)
        assert client.connect()

        # Place three bids at 5000.00
        client.send_order("limit,100,50000000,B,trader1")
        client.send_order("limit,75,50000000,B,trader2")
        client.send_order("limit,50,50000000,B,trader3")

        time.sleep(0.2)

        # Check aggregation
        bids, asks = server_obj._order_book.get_snapshot()
        assert len(bids) == 1
        assert bids[0].price == 50000000
        assert bids[0].size == 225  # 100 + 75 + 50
        assert bids[0].order_count == 3

        client.disconnect()

    def test_ask_aggregation_at_single_price(self, server):
        """Test that multiple asks at the same price are aggregated correctly."""
        server_obj, order_port, feed_port = server

        client = OrderClient('localhost', order_port)
        assert client.connect()

        # Place three asks at 5100.00
        client.send_order("limit,60,51000000,S,trader1")
        client.send_order("limit,40,51000000,S,trader2")
        client.send_order("limit,30,51000000,S,trader3")

        time.sleep(0.2)

        # Check aggregation
        bids, asks = server_obj._order_book.get_snapshot()
        assert len(asks) == 1
        assert asks[0].price == 51000000
        assert asks[0].size == 130  # 60 + 40 + 30
        assert asks[0].order_count == 3

        client.disconnect()

    def test_multiple_price_levels_with_aggregation(self, server):
        """Test aggregation across multiple price levels."""
        server_obj, order_port, feed_port = server

        client = OrderClient('localhost', order_port)
        assert client.connect()

        # Bids at three different prices
        client.send_order("limit,100,50000000,B,trader1")
        client.send_order("limit,75,50000000,B,trader2")  # Same as first
        client.send_order("limit,50,49000000,B,trader3")  # Different price
        client.send_order("limit,25,49000000,B,trader4")  # Same as third

        time.sleep(0.2)

        # Check aggregation
        bids, asks = server_obj._order_book.get_snapshot()
        assert len(bids) == 2

        # Best bid at 5000.00
        assert bids[0].price == 50000000
        assert bids[0].size == 175  # 100 + 75
        assert bids[0].order_count == 2

        # Second level at 4900.00
        assert bids[1].price == 49000000
        assert bids[1].size == 75  # 50 + 25
        assert bids[1].order_count == 2

        client.disconnect()
