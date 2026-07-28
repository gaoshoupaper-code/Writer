from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.ingestion.ingestion import router


class TraceV2W3CHttpTest(unittest.TestCase):
    def test_notify_route_passes_traceparent_to_background_ingestion(self) -> None:
        app = FastAPI()
        app.include_router(router, prefix="/api")
        traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"

        with patch(
            "app.ingestion.ingestion._ingest_async", new_callable=AsyncMock
        ) as ingest:
            response = TestClient(app).post(
                "/api/ingestion/notify",
                json={"trace_id": "trace-w3c", "status": "completed"},
                headers={"traceparent": traceparent},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["trace_id"], "trace-w3c")
        ingest.assert_awaited_once_with("trace-w3c", traceparent)


if __name__ == "__main__":
    unittest.main()
