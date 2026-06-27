"""evolution 服务配置。

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
    # 执行端 workspace 根目录。
    # Phase 3 重构后：trace 摄入不再读这里（改走 HTTP），仅 eval_extractor
    # 读生成产物（deliverable）时用。后续 eval_extractor 解耦后可移除此配置。
    executor_workspace: str = "executor/workspace"
    # SQLite 数据库文件路径
    evolution_db: str = "evolution.db"
    # 执行端服务地址（Phase 3：HTTP 拉取 trace 内容 + 活跃大盘轮询）
    executor_url: str = "http://localhost:7788"

    # ── LLM-judge 评估配置（第二期）──
    # 评估用模型（建议用便宜小模型，如 deepseek-chat / gpt-4o-mini）。
    # 留空则禁用 LLM-judge（仅规则标红）。
    judge_model: str = ""
    judge_api_key: str = ""
    judge_base_url: str = ""

    # ── compose Git 传输层（Phase 8，决策 D10b/D11a）──
    # harness bare repo 路径（同机阶段本地路径，异机时改 URL）。
    # evolution 工作目录 harnesses/current/ commit → push 到此 bare repo。
    # executor 从此 bare repo pull/clone。
    harness_bare_repo: str = "harness.git"
    # harness 工作目录（evolution 编辑源码的地方）。
    harness_work_dir: str = "harnesses/current"

    @property
    def _evolution_root(self) -> Path:
        """evolution/ 目录（本文件在 app/core/ 下，上三级是 evolution/）。"""
        return Path(__file__).resolve().parent.parent.parent

    @property
    def _project_root(self) -> Path:
        """项目根 Writer/（evolution/ 的上一级）。"""
        return self._evolution_root.parent

    @property
    def executor_workspace_path(self) -> Path:
        """执行端 workspace 的绝对路径（trace jsonl 根）。

        相对路径基于项目根 Writer/（executor 是 Writer/ 下的兄弟目录）。
        """
        path = Path(self.executor_workspace)
        if not path.is_absolute():
            path = self._project_root / path
        return path.resolve()

    @property
    def db_path(self) -> Path:
        """SQLite 数据库文件的绝对路径。相对路径基于 evolution/ 目录。"""
        path = Path(self.evolution_db)
        if not path.is_absolute():
            path = self._evolution_root / path
        return path.resolve()

    @property
    def harness_bare_repo_path(self) -> Path:
        """harness bare repo 绝对路径。相对路径基于 evolution/ 目录（决策 D10b）。"""
        path = Path(self.harness_bare_repo)
        if not path.is_absolute():
            path = self._evolution_root / path
        return path.resolve()

    @property
    def harness_work_dir_path(self) -> Path:
        """harness 工作目录绝对路径（决策 D11a）。"""
        path = Path(self.harness_work_dir)
        if not path.is_absolute():
            path = self._evolution_root / path
        return path.resolve()


settings = Settings()
