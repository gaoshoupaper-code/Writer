"""Platform 基座包（DD1）。

领域无关的共享基础设施，各能力域（writing/image/未来 video/game）在此之上构建。

工程纪律（DD1）：
- platform **不 import** 任何 domain（domains/writing、domains/image）。
  基座对能力域一无所知，只提供抽象。
- domain **可以 import** platform。
- main.py 是唯一组装点：实例化 platform 组件 + 挂载 domain router。

子包职责：
- ``core``：通用件（settings / security / checkpoint_pool）
- ``state``：状态层（thread_store 元数据 / artifact_store 产物 / style_store 风格）
- ``agent``：领域无关的 agent 编排骨架（BaseAgentService / middleware / runtime 隔离层 / streaming）
- ``trace``：trace 子系统（recorder / projector / schemas）
- ``tools``：跨域通用工具（ask_user）
- ``skills``：Skills 自进化系统的存储与加载
- ``providers``：外部能力抽象（图像生成 / 视觉理解 / ...）

注：core/ 已在 PR-13 拆解完毕，writer 已在 PR-11 降级为 domains/writing。
db 仍在顶层 app/db（数据库连接管理，PR-15 收敛）。
"""
