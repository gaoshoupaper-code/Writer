import type { CharacterGenerateRequest, CharacterGenerateResponse, CheckpointState, InitResponse, Style, ThreadSummary, TraceDetail, TraceRunSummary, WorkspaceBootstrapResponse, WorkspaceCharacterContent, WorkspaceDetailOutlineContent, WorkspaceNovelContent, WorkspaceOutlineContent, WorkspaceVolumeContent, WorkspaceWorldviewContent, WorkspaceStorylineGraphContent, WorkspaceSummary } from "./types";

export const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:7788";

async function parseJsonResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    throw new Error(`API returned ${response.status}`);
  }

  return (await response.json()) as T;
}

export async function fetchWorkspaces() {
  const response = await fetch(`${API_BASE_URL}/api/workspaces`);
  return parseJsonResponse<WorkspaceSummary[]>(response);
}

export async function fetchInit() {
  const response = await fetch(`${API_BASE_URL}/api/init`);
  return parseJsonResponse<InitResponse>(response);
}

export async function fetchWorkspaceBootstrap(workspaceId: string) {
  const response = await fetch(`${API_BASE_URL}/api/workspaces/${workspaceId}/bootstrap`);
  return parseJsonResponse<WorkspaceBootstrapResponse>(response);
}

export async function createWorkspace(outlineName: string) {
  const response = await fetch(`${API_BASE_URL}/api/workspaces`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ outline_name: outlineName }),
  });

  return parseJsonResponse<WorkspaceSummary>(response);
}

export async function deleteWorkspace(workspaceId: string) {
  const response = await fetch(`${API_BASE_URL}/api/workspaces/${workspaceId}`, {
    method: "DELETE",
  });

  return parseJsonResponse<{ status: string; deleted: string; deleted_threads: string[] }>(response);
}

export async function fetchThreads(workspaceId: string) {
  const response = await fetch(`${API_BASE_URL}/api/threads?workspace_id=${workspaceId}`);
  return parseJsonResponse<ThreadSummary[]>(response);
}

export async function createThread(workspaceId: string, sessionName?: string) {
  const response = await fetch(`${API_BASE_URL}/api/threads`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ workspace_id: workspaceId, session_name: sessionName }),
  });

  return parseJsonResponse<ThreadSummary>(response);
}

