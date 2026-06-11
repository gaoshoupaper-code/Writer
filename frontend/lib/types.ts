export type WorkspacePanel = "chat" | "characters" | "script" | "detail_outline" | "worldview" | "novel" | "trace";

export type Style = {
  style_id: string;
  name: string;
  meta_style: string;
  storybuilding_style: string;
  detail_outline_style: string;
  writing_style: string;
  created_at: string;
};

export type ScreenplayResponse = {
  mode: string;
  thread_id: string;
  workspace_id: string;
  session_name: string;
  workspace_path: string;
  title: string;
  content: string;
  logline: string;
  synopsis: string;
  beats: string[];
  markdown: string;
  evaluation_markdown: string;
};

export type ThreadSummary = {
  thread_id: string;
  workspace_id: string;
  session_name: string;
  workspace_path: string;
  created_at: string;
  updated_at: string;
};

export type WorkspaceSummary = {
  workspace_id: string;
  outline_name: string;
  workspace_path: string;
  created_at: string;
  updated_at: string;
  session_count: number;
  active_style_id: string | null;
};

export type WorkspaceOutlineContent = {
  workspace_id: string;
  markdown: string;
};

export type StorylineEntry = {
  filename: string;
  title: string;
  markdown: string;
};

export type WorkspaceStorylineContent = {
  workspace_id: string;
  index_markdown: string;
  entries: StorylineEntry[];
  file_count: number;
};

export type WorkspaceWorldviewContent = {
  workspace_id: string;
  markdown: string;
};

export type VolumeChapter = {
  filename: string;
  title: string;
  markdown: string;
};

export type WorkspaceVolumeContent = {
  workspace_id: string;
  chapters: VolumeChapter[];
  file_count: number;
};

export type DetailOutlineChapter = {
  filename: string;
  title: string;
  markdown: string;
};

export type WorkspaceDetailOutlineContent = {
  workspace_id: string;
  chapters: DetailOutlineChapter[];
  file_count: number;
};

export type WorkspaceNovelContent = {
  workspace_id: string;
  markdown: string;
  source: string;
  chapter_count: number;
};

export type CharacterMarkdownFile = {
  filename: string;
  name: string;
  markdown: string;
};

export type WorkspaceCharacterContent = {
  workspace_id: string;
  characters: CharacterMarkdownFile[];
};

export type StreamEvent = {
  type: "model_output" | "tool_call" | "tool_output" | "model_stream" | "final" | "trace_event" | "trace_snapshot";
  data: Record<string, unknown>;
};

export type TraceStatus = "running" | "completed" | "failed";

export type TraceEventType =
  | "run_start"
  | "run_end"
  | "run_error"
  | "llm_start"
  | "llm_end"
  | "llm_error"
  | "tool_start"
  | "tool_end"
  | "tool_error";

export type TraceUsage = {
  input_tokens?: number | null;
  output_tokens?: number | null;
  total_tokens?: number | null;
};

export type TraceContextRange = {
  start_anchor_id?: string | null;
  end_anchor_id?: string | null;
};

export type TraceNodeKind = "run" | "agent" | "llm" | "tool" | "todo" | "error" | "skill" | (string & {});
export type TraceAgentRole = "main" | "subagent" | (string & {});
export type TraceContextKind = "system" | "human" | "ai" | "tool" | "todo" | "error" | "skill" | (string & {});

export type TraceLogEvent = {
  trace_id: string;
  event_id: string;
  sequence: number;
  type: TraceEventType;
  status: TraceStatus;
  timestamp: string;
  source: "system" | "middleware";
  duration_ms?: number | null;
  run_id?: string | null;
  parent_run_id?: string | null;
  parent_event_id?: string | null;
  agent_name?: string | null;
  node_name?: string | null;
  model_name?: string | null;
  input?: unknown;
  output?: unknown;
  usage?: TraceUsage | null;
  tool_calls?: unknown;
  tool_call_id?: string | null;
  tool_name?: string | null;
  tool_args?: unknown;
  tool_output?: unknown;
  context_anchor_id?: string | null;
  input_context_range?: TraceContextRange | null;
  output_context_anchor_id?: string | null;
  error?: string | null;
  skill_name?: string | null;
};

