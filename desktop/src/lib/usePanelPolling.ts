
import { useEffect, useRef } from "react";
import type {
  CharacterMarkdownFile,
  DetailOutlineChapter,
  NovelChapter,
  StorylineEntry,
} from "./types";
import type { WorkspacePanel } from "./types";
import {
  fetchWorkspaceCharacters,
  fetchWorkspaceDetailOutline,
  fetchWorkspaceNovel,
  fetchWorkspaceOutline,
  fetchWorkspaceStoryline,
  fetchWorkspaceWorldview,
} from "./api";

const POLL_INTERVAL_MS = 2000;

// 不参与轮询的面板：chat 走独立 /generate/stream（聊天区不动）；
// storyline/trace 是自管数据的独立组件，不依赖本 Hook 维护的内容 state。
const NON_POLL_PANELS: ReadonlySet<WorkspacePanel> = new Set(["chat", "storyline", "trace"]);

/**
 * 内容面板的 setter 集合。签名刻意与 page.tsx 里 useState 返回的 setter 对齐，
 * 使本 Hook 能直接接管原 EventSource 订阅块写入的那批 state。
 * activeXxxFilename 的 setter 用函数式更新签名 ((cur)=>next)，
 * 因为选中项保持逻辑需要读取当前值。
 */
export interface PanelPollingSetters {
  setNovelChapters: (c: NovelChapter[]) => void;
  setActiveNovelFilename: (fn: (cur: string) => string) => void;
  setNovelLoading: (b: boolean) => void;

  setStorylineMarkdown: (s: string) => void;
  setStorylineEntries: (e: StorylineEntry[]) => void;
  setActiveStorylineFilename: (fn: (cur: string) => string) => void;

  setDetailOutlineChapters: (c: DetailOutlineChapter[]) => void;
  setActiveDetailChapterFilename: (fn: (cur: string) => string) => void;
  setDetailOutlineLoading: (b: boolean) => void;

  setCharacters: (c: CharacterMarkdownFile[]) => void;
  setActiveCharacterFilename: (fn: (cur: string) => string) => void;
  setCharactersLoading: (b: boolean) => void;

  setWorldviewMarkdown: (s: string) => void;
  setWorldviewLoading: (b: boolean) => void;

  setOutlineMarkdown: (s: string) => void;
  setOutlineLoading: (b: boolean) => void;
}

export interface UsePanelPollingParams {
  activeWorkspaceId: string;
  activePanel: WorkspacePanel;
  loading: boolean;
  bootstrapping: boolean;
  setters: PanelPollingSetters;
}

/**
 * 选中项保持：当前 filename 仍在新列表里就保持，否则回退到第一项。
 * 与原 EventSource 订阅块的行为一致——避免文件被重命名/删除后面板显示空。
 */
function keepActiveFilename(current: string, filenames: string[]): string {
  return filenames.some((f) => f === current) ? current : (filenames[0] ?? "");
}

/**
 * 内容面板轮询 Hook。
 *
 * 行为规约（对应需求/设计的冻结决策）：
 * - 仅 loading=true（生成中）时轮询；否则不发起任何请求。
 * - 仅轮询当前打开的面板；chat/storyline/trace 不参与。
 * - bootstrapping=true 时跳过当前周期（bootstrap 为权威源，避免切换工作区状态闪烁），
 *   但不取消定时器，等 bootstrap 结束后自然恢复。
 * - activePanel / activeWorkspaceId 变化时立即拉一次（切换即拉），再起 2s 周期。
 * - loading 从 true→false 过渡时补拉最后一次（保证显示 Agent 最终结果），之后停止。
 */
