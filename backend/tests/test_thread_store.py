import tempfile
import unittest
from pathlib import Path

from app.core.thread_store import ThreadStore


class ThreadStoreTest(unittest.TestCase):
    def test_create_workspace_uses_frontend_input_name_as_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ThreadStore(Path(tmpdir))

            workspace = store.create_workspace("星际 旅程")

            self.assertEqual(workspace.workspace_id, "星际 旅程")
            self.assertEqual(Path(workspace.workspace_path).name, "星际 旅程")
            self.assertTrue(Path(workspace.workspace_path).exists())


if __name__ == "__main__":
    unittest.main()
