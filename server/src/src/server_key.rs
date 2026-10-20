// =============================================================================
// 服务端密钥（id_ed25519 / id_ed25519.pub）的解析 —— 只认文件，不碰数据库
// =============================================================================
//
// 上游行为
// --------
// 上游把密钥当成**进程工作目录下的两个文件**：存在就用，不存在就生成。
// 单机部署没问题，但横向扩容 / 无状态容器场景下每台各生成一把私钥，
// 客户端拿到的服务端公钥来回变，表现为"时好时坏、偶尔报密钥不匹配"。
//
// 演进（2026-10-20 起）
// --------------------
// 曾用 PostgreSQL `server_config` 表托管密钥来解决多实例共享，但私钥进库
// 意味着**任何能读这个库的人（DBA、备份、拖库）都能拿到服务端私钥**，
// 进而可以签发任意客户端密钥。现改为 docker secrets 标准的文件挂载方式：
//
//   docker run ... -v /d/Git/secrets/id_ed25519:/run/secrets/id_ed25519:ro
//
// 解析顺序（先命中先用）
// ----------------------
//   1. 命令行 `-k/--key` 给的 64 字节私钥 —— 最高优先级（不落盘，仅本次生效）。
//   2. `KEY_FILE` 环境变量指定的私钥文件（显式声明，如 /run/secrets/id_ed25519）。
//   3. docker secrets 约定路径 `/run/secrets/id_ed25519`（标准挂载点）。
//   4. 工作目录 `id_ed25519`（上游行为，兼容既有部署）。
//   5. 旧版数据库托管的一次性迁移：库里还有私钥 → 写回工作目录文件，
//      随即**把密钥行从库里删除**。多实例共享请改用统一挂载同一 secret 文件。
//   6. 都没有 → 就地生成（只写文件，与上游一致）。
//
// 文件格式：与上游 `id_ed25519` 完全一致 —— 64 字节私钥的 base64 文本。
// =============================================================================

use crate::common::PRIVATE_KEY_FILE;
use crate::database::{self, Database};
use hbb_common::{log, ResultType};
use sodiumoxide::crypto::sign;

/// docker secrets 的标准挂载目录（容器内，只读挂载）。
const DOCKER_SECRET_DIR: &str = "/run/secrets";
/// 显式指定私钥文件路径的环境变量。
pub const KEY_FILE_ENV: &str = "KEY_FILE";

/// 密钥来自哪里。只用于启动日志，但对排障很值钱：
/// "ID 突然全变了" 往往对应这里从 file 变成了 generated。
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum KeySource {
    /// 命令行 `-k/--key`
    Arg,
    /// 私钥文件（KEY_FILE / /run/secrets / 工作目录）
    File,
    /// 旧版数据库托管迁移而来（已从库中删除）
    Migrated,
    /// 本次新生成
    Generated,
}

