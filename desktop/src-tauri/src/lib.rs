// Writer 桌面端 Rust 后端入口。
//
// 模块组织（设计文档 WBS）：
// - state:   app 级 reqwest Client 单例 + server_url 管理（T6/T8 地基）
// - http:    http_request / stream_request command（T6/T7 数据流中继）
// - config:  server_url 读写 command（T8）
// - updater: 自动更新检查 + 安装（T9）
// - cache:   离线缓存拉取 + 读取（T10）

mod cache;
mod config;
mod http;
mod state;
mod updater;

use std::sync::Arc;
use state::{AppState, SharedState};
use tauri::Manager;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_store::Builder::default().build())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .setup(|app| {
            // 初始化 app 级状态：读 server_url + 建 reqwest Client。
            // block_on 等 init 完成（init 内部用 spawn_blocking 读 Store，不卡 async 运行时）。
            let handle = app.handle().clone();
            let state =
                tauri::async_runtime::block_on(async move { AppState::init(&handle).await });
            app.manage(Arc::new(state) as SharedState);

            // 启动时自动检查更新（D16：可选更新 = 只 emit event 提示，不自动装）。
            // 失败静默——更新是锦上添花，不该阻塞启动。
            let update_handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                updater::check_on_startup(&update_handle).await;
            });

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            http::http_request,
            http::stream_request,
            config::get_server_url,
            config::set_server_url,
            config::reset_server_url,
            updater::check_update,
            updater::install_update,
            cache::refresh_cache,
            cache::read_cache,
            cache::clear_cache,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
