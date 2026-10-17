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
// 存储后端抽象
// ---------------------------------------------------------------------------
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
}

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
        for sql in PG_DDL_STATEMENTS {
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

// =============================================================================
// Database：对外门面
// =============================================================================
/// 判断是否使用 PostgreSQL。
/// 优先级：环境变量 DB_TYPE（sqlite / postgres）> DB_URL 连接串本身的前缀。
fn want_postgres(url: &str) -> bool {
    let explicit = crate::common::get_arg_or("DB_TYPE", String::new())
        .trim()
        .to_lowercase();
    match explicit.as_str() {
        "postgres" | "postgresql" | "pg" => true,
        "sqlite" | "sqlite3" => false,
        _ => is_pg_url(url),
    }
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
