use async_speed_limit::Limiter;
use async_trait::async_trait;
use hbb_common::{
    allow_err, bail,
    bytes::{Bytes, BytesMut},
    futures_util::{sink::SinkExt, stream::StreamExt},
    log,
    protobuf::Message as _,
    rendezvous_proto::*,
    sleep,
    tcp::FramedStream,
    timeout,
    tokio::{
        self,
        io::{AsyncReadExt, AsyncWriteExt},
        net::{TcpListener, TcpStream},
        sync::{Mutex, RwLock},
        time::{interval, Duration},
    },
    ResultType,
};
use sodiumoxide::crypto::sign;
use std::{
    collections::{HashMap, HashSet},
    io::prelude::*,
    io::Error,
    net::{IpAddr, SocketAddr},
    sync::atomic::{AtomicUsize, Ordering},
};

type Usage = (usize, usize, usize, usize);

lazy_static::lazy_static! {
    static ref PEERS: Mutex<HashMap<String, Box<dyn StreamTrait>>> = Default::default();
    static ref USAGE: RwLock<HashMap<String, Usage>> = Default::default();
    static ref BLACKLIST: RwLock<HashSet<String>> = Default::default();
    static ref BLOCKLIST: RwLock<HashSet<String>> = Default::default();
    // 单台被控设备（按 rf.id）当前的并发中继数。配对建立时 +1，中继结束时 -1。
    // 用于限制单设备被中继占用的并发连接，防恶意连接耗尽中继资源。
    static ref RELAY_CONN_PER_ID: std::sync::Mutex<HashMap<String, usize>> =
        std::sync::Mutex::new(HashMap::new());
}

/// 单台被控设备允许的最大并发中继数。
///
/// 默认 20；环境变量 `MAX_RELAY_CONNECTIONS_PER_ID` 可覆盖；设为 0 表示不限制。
/// 这是服务端硬限制（客户端还有一层软上限 MAX_REMOTE_SESSIONS）。
fn max_relay_connections_per_id() -> usize {
    crate::common::get_arg_or("MAX_RELAY_CONNECTIONS_PER_ID", "20".to_owned())
        .trim()
        .parse::<usize>()
        .unwrap_or(20)
}

/// 名单的存储后端：`None` 表示没配数据库，退回"文件 + 内存"的老行为。
///
/// 用 `Option<Database>` 而不是 `Database`：中继服务器本来就不需要数据库，
/// 强制它连通只会在单机部署上凭空制造一个故障点。只有运维显式配了
/// DB_TYPE/DB_URL/PG_* 时才启用落库。
static DB: once_cell::sync::OnceCell<Option<crate::database::Database>> =
    once_cell::sync::OnceCell::new();

static DOWNGRADE_THRESHOLD_100: AtomicUsize = AtomicUsize::new(66); // 0.66
static DOWNGRADE_START_CHECK: AtomicUsize = AtomicUsize::new(1_800_000); // in ms
static LIMIT_SPEED: AtomicUsize = AtomicUsize::new(32 * 1024 * 1024); // in bit/s
static TOTAL_BANDWIDTH: AtomicUsize = AtomicUsize::new(1024 * 1024 * 1024); // in bit/s
static SINGLE_BANDWIDTH: AtomicUsize = AtomicUsize::new(128 * 1024 * 1024); // in bit/s
const BLACKLIST_FILE: &str = "blacklist.txt";
const BLOCKLIST_FILE: &str = "blocklist.txt";

#[tokio::main(flavor = "multi_thread")]
pub async fn start_with_bind(
    bind_addr: Option<IpAddr>,
    port: &str,
    key: &str,
) -> ResultType<()> {
    let key = get_server_sk(key)?;
    init_access_lists().await;
    let port: u16 = port.parse()?;
    log::info!("Listening on tcp :{}", port);
    let port2 = port + 2;
    log::info!("Listening on websocket :{}", port2);
    // 健康检查 HTTP 服务：默认主端口+4（5188 → 5192），--health-port/HEALTH_PORT
    // 可覆盖，设 0 关闭。hbbr 不碰数据库，探针只报自身存活。
    if let Some(hp) = crate::health::resolve_port("health-port", port + 4) {
        crate::health::start(hp, "hbbr", None);
    }
    let main_task = async move {
        loop {
            log::info!("Start");
            io_loop(
                crate::common::listen_tcp(bind_addr, port).await?,
                crate::common::listen_tcp(bind_addr, port2).await?,
                crate::common::listen_console(bind_addr, port).await?,
                &key,
            )
            .await;
        }
    };
    // 中继节点自动注册：客户端不再配置中继地址，节点清单与调度由 hbbs 维护。
    // 注册失败只影响"本节点被调度到"的概率，不影响正在服务的连接，
    // 所以它是独立的后台任务，与转发主循环互不阻塞。
    tokio::spawn(register_loop(port));
    let listen_signal = crate::common::listen_signal();
    tokio::select!(
        res = main_task => res,
        res = listen_signal => res,
    )
}

