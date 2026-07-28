from __future__ import annotations

import json
import queue
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

from app.platform.core.settings import get_settings
from app.platform.trace.recorder import _notify_evolution


class TraceW3CHttpTest(unittest.TestCase):
    def test_evolution_notification_propagates_traceparent_over_http(self) -> None:
        received: queue.Queue[tuple[dict[str, str], dict[str, object]]] = queue.Queue()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("content-length", "0"))
                body = json.loads(self.rfile.read(length))
                received.put(({key.lower(): value for key, value in self.headers.items()}, body))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{}')

            def log_message(self, _format: str, *_args: object) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.handle_request, daemon=True)
        server_thread.start()
        settings = get_settings()
        old_url = settings.evolution_notify_url
        old_token = settings.evolution_notify_token
        traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        try:
            settings.evolution_notify_url = f"http://127.0.0.1:{server.server_port}/api/ingestion/notify"
            settings.evolution_notify_token = "notify-secret"
            _notify_evolution(
                SimpleNamespace(),
                "trace-w3c",
                "completed",
                {"external_refs": {"traceparent": traceparent}},
            )
            headers, body = received.get(timeout=3)
        finally:
            settings.evolution_notify_url = old_url
            settings.evolution_notify_token = old_token
            server.server_close()
            server_thread.join(timeout=3)

        self.assertEqual(headers["traceparent"], traceparent)
        self.assertEqual(headers["x-notify-token"], "notify-secret")
        self.assertEqual(body, {"trace_id": "trace-w3c", "status": "completed"})


if __name__ == "__main__":
    unittest.main()
