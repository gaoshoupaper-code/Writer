"""worker HTTP 服务（Phase 2 T2.1，S1 轻进程 + S6 HTTP/SSE）。

职责：每个 harness 版本 = 一个 worker 进程。worker 接收执行端转发的生成请求，
加载对应 harness 版本，请求级装配 agent（保留多用户隔离），跑 generate_stream，
SSE 透传回执行端。

隔离模型（S1/S7）：worker 进程/容器内只隔离「harness 代码加载」，agent 仍请求级
装配（按 workspace/model/owner 动态）。一个 worker 服务多个用户多个 workspace。

harness 加载（D2 代码定义）：按 version_id 从文件系统加载 harnesses/<id>/harness.py，
importlib 动态 import，取出 WriterHarness 子类实例。

注意：本模块是 worker 进程的入口（python -m app.worker.server）。
Docker 化（T2.2）时，容器以此模块为启动命令。

设计依据：设计文档 S1/S6/C5（代码 volume 挂载）/C8（workspace volume）。
"""
from app.worker.server import create_worker_app, run_worker

__all__ = ["create_worker_app", "run_worker"]
