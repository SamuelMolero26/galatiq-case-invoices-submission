"""Shared fixtures. Tests never touch the network or the real data/ databases."""

import argparse
import dataclasses
import json
import socket
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from invoice_pipeline import ledger, service
from invoice_pipeline.critic import offline_role
from invoice_pipeline.model import Agents, FindingCode, Outcome, finding
from tests.factories import NOW, make_arrival, make_catalog, make_invoice


@pytest.fixture
def no_network(monkeypatch):
    """Fail any attempt to open a socket (offline-safety guard)."""

    def _blocked(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)


@pytest.fixture
def loopback_only(monkeypatch):
    """Allow sockets to 127.0.0.1 only; any other destination fails the test."""
    real_connect = socket.socket.connect

    def _connect(self, address):
        if address[0] not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"network access attempted: {address[0]}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", _connect)


class StubLLM:
    """A loopback chat-completions server that replays a script and records requests.

    Script items: a dict (200 JSON body), `("status", code, text)`, or `("hang", seconds)`.
    """

    def __init__(self):
        self.requests: list[dict] = []
        self.items: list = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                stub.requests.append(
                    {
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": json.loads(body),
                    }
                )
                item = stub.items.pop(0) if stub.items else ("status", 500, "script exhausted")
                if isinstance(item, tuple) and item[0] == "hang":
                    time.sleep(item[1])
                    return
                code, payload = (200, json.dumps(item)) if isinstance(item, dict) else item[1:]
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload.encode())

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()

    def script(self, *items) -> StubLLM:
        self.items.extend(items)
        return self

    @staticmethod
    def reply(content=None, tool_calls=None, usage=None) -> dict:
        message: dict = {"role": "assistant", "content": content}
        if tool_calls:
            message["tool_calls"] = [
                {
                    "id": f"call_{i}",
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }
                for i, (name, arguments) in enumerate(tool_calls)
            ]
        body: dict = {"choices": [{"message": message}]}
        if usage:
            body["usage"] = usage
        return body


@pytest.fixture
def stub_llm(loopback_only):
    stub = StubLLM()
    yield stub
    stub.server.shutdown()
    stub.server.server_close()


class RecordingBank:
    """Canned bank adapter: records (vendor, amount, currency) calls, replays `reply`.

    An exception `reply` is raised instead of returned (declined/timeout path).
    """

    def __init__(self, reply=None):
        self.calls: list = []
        self.reply = {"status": "success"} if reply is None else reply

    def __call__(self, vendor, amount, currency):
        self.calls.append((vendor, amount, currency))
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply


def claimed(conn, number="INV-1", **invoice_kw):
    """An Approved arrival, recorded and claimed (Payment Pending) like the write phase does."""
    return ledger.record(
        conn, make_arrival(Outcome.APPROVED, make_invoice(number=number, **invoice_kw))
    )


def state_of(conn, arrival_id):
    return conn.execute(
        "SELECT state, amount_paid, payment_issue FROM arrivals WHERE id = ?", (arrival_id,)
    ).fetchone()


def later():
    return NOW + timedelta(minutes=5)


def bootstrap_runtime(tmp_path, llm=None, **overrides):
    """Offline-capable Runtime on temp databases, with caller-supplied overrides."""
    args = argparse.Namespace(
        llm=llm, ledger=tmp_path / "ledger.db", inventory=tmp_path / "inventory.db"
    )
    return dataclasses.replace(service.bootstrap(args), **overrides)


class ResolveEnv:
    """Fresh ledger + Runtime wired to a recording bank (resolve-suite harness)."""

    def __init__(self, tmp_path):
        self.path = tmp_path / "ledger.db"
        self.conn = ledger.connect(self.path)
        self.bank = RecordingBank()
        self.events = []
        self.rt = service.Runtime(
            catalog=make_catalog(),
            ledger_path=self.path,
            pay_fn=self.bank,
            on_event=self.events.append,
            now=lambda: NOW + timedelta(hours=1),
        )

    @property
    def calls(self):
        return self.bank.calls

    @property
    def reply(self):
        return self.bank.reply

    @reply.setter
    def reply(self, value):
        self.bank.reply = value

    def review(self, findings=(FindingCode.STOCK_SHORTAGE,), minutes=0, invoice=None, **kw):
        found = [finding(code, "x") for code in findings]
        return ledger.record(
            self.conn,
            make_arrival(
                Outcome.NEEDS_REVIEW,
                invoice or make_invoice(),
                found,
                arrived_at=NOW + timedelta(minutes=minutes),
                **kw,
            ),
        )

    def snapshot(self):
        return [tuple(r) for r in self.conn.execute("SELECT * FROM arrivals ORDER BY id")]

    def row(self, arrival_id):
        return self.conn.execute("SELECT * FROM arrivals WHERE id = ?", (arrival_id,)).fetchone()


class ServiceEnv:
    """Bootstrapped Runtime on temp databases, stub agents, recording bank."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.events, self.escalations = [], []
        self.bank = RecordingBank()
        self.fail_on = None  # invoice number whose escalate-only review raises
        self.rt = bootstrap_runtime(
            tmp_path,
            pay_fn=self.bank,
            on_event=self.events.append,
            agents=Agents(
                assess=lambda *a, **k: None,
                verify=lambda *a, **k: None,
                escalate_review=self.escalate,
                advise=lambda case_file, decision: offline_role("advisory"),
            ),
        )

    def escalate(self, case_file):
        self.escalations.append(case_file.invoice.invoice_number)
        if case_file.invoice.invoice_number == self.fail_on:
            raise RuntimeError("model blew up")
        return offline_role("escalate_review")

    @property
    def calls(self):
        return self.bank.calls

    def names(self):
        return [e.name for e in self.events]

    def rows(self):
        conn = ledger.connect(self.rt.ledger_path)
        try:
            return conn.execute("SELECT * FROM arrivals ORDER BY id").fetchall()
        finally:
            conn.close()


class GoldenRun:
    """Slice-1 corpus batch on fresh temp databases with a recording, succeeding bank."""

    def __init__(self, tmp_path, paths):
        self.bank = RecordingBank()
        self.rt = bootstrap_runtime(tmp_path, llm="offline", pay_fn=self.bank)
        self.paths = list(paths)
        self.batch = service.run_batch(self.paths, self.rt)
        self.by_file = {r.source: r for r in self.batch.results}

    @property
    def calls(self):
        return self.bank.calls

    def db(self):
        conn = ledger.connect(self.rt.ledger_path)
        try:
            return {r["id"]: r for r in conn.execute("SELECT * FROM arrivals ORDER BY id")}
        finally:
            conn.close()

    def row_of(self, arrival_file):
        return self.db()[self.by_file[arrival_file].arrival_id]
