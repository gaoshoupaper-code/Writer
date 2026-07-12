/**
 * SSE 流式请求封装（设计文档 S11/T12）。
 *
 * 桌面端 SSE 走 Rust 中继（stream_request command + sse_chunk/sse_end event）。
 * 本封装提供一个 **reader-like 接口**，模拟原 frontend 的 `response.body.getReader()`，
 * 让 T15 改造时只需把 `fetch(...)` 替换为 `streamRequest(...)`，
 * 下游的 TextDecoder / split("\n") / 解析 event:data 逻辑完全复用。
 *
 * 工作流：
 * 1. 生成 stream_id
 * 2. listen("sse_chunk") + listen("sse_end") 注册监听
 * 3. invoke("stream_request") 启动 Rust 流式拉取（后台逐 chunk emit）
 * 4. read() 从内部队列取 chunk（阻塞式 Promise）
 * 5. abort() 主动停止（用户点停止 / 心跳超时）
 */

import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";

/// Rust emit 的 chunk event payload（对应 src-tauri/src/http.rs SseChunk）。
interface SseChunkPayload {
  stream_id: string;
  chunk: string;
}

/// Rust emit 的结束 event payload（对应 src-tauri/src/http.rs SseEnd）。
interface SseEndPayload {
  stream_id: string;
  ok: boolean;
  error?: string;
}

/// reader.read() 返回值（模拟 ReadableStreamReadResult）。
export interface StreamReadResult {
  done: boolean;
  value: Uint8Array | undefined;
}

/// streamRequest 参数（与原 fetch 的 RequestInit 子集对齐）。
export interface StreamRequestInit {
  method?: string;
  headers?: Record<string, string>;
  body?: unknown;
}

/**
 * 发起流式请求，返回一个 reader-like 对象。
 *
 * 用法（替代原 `await fetch(url, {...})` + `response.body.getReader()`）：
 * ```ts
 * const reader = await streamRequest("/api/screenplay/generate/stream", {
 *   method: "POST",
 *   headers: { "Content-Type": "application/json" },
 *   body: { thread_id, prompt },
 * });
 * while (true) {
 *   const { done, value } = await reader.read();
 *   if (done) break;
 *   // TextDecoder 解码 value，按 \n 分行解析 SSE（原逻辑复用）
 * }
 * ```
 *
 * body 直接传对象（不是 JSON.stringify）——Rust 端 reqwest 接 serde_json::Value。
 * 原 frontend 传 JSON.stringify 的字符串，调用处改一下即可（T15 处理）。
 */
export async function streamRequest(path: string, init: StreamRequestInit = {}): Promise<{
  read: () => Promise<StreamReadResult>;
  cancel: () => Promise<void>;
}> {
  const streamId = crypto.randomUUID();

  // 内部状态
  const queue: Uint8Array[] = [];
  let done = false;
  let error: string | null = null;
  let waiter: ((r: StreamReadResult) => void) | null = null;

  // 解析 body：StreamRequestInit.body 已是对象（与 http.ts 的 apiFetch 不同，这里不二次 JSON.parse）。
  // 但兼容旧代码传字符串的情况。
  let bodyValue: unknown = init.body;
  if (typeof bodyValue === "string") {
    try {
      bodyValue = JSON.parse(bodyValue);
    } catch {
      // 纯文本 body，保留原样
    }
  }

  // 注册监听
  const unlistenChunk = await listen<SseChunkPayload>("sse_chunk", (event) => {
    if (event.payload.stream_id !== streamId) return;
    const bytes = new TextEncoder().encode(event.payload.chunk);
    queue.push(bytes);
    // 唤醒等待中的 read()
    if (waiter) {
      const w = waiter;
      waiter = null;
      w({ done: false, value: queue.shift() });
    }
  });

  const unlistenEnd = await listen<SseEndPayload>("sse_end", (event) => {
    if (event.payload.stream_id !== streamId) return;
    if (!event.payload.ok) {
      error = event.payload.error ?? "stream ended with error";
    }
    done = true;
    // 唤醒等待中的 read()（返回 done:true）
    if (waiter) {
      const w = waiter;
      waiter = null;
      w({ done: true, value: undefined });
    }
  });

  // 启动 Rust 流式拉取（后台运行，chunk 通过 event 推回）
  invoke("stream_request", {
    request: {
      path,
      method: init.method ?? "POST",
      headers: init.headers ?? null,
      body: bodyValue ?? null,
      stream_id: streamId,
    },
  }).catch((e) => {
    // invoke 本身失败（连接失败已在 Rust 端 emit sse_end，这里兜底）
    error = String(e);
    done = true;
    if (waiter) {
      const w = waiter;
      waiter = null;
      w({ done: true, value: undefined });
    }
  });

  // reader.read()：从队列取 chunk，空则阻塞等待 event。
  const read = (): Promise<StreamReadResult> => {
    if (queue.length > 0) {
      return Promise.resolve({ done: false, value: queue.shift() });
    }
    if (done) {
      if (error) throw new Error(error);
      return Promise.resolve({ done: true, value: undefined });
    }
    // 队列空 + 未结束：阻塞，等 chunk/end event 唤醒
    return new Promise<StreamReadResult>((resolve) => {
      waiter = resolve;
    });
  };

  // cancel()：用户主动停止 / 心跳超时。取消监听，标记 done。
  const cancel = async (): Promise<void> => {
    unlistenChunk();
    unlistenEnd();
    done = true;
    if (waiter) {
      const w = waiter;
      waiter = null;
      w({ done: true, value: undefined });
    }
  };

  return { read, cancel };
}

