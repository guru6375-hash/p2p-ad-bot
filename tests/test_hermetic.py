"""Hermeticity proof: the suite cannot reach the network even by accident.

``tests/conftest.py`` installs an autouse guard that replaces ``socket.socket.connect``
and ``socket.create_connection`` with a raising stub. This module demonstrates that the
guard is really in force, so any product path that tried to open a socket (rather than the
injected ``FakeTransport``) would fail loudly instead of silently hitting the internet.
"""

from __future__ import annotations

import socket
import urllib.request

import pytest

from conftest import NetworkBlocked


def test_direct_socket_connections_are_blocked() -> None:
    with pytest.raises(NetworkBlocked):
        socket.socket().connect(("127.0.0.1", 9))


def test_create_connection_is_blocked() -> None:
    with pytest.raises(NetworkBlocked):
        socket.create_connection(("127.0.0.1", 9), timeout=1)


def test_urllib_cannot_open_a_connection() -> None:
    """A real HTTP client would have to open a socket, so it is blocked too."""
    with pytest.raises(NetworkBlocked):
        urllib.request.urlopen("http://127.0.0.1:9/", timeout=1)
