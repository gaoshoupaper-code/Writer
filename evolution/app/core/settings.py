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
    # 执行端服务地址（Phase 3：HTTP 拉取 trace 内容 + 活跃大盘轮询）。
    # 桌面化后双重用途：内网 trace 拉取 + SSO 回调 /api/auth/me 验证。
    executor_url: str = "http://localhost:7788"

    # ── 大模型 API 配置（桌面化改造，2026-07-07）──
    # LLM key/base_url/model 不再从 env 读（删 judge_* 字段），改从 llm_config 表读
    # （桌面端填 → HTTP → evolution 加密存）。见 app/core/security.py + db.py。

    # AES-256-GCM 主密钥（hex 64 字符 或 urlsafe-base64）。
    # 用于加密 llm_config 表里的 api_key。生成：python -c "import secrets; print(secrets.token_hex(32))"
    # ⚠️ 设定后不可更改（历史加密 key 依赖它）。
    evolution_master_key: str = ""

    # SSO 白名单：允许访问 evolution 的 executor user_id（逗号分隔）。
    # 桌面端登录 executor → cookie 传给 evolution → evolution 回调 executor 验证 →
    # 校验 user_id ∈ 此白名单 → 放行/403。
    allowed_user_ids: str = ""

    # SSO 回调结果进程内缓存 TTL（秒）。避免每个请求都内网往返 executor。
    sso_cache_ttl_seconds: int = 60

    # 内网通知 token（executor → evolution /api/ingestion/notify 的鉴权）。
    # 替换旧 InternalKeyMiddleware 的 X-Internal-Key 机制。
    # 生成：python -c "import secrets; print(secrets.token_urlsafe(32))"
    notify_token: str = ""

    # ── 数据集维护者令牌（数据闭环设计 D17）──
    # growing→golden 升级需维护者校验。留空则不校验（开发模式兼容）。
    maintainer_token: str = ""

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