// =============================================================================
// 中继节点自动注册（上报给 hbbs）
// =============================================================================
//
// 设计要点：客户端不再维护中继服务器清单（配置项已关闭），改由每台 hbbr
// 主动向 hbbs 上报。hbbs 把节点写进 relay_nodes 表，P2P 打洞失败时按
// 权重 / 负载 / 时延打分下发（rendezvous_server.rs::get_relay_server）。
// 超过 3 个周期没上报的节点会被 hbbs 自动剔除，所以"下线"不需要人工操作。
//
// 地址口径：hbbr 用 RELAY_PUBLIC_ADDR（运维指定的客户端可达地址）自报；
// 没配时 hbbs 只能看到 TCP 来源 IP —— Docker 网络下那是网桥地址，
// 客户端连不到，所以容器部署必须显式设置。

/// hbbs 地址。环境变量 HBBS_HOST，默认 127.0.0.1:5187（compose 里用服务名 hbbs:5187）。
fn hbbs_addr() -> String {
    crate::common::get_arg_or("HBBS_HOST", "127.0.0.1:5187".to_owned())
}

/// 周期上报的间隔（秒）。与 hbbs 的 RELAY_REGISTER_INTERVAL 保持一致即可，
/// 真正的节奏以 hbbs 应答里的 interval_sec 为准，这里只做失败重试的兜底。
const REGISTER_INTERVAL_SEC: u64 = 60;

/// 注册主循环：先立即注册一次，之后按间隔心跳；失败 10 秒后快速重试。
async fn register_loop(port: u16) {
    let hbbs = hbbs_addr();
    let public = crate::common::get_arg("RELAY_PUBLIC_ADDR");
    let region = crate::common::get_arg_or("RELAY_REGION", String::new());
    let capacity: i32 = crate::common::get_arg_or("RELAY_CAPACITY", "0".to_owned())
        .parse()
        .unwrap_or(0);
    if public.is_empty() {
        log::warn!(
            "未配置 RELAY_PUBLIC_ADDR：hbbs 将按连接来源 IP 登记本节点，\
             Docker / 云主机场景下客户端可能连不到中继。\
             请设置 RELAY_PUBLIC_ADDR=<客户端可达的域名或 IP>"
        );
    }
    log::info!(
        "中继节点自动注册已启动：hbbs={} port={} public={} region={}",
        hbbs,
        port,
        if public.is_empty() {
            "(来源 IP)"
        } else {
            public.as_str()
        },
        if region.is_empty() { "-" } else { region.as_str() }
    );
    loop {
        let ok = register_once(&hbbs, port, &public, &region, capacity).await;
        // 失败快速重试（10s），成功按一个完整间隔 —— 注册是轻量心跳，
        // 没必要等满 60s 才发现 hbbs 挂了。
        sleep(if ok { REGISTER_INTERVAL_SEC as f32 } else { 10.0 }).await;
    }
}

/// 单次注册：连接 hbbs → 上报节点信息 → 等应答。返回是否登记成功。
async fn register_once(
    hbbs: &str,
    port: u16,
    public: &str,
    region: &str,
    capacity: i32,
) -> bool {
    let mut conn = match FramedStream::new(hbbs, None, 10_000).await {
        Ok(c) => c,
        Err(e) => {
            log::warn!("连接 hbbs({hbbs}) 失败，中继节点注册推迟到下个周期：{e}");
            return false;
        }
    };
    let connections = USAGE.read().await.len() as i32;
    let mut msg = RendezvousMessage::new();
    msg.set_relay_register(RelayRegister {
        host: public.to_owned(),
        port: port as i32,
        region: region.to_owned(),
        capacity,
        connections,
        version: crate::version::VERSION.to_owned(),
        ..Default::default()
    });
    if let Err(e) = conn.send(&msg).await {
        log::warn!("向 hbbs({hbbs}) 发送注册消息失败：{e}");
        return false;
    }
    match conn.next_timeout(10_000).await {
        Some(Ok(bytes)) => match RendezvousMessage::parse_from_bytes(&bytes) {
            Ok(resp) => {
                if let Some(rendezvous_message::Union::RelayRegisterResponse(r)) = resp.union {
                    if r.ok {
                        log::info!(
                            "中继节点已注册：{}（hbbs 认定的对外地址，建议间隔 {}s）",
                            r.relay_server,
                            r.interval_sec
                        );
                        return true;
                    }
                }
                log::warn!("hbbs 返回了无法识别的注册应答，下个周期重试");
                false
            }
            Err(e) => {
                log::warn!("解析 hbbs 注册应答失败：{e}");
                false
            }
        },
        _ => {
            log::warn!("等待 hbbs({hbbs}) 注册应答超时");
            false
        }
    }
}

