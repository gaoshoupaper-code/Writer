from langchain_openai import ChatOpenAI

from app.core.settings import Settings
from app.writer.deepseek_thinking import DeepSeekThinkingChatModel

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
) -> ChatOpenAI:
    """构建写作模型。

    多用户隔离（D9）：普通用户调用时传入其解密后的 api_key + base_url，
    覆盖 settings 里的全局 key（全局 key 现仅作管理员兜底）。
    两个参数都为 None 时回退到 settings（保留旧行为）。
    """
    provider, model_name = parse_writer_model(settings.writer_model)
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

    effective_key = api_key if api_key is not None else settings.openai_api_key
    effective_base = base_url if base_url is not None else settings.openai_base_url

    return model_class(
        model=model_name,
        api_key=effective_key,
        base_url=effective_base,
        request_timeout=120,
        stream_usage=provider != DEEPSEEK_PROVIDER,
        **provider_options,
    )
