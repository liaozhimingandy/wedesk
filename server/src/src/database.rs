// =============================================================================
// hbbs 持久化层：SQLite（默认） / PostgreSQL（可选）双后端
// =============================================================================
//
// 设计要点
// --------
// 1) 为什么抽象成 trait，而不是两套函数各写一遍？
//    上游原本把 SQL 直接写在 `Database` 的方法里，且大量使用 sqlx 的编译期宏
//    （`query!` / `query_as!`）。这些宏在**编译时**会连 `DATABASE_URL` 指向的真实库
//    校验 SQL 与表结构，宏展开结果里写死了一类数据库。
//    也就是说：同一个二进制里，`query!` 宏**不可能同时**服务于 SQLite 和 PostgreSQL。
//
//    所以这里把"存储能力"抽象成 `DbBackend` trait，两个实现各自持有自己的连接池：
//      - SqliteBackend：沿用原来的 `query!` 宏（编译期仍连 db_v2.sqlite3 校验）
//      - PgBackend    ：只用运行时 `query()` / `query_as()`，构建期不需要 Postgres
//
//    同理，**本文件里 2026-10 新增的两张表（server_config / connection_log）
//    一律不用 `query!` 宏**：宏会在编译期拿构建机上的模板库（`server/src/db_v2.sqlite3`，
//    里面只有 peer 表）去校验表名与列，新增表一旦写进宏就必然编译失败。
//    跨后端共享的 SQL 统一走运行时 `query()`。
//    `Database` 作为不变的外壳（facade），保持对外 API 与上游一致，
//    因此 `peer.rs` 无需任何改动。
//
// 2) 构建期 vs 运行期，两个变量必须分清（最容易踩的坑）
//      - 构建期：DATABASE_URL —— 被 sqlx 宏读取，必须仍是 sqlite 地址
//                （仓库根的 .env 里就是 `DATABASE_URL=sqlite://./db_v2.sqlite3`）
//      - 运行期：DB_URL        —— 由 `PeerMap::new()` 读取，可以是 SQLite 文件路径
//                                 也可以是 postgres:// 连接串
//                DB_TYPE      —— sqlite | postgres，决定用哪个后端
//    切勿把 DATABASE_URL 在运行期改成 postgres，那只会让下次编译的宏去连 Postgres
//    校验 SQLite 专用语法（`?` 占位符、`blob`、`without rowid`）而失败。
//
// 3) PostgreSQL 侧的方言差异（已在本文件处理）
//      - 占位符：`?` → `$1 $2 ...`
//      - 类型：blob→bytea，tinyint→smallint，datetime→timestamptz
//      - `user` 是 PostgreSQL 保留字，必须写成 "user"
//      - PREPARE 不支持多语句，建表/建索引必须逐条 execute（SQLite 可以一条搞定）
// =============================================================================
use async_trait::async_trait;
use hbb_common::{log, ResultType};
use sqlx::{
    postgres::{PgPool, PgPoolOptions},
    sqlite::SqliteConnectOptions,
    ConnectOptions, Connection, Error as SqlxError, Row, SqliteConnection,
};
use std::{ops::DerefMut, str::FromStr, sync::Arc};

// ---------------------------------------------------------------------------
// 对外数据结构：保持与上游完全一致，peer.rs 依赖这些字段
// ---------------------------------------------------------------------------
#[derive(Default, Clone)]
pub struct Peer {
    pub guid: Vec<u8>,
    pub id: String,
    pub uuid: Vec<u8>,
    pub pk: Vec<u8>,
    pub user: Option<Vec<u8>>,
    pub info: String,
    pub status: Option<i64>,
}

// ---------------------------------------------------------------------------
// server_config：服务端自身的键值状态
// ---------------------------------------------------------------------------
// 2026-10-20 起**不再托管密钥**：私钥/公钥改为只存文件（docker secrets
// 标准挂载，见 server_key.rs），库中不再写入任何密钥材料。
// 保留本表与常量只有两个用途：
//   1. 旧版本升级上来的部署，库里还残留着私钥 —— 启动时 server_key
//      会把它迁移回文件，然后调用 remove_config 把这三行删干净；
//   2. 未来若有需要落库的"非敏感"键值状态，可复用这张表。
/// 旧版私钥在 `server_config` 表里的键名（base64 后的 64 字节 ed25519 私钥）
pub const CFG_PRIVATE_KEY: &str = "server.private_key";
/// 旧版公钥在 `server_config` 表里的键名（迁移时一并删除）
pub const CFG_PUBLIC_KEY: &str = "server.public_key";
/// 旧版密钥来源标记（迁移时一并删除）
pub const CFG_KEY_ORIGIN: &str = "server.key_origin";

// ---------------------------------------------------------------------------
// connection_log：连接审计
//
// 上游 hbbs **完全不落任何连接记录**——谁连了谁、连了多久、走中继还是 P2P，
// 服务端一概不写库，只能从容器日志里翻到零星几行。要做合规审计就得自己在
// 前面加反向代理或流量镜像。这里补上落库，用开关控制（默认开），
// 高频场景下可关掉或改批处理。
// ---------------------------------------------------------------------------
#[derive(Clone, Debug)]
pub struct AuditEvent {
    /// 事件发生时刻（UTC）。落库时统一转成 UTC。
    pub ts: chrono::DateTime<chrono::Utc>,
    /// 事件类型：register_peer / register_pk / punch_hole / punch_hole_failed
    pub event: String,
    /// 发起方 ID（若可知）
    pub from_id: Option<String>,
    /// 发起方 IP
    pub from_ip: Option<String>,
    /// 被连接方 ID
    pub to_id: Option<String>,
    /// 被连接方 IP（若已知）
    pub to_ip: Option<String>,
    /// 本次连接是否被强制走中继
    pub via_relay: bool,
    /// 附加信息（如失败原因、NAT 类型）
    pub detail: Option<String>,
}

impl AuditEvent {
    /// 构造一个事件，ts 取当前时间。
    pub fn new(event: &str) -> Self {
        Self {
            ts: chrono::Utc::now(),
            event: event.to_owned(),
            from_id: None,
            from_ip: None,
            to_id: None,
            to_ip: None,
            via_relay: false,
            detail: None,
        }
    }
}

/// 审计落库开关：AUDIT_LOG=N 时完全不写（连缓冲都不进）。默认开。
pub fn audit_enabled() -> bool {
    !matches!(
        crate::common::get_arg_or("AUDIT_LOG", "Y".to_owned())
            .trim()
            .to_uppercase()
            .as_str(),
        "N" | "NO" | "0" | "FALSE"
    )
}

// ---------------------------------------------------------------------------
// 存储后端抽象
// ---------------------------------------------------------------------------
/// 中继节点登记表的一行（与 `relay_nodes` 表一一对应）。
///
/// `latency_ms` / `healthy` / `connections` 是运行期健康探测写入的实时值；
/// `region` / `weight` / `capacity` 是运维手动打标的静态调度权重。
#[derive(Debug, Clone)]
pub struct RelayNode {
    pub host: String,
    pub region: String,
    pub weight: i32,
    pub capacity: i32,
    pub latency_ms: Option<i32>,
    pub healthy: bool,
    pub connections: i32,
}