// =============================================================================
// 名单（黑名单 / 阻断名单）的持久化
// =============================================================================
//
// 上游的行为有个很实际的缺陷：两份名单只在**启动时**从 blacklist.txt /
// blocklist.txt 读一次，运行期 `blacklist-add` 的改动只落在内存里，
// 容器重启即全部丢失。而封禁 IP 恰恰是最需要跨重启保留的东西 ——
// 否则"重启"就等于一次全局解封，被封的地址只要等到重启就能重来。
//
// 现在的规则：
//   1. 内存集合仍然是运行期真值（热路径每秒查一次，不能打库）；
//   2. 配了数据库 → 落库，并在启动时把老文件里的条目一次性迁进去；
//      另起一个后台任务周期重载，让多台 hbbr 共享同一份名单；
//   3. 没配数据库 → 退回文件模式，但**补上写回**（上游没有），
//      至少单机部署不会丢。
//
// 写库失败时**保留内存改动**并告警，而不是回滚：对一台正在扛流量的中继来说，
// "封禁生效但重启会丢"远好过"拒绝生效"。
// =============================================================================
use crate::database::{ACCESS_KIND_BLACKLIST, ACCESS_KIND_BLOCKLIST};

const ACCESS_KINDS: [&str; 2] = [ACCESS_KIND_BLACKLIST, ACCESS_KIND_BLOCKLIST];

/// 名单种类 → 内存集合
fn set_of(kind: &str) -> &'static RwLock<HashSet<String>> {
    if kind == ACCESS_KIND_BLACKLIST {
        &BLACKLIST
    } else {
        &BLOCKLIST
    }
}

/// 名单种类 → 无数据库时使用的落盘文件
fn file_of(kind: &str) -> &'static str {
    if kind == ACCESS_KIND_BLACKLIST {
        BLACKLIST_FILE
    } else {
        BLOCKLIST_FILE
    }
}

fn db_handle() -> Option<&'static crate::database::Database> {
    DB.get().and_then(|o| o.as_ref())
}

/// 从文件读入（无数据库模式，或数据库模式下的老部署迁移源）。
async fn load_access_file(kind: &str) {
    if let Ok(mut file) = std::fs::File::open(file_of(kind)) {
        let mut contents = String::new();
        if file.read_to_string(&mut contents).is_ok() {
            for x in contents.split('\n') {
                if let Some(ip) = x.trim().split(' ').next() {
                    if !ip.is_empty() {
                        set_of(kind).write().await.insert(ip.to_owned());
                    }
                }
            }
        }
    }
}

/// 无数据库模式下的写回：整份重写，保证文件与内存一致（含删除）。
fn write_access_file(kind: &str, ips: &HashSet<String>) {
    let mut list: Vec<&String> = ips.iter().collect();
    list.sort();
    let contents: String = list.iter().map(|ip| format!("{ip}\n")).collect();
    if let Err(e) = std::fs::write(file_of(kind), contents) {
        log::error!("写 {} 失败，名单只存在于内存：{e}", file_of(kind));
    }
}

/// 尝试连接数据库。没配任何 DB_* 时返回 None（这是正常路径，不是错误）。
async fn maybe_connect_db() -> Option<crate::database::Database> {
    let keys = [
        "DB_TYPE",
        "DB_URL",
        "PG_HOST",
        "PG_USER",
        "PG_PASSWORD",
        "PG_DB",
    ];
    let configured: Vec<&str> = keys
        .iter()
        .filter(|k| !crate::common::get_arg(k).is_empty())
        .cloned()
        .collect();
    if configured.is_empty() {
        log::info!(
            "未配置数据库（DB_TYPE/DB_URL/PG_*），名单退回文件模式：{} / {}",
            BLACKLIST_FILE,
            BLOCKLIST_FILE
        );
        return None;
    }
    let url = crate::database::resolve_db_url();
    match crate::database::Database::new(&url).await {
        Ok(db) => {
            log::info!("名单持久化到数据库（后端 = {}）", db.backend_name());
            Some(db)
        }
        Err(e) => {
            // 不退出：中继的可用性优先于名单的持久化。
            // 数据库挂了的时候让中继也跟着起不来，等于把一个次要功能
            // 变成整条链路的单点故障。
            log::error!(
                "连接数据库失败，名单退回文件模式（运行期改动仍生效，但重启后丢失）：{e}"
            );
            None
        }
    }
}

/// 用数据库内容刷新内存集合。读取失败时保留现有内容，绝不清空 ——
/// 一次查询抖动就把全网封禁解掉，后果比"晚一点看到新增条目"严重得多。
async fn reload_access_lists() {
    let db = match db_handle() {
        Some(db) => db,
        None => return,
    };
    for kind in ACCESS_KINDS {
        match db.load_access_list(kind).await {
            Ok(entries) => {
                let set: HashSet<String> = entries.into_iter().map(|e| e.ip).collect();
                *set_of(kind).write().await = set;
            }
            Err(e) => log::warn!("读取名单 {kind} 失败，保留现有内容：{e}"),
        }
    }
}

/// 老部署迁移：库里为空、文件里有内容时，把文件内容灌进库（只做一次）。
async fn import_legacy_files() {
    let db = match db_handle() {
        Some(db) => db,
        None => return,
    };
    for kind in ACCESS_KINDS {
        let existing = match db.load_access_list(kind).await {
            Ok(v) => v,
            Err(e) => {
                log::warn!("迁移前读取名单 {kind} 失败，跳过迁移：{e}");
                continue;
            }
        };
        if !existing.is_empty() {
            continue;
        }
        let ips: Vec<String> = set_of(kind).read().await.iter().cloned().collect();
        if ips.is_empty() {
            continue;
        }
        let mut ok = 0;
        for ip in &ips {
            match db.add_access_entry(kind, ip, "migrated from file").await {
                Ok(()) => ok += 1,
                Err(e) => log::warn!("迁移 {kind} 条目 {ip} 失败：{e}"),
            }
        }
        if ok > 0 {
            log::info!("已从 {} 迁 {ok} 条 {kind} 进数据库", file_of(kind));
        }
    }
}

