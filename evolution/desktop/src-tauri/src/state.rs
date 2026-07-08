//! App 级状态：reqwest Client 单例 + server_url 管理。
//!
//! Evolution 桌面端（桌面化改造 2026-07-07），复用写作 desktop 基建：
//! - reqwest Client 长生命周期，持 cookie jar（登录态跨请求复用——SSO 必需）。
//! - 与写作 desktop 连同一个 siyen.site（SSO 同域 cookie 共享）。
//! - server_url 从 Store 读，变更时重建 Client + 清 cookie。

use std::sync::Arc;
use tauri::AppHandle;
use tauri_plugin_store::StoreExt;
use tokio::sync::RwLock;

/// 默认服务器地址。
/// - dev 构建：本地 evolution（127.0.0.1:7789），方便本地验证。
/// - release 构建：官方服务器 siyen.site。
#[cfg(debug_assertions)]
pub const DEFAULT_SERVER_URL: &str = "http://127.0.0.1:7789";
#[cfg(not(debug_assertions))]
pub const DEFAULT_SERVER_URL: &str = "https://siyen.site";

const STORE_FILE: &str = "settings.json";
const KEY_SERVER_URL: &str = "server_url";

/// App 级共享状态。Tauri manage() 注册，所有 command 通过 State 访问。
pub struct AppState {
    /// reqwest Client（持 cookie jar）。
    /// Arc<RwLock> 让请求并发读、重建时独占写。
    /// Option 允许"未初始化"状态（首次 server_url 读取失败时）。
    client: RwLock<Option<reqwest::Client>>,
    /// 当前 server_url（去尾斜杠）。重建 Client 时用。
    server_url: RwLock<String>,
}

impl AppState {
    /// 首次初始化：从 Store 读 server_url，建 Client。
    /// 失败回退 DEFAULT_SERVER_URL。
    pub async fn init(app: &AppHandle) -> Self {
        let server_url = load_server_url(app).await;
        let client = build_client();
        Self {
            client: RwLock::new(Some(client)),
            server_url: RwLock::new(server_url),
        }
    }

    /// 读当前 server_url（去尾斜杠）。
    pub async fn server_url(&self) -> String {
        self.server_url.read().await.clone()
    }

    /// 取当前 Client（克隆 Arc，零拷贝）。
    /// 返回 None 表示 Client 重建中或未初始化。
    pub async fn client(&self) -> Option<reqwest::Client> {
        self.client.read().await.clone()
    }

    /// 切换 server_url（S6 补充约束）。
    /// 重建 Client + 清 cookie——旧服务器 cookie 在新服务器无效，
    /// 不清会导致带着 A 站 session 请求 B 站的诡异 bug。
    pub async fn set_server_url(&self, new_url: String) {
        let normalized = normalize_url(&new_url);
        // 先重建 Client（新 Client = 新 cookie jar = 旧 cookie 清空）
        let new_client = build_client();
        // 写锁：同时更新 client 和 server_url
        {
            let mut client_guard = self.client.write().await;
            *client_guard = Some(new_client);
        }
        {
            let mut url_guard = self.server_url.write().await;
            *url_guard = normalized;
        }
    }
}

/// 构建带 cookie jar 的 reqwest Client。
/// cookie_store 特性开启后，Client 自动维护 jar，登录后后续请求自动带 session cookie。
fn build_client() -> reqwest::Client {
    reqwest::Client::builder()
        .cookie_store(true)
        // 跟随重定向（登录后可能 302）
        .redirect(reqwest::redirect::Policy::limited(5))
        // 连接建立超时：网络不通/服务器宕机时快速失败（10s），
        // 避免 fetchMeOrNull 等探测请求卡住前端登录守卫。
        .connect_timeout(std::time::Duration::from_secs(10))
        // 总超时：覆盖普通请求（SSE 流式请求在 stream_request 里单独处理，
        // 但共用 Client 无法完全区分——300s 足够覆盖大部分长生成场景）。
        .timeout(std::time::Duration::from_secs(300))
        .build()
        .expect("failed to build reqwest client")
}

/// 从 Store 读 server_url。
/// Store 不存在/key 缺失 → 回退 DEFAULT_SERVER_URL。
async fn load_server_url(app: &AppHandle) -> String {
    let app = app.clone();
    // Store 操作是同步的，放阻塞线程避免卡 async 运行时
    tokio::task::spawn_blocking(move || {
        match app.store(STORE_FILE) {
            Ok(store) => match store.get(KEY_SERVER_URL) {
                Some(v) => {
                    let s = v.as_str().unwrap_or(DEFAULT_SERVER_URL).to_string();
                    normalize_url(&s)
                }
                None => DEFAULT_SERVER_URL.to_string(),
            },
            Err(_) => DEFAULT_SERVER_URL.to_string(),
        }
    })
    .await
    .unwrap_or_else(|_| DEFAULT_SERVER_URL.to_string())
}

/// 写 server_url 到 Store（持久化）。
pub async fn save_server_url(app: &AppHandle, url: &str) -> Result<(), String> {
    let app = app.clone();
    let url = normalize_url(url);
    tokio::task::spawn_blocking(move || {
        let store = app
            .store(STORE_FILE)
            .map_err(|e| format!("打开 store 失败: {e}"))?;
        store.set(KEY_SERVER_URL, serde_json::json!(url));
        store.save().map_err(|e| format!("保存 store 失败: {e}"))?;
        Ok::<(), String>(())
    })
    .await
    .map_err(|e| format!("store 任务失败: {e}"))??;
    Ok(())
}

/// 规范化 URL：去尾斜杠，校验合法 http(s)。
/// 不合法时返回 DEFAULT，避免坏 URL 让整个 App 瘫。
fn normalize_url(url: &str) -> String {
    let trimmed = url.trim().trim_end_matches('/');
    if trimmed.is_empty() {
        return DEFAULT_SERVER_URL.to_string();
    }
    if !trimmed.starts_with("http://") && !trimmed.starts_with("https://") {
        return DEFAULT_SERVER_URL.to_string();
    }
    if url::Url::parse(trimmed).is_err() {
        return DEFAULT_SERVER_URL.to_string();
    }
    trimmed.to_string()
}

/// 用 Arc 包裹，注册到 Tauri manage。
/// AppState 内部已有 RwLock，外层 Arc 让多个 State<T> 句柄共享同一实例。
pub type SharedState = Arc<AppState>;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_normalize_url() {
        assert_eq!(normalize_url("https://siyen.site/"), "https://siyen.site");
        assert_eq!(normalize_url("https://siyen.site"), "https://siyen.site");
        assert_eq!(normalize_url(""), DEFAULT_SERVER_URL);
        assert_eq!(normalize_url("not-a-url"), DEFAULT_SERVER_URL);
        assert_eq!(normalize_url("ftp://bad"), DEFAULT_SERVER_URL);
        assert_eq!(
            normalize_url("http://localhost:7788"),
            "http://localhost:7788"
        );
    }
}
