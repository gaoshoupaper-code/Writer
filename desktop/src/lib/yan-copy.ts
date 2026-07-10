/**
 * 小衍人格文案库
 *
 * 所有执行体验中的拟人化文案集中管理。调性：活泼陪伴（执行中）→
 * 中性清晰（HITL 提问）→ 庆祝（交付）→ 温暖（失败/停止）。
 *
 * 使用方式：各 ExecutionView 子视图按 phase/stage 取文案。
 */

// ── 阶段人话名（与 stage.ts 的 StageType 对应）──
export const STAGE_DISPLAY_NAMES: Record<string, string> = {
  storybuilding: "构思故事",
  "detail-outline": "规划章节",
  writing: "动笔写作",
  general: "收尾整理",
};

// ── 黑屏期（booting）文案 ──
export const BOOTING_COPY = [
  "好的，我先理一下思路...",
  "让我想想这个故事怎么展开...",
  "准备中，马上开始...",
];

// ── 思考态（thinking）折叠摘要文案，按阶段推断 ──
export const THINKING_COPY: Record<string, string[]> = {
  storybuilding: [
    "正在构思故事的框架...",
    "想想主角和世界观的设定...",
    "梳理一下故事的核心冲突...",
  ],
  "detail-outline": [
    "正在梳理章节脉络...",
    "规划每一章的节奏和重点...",
    "理一下情节的先后顺序...",
  ],
  writing: [
    "酝酿一下文字的感觉...",
    "想想这段怎么写更自然...",
  ],
  general: [
    "整理一下收尾工作...",
    "做最后的检查和润色...",
  ],
};

// ── 写作态（writing）动态文案轮播池 ──
export const WRITING_COPY = [
  "好嘞，开写啦！",
  "嗯，这段我想让节奏快一点...",
  "写到这里感觉不错，继续...",
  "让我润色一下这个细节...",
  "嗯...这个转折得想想怎么写自然...",
  "快好了，再写一点就收尾...",
];

// ── 交付仪式（delivering）文案 ──
export const DELIVERY_COPY = [
  "写好啦！你看看怎么样～",
  "搞定！这次写了不少呢～",
  "完成啦！希望你喜欢～",
  "交稿！有要改的地方随时说～",
];

export const DELIVERY_INTERACTION = "你觉得怎么样？要改的话跟我说";

// ── HITL 提问（asking）引导语 ──
export const ASKING_INTRO = [
  "这里需要你拍个板——",
  "有个地方想问问你——",
  "这里我有点纠结——",
];

// ── 失败态（failed）文案 ──
export const FAILED_COPY: Record<string, string> = {
  heartbeat_timeout: "抱歉，连接好像断了...",
  credit_exhausted: "积分不够了，需要补充一下...",
  default: "抱歉，刚才出了点问题...",
};

export const FAILED_ACTION = "要我再试一次吗？";

// ── 停止态（stopped）文案 ──
export const STOPPED_COPY = "好的，停下了。";
export const STOPPED_ACTION = "要继续的话，点这里就好";

// ── 多轮记忆感（resume 时第二次发起）文案 ──
export const MEMORY_COPY = [
  "好的，接着上次的故事...",
  "嗯，我们继续——",
  "好嘞，接着来！",
];

// ── 工具函数：从数组中按某种策略取文案 ──

/** 随机取一条（不重复上次，如果可能） */
export function pickRandom(pool: string[], lastIndex: number = -1): { text: string; index: number } {
  if (pool.length === 1) return { text: pool[0], index: 0 };
  let idx = lastIndex;
  while (idx === lastIndex) {
    idx = Math.floor(Math.random() * pool.length);
  }
  return { text: pool[idx], index: idx };
}

/** 按 stage type 取思考态文案 */
export function getThinkingCopy(stageType: string | undefined): string {
  if (!stageType) return "正在思考...";
  const pool = THINKING_COPY[stageType] ?? THINKING_COPY.general;
  return pool[Math.floor(Math.random() * pool.length)];
}

/** 按 stage type 取阶段人话名 */
export function getStageDisplayName(stageType: string | undefined): string {
  if (!stageType) return "执行中";
  return STAGE_DISPLAY_NAMES[stageType] ?? "执行中";
}

/**
 * 把错误类型映射成人话失败文案。
 * errMsg 是 performSubmit catch 里的原始 error message。
 */
export function getFailedCopy(errMsg: string): string {
  if (errMsg.includes("HEARTBEAT_TIMEOUT") || errMsg.includes("连接已断开")) return FAILED_COPY.heartbeat_timeout;
  if (errMsg.includes("积分") || errMsg.includes("403") || errMsg.includes("冻结")) return FAILED_COPY.credit_exhausted;
  return FAILED_COPY.default;
}
