//! HTTP 中继 command（设计文档 S4/S5）。
//!
//! 统一入口 `http_request`：前端所有网络请求都走它。
//! - stream=false（默认）：普通请求，reqwest 发 → 返回 {status, headers, body}
//! - stream=true：流式请求（SSE），reqwest bytes_stream → 逐 chunk emit "sse_chunk" event
//!
//! 这样设计的理由（S5）：
//! - 绕过 WebView CORS/SameSite（reqwest 是原生层，不受浏览器同源策略约束）
//! - cookie 自动由 Client 的 cookie jar 管理（登录后所有请求带 session）
//! - executor 零改动（不需要加 tauri.localhost 到 CORS 白名单）

use crate::state::SharedState;
use futures_util::StreamExt;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use tauri::{AppHandle, Emitter, State};

/// 前端 invoke 时的请求参数。
#[derive(Debug, Deserialize)]
pub struct HttpRequest {
    /// 路径，如 "/api/auth/login"。会拼到 server_url 后。
    pub path: String,
    /// HTTP 方法，默认 GET。
    #[serde(default = "default_method")]
    pub method: String,
    /// 请求头（可选）。
    #[serde(default)]
    pub headers: Option<HashMap<String, String>>,
    /// 请求体（可选，JSON 值）。
    #[serde(default)]
    pub body: Option<serde_json::Value>,
    /// 是否走流式分支（SSE）。
    /// true → 不直接返回，改用 Tauri event 推 chunk（见 stream_request）。
    #[serde(default)]
    pub stream: bool,
}

fn default_method() -> String {
    "GET".to_string()
}

/// 普通（非流式）响应。前端解析 body（JSON 字符串）。
#[derive(Debug, Serialize)]
pub struct HttpResponse {
    pub status: u16,
    pub headers: HashMap<String, String>,
    /// 响应体文本。前端按需 JSON.parse。
    pub body: String,
}

/// 流式请求的标识（前端 invoke 时传，用于匹配 event）。
/// 每个流式请求生成唯一 id，chunk event 带这个 id，前端 listen 时按 id 过滤。
#[derive(Debug, Deserialize)]
pub struct StreamRequest {
    pub path: String,
    #[serde(default = "default_method")]
    pub method: String,
    #[serde(default)]
    pub headers: Option<HashMap<String, String>>,
    #[serde(default)]
    pub body: Option<serde_json::Value>,
    /// 唯一流标识（前端生成 UUID 传入）。
    pub stream_id: String,
}

/// SSE chunk event payload（emit 到前端）。
#[derive(Debug, Clone, Serialize)]
pub struct SseChunk {
    /// 匹配前端 stream_id。
    pub stream_id: String,
    /// 本次 chunk 的文本内容（SSE 原始格式，前端自行解析 data:/event: 行）。
    pub chunk: String,
}

