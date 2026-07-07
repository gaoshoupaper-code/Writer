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
    // TODO（T15）：如需服务端停止生成，调 /api/screenplay/stop（现有接口）
  };

  return { read, cancel };
}
