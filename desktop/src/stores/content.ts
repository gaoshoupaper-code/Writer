/**
 * contentStore —— 内容面板数据 + 轮询（从 home.tsx 迁移）
 *
 * 职责：大纲/人物/细纲/世界观/正文/故事线的数据 state，
 * 接收 workspaceStore.bootstrap 返回的 ContentData 并填充。
 * 轮询逻辑保留在 usePanelPolling hook（它直接读这个 store 的 setter）。
 */
import { create } from "zustand";
import type { CharacterMarkdownFile, DetailOutlineChapter, NovelChapter, StorylineEntry } from "@/lib/types";
import type { ContentData } from "./workspace";

interface ContentState {
  outlineMarkdown: string;
  outlineLoading: boolean;
  detailOutlineChapters: DetailOutlineChapter[];
  detailOutlineLoading: boolean;
  activeDetailChapterFilename: string;
  novelChapters: NovelChapter[];
  activeNovelFilename: string;
  novelLoading: boolean;
  characters: CharacterMarkdownFile[];
  charactersLoading: boolean;
  activeCharacterFilename: string;
  worldviewMarkdown: string;
  worldviewLoading: boolean;
  storylineMarkdown: string;
  storylineEntries: StorylineEntry[];
  activeStorylineFilename: string;

  // actions
  setContentData: (data: ContentData) => void;
  clearContent: () => void;
  setOutlineMarkdown: (v: string) => void;
  setStorylineMarkdown: (v: string) => void;
  setStorylineEntries: (v: StorylineEntry[]) => void;
  setActiveStorylineFilename: (v: string) => void;
  setWorldviewMarkdown: (v: string) => void;
  setDetailOutlineChapters: (v: DetailOutlineChapter[]) => void;
  setActiveDetailChapterFilename: (v: string) => void;
  setCharacters: (v: CharacterMarkdownFile[]) => void;
  setActiveCharacterFilename: (v: string) => void;
  setNovelChapters: (v: NovelChapter[]) => void;
  setActiveNovelFilename: (v: string) => void;
  setOutlineLoading: (v: boolean) => void;
  setDetailOutlineLoading: (v: boolean) => void;
  setNovelLoading: (v: boolean) => void;
  setCharactersLoading: (v: boolean) => void;
  setWorldviewLoading: (v: boolean) => void;
}

export const useContentStore = create<ContentState>((set) => ({
  outlineMarkdown: "",
  outlineLoading: false,
  detailOutlineChapters: [],
  detailOutlineLoading: false,
  activeDetailChapterFilename: "",
  novelChapters: [],
  activeNovelFilename: "",
  novelLoading: false,
  characters: [],
  charactersLoading: false,
  activeCharacterFilename: "",
  worldviewMarkdown: "",
  worldviewLoading: false,
  storylineMarkdown: "",
  storylineEntries: [],
  activeStorylineFilename: "",

  setContentData: (data) =>
    set({
      outlineMarkdown: data.outlineMarkdown,
      storylineMarkdown: data.storylineMarkdown,
      storylineEntries: data.storylineEntries,
      activeStorylineFilename: data.activeStorylineFilename,
      worldviewMarkdown: data.worldviewMarkdown,
      detailOutlineChapters: data.detailOutlineChapters,
      activeDetailChapterFilename: data.activeDetailChapterFilename,
      characters: data.characters,
      activeCharacterFilename: data.activeCharacterFilename,
      novelChapters: data.novelChapters,
      activeNovelFilename: data.activeNovelFilename,
    }),

  clearContent: () =>
    set({
      outlineMarkdown: "",
      storylineMarkdown: "",
      storylineEntries: [],
      activeStorylineFilename: "",
      worldviewMarkdown: "",
      detailOutlineChapters: [],
      activeDetailChapterFilename: "",
      characters: [],
      activeCharacterFilename: "",
      novelChapters: [],
      activeNovelFilename: "",
    }),

  setOutlineMarkdown: (v) => set({ outlineMarkdown: v }),
  setStorylineMarkdown: (v) => set({ storylineMarkdown: v }),
  setStorylineEntries: (v) => set({ storylineEntries: v }),
  setActiveStorylineFilename: (v) => set({ activeStorylineFilename: v }),
  setWorldviewMarkdown: (v) => set({ worldviewMarkdown: v }),
  setDetailOutlineChapters: (v) => set({ detailOutlineChapters: v }),
  setActiveDetailChapterFilename: (v) => set({ activeDetailChapterFilename: v }),
  setCharacters: (v) => set({ characters: v }),
  setActiveCharacterFilename: (v) => set({ activeCharacterFilename: v }),
  setNovelChapters: (v) => set({ novelChapters: v }),
  setActiveNovelFilename: (v) => set({ activeNovelFilename: v }),
  setOutlineLoading: (v) => set({ outlineLoading: v }),
  setDetailOutlineLoading: (v) => set({ detailOutlineLoading: v }),
  setNovelLoading: (v) => set({ novelLoading: v }),
  setCharactersLoading: (v) => set({ charactersLoading: v }),
  setWorldviewLoading: (v) => set({ worldviewLoading: v }),
}));