export function usePanelPolling({
  activeWorkspaceId,
  activePanel,
  loading,
  bootstrapping,
  setters,
}: UsePanelPollingParams): void {
  // 用 ref 镜像最新 props，使轮询回调始终读到最新值而不依赖闭包快照。
  const bootstrappingRef = useRef(bootstrapping);
  bootstrappingRef.current = bootstrapping;
  const settersRef = useRef(setters);
  settersRef.current = setters;

  // 跟踪上一次 loading 值，用于检测 true→false 过渡，触发“停前补拉”。
  const prevLoadingRef = useRef(loading);

  // 一次轮询：根据当前面板拉对应接口并写 state。bootstrap 期间跳过（不写）。
  const pollOnce = async (panel: WorkspacePanel, workspaceId: string) => {
    if (bootstrappingRef.current) return;
    const s = settersRef.current;
    try {
      switch (panel) {
        case "novel": {
          const data = await fetchWorkspaceNovel(workspaceId);
          s.setNovelChapters(data.chapters);
          s.setActiveNovelFilename((cur) => keepActiveFilename(cur, data.chapters.map((c) => c.filename)));
          s.setNovelLoading(false);
          break;
        }
        case "script": {
          // script 面板展示 storyline（markdown），同时顺带更新 outline（chat 辅助显示）。
          const data = await fetchWorkspaceStoryline(workspaceId);
          s.setStorylineMarkdown(data.index_markdown);
          s.setStorylineEntries(data.entries);
          s.setActiveStorylineFilename((cur) => keepActiveFilename(cur, data.entries.map((e) => e.filename)));
          try {
            const outline = await fetchWorkspaceOutline(workspaceId);
            if (outline?.markdown !== undefined) {
              s.setOutlineMarkdown(outline.markdown);
              s.setOutlineLoading(false);
            }
          } catch {
            // outline 顺带拉取失败不影响 script 主面板，吞掉。
          }
          break;
        }
        case "detail_outline": {
          const data = await fetchWorkspaceDetailOutline(workspaceId);
          s.setDetailOutlineChapters(data.chapters);
          s.setActiveDetailChapterFilename((cur) => keepActiveFilename(cur, data.chapters.map((c) => c.filename)));
          s.setDetailOutlineLoading(false);
          break;
        }
        case "characters": {
          const data = await fetchWorkspaceCharacters(workspaceId);
          s.setCharacters(data.characters);
          s.setActiveCharacterFilename((cur) => keepActiveFilename(cur, data.characters.map((c) => c.filename)));
          s.setCharactersLoading(false);
          break;
        }
        case "worldview": {
          const data = await fetchWorkspaceWorldview(workspaceId);
          s.setWorldviewMarkdown(data.markdown);
          s.setWorldviewLoading(false);
          break;
        }
        default:
          // chat/storyline/trace：不轮询。
          break;
      }
    } catch {
      // 单次轮询失败静默处理——下一周期会重试，无需把 loading 置回 true（避免面板闪烁）。
    }
  };

  useEffect(() => {
    // 无工作区、或面板不参与轮询、或未在生成中：什么都不做。
    if (!activeWorkspaceId || NON_POLL_PANELS.has(activePanel) || !loading) {
      prevLoadingRef.current = loading;
      return;
    }

    // 生成中：立即拉一次（切换即拉 / 首次进入），再起 2s 定时器。
    void pollOnce(activePanel, activeWorkspaceId);

    const timer = setInterval(() => {
      void pollOnce(activePanel, activeWorkspaceId);
    }, POLL_INTERVAL_MS);

    return () => clearInterval(timer);
    // 依赖 activePanel/activeWorkspaceId/loading：三者任一变化都重新挂载（切换即拉 / 起停）。
    // bootstrapping/setters 故意不进依赖（经 ref 读取，避免 bootstrap 期间频繁重挂）。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activePanel, activeWorkspaceId, loading]);

  // 独立的 effect：检测 loading true→false 过渡，做“停前补拉最后一次”。
  // 不能并入上面的 effect——上面 effect 在 loading=false 时会直接 return 不轮询，
  // 这里专门负责“结束这一刻”的最终拉取。
  useEffect(() => {
    const wasLoading = prevLoadingRef.current;
    prevLoadingRef.current = loading;
    if (wasLoading && !loading && activeWorkspaceId && !NON_POLL_PANELS.has(activePanel)) {
      void pollOnce(activePanel, activeWorkspaceId);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loading]);
}
