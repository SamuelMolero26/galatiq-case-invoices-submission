"""Shared fixtures. Tests never touch the network or the real data/ databases."""

import socket

import pytest


@pytest.fixture
def no_network(monkeypatch):
    """Fail any attempt to open a socket (offline-safety guard)."""

    def _blocked(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)
