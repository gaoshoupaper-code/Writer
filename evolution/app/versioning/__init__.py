"""versioning 包 —— harness 版本管理（去 DB 重构）。

harness 版本以独立 git 仓库为内容真相源，registry.json 为元信息真相源。
本包管理版本注册表的读写 + API 端点 + executor 通知。

模块：
  registry_repo.py     registry.json 读写层（版本谱系/production 指针/回滚日志）
  snapshot_api.py      版本查询 API（list/production/get，读 registry）
  elements_api.py      Harness 要素展示（从 git 源文件读取 prompt/skills/tools/middleware）
  snapshot_publisher.py executor 通知层（发版后 HTTP 通知 reload）

设计依据：.claude/md/20260713_003000_harness版本机制去DB重构设计.md。
"""