/// SSE 结束 event payload。
#[derive(Debug, Clone, Serialize)]
pub struct SseEnd {
    pub stream_id: String,
    /// 正常结束 vs 出错。
    pub ok: bool,
    /// 出错时的错误信息。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

/// 普通 HTTP 请求（stream=false）。
///
/// 前端用法：`const res = await invoke("http_request", { path, method, body })`
/// 返回 { status, headers, body }。body 是文本，前端按需 parse。
///
/// 响应后自动持久化 cookie jar（登录响应含 Set-Cookie 时 jar 已更新），
/// 重启后可恢复 session 免重登。持久化失败不阻断请求（只 log）。
#[tauri::command]
pub async fn http_request(
    app: AppHandle,
    state: State<'_, SharedState>,
    request: HttpRequest,
) -> Result<HttpResponse, String> {
    if request.stream {
        // 流式请求应调 stream_request，走这里说明前端误用——拒绝。
        return Err("流式请求请用 stream_request command".into());
    }

    let client = state
        .client()
        .await
        .ok_or_else(|| "HTTP client 未初始化".to_string())?;
    let server_url = state.server_url().await;
    let url = format!("{}{}", server_url, request.path);

    let method = parse_method(&request.method)?;
    let mut req = client.request(method, &url);
    if let Some(h) = &request.headers {
        for (k, v) in h {
            req = req.header(k, v);
        }
    }
    if let Some(body) = request.body {
        req = req.json(&body);
    }

    let resp = req
        .send()
        .await
        .map_err(|e| format!("请求失败: {e}"))?;

    let status = resp.status().as_u16();
    let mut headers = HashMap::new();
    for (k, v) in resp.headers() {
        if let Ok(vs) = v.to_str() {
            headers.insert(k.as_str().to_string(), vs.to_string());
        }
    }
    let body = resp
        .text()
        .await
        .map_err(|e| format!("读取响应体失败: {e}"))?;

    // 持久化 cookie jar（登录等响应含 Set-Cookie 时 jar 已更新）。
    // 失败不阻断请求——最坏情况是重启要重登，与改造前一致。
    if let Err(e) = state.save_cookie_jar(&app).await {
        eprintln!("warn: cookie jar 持久化失败（不阻断请求）: {e}");
    }

    Ok(HttpResponse {
        status,
        headers,
        body,
    })
}

/// 流式 HTTP 请求（SSE）。设计文档 S11。
///
/// 前端用法：
/// 1. 生成 stream_id
/// 2. `listen("sse_chunk", e => 过滤 stream_id 匹配的)`
/// 3. `listen("sse_end", e => 过滤 stream_id 匹配的)`
/// 4. `invoke("stream_request", { path, method, body, stream_id })`（不 await 结果，靠 event 收）
///
/// Rust 端：reqwest bytes_stream → 每个 chunk 转 String → emit "sse_chunk" → 流结束 emit "sse_end"
#[tauri::command]
pub async fn stream_request(
    app: AppHandle,
    state: State<'_, SharedState>,
    request: StreamRequest,
) -> Result<(), String> {
    let client = state
        .client()
        .await
        .ok_or_else(|| "HTTP client 未初始化".to_string())?;
    let server_url = state.server_url().await;
    let url = format!("{}{}", server_url, request.path);

    let method = parse_method(&request.method)?;
    let mut req = client.request(method, &url);
    if let Some(h) = &request.headers {
        for (k, v) in h {
            req = req.header(k, v);
        }
    }
    // SSE 是 POST + JSON body（写作 Agent 生成请求带 thread_id 等）
    if let Some(body) = request.body {
        req = req.json(&body);
    }

    let stream_id = request.stream_id.clone();

    // 发请求，拿 stream。send() 返回 Response，bytes_stream() 给 chunk 迭代器。
    let resp = req
        .send()
        .await
        .map_err(|e| {
            // 连接就失败：立即 emit end（ok=false）
            let _ = app.emit(
                "sse_end",
                SseEnd {
                    stream_id: stream_id.clone(),
                    ok: false,
                    error: Some(format!("连接失败: {e}")),
                },
            );
            format!("流式请求连接失败: {e}")
        })?;

    // 状态码非 2xx：不算正常流，把错误信息 emit 出去
    if !resp.status().is_success() {
        let status = resp.status().as_u16();
        let body = resp.text().await.unwrap_or_default();
        let _ = app.emit(
            "sse_end",
            SseEnd {
                stream_id: stream_id.clone(),
                ok: false,
                error: Some(format!("HTTP {status}: {body}")),
            },
        );
        return Ok(());
    }

    // 拆成 stream，逐 chunk 推送。
    // spawn 独立 task：让 command 立即返回 Ok(())，流式在后台推 event。
    // 否则 await 整个流会让 invoke 阻塞到生成结束（前端拿不到 Ok）。
    let app_clone = app.clone();
    let stream_id_task = stream_id.clone();
    tokio::spawn(async move {
        let mut stream = resp.bytes_stream();
        while let Some(chunk_result) = stream.next().await {
            match chunk_result {
                Ok(bytes) => {
                    let text = String::from_utf8_lossy(&bytes).to_string();
                    if text.is_empty() {
                        continue;
                    }
                    let _ = app_clone.emit(
                        "sse_chunk",
                        SseChunk {
                            stream_id: stream_id_task.clone(),
                            chunk: text,
                        },
                    );
                }
                Err(e) => {
                    let _ = app_clone.emit(
                        "sse_end",
                        SseEnd {
                            stream_id: stream_id_task.clone(),
                            ok: false,
                            error: Some(format!("流读取错误: {e}")),
                        },
                    );
                    return;
                }
            }
        }
        // 流正常结束
        let _ = app_clone.emit(
            "sse_end",
            SseEnd {
                stream_id: stream_id_task,
                ok: true,
                error: None,
            },
        );
    });

    Ok(())
}

fn parse_method(s: &str) -> Result<reqwest::Method, String> {
    match s.to_uppercase().as_str() {
        "GET" => Ok(reqwest::Method::GET),
        "POST" => Ok(reqwest::Method::POST),
        "PUT" => Ok(reqwest::Method::PUT),
        "DELETE" => Ok(reqwest::Method::DELETE),
        "PATCH" => Ok(reqwest::Method::PATCH),
        _ => Err(format!("不支持的 HTTP 方法: {s}")),
    }
}