// ---------------------------------------------------------------------------
// relay_access_list：hbbr 的"名单"（黑名单 / 阻断名单）
//
// 上游把这两份名单放在**内存**里，只在启动时从 blacklist.txt / blocklist.txt
// 读一次，运行期 `blacklist-add` 的改动**从不写回**——容器一重启就全部丢失，
// 而 IP 封禁恰恰是最需要跨重启保留的东西（否则每次重启都是一次"封禁失效"，
// 恶意 IP 只要等到容器重启就能重来）。
//
// 现在只要配了数据库就落库（hbbr 首次接入存储），没配库时退回原来的
// 文件 + 内存行为，单容器部署不受影响。
// ---------------------------------------------------------------------------
/// 黑名单：命中后被限速（走 blacklist_limiter），不拒绝连接
pub const ACCESS_KIND_BLACKLIST: &str = "blacklist";
/// 阻断名单：命中后直接断开，最严格
pub const ACCESS_KIND_BLOCKLIST: &str = "blocklist";

/// 名单里的一行。`created_at` 只用于展示/排错（已按后端格式化成字符串）。
#[derive(Debug, Clone)]
pub struct AccessEntry {
    pub ip: String,
    pub note: String,
    pub created_at: String,
}

#[async_trait]
pub trait DbBackend: Send + Sync {
    /// 后端名字，仅用于启动日志
    fn name(&self) -> &'static str;
    async fn create_tables(&self) -> ResultType<()>;
    async fn get_peer(&self, id: &str) -> ResultType<Option<Peer>>;
    async fn insert_peer(
        &self,
        id: &str,
        uuid: &[u8],
        pk: &[u8],
        info: &str,
    ) -> ResultType<Vec<u8>>;
    async fn update_pk(
        &self,
        guid: &[u8],
        id: &str,
        pk: &[u8],
        info: &str,
    ) -> ResultType<()>;

    // --- server_config：键值对状态 ----------------------------------------
    /// 读一个配置项；不存在返回 Ok(None)。
    /// （密钥已退出数据库，只剩旧版部署迁移时读取一次。）
    async fn get_config(&self, key: &str) -> ResultType<Option<String>>;
    /// 删除一个配置项（不存在视为成功）。旧版密钥迁移后清理用。
    async fn remove_config(&self, key: &str) -> ResultType<()>;
    /// 轻量探活（select 1），供健康检查使用。
    async fn ping(&self) -> ResultType<()>;

    // --- connection_log：连接审计 ----------------------------------------
    /// 批量写入审计事件。
    async fn insert_audit(&self, events: &[AuditEvent]) -> ResultType<()>;

    // --- relay_nodes：中继节点登记与实时健康 -----------------------------
    /// 确保某个中继主机在表中存在（不存在则插入一行默认配置）。
    async fn ensure_relay_node(&self, host: &str) -> ResultType<()>;
    /// 更新静态调度权重（区域 / 权重 / 容量）。
    async fn set_relay_meta(
        &self,
        host: &str,
        region: &str,
        weight: i32,
        capacity: i32,
    ) -> ResultType<()>;
    /// 写入一次健康探测结果（RTT、存活、当前连接数）。
    async fn record_relay_health(
        &self,
        host: &str,
        latency_ms: Option<i32>,
        healthy: bool,
        connections: i32,
    ) -> ResultType<()>;
    /// 读出全部中继节点（含静态权重与最近一次健康）。
    async fn load_relay_nodes(&self) -> ResultType<Vec<RelayNode>>;

    // --- relay_access_list：hbbr 名单（黑名单 / 阻断名单） ----------------
    /// 读出某个名单的全部条目（按加入时间升序）。
    async fn load_access_list(&self, kind: &str) -> ResultType<Vec<AccessEntry>>;
    /// 加一条；已存在则只更新备注（幂等，重复 add 不会报错）。
    async fn add_access_entry(&self, kind: &str, ip: &str, note: &str) -> ResultType<()>;
    /// 删一条；不存在视为成功。
    async fn remove_access_entry(&self, kind: &str, ip: &str) -> ResultType<()>;
    /// 清空某个名单。
    async fn clear_access_list(&self, kind: &str) -> ResultType<()>;
}

// =============================================================================
// SQLite 后端（默认）
// =============================================================================
type SqlitePool = deadpool::managed::Pool<SqliteDbPool>;

pub struct SqliteDbPool {
    url: String,
}

#[async_trait]
impl deadpool::managed::Manager for SqliteDbPool {
    type Type = SqliteConnection;
    type Error = SqlxError;
    async fn create(&self) -> Result<SqliteConnection, SqlxError> {
        let mut opt = SqliteConnectOptions::from_str(&self.url).unwrap();
        opt.log_statements(log::LevelFilter::Debug);
        SqliteConnection::connect_with(&opt).await
    }
    async fn recycle(
        &self,
        obj: &mut SqliteConnection,
    ) -> deadpool::managed::RecycleResult<SqlxError> {
        Ok(obj.ping().await?)
    }
}

pub struct SqliteBackend {
    pool: SqlitePool,
}

impl SqliteBackend {
    async fn new(url: &str) -> ResultType<Self> {
        if !std::path::Path::new(url).exists() {
            std::fs::File::create(url).ok();
        }
        let n: usize = crate::common::get_arg_or("MAX_DATABASE_CONNECTIONS", "1".to_owned())
            .parse()
            .unwrap_or(1);
        log::debug!("MAX_DATABASE_CONNECTIONS={}", n);
        let pool = SqlitePool::new(
            SqliteDbPool {
                url: url.to_owned(),
            },
            n,
        );
        let _ = pool.get().await?; // test
        Ok(Self { pool })
    }
}

