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

    # evolution 服务基址（Phase 5 T10）：执行端 prompt loader 从 evolution 拉 prompt。
    # 形如 http://localhost:7789。留空则 loader 降级为读本地 .md 文件（兼容）。
    evolution_url: str = ""

    # prompt loader 本地缓存目录（Phase 5 T10）：从 evolution 拉的 prompt 缓存到此，
    # evolution 不可用时降级读缓存。默认 executor/.prompt_cache。
    prompt_cache_dir: str = ".prompt_cache"

    # ── Self-Harness Phase 1（T1.3）───────────────────────────
    # 是否走 harness 装配路径（契约化 harness 驱动装配）。
    # 默认 False（走旧 _agent_for_workspace 直接装配，保证现有行为不变）。
    # T1.4 等价性验证通过后可手动打开。
    writer_use_harness: bool = False

    # ── Self-Harness Phase 6（surface 体系，T5.1）──────────────
    # 是否走 manifest 装配路径（surface 体系，经 evolution 拉 manifest 装配）。
    # 优先级：writer_use_manifest > writer_use_harness > 旧直接装配。
    # 默认 False。打开前需：① evolution 已 migrate_to_surface 生成首版 manifest；
    # ② worker 启动时能拉到 manifest。打开后走 _assemble_via_manifest。
    writer_use_manifest: bool = False

    # manifest loader 本地缓存目录（Phase 6 T4.1）：evolution 不可用时降级读缓存。
    # 默认 executor/.manifest_cache。
    manifest_cache_dir: str = ".manifest_cache"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
