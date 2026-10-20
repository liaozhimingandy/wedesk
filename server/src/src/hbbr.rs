use clap::App;
mod common;
mod relay_server;
use flexi_logger::*;
use hbb_common::{config::RELAY_PORT, log, ResultType};
use relay_server::*;
mod version;
mod health;
// hbbr 只在一件事上用到数据库：把黑名单 / 阻断名单持久化下来。
// （上游把它们放在内存里、重启即丢 —— 而封禁恰恰最需要跨重启保留。）
// 没配 DB_TYPE/DB_URL/PG_* 时完全不连库，行为与上游一致。
// allow(dead_code)：database.rs 是 hbbs 与 hbbr 共用的同一份文件，hbbr 只用
// 到其中名单那几个方法，其余（peer / 审计 / 中继节点）对它都是死代码 ——
// 不加这条每次编 hbbr 都会刷十几条 dead_code 警告，把真正的告警淹掉。
#[allow(dead_code)]
mod database;

fn main() -> ResultType<()> {
    let _logger = Logger::try_with_env_or_str("info")?
        .log_to_stdout()
        .format(opt_format)
        .write_mode(WriteMode::Async)
        .start()?;
    let args = format!(
        "-b, --bind=[IP] 'Sets the IP address to bind to (default: all interfaces)'
        -p, --port=[NUMBER(default={RELAY_PORT})] 'Sets the listening port'
        -k, --key=[KEY] 'Only allow the client with the same key'
        ",
    );
    let matches = App::new("hbbr")
        .version(version::VERSION)
        .author("Purslane Ltd. <info@rustdesk.com>")
        .about("RustDesk Relay Server")
        .args_from_usage(&args)
        .get_matches();
    if let Ok(v) = ini::Ini::load_from_file(".env") {
        if let Some(section) = v.section(None::<String>) {
            section.iter().for_each(|(k, v)| common::set_arg(k, v));
        }
    }
    // hbbr 只在"名单持久化"一件事上用数据库（详见 relay_server.rs），
    // 其余（ID 台账、审计）一概不碰。但镜像默认 CMD 是 hbbs，运维经常
    // docker run 时忘写 hbbr 命令而启动了 hbbs，然后对着"DB_URL 未设置 /
    // DB_TYPE=postgres…"的日志困惑。这里把两种可能一次说清。
    let db_keys = ["DB_TYPE", "DB_URL", "PG_HOST", "PG_USER", "PG_PASSWORD", "PG_DB"];
    let db_cfg: Vec<&str> = db_keys
        .iter()
        .filter(|k| !common::get_arg(k).is_empty())
        .cloned()
        .collect();
    if !db_cfg.is_empty() {
        log::info!(
            "检测到数据库配置 {}：hbbr 只用它持久化黑名单 / 阻断名单，\
             不写 ID 台账与审计表。如果你期望启动的是 hbbs（ID 注册服务器），\
             请检查容器命令 —— docker run 不带命令时镜像默认启动 hbbs，\
             跑中继必须显式指定 hbbr",
            db_cfg.join(", ")
        );
    }
    let mut port = RELAY_PORT;
    if let Some(v) = common::get_arg_opt("PORT") {
        let v: i32 = v.parse().unwrap_or_default();
        if v > 0 {
            port = v + 1;
        }
    }
    let bind = matches
        .value_of("bind")
        .map(str::to_owned)
        .unwrap_or_else(|| common::get_arg("BIND"));
    let bind_addr = common::parse_bind_address(&bind)?;
    let key = matches
        .value_of("key")
        .map(str::to_owned)
        .unwrap_or_else(|| common::get_arg("KEY"));
    start_with_bind(
        bind_addr,
        matches.value_of("port").unwrap_or(&port.to_string()),
        &key,
    )?;
    Ok(())
}