/// 启动入口：先读文件（兼容老部署），再接库、重载、迁移，最后起后台刷新。
async fn init_access_lists() {
    for kind in ACCESS_KINDS {
        load_access_file(kind).await;
    }
    let _ = DB.set(maybe_connect_db().await);
    if db_handle().is_some() {
        reload_access_lists().await;
        import_legacy_files().await;
        let secs: u64 = crate::common::get_arg_or("ACCESS_RELOAD_SECS", "60".to_owned())
            .parse()
            .unwrap_or(60);
        if secs > 0 {
            log::info!("名单每 {secs}s 从数据库重载一次（多实例共享同一份名单）");
            tokio::spawn(async move {
                loop {
                    tokio::time::sleep(Duration::from_secs(secs)).await;
                    reload_access_lists().await;
                }
            });
        }
    }
    for kind in ACCESS_KINDS {
        log::info!("#{kind}: {}", set_of(kind).read().await.len());
    }
}

/// 新增一条。返回一段人类可读的告警（没有错误返回 None）。
async fn access_add(kind: &str, ip: &str, note: &str) -> Option<String> {
    set_of(kind).write().await.insert(ip.to_owned());
    match db_handle() {
        Some(db) => match db.add_access_entry(kind, ip, note).await {
            Ok(()) => None,
            Err(e) => Some(format!("（内存中已生效，但写库失败：{e}）")),
        },
        None => {
            let ips = set_of(kind).read().await.clone();
            write_access_file(kind, &ips);
            None
        }
    }
}

/// 删除一条（`ip == "all"` 表示清空）。
async fn access_remove(kind: &str, ip: &str) -> Option<String> {
    if ip == "all" {
        set_of(kind).write().await.clear();
        return match db_handle() {
            Some(db) => match db.clear_access_list(kind).await {
                Ok(()) => None,
                Err(e) => Some(format!("（内存中已清空，但清库失败：{e}）")),
            },
            None => {
                write_access_file(kind, &HashSet::new());
                None
            }
        };
    }
    set_of(kind).write().await.remove(ip);
    match db_handle() {
        Some(db) => match db.remove_access_entry(kind, ip).await {
            Ok(()) => None,
            Err(e) => Some(format!("（内存中已移除，但删库失败：{e}）")),
        },
        None => {
            let ips = set_of(kind).read().await.clone();
            write_access_file(kind, &ips);
            None
        }
    }
}

/// 列出某个名单；`ip` 非空时改为判断是否存在。
async fn access_list(kind: &str, ip: Option<&str>) -> String {
    if let Some(ip) = ip {
        return format!("{}\n", set_of(kind).read().await.contains(ip));
    }
    match db_handle() {
        Some(db) => match db.load_access_list(kind).await {
            Ok(entries) => entries
                .into_iter()
                .map(|e| {
                    if e.note.is_empty() {
                        format!("{}\t{}", e.ip, e.created_at)
                    } else {
                        format!("{}\t{}\t{}", e.ip, e.note, e.created_at)
                    }
                })
                .collect::<Vec<_>>()
                .join("\n")
                + "\n",
            Err(e) => format!("读取数据库失败：{e}\n"),
        },
        None => {
            let mut list: Vec<String> = set_of(kind).read().await.iter().cloned().collect();
            list.sort();
            list.join("\n") + "\n"
        }
    }
}

fn check_params() {
    let tmp = crate::common::get_arg("DOWNGRADE_THRESHOLD")
        .parse::<f64>()
        .unwrap_or(0.);
    if tmp > 0. {
        DOWNGRADE_THRESHOLD_100.store((tmp * 100.) as _, Ordering::SeqCst);
    }
    log::info!(
        "DOWNGRADE_THRESHOLD: {}",
        DOWNGRADE_THRESHOLD_100.load(Ordering::SeqCst) as f64 / 100.
    );
    let tmp = crate::common::get_arg("DOWNGRADE_START_CHECK")
        .parse::<usize>()
        .unwrap_or(0);
    if tmp > 0 {
        DOWNGRADE_START_CHECK.store(tmp * 1000, Ordering::SeqCst);
    }
    log::info!(
        "DOWNGRADE_START_CHECK: {}s",
        DOWNGRADE_START_CHECK.load(Ordering::SeqCst) / 1000
    );
    let tmp = crate::common::get_arg("LIMIT_SPEED")
        .parse::<f64>()
        .unwrap_or(0.);
    if tmp > 0. {
        LIMIT_SPEED.store((tmp * 1024. * 1024.) as usize, Ordering::SeqCst);
    }
    log::info!(
        "LIMIT_SPEED: {}Mb/s",
        LIMIT_SPEED.load(Ordering::SeqCst) as f64 / 1024. / 1024.
    );
    let tmp = crate::common::get_arg("TOTAL_BANDWIDTH")
        .parse::<f64>()
        .unwrap_or(0.);
    if tmp > 0. {
        TOTAL_BANDWIDTH.store((tmp * 1024. * 1024.) as usize, Ordering::SeqCst);
    }

    log::info!(
        "TOTAL_BANDWIDTH: {}Mb/s",
        TOTAL_BANDWIDTH.load(Ordering::SeqCst) as f64 / 1024. / 1024.
    );
    let tmp = crate::common::get_arg("SINGLE_BANDWIDTH")
        .parse::<f64>()
        .unwrap_or(0.);
    if tmp > 0. {
        SINGLE_BANDWIDTH.store((tmp * 1024. * 1024.) as usize, Ordering::SeqCst);
    }
    log::info!(
        "SINGLE_BANDWIDTH: {}Mb/s",
        SINGLE_BANDWIDTH.load(Ordering::SeqCst) as f64 / 1024. / 1024.
    )
}

