// harness-api.ts —— 执行端 Agent 要素展示页的 API 封装。
// 复用 monitor-api.ts 的 API_BASE_URL + apiJson 模式。
//
// 设计依据：20260706_150000_Agent要素展示页_设计.md（接口契约）。

import { API_BASE_URL } from "./monitor-api";
import type {
  ElementsView,
  SnapshotListItem,
  SourceFile,
} from "./harness-types";

/** 统一 fetch 封装（与 monitor-api.ts 的 apiJson 同构）。 */
async function apiJson<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${API_BASE_URL}${path}`, init);
  if (!resp.ok) {
    throw new Error(`API ${resp.status}: ${path}`);
  }
  return (await resp.json()) as T;
}

/** 列快照（按版本倒序，已过滤 config_json=NULL 的无效行）。
 *  用于左侧版本树。 */
export async function fetchSnapshots(): Promise<SnapshotListItem[]> {
  return apiJson<SnapshotListItem[]>(`/api/snapshots`);
}

/** 当前 production 快照（版本树置顶 + 首屏默认选中用）。 */
export async function fetchProductionSnapshot(): Promise<SnapshotListItem | null> {
  try {
    return await apiJson<SnapshotListItem>(`/api/snapshots/production`);
  } catch {
    // 无 production（404）时返回 null，前端降级到列表第一项
    return null;
  }
}

/** 版本要素展示视图（主端点）：prompt/skills 全文已读，middleware 仅元信息。
 *  - 404: version 不存在或 config_json 为 NULL */
export async function fetchElements(version: number): Promise<ElementsView> {
  return apiJson<ElementsView>(`/api/snapshots/${version}/elements`);
}

/** 读指定版本指定文件的源码全文（middleware 懒加载用）。
 *  has_source=false 时前端不应调用此函数。
 *  - 404: version 不存在 / source_commit 缺失 / 文件在该 commit 不存在 */
export async function fetchSource(version: number, path: string): Promise<SourceFile> {
  const qs = new URLSearchParams({ path });
  return apiJson<SourceFile>(`/api/snapshots/${version}/source?${qs.toString()}`);
}
