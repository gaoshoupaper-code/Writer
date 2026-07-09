"""积分制核心模块（D1-D28）。

包结构：
- config_service.py：暗调参数读取 + 内存缓存（AD11）
- tier_parser.py：demand.md 篇幅档位解析（D9/D13）
- service.py：CreditsService——预扣/累加/结算/流水（D4/D23）
- middleware.py：CreditsMiddleware——model_call 计费+强停 / tool_call 预扣触发（AD2/AD6）
- exceptions.py：CreditExhaustedError 等
- dependencies.py：check_credits_frozen FastAPI 依赖（AD12）
"""