async fn check_cmd(cmd: &str, limiter: Limiter) -> String {
    use std::fmt::Write;

    let mut res = "".to_owned();
    let mut fds = cmd.trim().split(' ');
    match fds.next() {
        Some("h") => {
            res = format!(
                "{}\n{}\n{}\n{}\n{}\n{}\n{}\n{}\n{}\n{}\n{}\n{}\n",
                "blacklist-add(ba) <ip>",
                "blacklist-remove(br) <ip>",
                "blacklist(b) <ip>",
                "blocklist-add(Ba) <ip>",
                "blocklist-remove(Br) <ip>",
                "blocklist(B) <ip>",
                "downgrade-threshold(dt) [value]",
                "downgrade-start-check(t) [value(second)]",
                "limit-speed(ls) [value(Mb/s)]",
                "total-bandwidth(tb) [value(Mb/s)]",
                "single-bandwidth(sb) [value(Mb/s)]",
                "usage(u)"
            )
        }
        // 名单三件套：add / remove / list。
        // 与上游的两点差异：① 改动会落库（没配库就落文件），不再重启即丢；
        // ② add 可以在 IP 后面带备注，list 会把备注与加入时间一起打出来 ——
        //    十条 IP 里哪条是上周封的、为什么封的，光看 IP 是记不起来的。
        Some("blacklist-add" | "ba") => {
            if let Some(ip) = fds.next() {
                let note: String = fds.collect::<Vec<_>>().join(" ");
                for ip in ip.split('|') {
                    if let Some(warn) = access_add(ACCESS_KIND_BLACKLIST, ip, &note).await {
                        let _ = writeln!(res, "{}", warn);
                    }
                }
            }
        }
        Some("blacklist-remove" | "br") => {
            if let Some(ip) = fds.next() {
                if ip == "all" {
                    if let Some(warn) = access_remove(ACCESS_KIND_BLACKLIST, "all").await {
                        let _ = writeln!(res, "{}", warn);
                    }
                } else {
                    for ip in ip.split('|') {
                        if let Some(warn) = access_remove(ACCESS_KIND_BLACKLIST, ip).await {
                            let _ = writeln!(res, "{}", warn);
                        }
                    }
                }
            }
        }
        Some("blacklist" | "b") => {
            let ip = fds.next();
            res = access_list(ACCESS_KIND_BLACKLIST, ip).await;
        }
        Some("blocklist-add" | "Ba") => {
            if let Some(ip) = fds.next() {
                let note: String = fds.collect::<Vec<_>>().join(" ");
                for ip in ip.split('|') {
                    if let Some(warn) = access_add(ACCESS_KIND_BLOCKLIST, ip, &note).await {
                        let _ = writeln!(res, "{}", warn);
                    }
                }
            }
        }
        Some("blocklist-remove" | "Br") => {
            if let Some(ip) = fds.next() {
                if ip == "all" {
                    if let Some(warn) = access_remove(ACCESS_KIND_BLOCKLIST, "all").await {
                        let _ = writeln!(res, "{}", warn);
                    }
                } else {
                    for ip in ip.split('|') {
                        if let Some(warn) = access_remove(ACCESS_KIND_BLOCKLIST, ip).await {
                            let _ = writeln!(res, "{}", warn);
                        }
                    }
                }
            }
        }
        Some("blocklist" | "B") => {
            let ip = fds.next();
            res = access_list(ACCESS_KIND_BLOCKLIST, ip).await;
        }
        Some("downgrade-threshold" | "dt") => {
            if let Some(v) = fds.next() {
                if let Ok(v) = v.parse::<f64>() {
                    if v > 0. {
                        DOWNGRADE_THRESHOLD_100.store((v * 100.) as _, Ordering::SeqCst);
                    }
                }
            } else {
                res = format!(
                    "{}\n",
                    DOWNGRADE_THRESHOLD_100.load(Ordering::SeqCst) as f64 / 100.
                );
            }
        }
        Some("downgrade-start-check" | "t") => {
            if let Some(v) = fds.next() {
                if let Ok(v) = v.parse::<usize>() {
                    if v > 0 {
                        DOWNGRADE_START_CHECK.store(v * 1000, Ordering::SeqCst);
                    }
                }
            } else {
                res = format!("{}s\n", DOWNGRADE_START_CHECK.load(Ordering::SeqCst) / 1000);
            }
        }
        Some("limit-speed" | "ls") => {
            if let Some(v) = fds.next() {
                if let Ok(v) = v.parse::<f64>() {
                    if v > 0. {
                        LIMIT_SPEED.store((v * 1024. * 1024.) as _, Ordering::SeqCst);
                    }
                }
            } else {
                res = format!(
                    "{}Mb/s\n",
                    LIMIT_SPEED.load(Ordering::SeqCst) as f64 / 1024. / 1024.
                );
            }
        }
        Some("total-bandwidth" | "tb") => {
            if let Some(v) = fds.next() {
                if let Ok(v) = v.parse::<f64>() {
                    if v > 0. {
                        TOTAL_BANDWIDTH.store((v * 1024. * 1024.) as _, Ordering::SeqCst);
                        limiter.set_speed_limit(TOTAL_BANDWIDTH.load(Ordering::SeqCst) as _);
                    }
                }
            } else {
                res = format!(
                    "{}Mb/s\n",
                    TOTAL_BANDWIDTH.load(Ordering::SeqCst) as f64 / 1024. / 1024.
                );
            }
        }
        Some("single-bandwidth" | "sb") => {
            if let Some(v) = fds.next() {
                if let Ok(v) = v.parse::<f64>() {
                    if v > 0. {
                        SINGLE_BANDWIDTH.store((v * 1024. * 1024.) as _, Ordering::SeqCst);
                    }
                }
            } else {
                res = format!(
                    "{}Mb/s\n",
                    SINGLE_BANDWIDTH.load(Ordering::SeqCst) as f64 / 1024. / 1024.
                );
            }
        }
        Some("usage" | "u") => {
            let mut tmp: Vec<(String, Usage)> = USAGE
                .read()
                .await
                .iter()
                .map(|x| (x.0.clone(), *x.1))
                .collect();
            tmp.sort_by(|a, b| ((b.1).1).partial_cmp(&(a.1).1).unwrap());
            for (ip, (elapsed, total, highest, speed)) in tmp {
                if elapsed == 0 {
                    continue;
                }
                let _ = writeln!(
                    res,
                    "{}: {}s {:.2}MB {}kb/s {}kb/s {}kb/s",
                    ip,
                    elapsed / 1000,
                    total as f64 / 1024. / 1024. / 8.,
                    highest,
                    total / elapsed,
                    speed
                );
            }
        }
        _ => {}
    }
    res
}