export async function updateThread(threadId: string, sessionName: string) {
  const response = await fetch(`${API_BASE_URL}/api/threads/${threadId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_name: sessionName }),
  });

  return parseJsonResponse<ThreadSummary>(response);
}

export async function deleteThread(threadId: string) {
  const response = await fetch(`${API_BASE_URL}/api/threads/${threadId}`, {
    method: "DELETE",
  });

  return parseJsonResponse<{ status: string; deleted: string }>(response);
}

export async function fetchWorkspaceOutline(workspaceId: string) {
  const response = await fetch(`${API_BASE_URL}/api/workspaces/${workspaceId}/outline`);
  return parseJsonResponse<WorkspaceOutlineContent>(response);
}

export async function fetchWorkspaceWorldview(workspaceId: string) {
  const response = await fetch(`${API_BASE_URL}/api/workspaces/${workspaceId}/worldview`);
  return parseJsonResponse<WorkspaceWorldviewContent>(response);
}

export async function fetchWorkspaceVolume(workspaceId: string) {
  const response = await fetch(`${API_BASE_URL}/api/workspaces/${workspaceId}/volume`);
  return parseJsonResponse<WorkspaceVolumeContent>(response);
}

export async function fetchWorkspaceStorylineGraph(workspaceId: string) {
  const response = await fetch(`${API_BASE_URL}/api/workspaces/${workspaceId}/storyline-graph`);
  return parseJsonResponse<WorkspaceStorylineGraphContent>(response);
}

export async function fetchWorkspaceDetailOutline(workspaceId: string) {
  const response = await fetch(`${API_BASE_URL}/api/workspaces/${workspaceId}/detail-outline`);
  return parseJsonResponse<WorkspaceDetailOutlineContent>(response);
}

export async function fetchWorkspaceCharacters(workspaceId: string) {
  const response = await fetch(`${API_BASE_URL}/api/workspaces/${workspaceId}/characters`);
  return parseJsonResponse<WorkspaceCharacterContent>(response);
}

export async function fetchWorkspaceNovel(workspaceId: string) {
  const response = await fetch(`${API_BASE_URL}/api/workspaces/${workspaceId}/novel`);
  return parseJsonResponse<WorkspaceNovelContent>(response);
}

export function workspaceNovelPdfUrl(workspaceId: string) {
  return `${API_BASE_URL}/api/workspaces/${workspaceId}/novel/export.pdf`;
}

export function workspaceNovelWordUrl(workspaceId: string) {
  return `${API_BASE_URL}/api/workspaces/${workspaceId}/novel/export-word.zip`;
}

export async function fetchThreadTraces(threadId: string) {
  const response = await fetch(`${API_BASE_URL}/api/threads/${threadId}/traces`);
  return parseJsonResponse<TraceRunSummary[]>(response);
}

export async function fetchTraceDetail(threadId: string, traceId: string) {
  const response = await fetch(`${API_BASE_URL}/api/threads/${threadId}/traces/${traceId}`);
  return parseJsonResponse<TraceDetail>(response);
}

export async function deleteTrace(threadId: string, traceId: string) {
  const response = await fetch(`${API_BASE_URL}/api/threads/${threadId}/traces/${traceId}`, {
    method: "DELETE",
  });

  return parseJsonResponse<{ status: string; deleted: string }>(response);
}

export async function fetchThreadCheckpoint(threadId: string): Promise<CheckpointState> {
  const response = await fetch(`${API_BASE_URL}/api/threads/${threadId}/checkpoint`);
  if (!response.ok) throw new Error("Failed to fetch checkpoint");
  return response.json();
}

export async function fetchStyles() {
  const response = await fetch(`${API_BASE_URL}/api/styles`);
  return parseJsonResponse<Style[]>(response);
}

export async function createStyle(name: string, metaStyle = "", storybuildingStyle = "", detailOutlineStyle = "", writingStyle = "") {
  const response = await fetch(`${API_BASE_URL}/api/styles`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name,
      meta_style: metaStyle,
      storybuilding_style: storybuildingStyle,
      detail_outline_style: detailOutlineStyle,
      writing_style: writingStyle,
    }),
  });

  return parseJsonResponse<Style>(response);
}

export async function updateStyle(
  styleId: string,
  fields: { name?: string; meta_style?: string; storybuilding_style?: string; detail_outline_style?: string; writing_style?: string },
) {
  const response = await fetch(`${API_BASE_URL}/api/styles/${styleId}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(fields),
  });

  return parseJsonResponse<Style>(response);
}

export async function optimizeStyle(styleType: string, content: string) {
  const response = await fetch(`${API_BASE_URL}/api/styles/optimize`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ style_type: styleType, content }),
  });

  return parseJsonResponse<{ optimized: string }>(response);
}

export async function deleteStyle(styleId: string) {
  const response = await fetch(`${API_BASE_URL}/api/styles/${styleId}`, {
    method: "DELETE",
  });

  return parseJsonResponse<{ status: string; deleted: string }>(response);
}

export async function activateStyle(workspaceId: string, styleId: string | null) {
  const response = await fetch(`${API_BASE_URL}/api/workspaces/${workspaceId}/style`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ style_id: styleId }),
  });

  return parseJsonResponse<WorkspaceSummary>(response);
}

export async function fetchThreadOutline(threadId: string) {
  const response = await fetch(`${API_BASE_URL}/api/threads/${threadId}/outline`);
  return parseJsonResponse<WorkspaceOutlineContent>(response);
}

export async function generateCharacter(payload: CharacterGenerateRequest) {
  const response = await fetch(`${API_BASE_URL}/api/character/generate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  return parseJsonResponse<CharacterGenerateResponse>(response);
}
