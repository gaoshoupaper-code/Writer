"""evolve.driver 子包 —— 进化驱动器（编排层）。

驱动器是薄编排器：按固定顺序（plan→execute）委托子代理，自身不做
分析/诊断/写代码。PhaseGuardMiddleware 强制按序委托（越权即拦截）。

模块（按要素分层）：
  agent.py              驱动器装配（DeepAgent + 2 子代理 + PhaseGuard）
  prompt.py             驱动器 system prompt
  middleware/
    phase_guard.py      PhaseGuardMiddleware（2 阶段白名单护栏）
"""