#[async_trait]
impl DbBackend for SqliteBackend {
    fn name(&self) -> &'static str {
        "sqlite"
    }

    async fn create_tables(&self) -> ResultType<()> {
        sqlx::query!(
            "
            create table if not exists peer (
                guid blob primary key not null,
                id varchar(100) not null,
                uuid blob not null,
                pk blob not null,
                created_at datetime not null default(current_timestamp),
                user blob,
                status tinyint,
                note varchar(300),
                info text not null
            ) without rowid;
            create unique index if not exists index_peer_id on peer (id);
            create index if not exists index_peer_user on peer (user);
            create index if not exists index_peer_created_at on peer (created_at);
            create index if not exists index_peer_status on peer (status);
        "
        )
        .execute(self.pool.get().await?.deref_mut())
        .await?;
        // 新增表走运行时 SQL（理由见文件头第 1 节）
        for sql in SQLITE_EXTRA_DDL {
            let mut conn = self.pool.get().await?;
            sqlx::query(sql).execute(&mut *conn).await?;
        }
        Ok(())
    }

    async fn get_peer(&self, id: &str) -> ResultType<Option<Peer>> {
        Ok(sqlx::query_as!(
            Peer,
            "select guid, id, uuid, pk, user, status, info from peer where id = ?",
            id
        )
        .fetch_optional(self.pool.get().await?.deref_mut())
        .await?)
    }

    async fn insert_peer(
        &self,
        id: &str,
        uuid: &[u8],
        pk: &[u8],
        info: &str,
    ) -> ResultType<Vec<u8>> {
        let guid = uuid::Uuid::new_v4().as_bytes().to_vec();
        sqlx::query!(
            "insert into peer(guid, id, uuid, pk, info) values(?, ?, ?, ?, ?)",
            guid,
            id,
            uuid,
            pk,
            info
        )
        .execute(self.pool.get().await?.deref_mut())
        .await?;
        Ok(guid)
    }

    async fn update_pk(&self, guid: &[u8], id: &str, pk: &[u8], info: &str) -> ResultType<()> {
        sqlx::query!(
            "update peer set id=?, pk=?, info=? where guid=?",
            id,
            pk,
            info,
            guid
        )
        .execute(self.pool.get().await?.deref_mut())
        .await?;
        Ok(())
    }

    async fn get_config(&self, key: &str) -> ResultType<Option<String>> {
        let mut conn = self.pool.get().await?;
        let row = sqlx::query("select value from server_config where key = ?")
            .bind(key)
            .fetch_optional(&mut *conn)
            .await?;
        Ok(row.map(|r| r.get::<String, _>("value")))
    }

    async fn remove_config(&self, key: &str) -> ResultType<()> {
        let mut conn = self.pool.get().await?;
        sqlx::query("delete from server_config where key = ?")
            .bind(key)
            .execute(&mut *conn)
            .await?;
        Ok(())
    }

    async fn ping(&self) -> ResultType<()> {
        let mut conn = self.pool.get().await?;
        sqlx::query("select 1").execute(&mut *conn).await?;
        Ok(())
    }

    async fn insert_audit(&self, events: &[AuditEvent]) -> ResultType<()> {
        let mut conn = self.pool.get().await?;
        for e in events {
            sqlx::query(
                "insert into connection_log(ts, event, from_id, from_ip, to_id, to_ip, via_relay, detail)
                 values(?, ?, ?, ?, ?, ?, ?, ?)",
            )
            .bind(e.ts)
            .bind(&e.event)
            .bind(&e.from_id)
            .bind(&e.from_ip)
            .bind(&e.to_id)
            .bind(&e.to_ip)
            .bind(e.via_relay as i32)
            .bind(&e.detail)
            .execute(&mut *conn)
            .await?;
        }
        Ok(())
    }

    async fn ensure_relay_node(&self, host: &str) -> ResultType<()> {
        let mut conn = self.pool.get().await?;
        sqlx::query(
            "insert into relay_nodes(host, updated_at) values(?, current_timestamp)
             on conflict(host) do nothing",
        )
        .bind(host)
        .execute(&mut *conn)
        .await?;
        Ok(())
    }

    async fn set_relay_meta(
        &self,
        host: &str,
        region: &str,
        weight: i32,
        capacity: i32,
    ) -> ResultType<()> {
        let mut conn = self.pool.get().await?;
        sqlx::query(
            "update relay_nodes set region=?, weight=?, capacity=? where host=?",
        )
        .bind(region)
        .bind(weight)
        .bind(capacity)
        .bind(host)
        .execute(&mut *conn)
        .await?;
        Ok(())
    }

    async fn record_relay_health(
        &self,
        host: &str,
        latency_ms: Option<i32>,
        healthy: bool,
        connections: i32,
    ) -> ResultType<()> {
        let mut conn = self.pool.get().await?;
        sqlx::query(
            "update relay_nodes
             set latency_ms=?, healthy=?, connections=?, updated_at=current_timestamp
             where host=?",
        )
        .bind(latency_ms)
        .bind(healthy as i32)
        .bind(connections)
        .bind(host)
        .execute(&mut *conn)
        .await?;
        Ok(())
    }

    async fn load_relay_nodes(&self) -> ResultType<Vec<RelayNode>> {
        let mut conn = self.pool.get().await?;
        let rows = sqlx::query("select host, region, weight, capacity, latency_ms, healthy, connections from relay_nodes")
            .fetch_all(&mut *conn)
            .await?;
        Ok(rows
            .into_iter()
            .map(|r| RelayNode {
                host: r.get("host"),
                region: r.get("region"),
                weight: r.get("weight"),
                capacity: r.get("capacity"),
                latency_ms: r.get("latency_ms"),
                healthy: r.get::<i32, _>("healthy") != 0,
                connections: r.get("connections"),
            })
            .collect())
    }

    // --- relay_access_list ------------------------------------------------

    async fn load_access_list(&self, kind: &str) -> ResultType<Vec<AccessEntry>> {
        let mut conn = self.pool.get().await?;
        // created_at 统一 cast 成文本：SQLite 的 datetime 是动态类型，
        // 直接解码成 String 在少数写法下会失败，cast 是最省心的取法。
        let rows = sqlx::query(
            "select ip, ifnull(note, '') as note, \
             ifnull(cast(created_at as text), '') as created_at \
             from relay_access_list where kind = ? order by created_at, ip",
        )
        .bind(kind)
        .fetch_all(&mut *conn)
        .await?;
        Ok(rows
            .into_iter()
            .map(|r| AccessEntry {
                ip: r.get("ip"),
                note: r.get("note"),
                created_at: r.get("created_at"),
            })
            .collect())
    }

    async fn add_access_entry(&self, kind: &str, ip: &str, note: &str) -> ResultType<()> {
        let mut conn = self.pool.get().await?;
        sqlx::query(
            "insert into relay_access_list(kind, ip, note) values(?, ?, ?)
             on conflict(kind, ip) do update set note=excluded.note",
        )
        .bind(kind)
        .bind(ip)
        .bind(note)
        .execute(&mut *conn)
        .await?;
        Ok(())
    }

    async fn remove_access_entry(&self, kind: &str, ip: &str) -> ResultType<()> {
        let mut conn = self.pool.get().await?;
        sqlx::query("delete from relay_access_list where kind = ? and ip = ?")
            .bind(kind)
            .bind(ip)
            .execute(&mut *conn)
            .await?;
        Ok(())
    }

    async fn clear_access_list(&self, kind: &str) -> ResultType<()> {
        let mut conn = self.pool.get().await?;
        sqlx::query("delete from relay_access_list where kind = ?")
            .bind(kind)
            .execute(&mut *conn)
            .await?;
        Ok(())
    }
}

/// SQLite 侧的 server_config / connection_log DDL。
/// 与 PG 版一一对应，只是类型与自增写法不同。
const SQLITE_EXTRA_DDL: &[&str] = &[
    r#"create table if not exists server_config (
           key        varchar(100) primary key not null,
           value      text not null,
           updated_at datetime not null default(current_timestamp)
       )"#,
    r#"create table if not exists connection_log (
           id         integer primary key autoincrement,
           ts         datetime not null default(current_timestamp),
           event      varchar(32) not null,
           from_id    varchar(253),
           from_ip    varchar(64),
           to_id      varchar(253),
           to_ip      varchar(64),
           via_relay  tinyint,
           detail     text
       )"#,
    r#"create index if not exists index_connection_log_ts    on connection_log (ts)"#,
    r#"create index if not exists index_connection_log_event on connection_log (event)"#,
    r#"create table if not exists relay_nodes (
           host        varchar(255) primary key not null,
           region      varchar(64)  not null default '',
           weight      integer not null default 100,
           capacity    integer not null default 1000,
           latency_ms  integer,
           healthy     integer not null default 1,
           connections integer not null default 0,
           updated_at  datetime not null default(current_timestamp)
       )"#,
    r#"create table if not exists relay_access_list (
           kind       varchar(16) not null,
           ip         varchar(64) not null,
           note       text,
           created_at datetime not null default(current_timestamp),
           primary key (kind, ip)
       )"#,
];

// =============================================================================
// PostgreSQL 后端（可选，DB_TYPE=postgres 时启用）
//
// 只用运行时 SQL（不含 query! 宏），因此构建这个二进制时**不需要**连得上 Postgres。
// =============================================================================
pub struct PgBackend {
    pool: PgPool,
}

