"""app.domains 包：各能力域 plugin（DD1）。

- ``writing``：写作能力域（现有 app/writer，Phase 2 建立继承关系）
- ``image``：文生图能力域（Phase 3 新建）
- 未来：``video`` / ``game`` ...

工程纪律（DD1）：
- domain **可以 import** platform（基座）。
- domain **之间不 import**（writing 不依赖 image，反之亦然）。
- 产物读写、agent 编排、prompt 各 domain 独立。
"""
