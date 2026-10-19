// =============================================================================
// 服务端密钥（id_ed25519 / id_ed25519.pub）的解析与数据库托管
// =============================================================================
//
// 上游行为
// --------
// 上游把密钥当成**进程工作目录下的两个文件**：存在就用，不存在就生成。
// 单机部署没问题，但下面两个场景会出事：
//
//   1) 横向扩容 / 多实例：每台 hbbs 各自生成一把私钥，客户端拿到的服务端公钥
//      来回变，表现为"时好时坏、偶尔报密钥不匹配"——极难定位。
//   2) 无状态容器：数据目录只读或每次重建，密钥活不过重启，
//      每重启一次全团队客户端就得重配一次。
//
// 所以这里加一层数据库托管：钥匙存进 PostgreSQL 的 `server_config` 表，
// 所有实例读同一行。
//
// 解析顺序（先命中先用）
// ----------------------
//   1. 命令行 `-k/--key` 给的 64 字节私钥 —— 最高优先级，且**覆盖**库中旧值
//      （运维显式指定，意图明确，不留歧义）。
//   2. 本地文件 `id_ed25519` —— 兼容既有部署，保证老 ID 不漂移；
//      顺手回填数据库，让其他实例共用这一把。
//   3. 数据库 `server_config.server.private_key` —— 多实例共享的关键路径。
//   4. 都没有 → 就地生成，同时写文件与数据库。
//
// ⚠️ 安全权衡（说直白些）
// ----------------------
// 私钥进库意味着：**任何能读这个库的人都能拿到服务端私钥**，
// 进而可以签发任意客户端密钥。所以一旦启用托管，PostgreSQL 的访问控制、
// 账号权限、备份文件密级都必须按"服务端私钥"来对待，而不是普通业务库。
// 接受不了就设 `DB_STORE_KEY=N` 退回纯文件模式 —— 代价是多实例无法共享钥匙。
// =============================================================================

use crate::common::PRIVATE_KEY_FILE;
use crate::database::{self, Database};
use hbb_common::{allow_err, log, ResultType};
use sodiumoxide::crypto::sign;

/// 密钥来自哪里。只用于启动日志，但对排障很值钱：
/// "ID 突然全变了" 往往对应这里从 file 变成了 generated。
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum KeySource {
    /// 命令行 `-k/--key`
    Arg,
    /// 本地文件 `id_ed25519`
    File,
    /// 数据库 `server_config` 表
    Database,
    /// 本次新生成
    Generated,
}

impl KeySource {
    pub fn as_str(self) -> &'static str {
        match self {
            KeySource::Arg => "命令行 -k",
            KeySource::File => "本地文件 id_ed25519",
            KeySource::Database => "数据库 server_config",
            KeySource::Generated => "本次新生成",
        }
    }
}

/// 解析出的服务端密钥。
pub struct ServerKey {
    /// 公钥字符串（base64）；`-k` 给的是普通字符串时原样带回
    pub pk: String,
    /// 私钥；仅在我们持有私钥时有值
    pub sk: Option<sign::SecretKey>,
    pub source: KeySource,
}

/// 数据库托管是否启用：后端是 PostgreSQL 且 `DB_STORE_KEY` 未被关掉。
fn hosting_on(db: &Database) -> bool {
    db.is_postgres() && database::key_hosting_enabled()
}

/// 解析服务端密钥。`key_arg` 即 `hbbs -k` 的值（未指定时上游传 `-`）。
///
/// 与上游 `get_server_sk` 的语义保持一致，只在其上叠加数据库托管。
pub async fn resolve(key_arg: &str, db: &Database) -> ResultType<ServerKey> {
    let mut key = key_arg.to_owned();
    let db_on = hosting_on(db);

    // ---- 1) `-k` 给的是完整私钥 -----------------------------------------
    if let Ok(raw) = base64::decode(key.trim()) {
        if raw.len() == sign::SECRETKEYBYTES {
            log::info!("The key is a crypto private key");
            // 后 32 字节就是公钥，上游拿它当 license key 的取值
            key = base64::encode(&raw[(sign::SECRETKEYBYTES / 2)..]);
            let mut tmp = [0u8; sign::SECRETKEYBYTES];
            tmp[..].copy_from_slice(&raw);
            let secret = sign::SecretKey(tmp);
            if db_on {
                // 显式指定的钥匙意图明确，允许覆盖库中旧值
                overwrite(db, &secret).await;
            }
            return Ok(ServerKey {
                pk: key,
                sk: Some(secret),
                source: KeySource::Arg,
            });
        }
    }

    // ---- `-k` 没给私钥 ---------------------------------------------------
    // 上游语义：key 为 "" / "-" / "_" 时才去"找一把钥匙"；
    // 给了别的字符串就当它是 license key，本进程不持有私钥。
    if !(key.is_empty() || key == "-" || key == "_") {
        return Ok(ServerKey {
            pk: key,
            sk: None,
            source: KeySource::Arg,
        });
    }

    // 是否把找到的公钥写回 key（"" 时不写，"-" / "_" 时写）
    let want_pk_as_license = !key.is_empty();

    // ---- 2) 本地文件优先（兼容既有部署） --------------------------------
    if let Some(secret) = load_from_file() {
        log::info!("服务端私钥来自本地文件 id_ed25519");
        let pk = base64::encode(secret.public_key());
        if db_on {
            // 回填，让其他实例共用这把钥匙（已存在则不动）
            put_if_absent(db, &secret).await;
        }
        return Ok(ServerKey {
            pk: if want_pk_as_license { pk } else { String::new() },
            sk: Some(secret),
            source: KeySource::File,
        });
    }

    // ---- 3) 数据库 -------------------------------------------------------
    if db_on {
        match load_from_db(db).await {
            Ok(Some(secret)) => {
                log::info!("服务端私钥来自数据库（多实例共享同一套 ID）");
                let pk = base64::encode(secret.public_key());
                // 走到这里说明文件不存在（否则第 2 步就命中了），
                // 补一份本地副本让 rdctl.py key 与既有脚本照常工作。
                crate::common::mirror_key_to_files(&pk, &secret);
                return Ok(ServerKey {
                    pk: if want_pk_as_license { pk } else { String::new() },
                    sk: Some(secret),
                    source: KeySource::Database,
                });
            }
            Ok(None) => {}
            Err(err) => log::warn!("读取数据库私钥失败，回退到文件/生成：{err}"),
        }
    }

    // ---- 4) 生成，然后同时落到文件与数据库 ------------------------------
    let (pk, sk) = crate::common::gen_sk(0)?;
    let mut secret = match sk {
        Some(s) => s,
        None => {
            // 上游 gen_sk 恒返回 Some；走到这里说明上游行为变了，
            // 明确告警而不是静默降级，避免"以为有签名其实是空的"。
            log::error!("gen_sk 未返回私钥，服务端将以无私钥模式运行");
            return Ok(ServerKey {
                pk: if want_pk_as_license { pk } else { String::new() },
                sk: None,
                source: KeySource::Generated,
            });
        }
    };

    if db_on {
        put_if_absent(db, &secret).await;
        // 并发启动：多台实例可能同时各自生成一把钥匙。必须回读"赢家"
        // 那把，否则本实例会用一把没人认的孤儿钥匙 —— 表现为
        // "同一个客户端在不同实例上 ID 不一致"。
        if let Ok(Some(winner)) = load_from_db(db).await {
            secret = winner;
        }
    }

    let pk_out = if want_pk_as_license {
        base64::encode(secret.public_key())
    } else {
        String::new()
    };
    Ok(ServerKey {
        pk: pk_out,
        sk: Some(secret),
        source: KeySource::Generated,
    })
}