impl PgBackend {
    async fn new(url: &str) -> ResultType<Self> {
        // ---------------------------------------------------------------
        // 先挡掉最常见的配置错：只设了 DB_TYPE=postgres 却没设 DB_URL。
        // 此时 url 会退回默认的 db_v2.sqlite3（一个文件路径），sqlx 会报
        // "invalid URL" 之类跟 Postgres 毫不相干的错误，排查方向直接跑偏。
        // 这里把"缺的是什么、该填成什么样"一次说清。
        // ---------------------------------------------------------------
        if !is_pg_url(url) {
            return Err(hbb_common::anyhow::anyhow!(
                "DB_TYPE=postgres，但 DB_URL 不是 Postgres 连接串（当前是 \"{url}\"）。\
                 请同时设置 DB_URL=postgres://<user>:<password>@<host>:5432/<db>，\
                 或把 DB_TYPE 改回 sqlite"
            ));
        }
        // SQLite 写操作串行，默认 1 个连接即可；Postgres 可以并行，默认放宽到 8
        let n: u32 = crate::common::get_arg_or("MAX_DATABASE_CONNECTIONS", "8".to_owned())
            .parse()
            .unwrap_or(8);
        log::debug!("MAX_DATABASE_CONNECTIONS={}", n);
        let pool = connect_pg(url, n).await?;
        Ok(Self { pool })
    }
}

/// 连接串是否指向 PostgreSQL。
fn is_pg_url(url: &str) -> bool {
    let u = url.trim().to_lowercase();
    u.starts_with("postgres://") || u.starts_with("postgresql://")
}

/// 建立 PG 连接池，失败自动重试。
///
/// 为什么需要重试而不是直接 `?`：hbbs 与 Postgres 在容器里是并发启动的，
/// `depends_on: condition: service_healthy` 只能覆盖"首次编排"这一种情况 ——
/// PG 自身重启、宿主机重启后两者同时拉起、网络插件未就绪等场景下，
/// hbbs 依然会先于 PG 可用。一次连不上就退出，容器会进入 CrashLoop，
/// 表现为"服务起不来"而日志里只有一行看不懂的连接错误。
///
/// 重试次数可用 DB_CONNECT_RETRIES 调（默认 10 次 × 3 秒 ≈ 30 秒上限）；
/// 设为 0 或 1 即等价于只试一次，保持原来的快速失败行为。
async fn connect_pg(url: &str, max_connections: u32) -> ResultType<PgPool> {
    let retries: u32 = crate::common::get_arg_or("DB_CONNECT_RETRIES", "10".to_owned())
        .parse()
        .unwrap_or(10)
        .max(1);
    let mut last_err = None;
    for attempt in 1..=retries {
        match PgPoolOptions::new()
            .max_connections(max_connections)
            .connect(url)
            .await
        {
            Ok(pool) => {
                if attempt > 1 {
                    log::info!("PostgreSQL 连接成功（第 {} 次尝试）", attempt);
                }
                return Ok(pool);
            }
            Err(e) => {
                // 每失败一次都打日志：最终失败时用户能看到"是一直连不上"
                // 还是"慢了一点才连上"，这两种情况的处置完全不同。
                log::error!("连接 PostgreSQL 失败（第 {}/{} 次）：{}", attempt, retries, e);
                last_err = Some(e);
                if attempt < retries {
                    hbb_common::tokio::time::sleep(std::time::Duration::from_secs(3)).await;
                }
            }
        }
    }
    Err(hbb_common::anyhow::anyhow!(
        "连接 PostgreSQL 失败，已重试 {} 次：{}\n\
         排查顺序：① Postgres 进程是否在运行；② DB_URL 的主机/端口/库名是否正确；\
         ③ 用户名密码与 pg_hba.conf 是否放行该来源；④ 库是否已创建。\n\
         当前连接串（已隐藏密码）：{}",
        retries,
        last_err
            .map(|e| e.to_string())
            .unwrap_or_else(|| "未知错误".to_owned()),
        mask_password(url)
    ))
}

/// 连接串脱敏：把 `postgres://user:password@host/db` 的密码换成 ******。
/// 日志里打印完整连接串等于把数据库口令写进日志文件，而排障又确实需要
/// 知道连的是哪个库 —— 隐藏密码是这两者的唯一交集。
pub fn mask_password(url: &str) -> String {
    let scheme_end = match url.find("://") {
        Some(i) => i + 3,
        None => return url.to_owned(), // 不是 URL（如 SQLite 文件路径），原样返回
    };
    // 凭据段 = scheme 之后到最后一个 '@' 之前。找不到 '@' 说明没带凭据，无需脱敏。
    let at = match url[scheme_end..].rfind('@') {
        Some(i) => scheme_end + i,
        None => return url.to_owned(),
    };
    let credential = &url[scheme_end..at];
    match credential.find(':') {
        // 只有"带密码"才打码；user@host 这种无密码形式保持原样
        Some(colon) => format!(
            "{}{}:******{}",
            &url[..scheme_end],
            &credential[..colon],
            &url[at..]
        ),
        None => url.to_owned(),
    }
}