async fn io_loop(
    listener: TcpListener,
    listener2: TcpListener,
    listener_console: Option<TcpListener>,
    key: &str,
) {
    check_params();
    let limiter = <Limiter>::new(TOTAL_BANDWIDTH.load(Ordering::SeqCst) as _);
    loop {
        tokio::select! {
            res = listener.accept() => {
                match res {
                    Ok((stream, addr))  => {
                        stream.set_nodelay(true).ok();
                        handle_connection(stream, addr, &limiter, key, false).await;
                    }
                    Err(err) => {
                       log::error!("listener.accept failed: {}", err);
                       break;
                    }
                }
            }
            res = listener2.accept() => {
                match res {
                    Ok((stream, addr))  => {
                        stream.set_nodelay(true).ok();
                        handle_connection(stream, addr, &limiter, key, true).await;
                    }
                    Err(err) => {
                       log::error!("listener2.accept failed: {}", err);
                       break;
                    }
                }
            }
            res = crate::common::accept_or_pending(listener_console.as_ref()) => {
                match res {
                    Ok((stream, addr))  => {
                        stream.set_nodelay(true).ok();
                        handle_connection(stream, addr, &limiter, key, false).await;
                    }
                    Err(err) => {
                       log::error!("console listener.accept failed: {}", err);
                       break;
                    }
                }
            }
        }
    }
}

async fn handle_connection(
    stream: TcpStream,
    addr: SocketAddr,
    limiter: &Limiter,
    key: &str,
    ws: bool,
) {
    let ip = hbb_common::try_into_v4(addr).ip();
    if !ws && ip.is_loopback() {
        let limiter = limiter.clone();
        tokio::spawn(async move {
            let mut stream = stream;
            let mut buffer = [0; 1024];
            if let Ok(Ok(n)) = timeout(1000, stream.read(&mut buffer[..])).await {
                if let Ok(data) = std::str::from_utf8(&buffer[..n]) {
                    let res = check_cmd(data, limiter).await;
                    stream.write(res.as_bytes()).await.ok();
                }
            }
        });
        return;
    }
    let ip = ip.to_string();
    if BLOCKLIST.read().await.get(&ip).is_some() {
        log::info!("{} blocked", ip);
        return;
    }
    let key = key.to_owned();
    let limiter = limiter.clone();
    tokio::spawn(async move {
        allow_err!(make_pair(stream, addr, &key, limiter, ws).await);
    });
}