/**
 * evolution SSE 流式请求（自动加 /evolution-api 前缀）。
 * evolve/eval session 的实时流用这个。
 *
 * 返回一个 async generator，逐帧 yield 已解析的 SSE data JSON 对象。
 * evolve/eval 的 SSE 帧格式统一为 `data: {"type": "...", ...}\n\n`。
 */
export async function* evoSseStream(
  path: string,
  init: StreamRequestInit = {}
): AsyncGenerator<any, void, unknown> {
  // 本地 dev 直连 evolution 不带前缀；生产带 /evolution-api（nginx 反代）
  const EVO_PREFIX = import.meta.env.DEV ? "" : "/evolution-api";
  const fullPath = path.startsWith("/") ? `${EVO_PREFIX}${path}` : `${EVO_PREFIX}/${path}`;
  const reader = await streamRequest(fullPath, init);
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // SSE 帧以 \n\n 分隔
      let idx: number;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        // 解析 data: 行（evolve/eval 只有 data: 帧，无 event: 名）
        for (const line of frame.split("\n")) {
          if (line.startsWith("data:")) {
            const payload = line.slice(5).trim();
            if (!payload) continue;
            try {
              yield JSON.parse(payload);
            } catch {
              // 非 JSON（如 keepalive 注释），跳过
            }
          }
        }
      }
    }
  } finally {
    await reader.cancel();
  }
}

// ── trace SSE 专用封装 ──────────────────────────────────────────

/** trace SSE 帧类型（与 sse_stream.py 命名事件一一对应）。 */
export type TraceSseFrame =
  | { event: "data"; data: Record<string, unknown> }
  | { event: "snapshot"; data: { status?: string; event_count?: number } }
  | { event: "end"; data: Record<string, unknown> }
  | { event: "error"; data: { reason?: string } };

/**
 * trace SSE 流式请求（自动加 /evolution-api 前缀）。
 *
 * 与 evoSseStream 的差异：trace SSE 同时有 `event:` 行和 `data:` 行
 * （snapshot/end/error 命名事件 + 默认 data 事件），evoSseStream 只解析 data 行。
 * 本封装解析完整 SSE 帧，yield `{ event, data }` 统一结构。
 *
 * 帧格式（sse_stream.py）：
 *   data: {TraceLogEvent}       ← 默认事件（逐条增量）
 *   event: snapshot\ndata: {}   ← run 状态校准
 *   event: end\ndata: {}        ← 终态信号
 *   event: error\ndata: {...}   ← 错误降级
 */
export async function* evoTraceStream(
  path: string,
  init: StreamRequestInit = {}
): AsyncGenerator<TraceSseFrame, void, unknown> {
  const EVO_PREFIX = import.meta.env.DEV ? "" : "/evolution-api";
  const fullPath = path.startsWith("/") ? `${EVO_PREFIX}${path}` : `${EVO_PREFIX}/${path}`;
  const reader = await streamRequest(fullPath, init);
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // SSE 帧以 \n\n 分隔
      let idx: number;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);

        // 逐帧解析：收集 event: 行和 data: 行
        let eventName = "data"; // SSE 默认事件名
        let dataRaw = "";
        for (const line of frame.split("\n")) {
          if (line.startsWith("event:")) {
            eventName = line.slice(6).trim();
          } else if (line.startsWith("data:")) {
            dataRaw = line.slice(5).trim();
          }
        }
        if (!dataRaw) continue;
        try {
          const parsed = JSON.parse(dataRaw);
          yield { event: eventName, data: parsed } as TraceSseFrame;
        } catch {
          // 非 JSON（如 keepalive 注释），跳过
        }
      }
    }
  } finally {
    await reader.cancel();
  }
}
