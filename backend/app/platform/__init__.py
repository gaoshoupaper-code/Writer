"""Platform 基座包（DD1）。

领域无关的共享基础设施，各能力域（writing/image/未来 video/game）在此之上构建。

工程纪律（DD1）：
- platform **不 import** 任何 domain（domains/writing、domains/image）。
  基座对能力域一无所知，只提供抽象。
- domain **可以 import** platform。
- main.py 是唯一组装点：实例化 platform 组件 + 挂载 domain router。

子包职责：
- ``core``：通用件（db / checkpoint_pool / settings / security / trace）
- ``auth``：session / owner 机制
- ``agent``：领域无关的 agent 编排骨架（BaseAgentService / middleware / model factory）
- ``workspace``：workspace 元数据 + 路径管理（产物读写下沉到各 domain）
- ``skills``：Skills 自进化系统的存储与加载
- ``providers``：外部能力抽象（图像生成 / 视觉理解 / ...）

注：Phase 1 阶段此包逐步填充。Phase 1 完成前，写作功能仍走 ``app/writer`` 老路径
（双轨并行，DD10）。Phase 2 才把写作迁移为 platform 的 domain plugin。
"""
