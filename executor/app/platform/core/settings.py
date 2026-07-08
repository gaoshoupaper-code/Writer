from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    writer_model: str
    writer_temperature: float | None = Field(default=None, ge=0)
    writer_top_p: float | None = Field(default=None, ge=0, le=1)
    writer_agent_mode: str
    writer_frontend_origin: str
    # 全局默认 LLM key/base_url：多用户隔离下仅作管理员兜底。
    # 普通用户写作用各自设置页填写的 key（DB 加密存储，MASTER_KEY 解密），
    # 不依赖此全局值。留空时 executor 仍可启动——只要用户各自配了 key 即可写作。
    # （历史：曾为必填，2026-07 入侵后重建改为可空，key 不再集中存服务器）
    openai_api_key: str = ""
    openai_base_url: str = ""

    # ── 多用户隔离（Phase 1 新增）──────────────────────────────
    # API key 加密主密钥：32 字节，hex 或 base64 编码。AES-256-GCM 用。
    # 生成方式：python -c "import secrets; print(secrets.token_hex(32))"
    master_key: str

    # 元数据库路径（相对 executor 根，或绝对路径）。默认 executor/app.platform.core.db
    db_path: str = "app.platform.core.db"

    # 工作区根目录（默认 executor/workspace，内部按 user_id 分桶）
    workspace_root: str = "workspace"

    # 引导管理员：首启动若无管理员，按以下凭据创建
    admin_username: str = "admin"
    admin_password: str

    # Session Cookie 有效期（天），滚动续期
    session_ttl_days: int = 30

    # 默认作品配额（每用户作品数量上限）
    default_workspace_quota: int = 5

    # evolution 服务完成通知端点（trace 结束后 POST 通知 evolution 摄入）。
    # 留空则不通知（evolution 靠兜底扫描补）。
    evolution_notify_url: str = ""

    # evolution 内网通知 token（桌面化改造 2026-07-07）：
    # executor POST 到 evolution /api/ingestion/notify 时带 X-Notify-Token 头。
    # 与 evolution 的 settings.notify_token 必须一致。留空则不带（evolution 开发模式兼容）。
    evolution_notify_token: str = ""

    # evolution 服务基址（Phase 5 T10）：执行端 prompt loader 从 evolution 拉 prompt。
    # 形如 http://localhost:7789。留空则 loader 降级为读本地 .md 文件（兼容）。
    evolution_url: str = ""

    # prompt loader 本地缓存目录（Phase 5 T10）：从 evolution 拉的 prompt 缓存到此，
    # evolution 不可用时降级读缓存。默认 executor/.prompt_cache。
    prompt_cache_dir: str = ".prompt_cache"

    # manifest loader 本地缓存目录（Phase 6 T4.1）：evolution 不可用时降级读缓存。
    # 默认 executor/.manifest_cache。
    manifest_cache_dir: str = ".manifest_cache"

    # Agent 包目录路径（Phase 7 包化重构，D8=X 生产路径）。
    # 执行端同进程 import 此目录作为 Python package，调 assemble(ctx) 装配 agent。
    # 默认指向 evolution/harnesses/current/（同机部署，真理源在 evolution）。
    # 相对路径基于项目根 Writer/。
    harness_package_path: str = "evolution/harnesses/current"

    # ── compose Git 传输层（Phase 8，决策 D10b）──
    # harness bare repo 路径/URL。executor 从此 pull/clone 源码。
    # 同机阶段本地路径，异机时改 URL（如 git@host:harness.git）。
    harness_bare_repo: str = "evolution/harness.git"
    # harness 源码 clone 缓存目录（executor 本地，pull/clone 到此）。
    harness_clone_dir: str = ".harness_checkout"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
