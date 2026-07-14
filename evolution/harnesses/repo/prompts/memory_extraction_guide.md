# 记忆抽取中文引导规则

> 本文件是记忆系统 Graphiti add_episode 的中文实体消歧/叙事要素识别引导。
> evolution agent 可通过 edit_source 修改本文件来优化抽取质量。
>
> **注意**：Graphiti 0.19 的抽取引导主要通过 entity_types 的 Field description
> 实现（见 tools/narrative_schema.py）。本文件是补充的人类可读规则参考，
> 供 evolution agent 理解抽取策略 + 未来如需覆写 Graphiti 内置 prompt 时使用。

## 中文实体消歧规则

### 别名合并
- 同一角色的不同称呼必须合并为同一个 Character 节点：
  - "张三"、"张大侠"、"张公子"、"他"（上下文明确指向张三时）→ 合并为一个节点
  - aliases 字段记录所有称呼变体
- 消歧依据：上下文身份描述（如"桃花峪的弟子"指向同一人）

### 实体名纯中文
- 所有实体名必须是纯中文，不混入英文/拼音
- 实体类型标签（Character/Location/StoryNode）由 schema 自动标注，不出现在 name 中

### 叙事类型识别
- StoryNode 的 event_type 从事件描述中推断：
  - 冲突：角色之间的对抗/摩擦
  - 危机：角色面临重大抉择或危险
  - 反转：揭示出与预期相反的真相
  - 悬念：埋下未解的疑问
  - 揭露：真相大白
  - 胜利：角色达成目标
  - 交汇：多条故事线汇聚

## 关系抽取规则

### 关系极性（polarity）
- 正面：信任、友爱、忠诚、合作
- 负面：仇恨、背叛、敌对、恐惧
- 中性：陌生人、点头之交
- 矛盾：爱恨交织、表面友好实则对抗

### 因果链（CAUSED_BY）
- 只抽取显式因果（"因为""导致""于是"），不推测隐含因果
- 多跳因果保留中间环节（A→B→C 存三条边）

### 知识边界（KNOWS_ABOUT）
- 只在文本明确表达"X 知道/得知/发现 Y"时抽取
- 不推测角色"应该知道"的信息

## 时序标注规则

### valid_at（事实在故事世界中何时为真）
- 从事件描述中的时间线索提取（"三年前结拜" → valid_at = 当前时间 - 3年）
- 虚构历法由 StoryCalendar 映射为真实 datetime

### 双时序分离（event_chapter vs reveal_chapter）
- 倒叙/回忆场景：事件发生时间（valid_at）早于读者得知时间
- 揭示场景：揭示时间晚于事件发生时间
- Graphiti 的 valid_at 记录事件时间，created_at 记录入图时间（系统自动）