#[async_trait]
impl DbBackend for PgBackend {
    fn name(&self) -> &'static str {
        "postgres"
    }

    async fn create_tables(&self) -> ResultType<()> {
        // 注意：PostgreSQL 的 PREPARE 不允许一次多条语句，必须逐条执行
        for sql in PG_DDL_STATEMENTS.iter().chain(PG_EXTRA_DDL.iter()) {
            sqlx::query(sql).execute(&self.pool).await?;
        }
        Ok(())
    }

    async fn get_peer(&self, id: &str) -> ResultType<Option<Peer>> {
        // "user" 是 PostgreSQL 保留字，必须加双引号，否则报错
        let row = sqlx::query(
            r#"select guid, id, uuid, pk, "user", status, info from peer where id = $1"#,
        )
        .bind(id)
        .fetch_optional(&self.pool)
        .await?;
        Ok(row.map(|r| {
            let status: Option<i16> = r.get("status");
            Peer {
                guid: r.get("guid"),
                id: r.get("id"),
                uuid: r.get("uuid"),
                pk: r.get("pk"),
                user: r.get("user"),
                status: status.map(|s| s as i64),
                info: r.get("info"),
            }
        }))
    }

    async fn insert_peer(
        &self,
        id: &str,
        uuid: &[u8],
        pk: &[u8],
        info: &str,
    ) -> ResultType<Vec<u8>> {
        let guid = uuid::Uuid::new_v4().as_bytes().to_vec();
        sqlx::query(
            r#"insert into peer(guid, id, uuid, pk, info) values($1, $2, $3, $4, $5)"#,
        )
        .bind(&guid)
        .bind(id)
        .bind(uuid)
        .bind(pk)
        .bind(info)
        .execute(&self.pool)
        .await?;
        Ok(guid)
    }

    async fn update_pk(&self, guid: &[u8], id: &str, pk: &[u8], info: &str) -> ResultType<()> {
        sqlx::query(r#"update peer set id = $1, pk = $2, info = $3 where guid = $4"#)
            .bind(id)
            .bind(pk)
            .bind(info)
            .bind(guid)
            .execute(&self.pool)
            .await?;
        Ok(())
    }

    async fn get_config(&self, key: &str) -> ResultType<Option<String>> {
        let row = sqlx::query(r#"select value from server_config where key = $1"#)
            .bind(key)
            .fetch_optional(&self.pool)
            .await?;
        Ok(row.map(|r| r.get::<String, _>("value")))
    }

    async fn remove_config(&self, key: &str) -> ResultType<()> {
        sqlx::query(r#"delete from server_config where key = $1"#)
            .bind(key)
            .execute(&self.pool)
            .await?;
        Ok(())
    }

    async fn ping(&self) -> ResultType<()> {
        sqlx::query("select 1").execute(&self.pool).await?;
        Ok(())
    }

    async fn insert_audit(&self, events: &[AuditEvent]) -> ResultType<()> {
        // 批量写入放在一个事务里：要么整批进去，要么整批回滚，
        // 避免"刷盘到一半断连"留下半截记录。
        let mut tx = self.pool.begin().await?;
        for e in events {
            sqlx::query(
                r#"insert into connection_log(ts, event, from_id, from_ip, to_id, to_ip, via_relay, detail)
                   values($1, $2, $3, $4, $5, $6, $7, $8)"#,
            )
            .bind(e.ts)
            .bind(&e.event)
            .bind(&e.from_id)
            .bind(&e.from_ip)
            .bind(&e.to_id)
            .bind(&e.to_ip)
            .bind(e.via_relay)
            .bind(&e.detail)
            .execute(&mut tx)
            .await?;
        }
        tx.commit().await?;
        Ok(())
    }

    async fn ensure_relay_node(&self, host: &str) -> ResultType<()> {
        sqlx::query(
            r#"insert into relay_nodes(host, updated_at) values($1, now())
               on conflict (host) do nothing"#,
        )
        .bind(host)
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    async fn set_relay_meta(
        &self,
        host: &str,
        region: &str,
        weight: i32,
        capacity: i32,
    ) -> ResultType<()> {
        sqlx::query(
            r#"update relay_nodes set region=$1, weight=$2, capacity=$3 where host=$4"#,
        )
        .bind(region)
        .bind(weight)
        .bind(capacity)
        .bind(host)
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    async fn record_relay_health(
        &self,
        host: &str,
        latency_ms: Option<i32>,
        healthy: bool,
        connections: i32,
    ) -> ResultType<()> {
        sqlx::query(
            r#"update relay_nodes
               set latency_ms=$1, healthy=$2, connections=$3, updated_at=now()
               where host=$4"#,
        )
        .bind(latency_ms)
        .bind(healthy)
        .bind(connections)
        .bind(host)
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    async fn load_relay_nodes(&self) -> ResultType<Vec<RelayNode>> {
        let rows = sqlx::query(
            r#"select host, region, weight, capacity, latency_ms, healthy, connections
               from relay_nodes"#,
        )
        .fetch_all(&self.pool)
        .await?;
        Ok(rows
            .into_iter()
            .map(|r| RelayNode {
                host: r.get("host"),
                region: r.get("region"),
                weight: r.get("weight"),
                capacity: r.get("capacity"),
                latency_ms: r.get("latency_ms"),
                healthy: r.get::<bool, _>("healthy"),
                connections: r.get("connections"),
            })
            .collect())
    }

    // --- relay_access_list ------------------------------------------------

    async fn load_access_list(&self, kind: &str) -> ResultType<Vec<AccessEntry>> {
        // timestamptz 不能被解码成 String，用 to_char 在 SQL 侧转成文本：
        // 这样两个后端返回给上层的类型完全一致，调用方不必区分。
        let rows = sqlx::query(
            r#"select ip, coalesce(note, '') as note,
                      to_char(created_at, 'YYYY-MM-DD HH24:MI:SS') as created_at
               from relay_access_list where kind = $1 order by created_at, ip"#,
        )
        .bind(kind)
        .fetch_all(&self.pool)
        .await?;
        Ok(rows
            .into_iter()
            .map(|r| AccessEntry {
                ip: r.get("ip"),
                note: r.get("note"),
                created_at: r.get("created_at"),
            })
            .collect())
    }

    async fn add_access_entry(&self, kind: &str, ip: &str, note: &str) -> ResultType<()> {
        sqlx::query(
            r#"insert into relay_access_list(kind, ip, note) values($1, $2, $3)
               on conflict (kind, ip) do update set note = excluded.note"#,
        )
        .bind(kind)
        .bind(ip)
        .bind(note)
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    async fn remove_access_entry(&self, kind: &str, ip: &str) -> ResultType<()> {
        sqlx::query(r#"delete from relay_access_list where kind = $1 and ip = $2"#)
            .bind(kind)
            .bind(ip)
            .execute(&self.pool)
            .await?;
        Ok(())
    }

    async fn clear_access_list(&self, kind: &str) -> ResultType<()> {
        sqlx::query(r#"delete from relay_access_list where kind = $1"#)
            .bind(kind)
            .execute(&self.pool)
            .await?;
        Ok(())
    }
}

/// PostgreSQL 的 peer 表结构。类型承接 SQLite 版：
/// blob→bytea、tinyint→smallint、datetime→timestamptz，并去掉 SQLite 专有的 WITHOUT ROWID。
/// user 是保留字，全部加双引号。
const PG_DDL_STATEMENTS: &[&str] = &[
    r#"create table if not exists peer (
           guid bytea primary key not null,
           id varchar(100) not null,
           uuid bytea not null,
           pk bytea not null,
           created_at timestamptz not null default now(),
           "user" bytea,
           status smallint,
           note varchar(300),
           info text not null
       )"#,
    r#"create unique index if not exists index_peer_id on peer (id)"#,
    r#"create index if not exists index_peer_user on peer ("user")"#,
    r#"create index if not exists index_peer_created_at on peer (created_at)"#,
    r#"create index if not exists index_peer_status on peer (status)"#,
];

/// PG 侧的 server_config / connection_log DDL（与 SQLITE_EXTRA_DDL 一一对应）。
/// 同样必须逐条执行 —— PG 的 PREPARE 不接受多语句。
const PG_EXTRA_DDL: &[&str] = &[
    r#"create table if not exists server_config (
           key        varchar(100) primary key not null,
           value      text not null,
           updated_at timestamptz not null default now()
       )"#,
    r#"create table if not exists connection_log (
           id         bigserial primary key,
           ts         timestamptz not null default now(),
           event      varchar(32) not null,
           from_id    varchar(253),
           from_ip    varchar(64),
           to_id      varchar(253),
           to_ip      varchar(64),
           via_relay  boolean,
           detail     text
       )"#,
    r#"create index if not exists index_connection_log_ts    on connection_log (ts)"#,
    r#"create index if not exists index_connection_log_event on connection_log (event)"#,
    r#"create table if not exists relay_nodes (
           host        varchar(255) primary key not null,
           region      varchar(64)  not null default '',
           weight      integer not null default 100,
           capacity    integer not null default 1000,
           latency_ms  integer,
           healthy     boolean not null default true,
           connections integer not null default 0,
           updated_at  timestamptz not null default now()
       )"#,
    r#"create table if not exists relay_access_list (
           kind       varchar(16) not null,
           ip         varchar(64) not null,
           note       text,
           created_at timestamptz not null default now(),
           primary key (kind, ip)
       )"#,
];

// =============================================================================
// Database：对外门面
// =============================================================================
/// 内置 Postgres 的连接参数（`DB_URL` 省略时用它推导地址）。
struct PgSettings {
    user: String,
    password: String,
    host: String,
    port: String,
    db: String,
}

fn pg_settings() -> PgSettings {
    PgSettings {
        user: crate::common::get_arg_or("PG_USER", "rustdesk".to_owned()),
        password: crate::common::get_arg_or("PG_PASSWORD", "changeme".to_owned()),
        host: crate::common::get_arg_or("PG_HOST", "postgres".to_owned()),
        port: crate::common::get_arg_or("PG_PORT", "5432".to_owned()),
        db: crate::common::get_arg_or("PG_DB", "rustdesk".to_owned()),
    }
}

fn pg_url(s: &PgSettings) -> String {
    format!(
        "postgres://{}:{}@{}:{}/{}",
        s.user, s.password, s.host, s.port, s.db
    )
}

/// 后端选择 + 地址解析的**唯一决策点**（纯函数，不读环境变量）。
///
/// 返回 `(后端名, 地址)`。把两件事合在一个函数里是刻意的：
/// "用哪个后端" 和 "连哪个地址" 必须同时决定，分开判断迟早会出现
/// 「类型是 Postgres、地址却指向 SQLite 文件」这种半通配置。
///
/// 规则（`DB_TYPE` 为权威，默认 postgres）：
///   - `postgres` / `pg`：DB_URL 是连接串就用它；否则由 PG_* 推导
///   - `sqlite`         ：DB_URL 是文件路径就用它；否则用默认路径
///   - 未设置           ：默认 postgres；但 DB_URL 明显是 SQLite 文件时
///                        判定为旧部署，回退 SQLite（由调用方 WARN）
fn plan_db(db_type: &str, url: &str, pg: &PgSettings) -> (&'static str, String) {
    let url = url.trim();
    match db_type.trim().to_lowercase().as_str() {
        "postgres" | "postgresql" | "pg" => (
            "postgres",
            if is_pg_url(url) {
                url.to_owned()
            } else {
                pg_url(pg)
            },
        ),
        "sqlite" | "sqlite3" => (
            "sqlite",
            if !url.is_empty() && !is_pg_url(url) {
                url.to_owned()
            } else {
                default_sqlite_path()
            },
        ),
        _ => {
            if is_pg_url(url) {
                ("postgres", url.to_owned())
            } else if !url.is_empty() {
                // 按旧默认部署的环境：DB_TYPE 没写、DB_URL 给的是文件路径
                ("sqlite", url.to_owned())
            } else {
                ("postgres", pg_url(pg))
            }
        }
    }
}

/// 判断是否使用 PostgreSQL。走 `plan_db()`，保证与地址解析同源。
fn want_postgres(url: &str) -> bool {
    let db_type = crate::common::get_arg_or("DB_TYPE", String::new());
    plan_db(&db_type, url, &pg_settings()).0 == "postgres"
}

/// 运行期数据库地址的唯一解析入口。
///
/// 规则：先由 `want_postgres()` 定下后端类型（DB_TYPE 为权威，默认 postgres），
/// 再按这个类型挑地址 ——
///   - 选了 postgres：DB_URL 是连接串就用它；否则由 PG_* 拼一个
///   - 选了 sqlite  ：DB_URL 是文件路径就用它；否则用默认的 ./db_v2.sqlite3
///
/// 为什么要这样兜底，而不是"缺了就报错"：
/// 部署层有三处会分别写 DB_TYPE 与 DB_URL（.env / compose / rdrun.sh），
/// 只要漏改一处，就会出现"DB_TYPE 说 Postgres、DB_URL 却指向 SQLite 文件"
/// 这种**半通配置** —— 服务起得来，但数据落错了地方，日志里看不出来。
/// 让 DB_TYPE 成为唯一权威、DB_URL 可省，等于把这类错误直接消掉。
pub fn resolve_db_url() -> String {
    let db_type = crate::common::get_arg_or("DB_TYPE", String::new());
    let raw = crate::common::get_arg_or("DB_URL", String::new());
    let pg = pg_settings();

    let (backend, addr) = plan_db(&db_type, &raw, &pg);

    // 只在"类型和地址不自洽"时出声，正常路径保持安静。
    // 这些提示集中在这里，避免两个入口各打一遍日志。
    if backend == "postgres" && !is_pg_url(&raw) {
        if raw.trim().is_empty() {
            log::info!("DB_URL 未设置，按 PG_* 推导为 {}", mask_password(&addr));
        } else {
            log::warn!(
                "DB_TYPE=postgres，但 DB_URL=\"{}\" 不是连接串，已按 PG_* 推导为 {}。\
                 若要继续使用这个文件，请把 DB_TYPE 改成 sqlite。",
                raw.trim(),
                mask_password(&addr)
            );
        }
    } else if backend == "sqlite" && is_pg_url(&raw) {
        // 类型和地址互相打架，先吼一嗓子再退回默认路径。
        // 之所以不让它带着这个错误地址去连（那样必然 crash-loop）：
        // 崩溃循环下容器会反复重启、日志被刷爆，真正的配置错误反而更难读。
        log::error!(
            "DB_TYPE=sqlite，但 DB_URL 是 Postgres 连接串（{}）—— 两者必须自洽。\
             本次已改用默认的 {addr}。\
             要么把 DB_TYPE 改成 postgres，要么把 DB_URL 改成容器内文件路径。",
            mask_password(&raw)
        );
    } else if db_type.trim().is_empty() && !raw.trim().is_empty() {
        // 旧按默认部署的环境：DB_TYPE 没写、DB_URL 给的是 SQLite 文件路径。
        // 这里回退到 SQLite 以兼容既有部署，但要明确告知 —— 否则就会出现
        // "以为在用 PostgreSQL，其实还在写 SQLite" 的静默不一致。
        log::warn!(
            "DB_TYPE 未设置，但 DB_URL=\"{addr}\" 看起来是 SQLite 文件：\
             已回退到 SQLite 以兼容既有部署。\n\
             当前默认后端是 PostgreSQL。若要切过去，请同时设置 \
             DB_TYPE=postgres 与 DB_URL=postgres://<user>:<pwd>@<host>:5432/<db>；\
             若确实要继续使用 SQLite，请显式设置 DB_TYPE=sqlite 消除本告警。"
        );
    }

    addr
}

/// 与上游一致的 SQLite 默认路径。
///
/// `mut` 只在 Windows 分支里用得到，Linux/macOS 下会触发 unused_mut ——
/// 加 allow 而不是拆两个 cfg 分支，是为了让各平台的默认路径始终只有这一处定义。
fn default_sqlite_path() -> String {
    #[allow(unused_mut)]
    let mut db = "db_v2.sqlite3".to_owned();
    #[cfg(all(windows, not(debug_assertions)))]
    {
        if let Some(path) = hbb_common::config::Config::icon_path().parent() {
            db = format!("{}\\{}", path.to_str().unwrap_or("."), db);
        }
    }
    #[cfg(not(windows))]
    {
        db = format!("./{db}");
    }
    db
}

pub struct Database {
    backend: Arc<dyn DbBackend>,
}

// Arc<dyn ...> 本身可 Clone；这里显式实现是为了保持 `Database: Clone`，
// `peer.rs` 里有 `db.clone()` 的用法（PeerMap 派生 Clone）。
impl Clone for Database {
    fn clone(&self) -> Self {
        Self {
            backend: self.backend.clone(),
        }
    }
}

impl Database {
    pub async fn new(url: &str) -> ResultType<Database> {
        // 日志里只打印脱敏后的连接串 —— PG 连接串含明文口令，
        // 而排障时真正需要知道的（后端类型、主机、库名）脱敏后依然可见。
        let backend: Arc<dyn DbBackend> = if want_postgres(url) {
            let b = PgBackend::new(url).await?;
            log::info!(
                "Database backend = {} (DB_URL={})",
                b.name(),
                mask_password(url)
            );
            Arc::new(b)
        } else {
            let b = SqliteBackend::new(url).await?;
            log::info!("Database backend = {} (DB_URL={})", b.name(), url);
            Arc::new(b)
        };
        let db = Database { backend };
        // 走 facade 自己的 create_tables()，而不是直接调 backend 的：
        // 这样公开方法就是"活"的（避免 dead_code 误报），也保证两个后端
        // 都一定会在启动时建好表，不会因为将来加后端而漏掉这一步。
        db.create_tables().await?;
        Ok(db)
    }

    /// 建表 / 建索引。启动时由 `Database::new` 调用；也可由外部显式重入（幂等）。
    pub async fn create_tables(&self) -> ResultType<()> {
        self.backend.create_tables().await
    }

    pub async fn get_peer(&self, id: &str) -> ResultType<Option<Peer>> {
        self.backend.get_peer(id).await
    }

    pub async fn insert_peer(
        &self,
        id: &str,
        uuid: &[u8],
        pk: &[u8],
        info: &str,
    ) -> ResultType<Vec<u8>> {
        self.backend.insert_peer(id, uuid, pk, info).await
    }

    pub async fn update_pk(
        &self,
        guid: &[u8],
        id: &str,
        pk: &[u8],
        info: &str,
    ) -> ResultType<()> {
        self.backend.update_pk(guid, id, pk, info).await
    }

    /// 当前是否运行在 PostgreSQL 后端上。
    /// 密钥托管、审计落库都以它为开关 —— SQLite 模式下保持上游的纯文件行为。
    pub fn is_postgres(&self) -> bool {
        self.backend.name() == "postgres"
    }

    /// 后端名字（"sqlite" / "postgres"），供启动日志与控制台使用。
    pub fn backend_name(&self) -> &'static str {
        self.backend.name()
    }

    pub async fn get_config(&self, key: &str) -> ResultType<Option<String>> {
        self.backend.get_config(key).await
    }

    pub async fn remove_config(&self, key: &str) -> ResultType<()> {
        self.backend.remove_config(key).await
    }

    /// 数据库探活（select 1）；失败返回 false。健康检查用它判定 status。
    pub async fn ping(&self) -> bool {
        self.backend.ping().await.is_ok()
    }

    pub async fn insert_audit(&self, events: &[AuditEvent]) -> ResultType<()> {
        self.backend.insert_audit(events).await
    }

    // --- relay_nodes：中继节点登记与实时健康 -----------------------------

    /// 确保某个中继主机在 `relay_nodes` 表中存在（不存在则插入默认行）。
    pub async fn ensure_relay_node(&self, host: &str) -> ResultType<()> {
        self.backend.ensure_relay_node(host).await
    }

    /// 更新中继的静态调度权重（区域 / 权重 / 容量）。
    pub async fn set_relay_meta(
        &self,
        host: &str,
        region: &str,
        weight: i32,
        capacity: i32,
    ) -> ResultType<()> {
        self.backend.set_relay_meta(host, region, weight, capacity).await
    }

    /// 写入一次健康探测结果（时延 / 是否存活 / 当前连接数）。
    pub async fn record_relay_health(
        &self,
        host: &str,
        latency_ms: Option<i32>,
        healthy: bool,
        connections: i32,
    ) -> ResultType<()> {
        self.backend.record_relay_health(host, latency_ms, healthy, connections).await
    }

    /// 读出全部中继节点（含静态权重与最近一次健康）。
    pub async fn load_relay_nodes(&self) -> ResultType<Vec<RelayNode>> {
        self.backend.load_relay_nodes().await
    }

    // --- relay_access_list：hbbr 名单 -------------------------------------

    pub async fn load_access_list(&self, kind: &str) -> ResultType<Vec<AccessEntry>> {
        self.backend.load_access_list(kind).await
    }

    pub async fn add_access_entry(&self, kind: &str, ip: &str, note: &str) -> ResultType<()> {
        self.backend.add_access_entry(kind, ip, note).await
    }

    pub async fn remove_access_entry(&self, kind: &str, ip: &str) -> ResultType<()> {
        self.backend.remove_access_entry(kind, ip).await
    }

    pub async fn clear_access_list(&self, kind: &str) -> ResultType<()> {
        self.backend.clear_access_list(kind).await
    }
}

#[cfg(test)]
mod tests {
    use hbb_common::tokio;
    #[test]
    fn test_insert() {
        insert();
    }

    // -----------------------------------------------------------------------
    // 纯函数单测：不需要任何数据库，随 `cargo test` 一起跑。
    // 值钱之处在于它们锁住的是"日志里绝不能出现明文口令"这条底线 ——
    // 这类缺陷不会让构建失败，只会静默把口令写进日志文件。
    // -----------------------------------------------------------------------
    #[test]
    fn test_mask_password() {
        let url = "postgres://rustdesk:s3cr3t@db:5432/rustdesk";
        let masked = super::mask_password(url);
        assert!(!masked.contains("s3cr3t"), "密码泄漏: {masked}");
        assert!(masked.contains("rustdesk:"), "用户名应保留: {masked}");
        assert!(masked.contains("@db:5432/rustdesk"), "主机与库名应保留: {masked}");
        assert!(masked.contains("******"), "密码位应有掩码: {masked}");

        // 无密码的连接串（trust 认证）不该被改坏
        assert_eq!(super::mask_password("postgres://db:5432/rustdesk"), "postgres://db:5432/rustdesk");
        // 非连接串（SQLite 文件路径）原样返回，不能 panic
        assert_eq!(super::mask_password("./db_v2.sqlite3"), "./db_v2.sqlite3");
        // 有用户名但无密码（trust / peer 认证）不该被拼出多余的 ******
        assert_eq!(super::mask_password("postgres://u@h:5432/d"), "postgres://u@h:5432/d");
    }

    // -----------------------------------------------------------------------
    // 后端选择 + 地址解析的一致性
    //
    // 这组用例锁的是"类型与地址必须自洽"这条规则 —— 它是本次把 PostgreSQL
    // 设为默认后端后最容易出事的地方：DB_TYPE 与 DB_URL 在 .env / compose /
    // rdrun.sh 三处分别配置，漏改一处就会出现"说用 PG、实际写 SQLite"。
    // plan_db 是纯函数，所以这里可以断言，不需要任何数据库。
    // -----------------------------------------------------------------------
    fn pg() -> super::PgSettings {
        super::PgSettings {
            user: "rustdesk".to_owned(),
            password: "changeme".to_owned(),
            host: "postgres".to_owned(),
            port: "5432".to_owned(),
            db: "rustdesk".to_owned(),
        }
    }

    fn plan(db_type: &str, url: &str) -> (&'static str, String) {
        super::plan_db(db_type, url, &pg())
    }

    #[test]
    fn test_plan_db_default_is_postgres() {
        // 什么都不给 → 默认 PostgreSQL，地址由 PG_* 推导
        assert_eq!(
            plan("", ""),
            (
                "postgres",
                "postgres://rustdesk:changeme@postgres:5432/rustdesk".to_owned()
            )
        );
    }

    #[test]
    fn test_plan_db_pg_url_wins() {
        // DB_URL 是连接串 → 原样使用（不覆盖）
        let u = "postgres://u:p@db.example.com:5432/other";
        assert_eq!(plan("postgres", u), ("postgres", u.to_owned()));
        // 未设 DB_TYPE 但 DB_URL 是连接串 → 也应识别为 postgres
        assert_eq!(plan("", u), ("postgres", u.to_owned()));
    }

    #[test]
    fn test_plan_db_pg_type_derives_when_url_missing_or_wrong() {
        let derived = "postgres://rustdesk:changeme@postgres:5432/rustdesk".to_owned();
        // 显式 postgres 但忘了给 DB_URL
        assert_eq!(plan("postgres", ""), ("postgres", derived.clone()));
        // 显式 postgres 但 DB_URL 还是旧的文件路径（三处配置漏改的典型）
        assert_eq!(plan("postgres", "/root/db_v2.sqlite3"), ("postgres", derived.clone()));
        // 大小写不敏感
        assert_eq!(plan("PG", ""), ("postgres", derived));
    }

    #[test]
    fn test_plan_db_sqlite_keeps_file_path() {
        assert_eq!(plan("sqlite", "/root/db_v2.sqlite3"), ("sqlite", "/root/db_v2.sqlite3".to_owned()));
    }

    #[test]
    fn test_plan_db_sqlite_rejects_pg_url() {
        // 类型与地址打架 → 必须退回 SQLite 默认路径，绝不能拿连接串去当文件名
        let (backend, addr) = plan("sqlite", "postgres://u:p@h:5432/d");
        assert_eq!(backend, "sqlite", "类型是 sqlite 时后端必须是 sqlite");
        assert!(
            !addr.starts_with("postgres://"),
            "地址不应仍是连接串，实际是 {addr}"
        );
        assert!(addr.ends_with("db_v2.sqlite3"), "应退回 SQLite 默认路径，实际是 {addr}");
    }

    #[test]
    fn test_plan_db_legacy_env_falls_back_to_sqlite() {
        // 旧部署：DB_TYPE 没写、DB_URL 是文件路径 → 回退 SQLite（并 WARN）
        let (backend, addr) = plan("", "./db_v2.sqlite3");
        assert_eq!(backend, "sqlite");
        assert_eq!(addr, "./db_v2.sqlite3");
    }

    // -----------------------------------------------------------------------
    // 名单（黑名单 / 阻断名单）落库：只用 SQLite 就能跑，不需要 Postgres。
    //
    // 这条用例锁的是"重复 add 幂等 + 删除不存在的条目不报错"——hbbr 的控制台
    // 命令会被反复敲、也会被脚本批量重放，任何一个不幂等都会让脚本失败或
    // 名单被意外清空。PG 侧走同一套 trait 与同样形状的 SQL，这里通过即等价覆盖。
    // -----------------------------------------------------------------------
    #[test]
    fn test_sqlite_access_list_roundtrip() {
        let file = "test_access_list.db";
        let _ = std::fs::remove_file(file);
        tokio::runtime::Runtime::new().unwrap().block_on(async {
            let db = super::Database::new(file).await.unwrap();
            let bl = super::ACCESS_KIND_BLACKLIST;
            let bk = super::ACCESS_KIND_BLOCKLIST;

            db.add_access_entry(bl, "1.2.3.4", "brute force").await.unwrap();
            // 重复 add 必须幂等：不报错、不重复，只刷新备注
            db.add_access_entry(bl, "1.2.3.4", "again").await.unwrap();
            db.add_access_entry(bk, "5.6.7.8", "").await.unwrap();

            let list = db.load_access_list(bl).await.unwrap();
            assert_eq!(list.len(), 1, "重复 add 不应产生重复行，实际 {list:?}");
            assert_eq!(list[0].ip, "1.2.3.4");
            assert_eq!(list[0].note, "again", "重复 add 应更新备注");
            assert!(!list[0].created_at.is_empty(), "created_at 应能取到");
            assert_eq!(db.load_access_list(bk).await.unwrap().len(), 1);

            db.remove_access_entry(bl, "1.2.3.4").await.unwrap();
            assert!(db.load_access_list(bl).await.unwrap().is_empty());
            // 删除不存在的条目：必须视为成功，否则批量脚本会中断
            db.remove_access_entry(bl, "9.9.9.9").await.unwrap();

            db.clear_access_list(bk).await.unwrap();
            assert!(db.load_access_list(bk).await.unwrap().is_empty());
        });
        let _ = std::fs::remove_file(file);
    }

    #[test]
    fn test_is_pg_url() {
        assert!(super::is_pg_url("postgres://u:p@h:5432/d"));
        assert!(super::is_pg_url("POSTGRES://u:p@h:5432/d"), "大小写不敏感");
        assert!(super::is_pg_url("postgresql://u:p@h:5432/d"));
        assert!(!super::is_pg_url("/root/db_v2.sqlite3"), "SQLite 文件路径不能误判为 PG");
        assert!(!super::is_pg_url("db_v2.sqlite3"));
    }

    #[tokio::main(flavor = "multi_thread")]
    async fn insert() {
        // 测试用例固定走 SQLite：不需要外部 Postgres 即可跑 `cargo test`
        let db = super::Database::new("test.sqlite3").await.unwrap();
        let mut jobs = vec![];
        for i in 0..10000 {
            let cloned = db.clone();
            let id = i.to_string();
            let a = tokio::spawn(async move {
                let empty_vec = Vec::new();
                cloned
                    .insert_peer(&id, &empty_vec, &empty_vec, "")
                    .await
                    .unwrap();
            });
            jobs.push(a);
        }
        for i in 0..10000 {
            let cloned = db.clone();
            let id = i.to_string();
            let a = tokio::spawn(async move {
                cloned.get_peer(&id).await.unwrap();
            });
            jobs.push(a);
        }
        hbb_common::futures::future::join_all(jobs).await;
    }

    // -----------------------------------------------------------------------
    // PostgreSQL 后端集成测试
    //
    // 编译通过不等于能跑通：位元组类型（bytea）、`"user"` 保留字引号、`$n` 占位符、
    // 以及"PREPARE 不允许一次多语句"这几件事只有在连上真实 Postgres 后才验得出来。
    // 默认 #[ignore]，不进 CI；本地需要时显式执行：
    //
    //   PG_TEST_URL=postgres://postgres:<pwd>@127.0.0.1:<port>/<db> \
    //     cargo test --locked pg_backend_roundtrip -- --ignored --nocapture
    // -----------------------------------------------------------------------
    #[tokio::main(flavor = "multi_thread")]
    #[test]
    #[ignore]
    async fn test_pg_backend_roundtrip() {
        let url = std::env::var("PG_TEST_URL").expect("需要设置 PG_TEST_URL");
        let db = super::Database::new(&url).await.expect("连接 Postgres 失败");

        // create_tables 必须幂等：连着跑两次不应报错（PG 用 create table if not exists）
        db.create_tables().await.expect("建表失败（第 1 次）");
        db.create_tables().await.expect("建表失败（第 2 次，应幂等）");

        let id = format!("test-{}", uuid::Uuid::new_v4());
        let uuid_bytes = b"device-uuid-bytes";
        let pk = b"public-key-bytes";
        let info = r#"{"os":"windows","ver":"1.2.3"}"#;

        // 1) 插入
        let guid = db
            .insert_peer(&id, uuid_bytes, pk, info)
            .await
            .expect("insert_peer 失败");
        assert_eq!(guid.len(), 16, "guid 应是 16 字节 UUID");

        // 2) 读回并逐字段校验（覆盖 blob→bytea 映射）
        let p = db.get_peer(&id).await.expect("get_peer 失败").unwrap_or_else(|| panic!("查不到 {}", id));
        assert_eq!(p.id, id, "id 字段不一致");
        assert_eq!(p.uuid.as_slice(), uuid_bytes, "uuid(bytea) 往返不一致");
        assert_eq!(p.pk.as_slice(), pk, "pk(bytea) 往返不一致");
        assert_eq!(p.info, info, "info 字段不一致");
        assert_eq!(p.guid, guid, "返回的 guid 与插入时不一致");

        // 3) 更新
        let new_pk = b"rotated-key";
        db.update_pk(&guid, &id, new_pk, "{}")
            .await
            .expect("update_pk 失败");
        let p2 = db.get_peer(&id).await.unwrap().expect("更新后查不到");
        assert_eq!(p2.pk.as_slice(), new_pk, "update_pk 未生效");
        assert_eq!(p2.guid, guid, "guid 不应变化");

        // 4) 查不存在的 id 应返回 None，而非报错
        let none = db.get_peer("no-such-id").await.expect("查询不应报错");
        assert!(none.is_none(), "不存在的 id 应返回 None");

        // 5) 清理，保证可重复运行
        let pool = sqlx::postgres::PgPoolOptions::new()
            .max_connections(1)
            .connect(&url)
            .await
            .unwrap();
        sqlx::query("delete from peer where guid = $1")
            .bind(&guid)
            .execute(&pool)
            .await
            .expect("清理失败");

        println!("[OK] PostgreSQL 后端往返测试通过");
    }
}