impl KeySource {
    pub fn as_str(self) -> &'static str {
        match self {
            KeySource::Arg => "命令行 -k",
            KeySource::File => "私钥文件",
            KeySource::Migrated => "旧库迁移（已从数据库删除）",
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

/// 解析服务端密钥。`key_arg` 即 `hbbs -k` 的值（未指定时上游传 `-`）。
///
/// 与上游 `get_server_sk` 的语义保持一致，只在其上叠加文件路径扩展与
/// 旧库一次性迁移。**全程不向数据库写入任何密钥材料。**
pub async fn resolve(key_arg: &str, db: &Database) -> ResultType<ServerKey> {
    let mut key = key_arg.to_owned();

    // ---- 1) `-k` 给的是完整私钥 -----------------------------------------
    if let Ok(raw) = base64::decode(key.trim()) {
        if raw.len() == sign::SECRETKEYBYTES {
            log::info!("The key is a crypto private key");
            // 后 32 字节就是公钥，上游拿它当 license key 的取值
            key = base64::encode(&raw[(sign::SECRETKEYBYTES / 2)..]);
            let mut tmp = [0u8; sign::SECRETKEYBYTES];
            tmp[..].copy_from_slice(&raw);
            let secret = sign::SecretKey(tmp);
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

    // ---- 2/3/4) 依次尝试候选私钥文件 -------------------------------------
    for candidate in candidate_key_files() {
        if let Some(secret) = load_sk_from_path(&candidate) {
            log::info!("服务端私钥来自文件 {}", candidate.display());
            return Ok(ServerKey {
                pk: if want_pk_as_license {
                    base64::encode(secret.public_key())
                } else {
                    String::new()
                },
                sk: Some(secret),
                source: KeySource::File,
            });
        }
    }

    // ---- 5) 旧版数据库托管的一次性迁移 -----------------------------------
    // 老版本把私钥存进了 server_config 表；升级后库里可能还留着。
    // 读出来写回工作目录文件，然后立刻从库里删掉 —— 库里不再保存任何密钥。
    match migrate_from_db(db).await {
        Ok(Some(secret)) => {
            let pk = base64::encode(secret.public_key());
            return Ok(ServerKey {
                pk: if want_pk_as_license { pk } else { String::new() },
                sk: Some(secret),
                source: KeySource::Migrated,
            });
        }
        Ok(None) => {}
        Err(err) => log::warn!("读取旧版数据库密钥失败，跳过迁移：{err}"),
    }

    // ---- 6) 生成，只落文件 ------------------------------------------------
    let (pk, sk) = crate::common::gen_sk(0)?;
    let sk = match sk {
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
    let pk_out = if want_pk_as_license {
        base64::encode(sk.public_key())
    } else {
        String::new()
    };
    Ok(ServerKey {
        pk: pk_out,
        sk: Some(sk),
        source: KeySource::Generated,
    })
}

/// 候选私钥文件，按优先级返回（存在性由调用方逐个尝试）。
/// 1. `KEY_FILE` 环境变量指定的路径
/// 2. docker secrets 约定路径 `/run/secrets/id_ed25519`
/// 3. 工作目录 `id_ed25519`（上游行为）
fn candidate_key_files() -> Vec<std::path::PathBuf> {
    let mut candidates = Vec::new();
    if let Ok(path) = std::env::var(KEY_FILE_ENV) {
        let path = path.trim().to_owned();
        if !path.is_empty() {
            candidates.push(std::path::PathBuf::from(path));
        }
    }
    candidates.push(std::path::Path::new(DOCKER_SECRET_DIR)
        .join(PRIVATE_KEY_FILE));
    candidates.push(std::path::PathBuf::from(PRIVATE_KEY_FILE));
    candidates
}

/// 从指定路径读私钥；不存在或不可解析返回 None。
fn load_sk_from_path(path: &std::path::Path) -> Option<sign::SecretKey> {
    let contents = std::fs::read_to_string(path).ok()?;
    let raw = base64::decode(contents.trim()).ok()?;
    // sodiumoxide 的 from_slice 返回 Option（长度不符时为 None），
    // 不是 Result —— 这里不要加 .ok()。
    sign::SecretKey::from_slice(&raw)
}

/// 旧版数据库托管的一次性迁移。
///
/// - 库里没有私钥 → Ok(None)，安静返回；
/// - 有 → 写回工作目录 `id_ed25519`（绝不覆盖已有文件），写成功后
///   **把私钥/公钥/来源三行全部从 server_config 删除**；
/// - 文件写不进去（只读 fs 等）→ 仍在内存里用这把钥匙，但**不删库**，
///   留给下次有写权限时再迁移。
async fn migrate_from_db(db: &Database) -> ResultType<Option<sign::SecretKey>> {
    let Some(value) = db.get_config(database::CFG_PRIVATE_KEY).await? else {
        return Ok(None);
    };
    let secret = match base64::decode(value.trim())
        .ok()
        .and_then(|raw| sign::SecretKey::from_slice(&raw))
    {
        Some(secret) => secret,
        None => {
            log::error!("数据库中的旧私钥不是合法的 64 字节 base64，已忽略并从库中清除");
            // 内容已经损坏，留着也是垃圾数据，顺手清掉
            let _ = db.remove_config(database::CFG_PRIVATE_KEY).await;
            return Ok(None);
        }
    };
    let pk = base64::encode(secret.public_key());
    log::warn!(
        "检测到旧版密钥托管：私钥仍在数据库 server_config 表中，开始迁移到文件（迁移完成后将删除库中密钥）"
    );
    match crate::common::create_private_key_file(
        PRIVATE_KEY_FILE,
        base64::encode(&secret).as_bytes(),
    ) {
        Ok(()) => {
            log::info!("私钥已迁移到本地文件 {PRIVATE_KEY_FILE}");
            for cfg in [
                database::CFG_PRIVATE_KEY,
                database::CFG_PUBLIC_KEY,
                database::CFG_KEY_ORIGIN,
            ] {
                if let Err(err) = db.remove_config(cfg).await {
                    log::error!("从数据库删除密钥行 {cfg} 失败，请手动清理：{err}");
                }
            }
            log::warn!("数据库中的密钥已删除；此后私钥只存于文件，请按 docker secrets 方式管理");
        }
        Err(err) => {
            log::error!(
                "私钥写回文件 {PRIVATE_KEY_FILE} 失败（{err}）。\
                 本次启动继续使用该密钥，但数据库中的副本暂不删除，下次启动重试迁移"
            );
        }
    }
    log::info!("服务端公钥: {pk}");
    Ok(Some(secret))
}
