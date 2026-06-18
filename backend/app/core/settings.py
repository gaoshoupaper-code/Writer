from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    writer_model: str
    writer_temperature: float | None = Field(default=None, ge=0)
    writer_top_p: float | None = Field(default=None, ge=0, le=1)
    writer_agent_mode: str
    writer_frontend_origin: str
    openai_api_key: str
    openai_base_url: str

    # ── 多用户隔离（Phase 1 新增）──────────────────────────────
    # API key 加密主密钥：32 字节，hex 或 base64 编码。AES-256-GCM 用。
    # 生成方式：python -c "import secrets; print(secrets.token_hex(32))"
    master_key: str

    # 元数据库路径（相对 backend 根，或绝对路径）。默认 backend/app.db
    db_path: str = "app.db"

    # 工作区根目录（默认 backend/workspace，内部按 user_id 分桶）
    workspace_root: str = "workspace"

    # 引导管理员：首启动若无管理员，按以下凭据创建
    admin_username: str = "admin"
    admin_password: str

    # Session Cookie 有效期（天），滚动续期
    session_ttl_days: int = 30

    # 默认作品配额（每用户作品数量上限）
    default_workspace_quota: int = 5

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
