// =============================================================================
// 连接审计：把"谁在什么时候连了谁、走中继还是 P2P"落进数据库
// =============================================================================
//
// 为什么需要它
// ------------
// 上游 hbbs **完全不落任何连接记录** —— 谁连了谁、连了多久、走中继还是 P2P，
// 服务端一概不写库，只能从容器日志里翻到零星几行。想要合规审计，文档里
// 一直的结论是"自己在前面加反向代理或流量镜像"。这里补成原生能力。
//
// 为什么不同步写
// --------------
// 打洞请求发生在连接建立的**热路径**上，一次同步 INSERT 会把网络往返与
// 事务开销直接加进连接时延 —— 为了审计拖慢每个用户的连接不划算。
// 所以做法是「内存环形缓冲 + 后台任务批量刷盘」：
//
//     热路径 record() ──push──▶ VecDeque（有上限）
//                                   │
//     后台任务每 N 秒 ──drain───────┘──▶ 一个事务批量 INSERT
//
// 三个刻意的取舍
// --------------
// 1. `record()` 用 `try_lock`：拿不到锁（后台任务正在 drain）就丢弃这条记录。
//    审计是可观测性数据，不该反过来拖住主流程。
// 2. 落库失败时把事件放回缓冲等下一轮重试，但总量封顶（AUDIT_MAX_BUFFER），
//    避免数据库长时间不可用时内存无限增长。
// 3. 任何错误只记日志，绝不 panic —— 数据库挂掉应由 hbbs 自身的健康检查
//    去体现，而不是被审计模块放大成崩溃。
// =============================================================================

use crate::database::{self, AuditEvent, Database};
use hbb_common::{log, tokio, ResultType};
use once_cell::sync::{Lazy, OnceCell};
use std::{collections::VecDeque, sync::atomic::AtomicBool, sync::atomic::Ordering, time::Duration};

/// 待落库的事件缓冲
static BUFFER: Lazy<tokio::sync::Mutex<VecDeque<AuditEvent>>> =
    Lazy::new(|| tokio::sync::Mutex::new(VecDeque::new()));
/// 审计总开关（由 AUDIT_LOG 与后端类型共同决定）
static ENABLED: AtomicBool = AtomicBool::new(false);
/// 启动时注入的数据库连接（后台刷盘任务用）
static DB: OnceCell<Database> = OnceCell::new();
/// 缓冲上限，超出丢弃最旧事件
static MAX_BUFFER: Lazy<usize> =
    Lazy::new(|| get_env_usize("AUDIT_MAX_BUFFER", 20_000));
/// 刷盘间隔（秒）
static FLUSH_SECS: Lazy<u64> = Lazy::new(|| get_env_u64("AUDIT_FLUSH_SECS", 5));

/// 事件类型常量，避免各处硬编码字符串写歪。
pub mod ev {
    /// 客户端注册 / 更新公钥：ID 与设备绑定的核心事件
    pub const REGISTER_PK: &str = "register_pk";
    /// 打洞请求已转发给被控端：A 请求连 B
    pub const PUNCH_HOLE: &str = "punch_hole";
    /// 打洞请求被拒绝（对端离线 / ID 不存在 / license 不匹配等）
    pub const PUNCH_HOLE_FAILED: &str = "punch_hole_failed";
}

/// 启动审计后台刷盘任务。
///
/// 只有 AUDIT_LOG 未关闭时才真正拉起任务；未启用时 `record()` 直接返回，
/// 因此热路径零开销。
pub fn start(db: Database) {
    if !database::audit_enabled() {
        log::info!("连接审计已关闭（AUDIT_LOG=N）");
        return;
    }
    if DB.set(db.clone()).is_err() {
        log::warn!("audit::start 被重复调用，忽略");
        return;
    }
    ENABLED.store(true, Ordering::SeqCst);
    tokio::spawn(async move {
        // 第一次 tick 立即返回，之后按间隔循环
        let mut ticker = tokio::time::interval(Duration::from_secs(*FLUSH_SECS));
        loop {
            ticker.tick().await;
            if let Err(err) = flush_with(&db).await {
                log::error!("连接审计落库失败：{err}");
            }
        }
    });
    log::info!(
        "连接审计已启用（每 {} 秒批量落库，缓冲上限 {} 条）",
        *FLUSH_SECS,
        *MAX_BUFFER
    );
}

/// 审计是否启用。热路径先用它做一次廉价判断，再构造事件。
#[inline]
pub fn enabled() -> bool {
    ENABLED.load(Ordering::SeqCst)
}

/// 记一条审计事件。
///
/// 必须**非阻塞**：内部用 `try_lock`，拿不到锁就丢弃。审计掉几条记录
/// 远好过把连接请求卡住。
pub fn record(event: AuditEvent) {
    if !enabled() {
        return;
    }
    if let Ok(mut buf) = BUFFER.try_lock() {
        if buf.len() >= *MAX_BUFFER {
            buf.pop_front();
        }
        buf.push_back(event);
    }
}

/// 记一条审计事件的便捷入口（省去调用方自己填 ts）。
pub fn log(
    event: &str,
    from_id: Option<&str>,
    from_ip: Option<&str>,
    to_id: Option<&str>,
    to_ip: Option<&str>,
    via_relay: bool,
    detail: Option<String>,
) {
    if !enabled() {
        return;
    }
    record(AuditEvent {
        ts: chrono::Utc::now(),
        event: event.to_owned(),
        from_id: from_id.map(|s| s.to_owned()),
        from_ip: from_ip.map(|s| s.to_owned()),
        to_id: to_id.map(|s| s.to_owned()),
        to_ip: to_ip.map(|s| s.to_owned()),
        via_relay,
        detail,
    });
}

/// 用启动时注入的数据库连接刷盘一次，返回落库条数。
pub async fn flush() -> ResultType<usize> {
    match DB.get() {
        Some(db) => flush_with(db).await,
        None => Ok(0),
    }
}

/// 把缓冲里的事件一次性写进数据库；失败时放回缓冲（封顶）。
async fn flush_with(db: &Database) -> ResultType<usize> {
    // 整体取走后立刻释放锁，让 record() 在 INSERT 期间也能继续工作
    let events: Vec<AuditEvent> = {
        let mut buf = BUFFER.lock().await;
        buf.drain(..).collect()
    };
    if events.is_empty() {
        return Ok(0);
    }
    let n = events.len();
    match db.insert_audit(&events).await {
        Ok(()) => Ok(n),
        Err(err) => {
            let mut buf = BUFFER.lock().await;
            // 倒序 push_front 以还原原始顺序
            for e in events.into_iter().rev() {
                buf.push_front(e);
            }
            // 超出上限丢弃最旧的
            while buf.len() > *MAX_BUFFER {
                buf.pop_front();
            }
            Err(err)
        }
    }
}

fn get_env_usize(name: &str, default: usize) -> usize {
    crate::common::get_arg_or(name, default.to_string())
        .parse()
        .unwrap_or(default)
}

fn get_env_u64(name: &str, default: u64) -> u64 {
    crate::common::get_arg_or(name, default.to_string())
        .parse()
        .unwrap_or(default)
}
