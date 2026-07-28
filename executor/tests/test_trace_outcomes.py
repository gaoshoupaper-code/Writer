from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.platform.trace.outcomes import OutcomeBuffer


class OutcomeBufferTest(unittest.TestCase):
    def test_outcome_survives_restart_and_is_acked_only_after_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "outcomes.db"
            first = OutcomeBuffer(path, evolution_url="http://evolution")
            outcome_id, capture_status = first.enqueue(
                target_trace_id="trace-1",
                outcome_type="copy",
                actor_user_id="user-1",
                payload={"content_preview": "正文"},
            )
            self.assertEqual(capture_status, "captured")

            restarted = OutcomeBuffer(path, evolution_url="http://evolution")
            response = unittest.mock.Mock(status_code=200)
            response.raise_for_status.return_value = None
            with patch("httpx.post", return_value=response) as post:
                self.assertEqual(restarted.deliver_pending(), 1)

            body = post.call_args.kwargs["json"]
            self.assertEqual(body["outcome_id"], outcome_id)
            self.assertEqual(body["target_id"], "trace-1")
            self.assertEqual(restarted.pending_count(), 0)

    def test_forbidden_payload_is_not_persisted_but_structural_outcome_remains(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "outcomes.db"
            buffer = OutcomeBuffer(path, evolution_url="")
            _, capture_status = buffer.enqueue(
                target_trace_id="trace-2",
                outcome_type="regenerate",
                actor_user_id="user-1",
                payload={"authorization": "Bearer must-not-be-stored"},
            )

            self.assertEqual(capture_status, "payload_rejected")
            self.assertNotIn(b"must-not-be-stored", path.read_bytes())
            self.assertEqual(buffer.pending_count(), 1)


if __name__ == "__main__":
    unittest.main()