async fn make_pair(
    stream: TcpStream,
    mut addr: SocketAddr,
    key: &str,
    limiter: Limiter,
    ws: bool,
) -> ResultType<()> {
    if ws {
        use tokio_tungstenite::tungstenite::handshake::server::{Request, Response};
        let callback = |req: &Request, response: Response| {
            let headers = req.headers();
            // X-Real-IP / X-Forwarded-For are trusted as-is so that the real
            // client IP is preserved when the WebSocket port runs behind a
            // reverse proxy (WSS). They are NOT validated: anyone who can reach
            // this port directly can spoof an arbitrary IP, bypassing IP-based
            // rate limiting / blocking and corrupting logged IPs. Do not expose
            // the WebSocket port directly to untrusted networks; only the
            // reverse proxy, which overwrites these headers, should be able to
            // connect to it.
            // https://github.com/rustdesk/rustdesk-server/issues/634
            let real_ip = headers
                .get("X-Real-IP")
                .or_else(|| headers.get("X-Forwarded-For"))
                .and_then(|header_value| header_value.to_str().ok());
            if let Some(ip) = real_ip {
                if ip.contains('.') {
                    addr = format!("{ip}:0").parse().unwrap_or(addr);
                } else {
                    addr = format!("[{ip}]:0").parse().unwrap_or(addr);
                }
            }
            Ok(response)
        };
        let ws_stream = tokio_tungstenite::accept_hdr_async(stream, callback).await?;
        make_pair_(ws_stream, addr, key, limiter).await;
    } else {
        make_pair_(FramedStream::from(stream, addr), addr, key, limiter).await;
    }
    Ok(())
}

async fn make_pair_(stream: impl StreamTrait, addr: SocketAddr, key: &str, limiter: Limiter) {
    let mut stream = stream;
    if let Ok(Some(Ok(bytes))) = timeout(30_000, stream.recv()).await {
        if let Ok(msg_in) = RendezvousMessage::parse_from_bytes(&bytes) {
            if let Some(rendezvous_message::Union::RequestRelay(rf)) = msg_in.union {
                if !key.is_empty() && rf.licence_key != key {
                    log::warn!("Relay authentication failed from {} - invalid key", addr);
                    return;
                }
                if !rf.uuid.is_empty() {
                    let mut peer = PEERS.lock().await.remove(&rf.uuid);
                    if let Some(peer) = peer.as_mut() {
                        log::info!("Relayrequest {} from {} got paired", rf.uuid, addr);
                        // 硬限制：单台被控设备（rf.id）的并发中继数，防止恶意连接耗尽中继资源。
                        // 默认上限 20，环境变量 MAX_RELAY_CONNECTIONS_PER_ID 可覆盖，0 表示不限制。
                        let id_key = if rf.id.is_empty() {
                            rf.uuid.clone()
                        } else {
                            rf.id.clone()
                        };
                        let per_id_limit = max_relay_connections_per_id();
                        if per_id_limit != 0 {
                            let mut counts = RELAY_CONN_PER_ID.lock().unwrap();
                            let n = counts.entry(id_key.clone()).or_insert(0);
                            if *n >= per_id_limit {
                                log::warn!(
                                    "拒绝中继配对 {}：被控设备 {} 并发中继已达上限 {}",
                                    rf.uuid, id_key, per_id_limit
                                );
                                return;
                            }
                            *n += 1;
                        }
                        let id = format!("{}:{}", addr.ip(), addr.port());
                        USAGE.write().await.insert(id.clone(), Default::default());
                        if !stream.is_ws() && !peer.is_ws() {
                            peer.set_raw();
                            stream.set_raw();
                            log::info!("Both are raw");
                        }
                        if let Err(err) = relay(addr, &mut stream, peer, limiter, id.clone()).await
                        {
                            log::info!("Relay of {} closed: {}", addr, err);
                        } else {
                            log::info!("Relay of {} closed", addr);
                        }
                        if per_id_limit != 0 {
                            let mut counts = RELAY_CONN_PER_ID.lock().unwrap();
                            if let Some(n) = counts.get_mut(&id_key) {
                                *n -= 1;
                                if *n == 0 {
                                    counts.remove(&id_key);
                                }
                            }
                        }
                        USAGE.write().await.remove(&id);
                    } else {
                        log::info!("New relay request {} from {}", rf.uuid, addr);
                        PEERS.lock().await.insert(rf.uuid.clone(), Box::new(stream));
                        sleep(30.).await;
                        PEERS.lock().await.remove(&rf.uuid);
                    }
                }
            }
        }
    }
}

