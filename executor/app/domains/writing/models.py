from langchain_openai import ChatOpenAI

from app.platform.core.settings import Settings
from app.domains.writing.deepseek_thinking import DeepSeekThinkingChatModel

DEEPSEEK_PROVIDER = "deepseek"


def parse_writer_model(raw_model: str) -> tuple[str, str]:
    if ":" not in raw_model:
        provider = DEEPSEEK_PROVIDER if raw_model.startswith("deepseek-") else "openai"
        return provider, raw_model

    provider, model_name = raw_model.split(":", 1)
    if not provider or not model_name:
        raise ValueError("writer_model must be '<provider>:<model>' or '<model>'.")

    return provider.lower(), model_name


def build_writer_model(
    settings: Settings,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    model_name_override: str | None = None,
) -> ChatOpenAI:
    """构建写作模型。

    LLM 配置来源优先级（key/base_url/model 三者同源，避免混用）：
      1. 显式参数（api_key/base_url/model_name_override，测试/旧路径用）
      2. evolution 激活配置（桌面端「LLM 配置」页，通过 llm_config_loader 拉取）
      3. 环境变量 PLATFORM_API_KEY / OPENAI_API_KEY（历史兼容，可能为空）
      4. 占位 key（启动安全，让 ChatOpenAI 构造不崩）

    生产写作和 A/B 测试共用此函数，因此两者都受益于 evolution 配置打通。
    """
    from app.platform.llm_config.loader import get_active_llm_config

    # evolution 激活配置（拉取失败/未配置时为 None，降级到环境变量）
    evo_config = get_active_llm_config()

    # model：显式 override > evolution > settings.writer_model
    effective_model = (
        model_name_override
        or (evo_config.model if evo_config and evo_config.model else None)
        or settings.writer_model
    )
    provider, model_name = parse_writer_model(effective_model)
    provider_options = {}

    if settings.writer_temperature is not None:
        provider_options["temperature"] = settings.writer_temperature
    if settings.writer_top_p is not None:
        provider_options["top_p"] = settings.writer_top_p

    if provider == DEEPSEEK_PROVIDER:
        provider_options["extra_body"] = {"thinking": {"type": "enabled"}}
        model_class = DeepSeekThinkingChatModel
    else:
        model_class = ChatOpenAI

    # AD9：平台代付——优先用 PLATFORM_API_KEY（积分制专用），其次兼容旧 OPENAI_API_KEY。
    # api_key 参数仅为测试/旧路径保留，生产路径不再传用户 key（D22 一刀切）。
    # evolution 配置插入中间：环境变量空时由 evolution 的 key 兜底（桌面化打通）。
    effective_key = (
        api_key
        or (evo_config.api_key if evo_config and evo_config.api_key else None)
        or getattr(settings, "platform_api_key", "")
        or settings.openai_api_key
    )
    effective_base = (
        base_url
        or (evo_config.base_url if evo_config and evo_config.base_url else None)
        or getattr(settings, "platform_base_url", "")
        or settings.openai_base_url
    )

    # 启动安全：key 可能留空（未配置）。langchain ChatOpenAI 构造时强制要求 key
    # （pydantic validate_information），空 key 会崩溃。传占位 key 让构造通过。
    if not effective_key:
        effective_key = "placeholder-key-set-per-user-at-runtime"

    return model_class(
        model=model_name,
        api_key=effective_key,
        base_url=effective_base,
        request_timeout=120,
        # AD5：deepseek 也开启 stream_usage——流式模式下需要 usage 做积分计费（D3）。
        # ChatOpenAI 的 stream_usage=True 会传 stream_options={"include_usage": true}，
        # deepseek 兼容 OpenAI 协议会返回 usage。
        stream_usage=True,
        **provider_options,
    )
