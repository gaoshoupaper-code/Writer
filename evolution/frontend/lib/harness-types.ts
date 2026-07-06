// harness-types.ts —— 执行端 Agent 要素展示页的类型定义。
// 对齐后端 elements_api.py 的响应 schema（设计文档 D1-D7）。

/** Prompt 要素：直接投影 config 里的 system_prompt.body。 */
export interface PromptInfo {
  body: string;
}

/** Skill 要素：SKILL.md 全文（后端 git show 读取，失败则 content=null）。 */
export interface SkillInfo {
  path: string;          // config 里的 skill 相对路径（如 "skills/meta/auto-pipeline"）
  name: string;          // 末段路径（如 "auto-pipeline"）
  content: string | null; // SKILL.md 全文；读取失败 = null
  load_error: string | null; // 失败原因；成功 = null
}

/** Middleware 要素：元信息（源码懒加载）。 */
export interface MiddlewareInfo {
  hook: string;          // 生命周期 hook（before_agent/before_model/...）
  group: string;         // processor 组名
  class_name: string;    // 类名（如 "GoalMiddleware"）
  params: Record<string, unknown>; // config 里的静态参数
  source_path: string | null; // 预解析的源码路径（如 "middleware/goal.py"），前端懒加载用
}

/** 单个 Agent（meta 或 subagent）的要素集合。 */
export interface AgentElements {
  name: string;          // "meta" 或 subagent 名
  kind: "meta" | "subagent";
  prompt: PromptInfo;
  skills: SkillInfo[];
  middlewares: MiddlewareInfo[];
}

/** Subagents Tab 编排关系（meta → subagent）。 */
export interface SubagentRelation {
  from: string;          // 固定 "meta"
  to: string;            // subagent 名
  role: string;          // 中文职责名（后端固定映射表）
}

/** GET /api/snapshots/{version}/elements 主端点响应。 */
export interface ElementsView {
  source_commit: string | null;
  has_source: boolean;    // source_commit 是否存在；false → 前端禁用所有源码折叠
  agents: AgentElements[]; // meta 在前，subagents 按 config 出现顺序
  subagent_relations: SubagentRelation[];
}

/** GET /api/snapshots/{version}/source 懒加载端点响应。 */
export interface SourceFile {
  path: string;
  content: string;
}

/** 版本树用的快照列表项（复用 snapshot_api 的 list_snapshots 元数据）。 */
export interface SnapshotListItem {
  version: number;
  parent_version: number | null;
  source_commit: string | null;
  change_summary: string | null;
  status: string;        // "production" | "retired"
  created_at: string;
}
