"""Alpaca requests time out instead of hanging the cycle.

alpaca-py sends every request with no timeout, and requests waits forever by
default: on 2026-10-19 one dead socket held the cycle for 2h40m. Every client
broker.py builds must pass config.ALPACA_TIMEOUT unless a caller sets its own.
"""

import pytest

import broker
import config


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")


def capture(client, monkeypatch):
    """Swap the session's transport so no request leaves the machine."""
    seen = {}

    def send(request, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        raise RuntimeError("stop before the network")

    monkeypatch.setattr(client._session, "send", send)
    return seen


@pytest.mark.parametrize("make", [
    lambda: broker._trading_client("paper"),
    broker._data_client,
    broker._news_client,
])
def test_every_client_sends_the_default_timeout(make, monkeypatch):
    client = make()
    seen = capture(client, monkeypatch)
    with pytest.raises(RuntimeError):
        client._session.request("GET", "https://example.invalid/")
    assert seen["timeout"] == config.ALPACA_TIMEOUT


def test_an_explicit_timeout_is_kept(monkeypatch):
    client = broker._data_client()
    seen = capture(client, monkeypatch)
    with pytest.raises(RuntimeError):
        client._session.request("GET", "https://example.invalid/", timeout=3)
    assert seen["timeout"] == 3
