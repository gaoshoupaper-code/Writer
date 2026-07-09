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

    平台代付模式（D1/D22/AD9）：所有用户统一使用平台 key，不再读用户自带 key。
    api_key/base_url 参数保留仅为向后兼容（测试/旧路径），优先级低于平台配置。
    """
    effective_model = model_name_override if model_name_override else settings.writer_model
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
    effective_key = (
        api_key
        or getattr(settings, "platform_api_key", "")
        or settings.openai_api_key
    )
    effective_base = (
        base_url
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
