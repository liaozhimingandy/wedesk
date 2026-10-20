// =============================================================================
// 健康检查 HTTP 接口 —— 供实时监控 / docker healthcheck / 负载均衡探针使用
// =============================================================================
//
// hbbs 与 hbbr 共用本模块（hbbr 通过 `mod health;` 引入同一份文件）。
//
// 端口
// ----
//   hbbs 默认 = 主端口 + 4（5187 → 5191）
//   hbbr 默认 = 主端口 + 2 + 2（5188 中继 → 5192）
//   与既有"端口由主端口推导"的惯例一致。可用 `--health-port <N>` 或
//   环境变量 `HEALTH_PORT` 覆盖；设 0 表示关闭健康检查服务。
//
// 接口
// ----
//   GET /health（或 /）→ 200
//     {"service":"hbbs","status":"ok","version":"...","uptime_secs":123,
//      "timestamp":1760000000,"database":{"backend":"postgres","ok":true}}
//   探活失败（如数据库连不上）→ 503，status 变 "degraded"
//   其他路径 → 404
//
// 实现
// ----
// 刻意不引入 HTTP 框架：监控探针只需要一个能回 JSON 的 TCP 端口。
// 每个连接最多读 8 KB 请求头就回包，不解析 body，Connection: close。
// =============================================================================

use hbb_common::{log, tokio, ResultType};
use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

/// 额外探针：返回 (JSON 片段, 是否健康)。
/// JSON 片段直接拼进响应体（调用方保证逗号位置），如
/// `"database":{"backend":"postgres","ok":true}`。
pub type ProbeFn = Arc<
    dyn Fn() -> Pin<Box<dyn Future<Output = (String, bool)> + Send>> + Send + Sync + 'static,
>;

/// 解析健康检查端口：`--health-port` / `HEALTH_PORT` / 默认值；0 = 关闭。
pub fn resolve_port(name: &str, default_port: u16) -> Option<u16> {
    let raw = crate::common::get_arg_or(name, default_port.to_string());
    match raw.trim().parse::<u16>() {
        Ok(0) => None,
        Ok(p) => Some(p),
        Err(_) => {
            log::warn!("health-port {raw} 不合法，健康检查已禁用");
            None
        }
    }
}

/// 启动健康检查 HTTP 服务（后台 task）。绑定 0.0.0.0:port。
/// 必须在 tokio runtime 内调用。
pub fn start(port: u16, service: &'static str, probe: Option<ProbeFn>) {
    tokio::spawn(async move {
        if let Err(err) = run(port, service, probe).await {
            log::error!("健康检查服务退出：{err}");
        }
    });
}

async fn run(port: u16, service: &'static str, probe: Option<ProbeFn>) -> ResultType<()> {
    let listener = TcpListener::bind(("0.0.0.0", port)).await?;
    log::info!("健康检查 HTTP 服务监听 :{port} (GET /health)");
    let started = Instant::now();
    loop {
        let (stream, _) = listener.accept().await?;
        let probe = probe.clone();
        tokio::spawn(async move {
            if let Err(err) = handle_conn(stream, service, started, probe).await {
                log::debug!("health connection error: {err}");
            }
        });
    }
}

async fn handle_conn(
    mut stream: tokio::net::TcpStream,
    service: &'static str,
    started: Instant,
    probe: Option<ProbeFn>,
) -> ResultType<()> {
    // 请求头足够小，一次读取即可；读不满也无所谓，POST body 我们不关心。
    let mut buf = [0u8; 8 * 1024];
    let n = stream.read(&mut buf).await?;
    let req = String::from_utf8_lossy(&buf[..n]);
    // "GET /health HTTP/1.1" → 取中间的 path，再去掉 query
    let path = req
        .split_whitespace()
        .nth(1)
        .unwrap_or("")
        .split('?')
        .next()
        .unwrap_or("");

    let (extra_json, extra_ok) = match &probe {
        Some(p) => p().await,
        None => (String::new(), true),
    };

    if path != "/health" && path != "/" {
        respond(
            &mut stream,
            404,
            r#"{"error":"not found","hint":"GET /health"}"#,
        )
        .await?;
        return Ok(());
    }

    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or_default();
    let status = if extra_ok { "ok" } else { "degraded" };
    let mut body = format!(
        r#"{{"service":"{service}","status":"{status}","version":{},"uptime_secs":{},"timestamp":{ts}"#,
        jstr(env!("CARGO_PKG_VERSION")),
        started.elapsed().as_secs(),
    );
    if !extra_json.is_empty() {
        body.push(',');
        body.push_str(&extra_json);
    }
    body.push('}');

    let code = if extra_ok { 200 } else { 503 };
    respond(&mut stream, code, &body).await?;
    Ok(())
}

async fn respond(stream: &mut tokio::net::TcpStream, code: u16, body: &str) -> ResultType<()> {
    let status = match code {
        200 => "OK",
        404 => "Not Found",
        _ => "Service Unavailable",
    };
    let resp = format!(
        "HTTP/1.1 {code} {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    stream.write_all(resp.as_bytes()).await?;
    stream.flush().await?;
    Ok(())
}

/// 把字符串转成带双引号转义的 JSON 字面量（版本号等简单场景够用）。
fn jstr(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            _ => out.push(c),
        }
    }
    out.push('"');
    out
}

// 供健康检查调用方使用的超时长度：探针超过这个时间没返回就视为不健康。
pub const PROBE_TIMEOUT: Duration = Duration::from_secs(2);
