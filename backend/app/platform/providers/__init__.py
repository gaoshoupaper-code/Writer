"""platform.providers 子包：外部能力抽象（多提供商可插拔）。

- ``image_generation``：图像生成能力（DD8c）
- ``image_understanding``：图像理解/视觉能力（DD8c）
- 未来：video_generation / video_understanding / game_runtime ...

约束（DD1）：所有外部 API 调用必须经抽象接口，domain 不直接耦合具体提供商。
"""
