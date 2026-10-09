"""The model transport keeps one connection per thread and reuses it."""

import http.server
import threading

import pytest

from invoice_pipeline.llm import LLMError, TierConfig, chat


class _Model(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, status=200, close_after_reply=False):
        self.connections, self.status, self.close_after_reply = 0, status, close_after_reply
        super().__init__(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def get_request(self):
        self.connections += 1
        return super().get_request()

    @property
    def tier(self):
        return TierConfig(
            tier="grok",
            model="m",
            base_url=f"http://127.0.0.1:{self.server_address[1]}/v1",
            timeout_s=5,
            api_key="k",
        )


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        body = b'{"choices":[{"message":{"content":"{}"}}]}' if self.server.status == 200 else b"no"
        self.send_response(self.server.status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        # closing without saying so leaves the client holding a dead keep-alive socket
        self.close_connection = self.server.close_after_reply

    def log_message(self, *args):
        pass


@pytest.fixture
def model():
    servers = []

    def make(**kwargs):
        servers.append(_Model(**kwargs))
        return servers[-1]

    yield make
    for server in servers:
        server.shutdown()
        server.server_close()


def test_chat_reuses_one_connection(model):
    server = model()
    for _ in range(3):
        assert chat(server.tier, [{"role": "user", "content": "hi"}]).content == "{}"
    assert server.connections == 1


def test_chat_reconnects_when_the_idle_connection_died(model):
    server = model(close_after_reply=True)
    for _ in range(2):
        assert chat(server.tier, [{"role": "user", "content": "hi"}]).content == "{}"
    assert server.connections == 2


def test_chat_reports_an_http_error(model):
    server = model(status=500)
    with pytest.raises(LLMError, match="HTTP 500"):
        chat(server.tier, [{"role": "user", "content": "hi"}])
