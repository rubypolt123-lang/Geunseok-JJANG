"""The autouse ``_block_network`` fixture: loopback works, everything else is blocked (SPEC §14.1)."""

from __future__ import annotations

import os
import socket
import threading

import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import NETWORK_DISABLED_MESSAGE


def test_loopback_allowed_and_testclient_works() -> None:
    app = FastAPI()

    @app.get("/x")
    def x() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app) as client:
        response = client.get("/x")
    assert response.status_code == 200
    assert response.json() == {"ok": True}

    # plain loopback TCP (127.0.0.1) and socketpair also work
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        accepted: list[socket.socket] = []
        thread = threading.Thread(target=lambda: accepted.append(server.accept()[0]))
        thread.start()
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client_sock:
            client_sock.sendall(b"ping")
            thread.join(timeout=5)
            assert accepted
            with accepted[0] as conn:
                assert conn.recv(4) == b"ping"
    finally:
        server.close()
    a, b = socket.socketpair()
    with a, b:
        a.sendall(b"x")
        assert b.recv(1) == b"x"
    assert socket.getaddrinfo("localhost", 80)


def test_external_connect_blocked() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(RuntimeError, match=NETWORK_DISABLED_MESSAGE):
            sock.connect(("203.0.113.10", 443))  # TEST-NET-3, never routed
        with pytest.raises(RuntimeError, match=NETWORK_DISABLED_MESSAGE):
            sock.connect_ex(("203.0.113.10", 443))
    with pytest.raises(RuntimeError, match=NETWORK_DISABLED_MESSAGE):
        socket.create_connection(("fapi.binance.com", 443), timeout=1)
    with pytest.raises(RuntimeError, match=NETWORK_DISABLED_MESSAGE):
        socket.getaddrinfo("fapi.binance.com", 443)
    with pytest.raises(RuntimeError, match=NETWORK_DISABLED_MESSAGE):
        requests.get("https://fapi.binance.com/fapi/v1/time", timeout=1)


def test_env_is_isolated() -> None:
    for key in (
        "CONFIRM_LIVE_TRADING",
        "BINANCE_API_KEY",
        "BINANCE_API_SECRET",
        "BINANCE_TESTNET_API_KEY",
        "BINANCE_TESTNET_API_SECRET",
    ):
        assert key not in os.environ