async fn relay(
    addr: SocketAddr,
    stream: &mut impl StreamTrait,
    peer: &mut Box<dyn StreamTrait>,
    total_limiter: Limiter,
    id: String,
) -> ResultType<()> {
    let ip = addr.ip().to_string();
    let mut tm = std::time::Instant::now();
    let mut elapsed = 0;
    let mut total = 0;
    let mut total_s = 0;
    let mut highest_s = 0;
    let mut downgrade: bool = false;
    let mut blacked: bool = false;
    let sb = SINGLE_BANDWIDTH.load(Ordering::SeqCst) as f64;
    let limiter = <Limiter>::new(sb);
    let blacklist_limiter = <Limiter>::new(LIMIT_SPEED.load(Ordering::SeqCst) as _);
    let downgrade_threshold =
        (sb * DOWNGRADE_THRESHOLD_100.load(Ordering::SeqCst) as f64 / 100. / 1000.) as usize; // in bit/ms
    let mut timer = interval(Duration::from_secs(3));
    let mut last_recv_time = std::time::Instant::now();
    loop {
        tokio::select! {
            res = peer.recv() => {
                if let Some(Ok(bytes)) = res {
                    last_recv_time = std::time::Instant::now();
                    let nb = bytes.len() * 8;
                    if blacked || downgrade {
                        blacklist_limiter.consume(nb).await;
                    } else {
                        limiter.consume(nb).await;
                    }
                    total_limiter.consume(nb).await;
                    total += nb;
                    total_s += nb;
                    if !bytes.is_empty() {
                        stream.send_raw(bytes.into()).await?;
                    }
                } else {
                    break;
                }
            },
            res = stream.recv() => {
                if let Some(Ok(bytes)) = res {
                    last_recv_time = std::time::Instant::now();
                    let nb = bytes.len() * 8;
                    if blacked || downgrade {
                        blacklist_limiter.consume(nb).await;
                    } else {
                        limiter.consume(nb).await;
                    }
                    total_limiter.consume(nb).await;
                    total += nb;
                    total_s += nb;
                    if !bytes.is_empty() {
                        peer.send_raw(bytes.into()).await?;
                    }
                } else {
                    break;
                }
            },
            _ = timer.tick() => {
                if last_recv_time.elapsed().as_secs() > 30 {
                    bail!("Timeout");
                }
            }
        }

        let n = tm.elapsed().as_millis() as usize;
        if n >= 1_000 {
            if BLOCKLIST.read().await.get(&ip).is_some() {
                log::info!("{} blocked", ip);
                break;
            }
            blacked = BLACKLIST.read().await.get(&ip).is_some();
            tm = std::time::Instant::now();
            let speed = total_s / n;
            if speed > highest_s {
                highest_s = speed;
            }
            elapsed += n;
            USAGE.write().await.insert(
                id.clone(),
                (elapsed as _, total as _, highest_s as _, speed as _),
            );
            total_s = 0;
            if elapsed > DOWNGRADE_START_CHECK.load(Ordering::SeqCst)
                && !downgrade
                && total > elapsed * downgrade_threshold
            {
                downgrade = true;
                log::info!(
                    "Downgrade {}, exceed downgrade threshold {}bit/ms in {}ms",
                    id,
                    downgrade_threshold,
                    elapsed
                );
            }
        }
    }
    Ok(())
}

fn get_server_sk(key: &str) -> ResultType<String> {
    let mut key = key.to_owned();
    if let Ok(sk) = base64::decode(&key) {
        if sk.len() == sign::SECRETKEYBYTES {
            log::info!("The key is a crypto private key");
            key = base64::encode(&sk[(sign::SECRETKEYBYTES / 2)..]);
        }
    }

    if key == "-" || key == "_" {
        let (pk, _) = crate::common::gen_sk(300)?;
        key = pk;
    }

    if !key.is_empty() {
        log::info!("Key: {}", key);
    }

    Ok(key)
}

#[async_trait]
trait StreamTrait: Send + Sync + 'static {
    async fn recv(&mut self) -> Option<Result<BytesMut, Error>>;
    async fn send_raw(&mut self, bytes: Bytes) -> ResultType<()>;
    fn is_ws(&self) -> bool;
    fn set_raw(&mut self);
}

#[async_trait]
impl StreamTrait for FramedStream {
    async fn recv(&mut self) -> Option<Result<BytesMut, Error>> {
        self.next().await
    }

    async fn send_raw(&mut self, bytes: Bytes) -> ResultType<()> {
        self.send_bytes(bytes).await
    }

    fn is_ws(&self) -> bool {
        false
    }

    fn set_raw(&mut self) {
        self.set_raw();
    }
}

#[async_trait]
impl StreamTrait for tokio_tungstenite::WebSocketStream<TcpStream> {
    async fn recv(&mut self) -> Option<Result<BytesMut, Error>> {
        if let Some(msg) = self.next().await {
            match msg {
                Ok(msg) => {
                    match msg {
                        tungstenite::Message::Binary(bytes) => {
                            Some(Ok(bytes[..].into())) // to-do: poor performance
                        }
                        _ => Some(Ok(BytesMut::new())),
                    }
                }
                Err(err) => Some(Err(Error::new(std::io::ErrorKind::Other, err.to_string()))),
            }
        } else {
            None
        }
    }

    async fn send_raw(&mut self, bytes: Bytes) -> ResultType<()> {
        Ok(self
            .send(tungstenite::Message::Binary(bytes.to_vec()))
            .await?) // to-do: poor performance
    }

    fn is_ws(&self) -> bool {
        true
    }

    fn set_raw(&mut self) {}
}
