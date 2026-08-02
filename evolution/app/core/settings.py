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
    # SQLite 数据库文件路径。
    # 默认放 data/ 子目录（2026-07-08）：与源码分离，便于 docker volume 只挂 data/
    # 做增量更新（源码不挂卷，避免旧源码覆盖镜像新源码）。旧库迁移见 update-evolution.sh。
    evolution_db: str = "data/evolution.db"
    # Trace V2 完整正文内容寻址存储。SQLite 只存元数据与热索引。
    trace_payload_dir: str = "data/trace_payloads"
    # 可选单向 OTLP/HTTP JSON 出口。留空时不启动导出 worker；canonical Trace
    # 的采集、查询、血缘和分析不依赖外部平台。
    trace_otlp_endpoint: str = ""
    trace_otlp_headers_json: str = "{}"
    trace_otlp_timeout_seconds: float = 2.0
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

    # 产品负责人白名单（REQ-20260802-211032，DEC-005）：允许对"用户主动停止但有价值"
    # 的 trace 发起人工确认进证据编纂的 executor user_id（逗号分隔）。复用 SSO 写入的
    # request.state.user_id 做服务端真强制。留空时退化为放行（开发模式兼容，与
    # allowed_user_ids 同构），生产环境须填配置否则功能不安全（线上验证须确认已填）。
    product_owner_user_ids: str = ""

    # 内网通知 token（executor → evolution /api/ingestion/notify 的鉴权）。
    # 替换旧 InternalKeyMiddleware 的 X-Internal-Key 机制。
    # 生成：python -c "import secrets; print(secrets.token_urlsafe(32))"
    notify_token: str = ""

    # ── 数据集维护者令牌（数据闭环设计 D17）──
    # growing→golden 升级需维护者校验。留空则不校验（开发模式兼容）。
    maintainer_token: str = ""

    # ── harness 版本管理（独立 git 仓库，去 DB 重构）──
    # harness 独立 git 仓库的工作目录（evolution 编辑源码 + registry.json 的地方）。
    # 改动 commit → push 到 bare repo；executor 从 bare repo pull main = production。
    harness_work_dir: str = "harnesses/repo"
    # harness bare repo 路径（evolution push → executor pull 的中转）。
    harness_bare_repo: str = "harness.git"

    # ── 进化对话式共创工作台 checkpointer（Phase 2A，决策 T5）──
    # 每个 evolve session 一个独立 SQLite 文件（evolve_<session_id>.db），
    # LangGraph 通过 thread_id 自动恢复对话史。discarded session 删文件清理。
    evolve_checkpoints_dir: str = "data/checkpoints"

    # ── 问题知识库 embedding（一期相似检索，需求 20260731 REQ-04/AC-39）──
    # 向量重排用智谱 embedding-3（2048 维）。配置留空 → 向量降级，仅用结构化+FTS（AC-26）。
    # api_key/base_url 默认复用 evolution scope 的 llm_config（同一供应商 key 通常通用），
    # 也可单独配置。model 固定 embedding-3（与 executor 端对齐）。
    problem_kb_embed_api_key: str = ""   # 留空则复用 llm_config evolution scope 的 key
    problem_kb_embed_base_url: str = ""  # 留空则复用 llm_config evolution scope 的 base_url
    problem_kb_embed_model: str = "embedding-3"

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
        """SQLite 数据库文件的绝对路径。相对路径基于 evolution/ 目录。

        自动创建父目录（data/ 子目录在 docker volume 挂载时需确保存在）。
        """
        path = Path(self.evolution_db)
        if not path.is_absolute():
            path = self._evolution_root / path
        path = path.resolve()
        # 确保父目录存在（首次挂载空 volume 时 data/ 可能不存在）
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def trace_payload_path(self) -> Path:
        path = Path(self.trace_payload_dir)
        if not path.is_absolute():
            path = self._evolution_root / path
        path = path.resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

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

    @property
    def evolve_checkpoints_path(self) -> Path:
        """进化对话 checkpointer 根目录绝对路径（Phase 2A，决策 T5）。

        每 session 一个 SQLite 文件，自动创建父目录。
        """
        path = Path(self.evolve_checkpoints_dir)
        if not path.is_absolute():
            path = self._evolution_root / path
        path = path.resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path


settings = Settings()
