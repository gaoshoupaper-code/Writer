//! 离线缓存（设计文档 D13/D14/S10/T10）。
//!
//! 行为（S10）：
//! - refresh_cache：登录成功后后台调，拉作品列表 + 各作品章节正文，存 JSON。
//! - read_cache：断网时 UI 降级读，按 user_id 区分（切账号不串数据）。
//! - 失败静默：保留旧缓存，不报错（缓存是锦上添花，不该打扰用户）。
//!
//! 存储：Tauri app_data_dir/cache/<user_id>.json
//! 数据量小（几十 KB），JSON 文件足够，不上 SQLite。

use crate::state::SharedState;
use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use tauri::{AppHandle, Manager, State};

/// 缓存数据结构（对应设计文档 S10）。
/// 用 serde_json::Value 存原始 API 返回，不重复定义后端 schema——
/// 后端字段变了缓存自动跟着变，Rust 端不耦合业务类型。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CacheData {
    pub user_id: String,
    /// 时间戳，断网时 UI 显示"缓存于 xxx"。
    pub fetched_at: String,
    /// /api/workspaces 的原始返回（作品列表）。
    pub workspaces: serde_json::Value,
    /// /api/workspaces/{id}/novel 的原始返回，按 workspace_id 索引。
    pub novels: serde_json::Value,
}

/// refresh_cache 的请求参数。
#[derive(Debug, Deserialize)]
pub struct RefreshCacheRequest {
    /// 当前登录用户 id（前端登录后传入，用于按用户隔离缓存文件）。
    pub user_id: String,
}

/// 拉 + 存缓存。
///
/// 前端用法（登录成功后调）：
/// `await invoke("refresh_cache", { userId: "xxx" })`
///
/// 失败返回 Err，但前端应忽略错误（静默降级用旧缓存）。
#[tauri::command]
pub async fn refresh_cache(
    app: AppHandle,
    state: State<'_, SharedState>,
    request: RefreshCacheRequest,
) -> Result<(), String> {
    // 直接复用 SharedState 的 client（不走 http_request command，避免 command 互调的签名问题）
    let client = state
        .client()
        .await
        .ok_or_else(|| "client 未初始化".to_string())?;
    let server_url = state.server_url().await;

    // 1. 拉作品列表
    let list_url = format!("{}/api/workspaces", server_url);
    let list_resp = client
        .get(&list_url)
        .send()
        .await
        .map_err(|e| format!("拉作品列表失败: {e}"))?;
    let workspaces: Vec<serde_json::Value> = list_resp
        .json()
        .await
        .map_err(|e| format!("解析作品列表失败: {e}"))?;

    // 2. 并行拉每个作品的章节正文
    let mut tasks = Vec::new();
    for ws in &workspaces {
        let ws_id = match ws.get("workspace_id").and_then(|v| v.as_str()) {
            Some(id) => id.to_string(),
            None => continue,
        };
        let client_clone = client.clone();
        let url = format!("{}/api/workspaces/{}/novel", server_url, ws_id);
        tasks.push(async move {
            let resp = client_clone
                .get(&url)
                .send()
                .await
                .map_err(|e| format!("拉 novel 失败: {e}"))?;
            let json: serde_json::Value =
                resp.json().await.map_err(|e| format!("解析 novel 失败: {e}"))?;
            Ok::<(String, serde_json::Value), String>((ws_id, json))
        });
    }

    let results = futures_util::future::join_all(tasks).await;
    let mut novels = serde_json::Map::new();
    for result in results {
        match result {
            Ok((ws_id, json)) => {
                novels.insert(ws_id, json);
            }
            // 单个作品拉取失败不阻塞其他——静默跳过
            Err(_) => continue,
        }
    }

    // 3. 组装 + 存盘
    let cache = CacheData {
        user_id: request.user_id.clone(),
        fetched_at: chrono_now_iso(),
        workspaces: serde_json::Value::Array(workspaces),
        novels: serde_json::Value::Object(novels),
    };

    let cache_path = cache_file_path(&app, &request.user_id)?;
    if let Some(parent) = cache_path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| format!("创建缓存目录失败: {e}"))?;
    }
    let json =
        serde_json::to_string_pretty(&cache).map_err(|e| format!("序列化缓存失败: {e}"))?;
    std::fs::write(&cache_path, json).map_err(|e| format!("写缓存文件失败: {e}"))?;

    Ok(())
}

/// 读缓存（断网时 UI 降级用）。
///
/// 前端用法：`const cache = await invoke("read_cache", { userId: "xxx" })`
/// 返回 null 表示无缓存（首次使用或缓存被清）。
#[tauri::command]
pub async fn read_cache(
    app: AppHandle,
    user_id: String,
) -> Result<Option<CacheData>, String> {
    let path = cache_file_path(&app, &user_id)?;
    if !path.exists() {
        return Ok(None);
    }
    let content = std::fs::read_to_string(&path)
        .map_err(|e| format!("读缓存文件失败: {e}"))?;
    let cache: CacheData =
        serde_json::from_str(&content).map_err(|e| format!("解析缓存失败: {e}"))?;
    Ok(Some(cache))
}

/// 清缓存（切账号/登出时可选调）。
#[tauri::command]
pub async fn clear_cache(app: AppHandle, user_id: String) -> Result<(), String> {
    let path = cache_file_path(&app, &user_id)?;
    if path.exists() {
        std::fs::remove_file(&path).map_err(|e| format!("删缓存失败: {e}"))?;
    }
    Ok(())
}

/// 缓存文件路径：app_data_dir/cache/<user_id>.json
fn cache_file_path(app: &AppHandle, user_id: &str) -> Result<PathBuf, String> {
    let data_dir = app
        .path()
        .app_data_dir()
        .map_err(|e| format!("获取 app_data_dir 失败: {e}"))?;
    Ok(data_dir.join("cache").join(format!("{user_id}.json")))
}

/// 当前时间 ISO 字符串（不引 chrono 依赖，手写足够）。
fn chrono_now_iso() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    // 简单 Unix 时间戳转字符串——精确到秒足够（缓存时间戳只给 UI 显示用）
    format!("unix:{secs}")
}
