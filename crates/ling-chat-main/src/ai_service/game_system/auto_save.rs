use std::hash::{DefaultHasher, Hash, Hasher};
use std::sync::Arc;
use std::time::Duration;

use chrono::Local;
use sea_orm::DatabaseConnection;
use serde::Serialize;
use tauri::{AppHandle, Emitter, WebviewWindow};
use tokio::sync::Mutex;

use crate::ai_service::service::SharedAIService;
use crate::ai_service::types::GameLine;
use crate::config::AppConfig;
use crate::db::entities::line::LineAttribute;
use crate::db::managers::save_repo::SaveRepo;

const AUTO_SAVE_PREFIX: &str = "自动存档";
const EXIT_SAVE_TIMEOUT_SECS: u64 = 5;

/// 一行是否属于「真实对话」（玩家发言或角色回复）；人设行与旁白/系统/剧情提示不计。
pub(crate) fn is_real_dialogue(line: &GameLine) -> bool {
    match line.attribute() {
        // 角色回复：assistant 且归属某角色（工具调用回填的前缀行 sender 为空，排除）
        LineAttribute::Assistant => line.base.sender_role_id.is_some(),
        // 玩家发言：sender_role_id==0（DB 不变量）且非旁白/系统/剧情内容
        LineAttribute::User => {
            line.base.sender_role_id == Some(0)
                && !line
                    .base
                    .content
                    .starts_with(crate::utils::prompt::NARRATION_TAG)
        },
        _ => false,
    }
}

/// 计算「真实对话」内容指纹；无真实对话返回 `None`。
pub fn hash_of_real_lines(lines: &[GameLine]) -> Option<u64> {
    let mut hasher = DefaultHasher::new();
    let mut real = 0u64;
    for line in lines {
        if !is_real_dialogue(line) {
            continue;
        }
        real += 1;
        line.base.content.hash(&mut hasher);
        line.base.sender_role_id.hash(&mut hasher);
        line.base.attribute.as_str().hash(&mut hasher);
    }
    if real == 0 {
        None
    } else {
        Some(hasher.finish())
    }
}

/// Payload emitted to frontend after each successful auto-save.
#[derive(Debug, Clone, Serialize)]
struct AutoSaveEventPayload {
    save_id: i32,
    title: String,
    timestamp: String,
}

pub struct AutoSaveManager {
    app: AppHandle,
    db: DatabaseConnection,
    ai_service: SharedAIService,
    /// Hash of line_list at the moment of the last successful auto-save.
    last_saved_hash: Option<u64>,
    /// Resolved auto-save slot ID (lazily found or created on first save).
    auto_save_id: Option<i32>,
}

impl AutoSaveManager {
    pub fn new(app: AppHandle, db: DatabaseConnection, ai_service: SharedAIService) -> Self {
        Self {
            app,
            db,
            ai_service,
            last_saved_hash: None,
            auto_save_id: None,
        }
    }

    // ========== Periodic Loop ==========

    /// 定时自动存档循环，不返回；每轮热重读开关与间隔。
    pub async fn run_periodic(manager: Arc<Mutex<Self>>) {
        loop {
            let (enabled, interval_secs) = {
                let mgr = manager.lock().await;
                let cfg = AppConfig::load(&mgr.app).unwrap_or_default();
                (cfg.auto_save_enabled, cfg.auto_save_interval_secs)
            };

            tokio::time::sleep(Duration::from_secs(interval_secs as u64)).await;

            if !enabled {
                continue;
            }

            let mut mgr = manager.lock().await;
            if let Err(e) = mgr.perform_save().await {
                tracing::warn!("[AutoSave] 自动存档失败: {e}");
            }
        }
    }

    // ========== Close Handler ==========

