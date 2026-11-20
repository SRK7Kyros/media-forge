"""How the Jellyfin client authenticates.

Jellyfin 12.4 rejects ``X-Emby-Token`` with a 401; the header it accepts is
``Authorization: MediaBrowser Token="<key>"``. 10.x accepts the same scheme, so
it is the version-agnostic choice - the client must send that and only that.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from mediaforge.jellyfin import HttpJellyfinClient


class _Recorder(BaseHTTPRequestHandler):
    seen: list[dict[str, str]] = []

    def _answer(self) -> None:
        type(self).seen.append({k.lower(): v for k, v in self.headers.items()})
        body = b"[]"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._answer()

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._answer()

    def log_message(self, *args: object) -> None:
        """Silence the stub server's stderr chatter."""


def _serve() -> HTTPServer:
    server = HTTPServer(("127.0.0.1", 0), _Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_sends_the_mediabrowser_authorization_header() -> None:
    _Recorder.seen = []
    server = _serve()
    try:
        client = HttpJellyfinClient(f"http://127.0.0.1:{server.server_port}", "abc123")
        assert client.libraries() == []
    finally:
        server.shutdown()
        server.server_close()

    assert _Recorder.seen, "no request reached the stub server"
    headers = _Recorder.seen[-1]
    assert headers.get("authorization") == 'MediaBrowser Token="abc123"'
    assert "x-emby-token" not in headers


def test_public_endpoint_carries_no_credentials() -> None:
    _Recorder.seen = []
    server = _serve()
    try:
        client = HttpJellyfinClient(f"http://127.0.0.1:{server.server_port}", "abc123")
        client.available()
    finally:
        server.shutdown()
        server.server_close()

    assert _Recorder.seen, "no request reached the stub server"
    headers = _Recorder.seen[-1]
    assert "authorization" not in headers
    assert "x-emby-token" not in headers
