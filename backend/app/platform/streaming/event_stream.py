"""platform.streaming —— SSE 流式编排统一骨架（PR-07a）。

消灭三份重复的 SSE 编排（main._event_generator / main._image_event_generator /
MetaAgentService.generate_stream）。本模块提供共同骨架：

- ``sse(event_type, payload)``：构造标准 SSE 帧（event: X\\ndata: Y\\n\\n）
- ``heartbeat()``：SSE 心跳注释行（: ping\\n\\n）
- ``run_agent_stream(...)``：心跳 + astream_events 迭代的核心循环，
  把每个 agent 事件交给 ``EventSink`` 回调，sink 决定产出哪些 SSE 帧。

设计原则（薄骨架 + 钩子）：
- 骨架只管「agent 事件循环 + 心跳 + 额外异步任务 + finally 清理」，不含事件分发逻辑。
- 事件分发（model_stream / tool_call / task 派发等）由各 domain 的 EventSink 实现，
  保留领域差异（writing 有 task/章节计数 + trace_pump，image 简单）。
- 时序保真：额外任务（如 writing 的 trace_pump）与 agent 事件/心跳公平竞争 asyncio.wait，
  trace 事件能即时推送，语义与重构前完全一致。
- 契约零变化：产出的 SSE 事件类型与字段与重构前完全一致。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Protocol, runtime_checkable

# 心跳间隔：浏览器忽略的 SSE 注释行（: ping），保持字节流动避免代理空闲超时掐断。
# 15s 与原 writing/image 实现一致（meta/agent.py 的 SSE_HEARTBEAT_INTERVAL）。
DEFAULT_HEARTBEAT_INTERVAL = 15


def sse(event_type: str, payload: object) -> str:
    """构造标准 SSE 帧。

    格式：``event: <type>\\ndata: <json>\\n\\n``。payload 用 json.dumps 序列化，
    ensure_ascii=False 保留中文，default=str 兜底不可序列化对象（如 datetime）。
    """
    data = json.dumps(payload, ensure_ascii=False, default=str)
    return f"event: {event_type}\ndata: {data}\n\n"


def heartbeat() -> str:
    """SSE 心跳注释行（浏览器忽略，保持连接活跃）。"""
    return ": ping\n\n"


@runtime_checkable
class EventSink(Protocol):
    """agent 事件处理器（领域差异的注入点）。

    run_agent_stream 把每个原始 LangGraph astream_events 事件交给 sink，
    sink 返回要产出的 SSE 字符串列表（可空——不产出任何事件）。

    on_event 是协程——允许领域逻辑里做异步操作（如 writing 的
    asyncio.to_thread 章节计数 / 流程图生成）。

    实现示例：
        - WritingEventSink：处理 model_output/tool_call/task 派发/章节计数（含 await）
        - ImageEventSink：只处理 model_stream/tool_output（纯同步）
    """

    async def on_event(self, event: dict) -> list[str]:
        """处理一个 agent 事件，返回要 yield 的 SSE 帧（可空列表）。"""
        ...


@dataclass
class ExtraTask:
    """额外的并发异步任务（如 writing 的 trace_pump）。

    保持与 agent 事件/心跳公平竞争 asyncio.wait，确保额外源的事件即时推送。
    每轮循环该任务完成时，``on_result`` 处理其返回值，产出 SSE 帧；
    若返回 None 表示流应终止（如 trace 队列关闭）。

    Attributes:
        task: asyncio.Task，其 result() 传给 on_done。
        on_done: 任务完成时的回调，参数为 task，返回 (要重建的新 task 或 None, SSE 帧列表)。
                 重建新 task 时骨架把它重新纳入 wait 循环（实现持续 pump）。
    """

    task: asyncio.Task
    on_done: Callable[[asyncio.Task], tuple[asyncio.Task | None, list[str]]]


@dataclass
class StreamResult:
    """run_agent_stream 结束后的状态，供调用方构造 final/interrupt。

    Attributes:
        full_result: on_chain_end(name=="LangGraph") 的 output（agent 最终输出）。
        pending_interrupt: 若有 HITL interrupt 暂停，为其 interrupt value；否则 None。
    """

    full_result: Any = None
    pending_interrupt: Any = None


async def run_agent_stream(
    agent: Any,
    agent_input: Any,
    config: dict,
    sink: EventSink,
    *,
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    detect_interrupt: bool = True,
    extra_tasks: list[ExtraTask] | None = None,
) -> tuple[AsyncIterator[str], StreamResult]:
    """SSE 流式编排核心循环（writing/image 共用）。

    职责（骨架）：
    1. ``astream_events(version="v2")`` 迭代 agent 事件
    2. asyncio.wait 多路复用 agent 事件 + 心跳 + 额外任务（公平竞争，时序保真）
    3. 心跳触发时 yield ``: ping``（保持连接）
    4. agent 事件到达时交给 ``sink.on_event``，yield 其返回的 SSE 帧
    5. 额外任务完成时调用其 on_done，yield 产出的帧并按需重建任务（持续 pump）
    6. finally 取消所有任务并 aclose 迭代器
    7. 流结束后检测 interrupt，结果填入 StreamResult

    本函数只产出 sink/extra_tasks 决定的事件 + 心跳；**不**产出
    final/interrupt/status——那些由调用方根据返回的 StreamResult 自行构造。

    Args:
        agent: 已编译的 DeepAgent / CompiledSubAgent 实例。
        agent_input: agent 输入（messages dict 或 Command(resume=...)）。
        config: LangGraph 运行配置（thread_id / callbacks / recursion_limit）。
        sink: 事件处理器，决定每个 agent 事件产出什么 SSE。
        heartbeat_interval: 心跳间隔秒数。
        detect_interrupt: 流结束后是否检测 HITL interrupt。
        extra_tasks: 额外的并发任务（如 writing 的 trace_pump），与 agent 事件公平竞争。

    Returns:
        (sse_iterator, result)：sse_iterator 产出 SSE 帧；
        result 在迭代结束后填好 full_result / pending_interrupt。
        调用方必须先消费完 sse_iterator，result 才有效。

    Raises:
        原样向上抛出 agent 执行异常（调用方在 try/except 里处理 trace fail_run 等）。
    """
    result = StreamResult()
    sse_iterator = _run_loop(
        agent, agent_input, config, sink, result,
        heartbeat_interval=heartbeat_interval,
        detect_interrupt=detect_interrupt,
        extra_tasks=list(extra_tasks) if extra_tasks else None,
    )
    return sse_iterator, result


async def _run_loop(
    agent: Any,
    agent_input: Any,
    config: dict,
    sink: EventSink,
    result: StreamResult,
    *,
    heartbeat_interval: float,
    detect_interrupt: bool,
    extra_tasks: list[ExtraTask] | None,
) -> AsyncIterator[str]:
    """run_agent_stream 的内部实现（闭包捕获 result 以便回填）。"""
    agent_events = agent.astream_events(agent_input, config=config, version="v2")
    agent_task = asyncio.create_task(agent_events.__anext__())
    heartbeat_task = asyncio.create_task(asyncio.sleep(heartbeat_interval))
    # extra_tasks 按值持有（on_done 可能重建 task），用列表包一层便于原地替换
    extras: list[ExtraTask] = list(extra_tasks) if extra_tasks else []

    try:
        while True:
            wait_set: set[asyncio.Task] = {agent_task, heartbeat_task}
            for et in extras:
                wait_set.add(et.task)
            done, _ = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)

            if heartbeat_task in done:
                heartbeat_task.result()
                yield heartbeat()
                heartbeat_task = asyncio.create_task(asyncio.sleep(heartbeat_interval))

            # 处理额外任务完成（如 trace_pump 返回一条 trace 更新）
            for et in extras:
                if et.task in done:
                    new_task, frames = et.on_done(et.task)
                    for frame in frames:
                        yield frame
                    et.task = new_task  # None 表示该源终止，下一轮 wait 不再纳入

            if agent_task in done:
                try:
                    event = agent_task.result()
                except StopAsyncIteration:
                    break
                agent_task = asyncio.create_task(agent_events.__anext__())

                # 捕获最终输出（on_chain_end name=="LangGraph"）
                if event.get("event") == "on_chain_end" and event.get("name") == "LangGraph":
                    result.full_result = event.get("data", {}).get("output")

                # 交给领域 sink 处理（含异步操作），产出 SSE 帧
                for frame in await sink.on_event(event):
                    yield frame

    finally:
        heartbeat_task.cancel()
        agent_task.cancel()
        for et in extras:
            et.task.cancel()
        with contextlib.suppress(BaseException):
            await agent_events.aclose()

    # interrupt 检测：流结束（非异常）后检查是否有 pending HITL interrupt
    if detect_interrupt:
        state = await agent.aget_state(config)
        pending = [t for t in state.tasks if t.interrupts]
        if pending:
            result.pending_interrupt = pending[0].interrupts[0].value


__all__ = [
    "DEFAULT_HEARTBEAT_INTERVAL",
    "EventSink",
    "ExtraTask",
    "StreamResult",
    "heartbeat",
    "run_agent_stream",
    "sse",
]
