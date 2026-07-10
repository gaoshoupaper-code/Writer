//! App 级状态：reqwest Client 单例 + server_url 管理。
//!
//! Evolution 桌面端（桌面化改造 2026-07-07），复用写作 desktop 基建：
//! - reqwest Client 长生命周期，持 cookie jar（登录态跨请求复用——SSO 必需）。
//! - 与写作 desktop 连同一个 siyen.site（SSO 同域 cookie 共享）。
//! - server_url 从 Store 读，变更时重建 Client + 清 cookie。
//! - cookie jar 可序列化，持久化到 Tauri Store——重启免重登。

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
const KEY_COOKIE_JAR: &str = "cookie_jar";

/// App 级共享状态。Tauri manage() 注册，所有 command 通过 State 访问。
pub struct AppState {
    /// reqwest Client（通过 cookie_provider 共享下面的 jar）。
    /// Arc<RwLock> 让请求并发读、重建时独占写。
    /// Option 允许"未初始化"状态（首次 server_url 读取失败时）。
    client: RwLock<Option<reqwest::Client>>,
    /// 当前 server_url（去尾斜杠）。重建 Client 时用。
    server_url: RwLock<String>,
    /// 可序列化的 cookie jar（重启后从 Store 恢复，免重登）。
    /// 用 std::sync::RwLock（非 tokio），因为 reqwest 的 CookieStore trait 方法是同步的。
    cookie_jar: Arc<std::sync::RwLock<cookie_store::CookieStore>>,
}

impl AppState {
    /// 首次初始化：从 Store 读 server_url + cookie jar，建 Client。
    /// 失败回退 DEFAULT_SERVER_URL / 空 jar。
    pub async fn init(app: &AppHandle) -> Self {
        let server_url = load_server_url(app).await;
        let cookie_jar = load_cookie_jar(app).await;
        let jar_arc = Arc::new(std::sync::RwLock::new(cookie_jar));
        let client = build_client(jar_arc.clone());
        Self {
            client: RwLock::new(Some(client)),
            server_url: RwLock::new(server_url),
            cookie_jar: jar_arc,
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
        // 清空 cookie jar（新服务器旧 cookie 无效）
        {
            let mut jar = self.cookie_jar.write().unwrap();
            jar.clear();
        }
        // 重建 Client（复用同一个 jar 实例，只是清空了内容）
        let new_client = build_client(self.cookie_jar.clone());
        {
            let mut client_guard = self.client.write().await;
            *client_guard = Some(new_client);
        }
        {
            let mut url_guard = self.server_url.write().await;
            *url_guard = normalized;
        }
    }

    /// 持久化当前 cookie jar 到 Store（http_request 响应后调用）。
    /// 登录响应含 Set-Cookie 时 jar 已被 reqwest 自动更新，这里序列化落盘。
    pub async fn save_cookie_jar(&self, app: &AppHandle) -> Result<(), String> {
        let json = {
            let jar = self.cookie_jar.read().unwrap();
            serde_json::to_string(&*jar)
                .map_err(|e| format!("序列化 cookie jar 失败: {e}"))?
        };
        let app = app.clone();
        tokio::task::spawn_blocking(move || {
            let store = app.store(STORE_FILE)
                .map_err(|e| format!("打开 store 失败: {e}"))?;
            store.set(KEY_COOKIE_JAR, serde_json::json!(json));
            store.save().map_err(|e| format!("保存 store 失败: {e}"))?;
            Ok::<(), String>(())
        })
        .await
        .map_err(|e| format!("store 任务失败: {e}"))??;
        Ok(())
    }
}

/// reqwest CookieStore trait 的适配器。
/// cookie_store::CookieStore（可序列化）不直接实现 reqwest::cookie::CookieStore，
/// 需要一个 wrapper 来桥接：把 reqwest 的同步 set_cookies/cookies 调用代理到内部 jar。
struct ReqwestCookieJar(Arc<std::sync::RwLock<cookie_store::CookieStore>>);

impl reqwest::cookie::CookieStore for ReqwestCookieJar {
    fn set_cookies(
        &self,
        cookie_headers: &mut dyn Iterator<Item = &reqwest::header::HeaderValue>,
        url: &url::Url,
    ) {
        let mut store = self.0.write().unwrap();
        // 把 Set-Cookie header 值解析成 cookie::Cookie，再存入 jar
        let cookies = cookie_headers.filter_map(|hv| {
            let s = hv.to_str().ok()?;
            cookie::Cookie::parse(s.to_owned()).ok()
        });
        store.store_response_cookies(cookies, url);
    }

    fn cookies(&self, url: &url::Url) -> Option<reqwest::header::HeaderValue> {
        let store = self.0.read().unwrap();
        // get_request_values 返回 (&str, &str) 键值对，拼成 "k1=v1; k2=v2" 格式
        let pairs: Vec<(&str, &str)> = store.get_request_values(url).collect();
        if pairs.is_empty() {
            return None;
        }
        let header = pairs
            .iter()
            .map(|(k, v)| format!("{k}={v}"))
            .collect::<Vec<_>>()
            .join("; ");
        reqwest::header::HeaderValue::from_str(&header).ok()
    }
}

/// 构建带共享 cookie jar 的 reqwest Client。
/// 用 cookie_provider 注入可序列化的外部 jar（而非 cookie_store(true) 黑盒 jar），
/// 这样 jar 可以持久化到 Tauri Store，重启后恢复 session。
fn build_client(jar: Arc<std::sync::RwLock<cookie_store::CookieStore>>) -> reqwest::Client {
    reqwest::Client::builder()
        .cookie_provider(Arc::new(ReqwestCookieJar(jar)))
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

/// 从 Store 反序列化 cookie jar。失败/不存在 → 空 jar（首次启动或持久化损坏）。
async fn load_cookie_jar(app: &AppHandle) -> cookie_store::CookieStore {
    let app = app.clone();
    tokio::task::spawn_blocking(move || {
        let store = match app.store(STORE_FILE) {
            Ok(s) => s,
            Err(_) => return cookie_store::CookieStore::default(),
        };
        match store.get(KEY_COOKIE_JAR) {
            Some(v) => {
                let json_str = match v.as_str() {
                    Some(s) => s,
                    None => return cookie_store::CookieStore::default(),
                };
                serde_json::from_str::<cookie_store::CookieStore>(json_str)
                    .unwrap_or_else(|_| cookie_store::CookieStore::default())
            }
            None => cookie_store::CookieStore::default(),
        }
    })
    .await
    .unwrap_or_else(|_| cookie_store::CookieStore::default())
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