    /// 在主窗口注册关闭前存档的处理器。
    pub fn setup_close_handler(app: AppHandle, window: WebviewWindow, manager: Arc<Mutex<Self>>) {
        window.clone().on_window_event(move |event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();

                let mgr = manager.clone();
                let ah = app.clone();

                tauri::async_runtime::spawn(async move {
                    tracing::info!("[AutoSave] 正在执行退出前自动存档...");

                    let save_result =
                        tokio::time::timeout(Duration::from_secs(EXIT_SAVE_TIMEOUT_SECS), async {
                            let mut mgr = mgr.lock().await;
                            mgr.perform_exit_save().await
                        })
                        .await;

                    match save_result {
                        Ok(Ok(())) => tracing::info!("[AutoSave] 退出前存档完成"),
                        Ok(Err(ref e)) => tracing::error!("[AutoSave] 退出前存档失败: {}", e),
                        Err(_) => tracing::warn!(
                            "[AutoSave] 退出前存档超时（{} 秒），放弃等待",
                            EXIT_SAVE_TIMEOUT_SECS
                        ),
                    }

                    drop(save_result);

                    // 通知前端存档已完成，由前端决定是否退出
                    let _ = ah.emit("app:close-ready", ());
                });
            }
        });
    }

    // ========== Core Save Logic ==========

    /// Perform a save only when there is real dialogue whose content changed since last save.
    async fn perform_save(&mut self) -> Result<(), String> {
        let cfg = AppConfig::load(&self.app).unwrap_or_default();
        if !cfg.auto_save_enabled {
            return Ok(());
        }

        let current_hash = self.compute_line_hash().await;

        let current_hash = match current_hash {
            Some(h) => h,
            None => {
                // 无真实对话（主菜单/仅人设/仅旁白）— 绝不覆盖旧档
                return Ok(());
            },
        };

        if self.last_saved_hash == Some(current_hash) {
            return Ok(());
        }

        let save_id = self.find_or_create_slot().await?;

        let mut service = self.ai_service.lock().await;
        let lines = service.game_status.lock().await.line_list.clone();

        SaveRepo::sync_lines(&self.db, save_id, &lines)
            .await
            .map_err(|e| format!("同步台词失败: {}", e))?;

        service.game_status.lock().await.active_save_id = Some(save_id);

        // 记录本局使用的「我的身份」，否则读自动存档会沿用全局当前身份。
        {
            let identity_id = service.game_status.lock().await.player.identity_id.clone();
            if let Some(ref id) = identity_id {
                if !id.trim().is_empty() {
                    if let Err(e) = SaveRepo::upsert_save_identity(&self.db, save_id, id).await {
                        tracing::warn!("[AutoSave] 记录本局身份失败: {e}");
                    }
                }
            }
        }

        let snapshot = service.game_status.lock().await.to_snapshot();
        let snapshot_json =
            serde_json::to_string(&snapshot).map_err(|e| format!("序列化状态失败: {}", e))?;
        SaveRepo::update_save_status(&self.db, save_id, &snapshot_json)
            .await
            .map_err(|e| format!("保存状态失败: {}", e))?;

        service
            .persist_memory_banks(save_id)
            .await
            .map_err(|e| format!("保存记忆库失败: {}", e))?;

        let script_status_snapshot = service.game_status.lock().await.script_status.clone();
        match script_status_snapshot {
            Some(script_status) => {
                let vars_json = serde_json::to_string(&script_status.vars).unwrap_or_default();
                // 与手动存档统一：写 path_key() 并归一化分隔符。
                let script_folder = script_status.path_key().replace('\\', "/");
                let _ = SaveRepo::upsert_running_script(
                    &self.db,
                    save_id,
                    &script_folder,
                    &vars_json,
                    &script_status.current_chapter_key,
                    script_status.current_event_process,
                )
                .await
                .map_err(|e| {
                    tracing::warn!("[AutoSave] 保存剧本状态失败: {}", e);
                });
            },
            None => {
                // 无剧本在跑：清掉旧的进度行。
                if let Err(e) = SaveRepo::clear_running_script(&self.db, save_id).await {
                    tracing::warn!("[AutoSave] 清理旧剧本状态失败: {}", e);
                }
            },
        }

        drop(service);

        self.last_saved_hash = Some(current_hash);

        let now = Local::now();
        let title = format!("{} {}", AUTO_SAVE_PREFIX, now.format("%Y-%m-%d %H:%M:%S"));
        let timestamp = now.format("%H:%M:%S").to_string();

        let _ = self.app.emit(
            "save:auto-saved",
            AutoSaveEventPayload {
                save_id,
                title,
                timestamp,
            },
        );

        tracing::info!("[AutoSave] 自动存档完成 save_id={}", save_id);
        Ok(())
    }

    /// 退出前存档：与定时存档同一套闸门。
    async fn perform_exit_save(&mut self) -> Result<(), String> {
        self.perform_save().await
    }

    // ========== Helpers ==========

    /// 载入/手动存档成功后同步基线，避免下一次 tick 重复存档。
    pub fn set_baseline(&mut self, hash: Option<u64>) {
        self.last_saved_hash = hash;
    }

    /// Compute a hash of the current real-dialogue contents (see [`hash_of_real_lines`]).
    async fn compute_line_hash(&self) -> Option<u64> {
        let service = self.ai_service.lock().await;
        let lines = &service.game_status.lock().await.line_list;
        hash_of_real_lines(lines)
    }

    /// 按标题前缀找自动存档槽，没有则新建。
    async fn find_or_create_slot(&mut self) -> Result<i32, String> {
        let main_id = {
            let service = self.ai_service.lock().await;
            let gs = service.game_status.lock().await;
            gs.main_role_id
        };

        if let Ok(Some(existing)) =
            SaveRepo::find_save_by_title_prefix(&self.db, AUTO_SAVE_PREFIX).await
        {
            let save_id = existing.id;
            let new_title = format!(
                "{} {}",
                AUTO_SAVE_PREFIX,
                Local::now().format("%Y-%m-%d %H:%M:%S")
            );
            SaveRepo::update_save_title(&self.db, save_id, &new_title)
                .await
                .map_err(|e| format!("更新自动存档标题失败: {}", e))?;
            // 同步 main_role_id，防止切角色后指向旧角色
            SaveRepo::update_save_main_role(&self.db, save_id, main_id)
                .await
                .map_err(|e| format!("设置主角失败: {}", e))?;
            self.auto_save_id = Some(save_id);
            return Ok(save_id);
        }

        let title = format!(
            "{} {}",
            AUTO_SAVE_PREFIX,
            Local::now().format("%Y-%m-%d %H:%M:%S")
        );
        let model = SaveRepo::create_save(&self.db, &title)
            .await
            .map_err(|e| format!("创建自动存档失败: {}", e))?;
        let save_id = model.id;

        SaveRepo::update_save_main_role(&self.db, save_id, main_id)
            .await
            .map_err(|e| format!("设置主角失败: {}", e))?;

        self.auto_save_id = Some(save_id);
        Ok(save_id)
    }
}
