"""管理后台代理路由（AD3/AD7）。

进化端前端 → evolution 后端（本模块）→ 带 SSO cookie 转发到 executor /api/admin/*。
executor 侧校验 is_super_admin（D24/D28），evolution 侧也做 super_admin 前置校验（双保险）。

所有路由统一前缀 /api/admin，转发到 executor 同名路径。
"""
