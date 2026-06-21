"""monitoring 服务配置。

配置项见 .env.example。通过 pydantic-settings 从环境变量/.env 加载。
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 服务端口
    port: int = 7789
    # 后端 workspace 根目录（trace jsonl 所在）
    backend_workspace: str = "backend/workspace"
    # SQLite 数据库文件路径
    monitoring_db: str = "monitoring.db"
    # 后端服务地址（Phase 6 T15：活跃大盘轮询 + 拉取 trace 通知）
    backend_url: str = "http://localhost:8000"

    # ── LLM-judge 评估配置（第二期）──
    # 评估用模型（建议用便宜小模型，如 deepseek-chat / gpt-4o-mini）。
    # 留空则禁用 LLM-judge（仅规则标红）。
    judge_model: str = ""
    judge_api_key: str = ""
    judge_base_url: str = ""

    # ── Self-Harness（Phase 2）──
    # harness 代码根目录（harnesses/<version>/harness.py）。相对路径基于项目根。
    harnesses_root: str = "backend/app/harnesses"

    @property
    def _monitoring_root(self) -> Path:
        """monitoring/ 目录（本文件在 app/ 下，上一级是 monitoring/）。"""
        return Path(__file__).resolve().parent.parent

    @property
    def _project_root(self) -> Path:
        """项目根 Writer/（monitoring/ 的上一级）。"""
        return self._monitoring_root.parent

    @property
    def backend_workspace_path(self) -> Path:
        """后端 workspace 的绝对路径（trace jsonl 根）。

        相对路径基于项目根 Writer/（backend 是 Writer/ 下的兄弟目录）。
        """
        path = Path(self.backend_workspace)
        if not path.is_absolute():
            path = self._project_root / path
        return path.resolve()

    @property
    def db_path(self) -> Path:
        """SQLite 数据库文件的绝对路径。相对路径基于 monitoring/ 目录。"""
        path = Path(self.monitoring_db)
        if not path.is_absolute():
            path = self._monitoring_root / path
        return path.resolve()


settings = Settings()
