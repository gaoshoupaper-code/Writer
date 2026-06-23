"""app/routers —— domain REST 路由（PR-14 从 main.py 抽出）。

main.py 只保留 app 实例化 + lifespan + router include，
端点逻辑按 domain 归位到各 router 模块。
"""