export type TraceNode = {
  node_id: string;
  parent_node_id?: string | null;
  kind: TraceNodeKind;
  label: string;
  status: TraceStatus;
  agent_name?: string | null;
  agent_role?: TraceAgentRole | null;
  depth: number;
  started_at?: string | null;
  ended_at?: string | null;
  duration_ms?: number | null;
  model_name?: string | null;
  tool_name?: string | null;
  skill_name?: string | null;
  usage?: TraceUsage | null;
  context_anchor_id?: string | null;
  input_context_range?: TraceContextRange | null;
  output_context_anchor_id?: string | null;
  raw_event_ids: string[];
  error?: string | null;
  chain_summary?: string | null;
  parallel_group_id?: string | null;
};

export type TraceContextSegment = {
  anchor_id: string;
  sequence: number;
  kind: TraceContextKind;
  agent_name?: string | null;
  agent_role?: TraceAgentRole | null;
  depth: number;
  title: string;
  content: unknown;
  metadata: Record<string, unknown>;
  tool_call_names: string[];
  related_node_id?: string | null;
  collapsed_by_default: boolean;
};

export type TraceTodoItem = {
  id?: string | null;
  content: string;
  status: "pending" | "in_progress" | "completed";
};

export type TraceTodoSnapshot = {
  anchor_id: string;
  agent_name?: string | null;
  items: TraceTodoItem[];
  active_item?: string | null;
};

export type TraceRunSummary = {
  trace_id: string;
  workspace_id: string;
  thread_id: string;
  session_name: string;
  workspace_path: string;
  endpoint: string;
  status: TraceStatus;
  started_at: string;
  ended_at?: string | null;
  duration_ms?: number | null;
  event_count: number;
  path: string;
  error?: string | null;
};

export type TraceDetail = {
  run: TraceRunSummary;
  events: TraceLogEvent[];
  nodes: TraceNode[];
  context: TraceContextSegment[];
  todos: TraceTodoSnapshot[];
};

export type ToolStatus = {
  key: string;
  name: string;
  status: "running" | "done" | "failed";
  parentKey?: string;
  subagentName?: string;
};

export type ChatMessage = {
  role: "assistant" | "user";
  content: string;
  tools?: ToolStatus[];
  contentFormat?: "text" | "markdown";
};

export type CheckpointToolCall = {
  name: string;
  id: string;
};

export type CheckpointMessage = {
  role: "system" | "human" | "ai" | "tool";
  content: string;
  tool_calls?: CheckpointToolCall[];
  name?: string;
};

export type CheckpointState = {
  thread_id: string;
  messages: CheckpointMessage[];
};

export type InitResponse = {
  workspaces: WorkspaceSummary[];
  styles: Style[];
};

export type CharacterGenerateRequest = {
  thread_id: string;
  prompt?: string;
  content?: string;
  text?: string;
  name?: string;
  role?: string;
  description?: string;
};

export type CharacterGenerateResponse = {
  mode: string;
  thread_id: string;
  workspace_id: string;
  session_name: string;
  workspace_path: string;
  name: string;
  identity: string;
  appearance: string;
  personality: string;
  current_state: string;
  relationships: string;
  markdown: string;
};

export type WorkspaceBootstrapResponse = {
  threads: ThreadSummary[];
  outline: WorkspaceOutlineContent | null;
  storyline: WorkspaceStorylineContent | null;
  volume: WorkspaceVolumeContent | null;
  detail_outline: WorkspaceDetailOutlineContent | null;
  characters: WorkspaceCharacterContent | null;
  novel: WorkspaceNovelContent | null;
  worldview: WorkspaceWorldviewContent | null;
};
