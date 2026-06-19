import tempfile
import unittest
from pathlib import Path

from app.platform.core.security import generate_master_key, load_master_key
from app.platform.state.thread_store import ThreadStore
from app.db import Database, UserRepository, init_database


class ThreadStoreTest(unittest.TestCase):
    """多用户改造后的 ThreadStore 冒烟测试。

    旧行为（workspace_id = 作品名、全局共享）已被取代为：
    workspace_id = uuid hex、目录 = workspace/<owner_id>/<workspace_id>/。
    """

    def test_create_workspace_is_owner_scoped_with_uuid(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "app.db", load_master_key(generate_master_key()))
            init_database(db)
            store = ThreadStore(db, Path(tmpdir) / "workspace")
            owner = UserRepository(db).create(username="owner", password="pw123456")

            workspace = store.create_workspace(owner["user_id"], "星际 旅程")

            # workspace_id 是 uuid hex，不再是作品名
            self.assertNotEqual(workspace.workspace_id, "星际 旅程")
            self.assertEqual(len(workspace.workspace_id), 32)
            # 目录在 owner 子目录下
            self.assertTrue(workspace.workspace_path.endswith(workspace.workspace_id))
            self.assertIn(owner["user_id"], workspace.workspace_path)
            self.assertTrue(Path(workspace.workspace_path).exists())

            db.close()  # Windows 下释放文件锁，避免 tmpdir 清理报错


if __name__ == "__main__":
    unittest.main()
