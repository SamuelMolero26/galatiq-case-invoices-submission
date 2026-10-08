"""LLM threat matrix (REQ-THR-1..5): scripted hostile inputs must not exceed role authority."""

import io
import socket

import pytest


def test_no_network_blocks_connect(no_network):
    with pytest.raises(RuntimeError):
        socket.create_connection(("127.0.0.1", 9))
    with pytest.raises(RuntimeError):
        socket.getaddrinfo("example.invalid", 80)
    with pytest.raises(RuntimeError):
        socket.socket().connect(("127.0.0.1", 9))
    assert [kind for kind, _ in no_network] == ["create_connection", "getaddrinfo", "connect"]


def test_no_fs_access_records_open(no_fs_access, tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("x")
    with no_fs_access() as attempts:
        with pytest.raises(PermissionError):
            open(target)
        with pytest.raises(PermissionError):
            io.open(target)
        with pytest.raises(PermissionError):
            eval("1 + 1")
    assert [kind for kind, _ in attempts] == ["open", "io.open", "eval"]
    assert target.read_text() == "x"  # guards are released outside the with
