"""积分制异常定义。"""


class CreditError(Exception):
    """积分制基础异常。"""


class CreditExhaustedError(CreditError):
    """余额触及负债上限（D27），创作被强制停止。

    在 CreditsMiddleware.awrap_model_call 中抛出，中断 agent 执行。
    agent.py 的 CancelledError 处理逻辑应捕获此异常，标记 trace reason=credit_stop。
    """


class InsufficientCreditsError(CreditError):
    """积分余额不足，无法开始创作（预扣失败 / 冻结用户）。

    在 CreditsMiddleware.awrap_tool_call（预扣阶段）抛出，
    或在 API 层 check_credits_frozen 依赖中作为 HTTP 403 返回。
    """
