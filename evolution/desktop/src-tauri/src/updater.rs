//! 自动更新（2026-07-08 新增，照搬写作端 desktop/src-tauri/src/updater.rs）。
//!
//! 行为：
//! - check_update：检查是否有新版（fetch latest-evo.json 对比版本）。
//!   有新版 → emit "update_available" event（前端 UpdateBanner 提示用户）。
//!   可选更新——只提示，不自动下载安装。用户确认后才调 install_update。
//! - install_update：用户确认后，下载 + 验签 + 安装 + 重启。
//!
//! 签名密钥：
//! - 公钥写 tauri.conf.json 的 plugins.updater.pubkey。
//! - 私钥由用户个人保管（不上 git），构建签名时用 TAURI_SIGNING_PRIVATE_KEY 环境变量。
//! - 进化端复用写作端同一把签名密钥（公钥本就在 git 里，无安全顾虑）。

use serde::Serialize;
use tauri::{AppHandle, Emitter};
use tauri_plugin_updater::UpdaterExt;

/// 更新信息（emit 到前端 / 命令返回值）。
#[derive(Debug, Clone, Serialize)]
pub struct UpdateInfo {
    /// 是否有新版。
    pub available: bool,
    /// 检查结果状态（让前端精确区分三种情况，不再只靠 available）：
    ///   "update_available" —— 有新版，提示用户更新
    ///   "up_to_date"       —— 已是最新
    ///   "check_failed"     —— 检查失败（网络断/endpoint 没配），body 带错误详情
    pub status: String,
    /// 当前版本。
    pub current_version: String,
    /// 最新版本（available=true 时有值）。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub version: Option<String>,
    /// 发布日期。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub date: Option<String>,
    /// 更新内容（changelog）；检查失败时存放错误详情。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub body: Option<String>,
}

/// 检查更新（不自动安装）。
///
/// 前端用法：`const info = await invoke("check_update")`
/// 也可 listen "update_available" event（启动时 Rust 自动调一次）。
#[tauri::command]
pub async fn check_update(app: AppHandle) -> Result<UpdateInfo, String> {
    let updater = app
        .updater()
        .map_err(|e| format!("初始化 updater 失败: {e}"))?;

    let current_version = app.package_info().version.to_string();

    match updater.check().await {
        Ok(Some(update)) => {
            let info = UpdateInfo {
                available: true,
                status: "update_available".to_string(),
                current_version,
                version: Some(update.version.clone()),
                date: update.date.map(|d| d.to_string()),
                body: update.body.clone(),
            };
            // emit 给前端（启动时自动检查的场景用 event，主动调用用返回值）
            let _ = app.emit("update_available", info.clone());
            Ok(info)
        }
        Ok(None) => Ok(UpdateInfo {
            available: false,
            status: "up_to_date".to_string(),
            current_version,
            version: None,
            date: None,
            body: None,
        }),
        Err(e) => {
            // 检查失败（网络断/updater endpoint 没配）：status="check_failed" + body 带原始错误。
            // 不再用 available:false 伪装成"已是最新"——前端凭 status 精确提示，不再谎报。
            // 仍返回 Ok（不抛错）以免 invoke 走 catch 分支丢掉结构化信息；启动检查也不打扰用户。
            Ok(UpdateInfo {
                available: false,
                status: "check_failed".to_string(),
                current_version,
                version: None,
                date: None,
                body: Some(format!("检查更新失败: {e}")),
            })
        }
    }
}

/// 下载并安装更新（用户确认后调）。
///
/// 前端用法：`await invoke("install_update")`
/// 安装完成后 App 会重启。
#[tauri::command]
pub async fn install_update(app: AppHandle) -> Result<(), String> {
    let updater = app
        .updater()
        .map_err(|e| format!("初始化 updater 失败: {e}"))?;

    let update = updater
        .check()
        .await
        .map_err(|e| format!("检查更新失败: {e}"))?
        .ok_or_else(|| "没有可用更新".to_string())?;

    // 下载 + 验签（pubkey 在 tauri.conf.json 配置）+ 安装
    // 进度回调（已下载字节, 总字节 Option）；完成回调无参。
    // 当前不展示下载进度（只做"提示 + 确认安装"），留空回调。
    update
        .download_and_install(|_downloaded, _total| {}, || {})
        .await
        .map_err(|e| format!("下载安装失败: {e}"))?;

    // 安装完成，重启 App
    app.restart();
}

/// 启动时自动检查更新（只检查 + emit event，不自动装）。在 lib.rs setup 里调用。
pub async fn check_on_startup(app: &AppHandle) {
    let _ = check_update(app.clone()).await;
}
