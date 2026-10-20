use clap::App;
mod common;
mod relay_server;
use flexi_logger::*;
use hbb_common::{config::RELAY_PORT, log, ResultType};
use relay_server::*;
mod version;
mod health;

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
    // hbbr（中继）完全不使用数据库。但镜像默认 CMD 是 hbbs，运维经常把
    // 数据库配置挂在中继容器上、或 docker run 时忘写 hbbr 命令启动了 hbbs，
    // 然后对着"DB_URL 未设置/DB_TYPE=postgres…"的日志困惑。这里主动出声：
    // 检测到数据库相关配置就说明 hbbr 会忽略它们，并提示怎么确认自己是谁。
    let db_keys = ["DB_TYPE", "DB_URL", "PG_HOST", "PG_USER", "PG_PASSWORD", "PG_DB"];
    let db_cfg: Vec<&str> = db_keys
        .iter()
        .filter(|k| !common::get_arg(k).is_empty())
        .cloned()
        .collect();
    if !db_cfg.is_empty() {
        log::info!(
            "hbbr（中继服务器）不使用数据库，忽略配置: {}（它们只对 hbbs 生效）。\
             如果你期望启动的是 hbbs（ID 注册服务器），请检查容器命令 —— \
             docker run 不带命令时镜像默认启动 hbbs，跑中继必须显式指定 hbbr",
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