/// 从本地文件 `id_ed25519` 读私钥；不存在或不可解析返回 None。
fn load_from_file() -> Option<sign::SecretKey> {
    let contents = std::fs::read_to_string(PRIVATE_KEY_FILE).ok()?;
    let raw = base64::decode(contents.trim()).ok()?;
    // sodiumoxide 的 from_slice 返回 Option（长度不符时为 None），
    // 不是 Result —— 这里不要加 .ok()。
    sign::SecretKey::from_slice(&raw)
}

/// 从数据库读私钥；不存在返回 Ok(None)。
async fn load_from_db(db: &Database) -> ResultType<Option<sign::SecretKey>> {
    let Some(value) = db.get_config(database::CFG_PRIVATE_KEY).await? else {
        return Ok(None);
    };
    let raw = match base64::decode(value.trim()) {
        Ok(raw) => raw,
        Err(err) => {
            log::error!("数据库中的私钥不是合法 base64，已忽略：{err}");
            return Ok(None);
        }
    };
    match sign::SecretKey::from_slice(&raw) {
        Some(secret) => Ok(Some(secret)),
        None => {
            log::error!(
                "数据库中的私钥长度不是 {} 字节，已忽略",
                sign::SECRETKEYBYTES
            );
            Ok(None)
        }
    }
}

/// 把私钥（以及由它推导出的公钥）写进数据库，**已存在则不动**。
///
/// 写失败只告警：钥匙在库里是"共享能力"，不是本次启动的必要条件。
async fn put_if_absent(db: &Database, secret: &sign::SecretKey) {
    let pk = base64::encode(secret.public_key());
    let sk_b64 = base64::encode(secret);
    match db
        .put_config_if_absent(database::CFG_PRIVATE_KEY, &sk_b64)
        .await
    {
        Ok(true) => {
            // 公钥/来源只是私钥的冗余副本，覆盖无风险，方便运维直接查库核对
            allow_err!(db.set_config(database::CFG_PUBLIC_KEY, &pk).await);
            allow_err!(db.set_config(database::CFG_KEY_ORIGIN, &origin()).await);
            log::info!("服务端密钥已托管到数据库");
        }
        Ok(false) => {
            log::debug!("数据库中已存在服务端密钥，未覆盖");
        }
        Err(err) => log::warn!("服务端密钥写入数据库失败（不影响本次启动）：{err}"),
    }
}

/// 强制覆盖数据库中的密钥（`-k` 显式指定时使用）。
async fn overwrite(db: &Database, secret: &sign::SecretKey) {
    let pk = base64::encode(secret.public_key());
    let sk_b64 = base64::encode(secret);
    if let Err(err) = db.set_config(database::CFG_PRIVATE_KEY, &sk_b64).await {
        log::warn!("服务端密钥覆盖数据库失败（不影响本次启动）：{err}");
        return;
    }
    allow_err!(db.set_config(database::CFG_PUBLIC_KEY, &pk).await);
    allow_err!(db.set_config(database::CFG_KEY_ORIGIN, &origin()).await);
    log::info!("服务端密钥已覆盖写入数据库（来自 -k）");
}

/// 钥匙来源标记，形如 `hostname@1700000000`。
///
/// 用 `fallible::hostname()` 而不是 `hostname()`：后者在取不到主机名时
/// 内部直接 panic。密钥托管只是记账用途，不该因为一次 hostname 查询失败
/// 就把整个 hbbs 带崩。
fn origin() -> String {
    let host = whoami::fallible::hostname().unwrap_or_else(|_| "unknown".to_owned());
    let ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or_default();
    format!("{host}@{ts}")
}
