# 10 · 端口与网络规划

> 面向：**需要把 wedesk 服务暴露给不同网络、或者反感"默认端口被扫"的部署场景**。
> 只想内网跑起来、不改任何默认值的人，看完第 1 节就够了。

---

## 1. 一句话结论

**对外只需要 3 个端口，全部在 hbbs + hbbr 所在的那一台机器上：**

| 端口 | 协议 | 谁在用 | 必须开吗 |
|---|---|---|---|
| **21115** | TCP | hbbs — NAT 类型探测 | 是 |
| **21116** | TCP **+ UDP** | hbbs — ID 注册 / 心跳 / TCP 打洞 | 是（**两种协议都要**） |
| **21117** | TCP | hbbr — 中继转发 | 是 |

客户端不需要任何固定的入站端口。员工机器只需要能**出站**访问上面三个端口。

`server/docker-compose.yml` 默认就只 publish 这三个 —— 直接用即可，不用改。

---

## 2. 端口全景（含"藏起来的"端口）

RustDesk 服务端的端口不是一列固定值，而是**从主端口推导出来的**。这是本文档最需要记住的一点。

### 2.1 推导规则

设 `P` = hbbs 主端口（默认 21116），`R` = hbbr 主端口（默认 21117）：

```
hbbs  →  UDP  P          ID 注册、心跳、UDP 打洞
        TCP  P          TCP 打洞协调 / rendezvous 主连接
        TCP  P - 1      NAT 类型探测（额外端口）
        TCP  P + 2      WebSocket 监听（自动开，无开关）
        TCP  127.0.0.1:P-1   管理 console（仅当 bind 到指定 IP 时）

hbbr  →  TCP  R          中继主端口
        TCP  R + 2      WebSocket 中继（自动开，无开关）
        TCP  127.0.0.1:R     管理 console（仅当 bind 到指定 IP 时）
```

源码依据（vendored 在 `server/rustdesk-server/`）：

- `src/rendezvous_server.rs:134-180` — `nat_port = port - 1` / `ws_port = port + 2`
- `src/relay_server.rs:85-95` — `port2 = port + 2`
- `src/common.rs:console_addr()` — 判断要不要额外绑一个 loopback console

**推论**：你把 hbbs 的主端口改成 `-p 9000`，它实际会占用 **UDP 9000 + TCP 9000 + TCP 8999 + TCP 9002** 共四个云原生端口。
规划防火墙时如果只算了 9000，客户端会卡在"连接中"然后超时。

### 2.2 完整清单

| 端口 | 协议 | 进程 | 默认 | 用途 | 建议是否对外 |
|---|---|---|---|---|---|
| 21114 | TCP | — | 未监听 | 只出现在 `rustdesk-utils doctor` 的检查列表里，OSS 版不监听（Pro 的 API 用） | 否 |
| **21115** | TCP | hbbs | 开 | NAT 类型探测 | ✅ 是 |
| **21116** | TCP | hbbs | 开 | TCP 打洞与 rendezvous 主连接 | ✅ 是 |
| **21116** | UDP | hbbs | 开 | ID 注册 + 心跳 + UDP 打洞 | ✅ 是 |
| **21117** | TCP | hbbr | 开 | 中继转发画面/键鼠数据 | ✅ 是 |
| 21118 | TCP | hbbs | **容器内默认监听** | WebSocket rendezvous | ❌ 否（详见第 3 节） |
| 21119 | TCP | hbbr | **容器内默认监听** | WebSocket 中继 | ❌ 否（详见第 3 节） |

> 21118/21119 之所以"看不到"，是因为 compose **没有把它们 publish 出来**，不是进程没开。
> `docker exec rd-hbbs ss -ltnp` 能看到它们在容器里听着。

---

## 3. 三个必须先知道的坑

### 坑 1：WebSocket 端口受 trust 反向代理头 —— 默认全 trust

`src/relay_server.rs` 的 WebSocket 分支会读 `X-Real-IP` / `X-Forwarded-For`，**并且直接信任、不做任何来源校验**。
任何能直连该端口的人都能伪造 IP，从而：

- 绕过基于 IP 的限流与封禁；
- 把你审计日志里的来源 IP 写成别人。

源码注释自己都写了 *"Do not expose the WebSocket port directly to untrusted networks"*（上游 issue #634）。

所以：

- **保持 compose 现状**（不 publish 21118/21119）；
- 真要用 Web 客户端，就让反向代理去连容器的 21118/21119，由代理**覆写**这两个头，再用防火墙只允许代理 IP 访问；
- **一旦改用 `network_mode: host`**，容器内外网络栈合一，21118/21119 会直接对外 —— 切之前先在 iptables / 安全组 DROP 掉这两个端口。`docker-compose.yml` 末尾注释里已经补了这条。

### 坑 2：`hbbr` 的环境变量 `PORT` 会 +1

```rust
// server/rustdesk-server/src/hbbr.rs
let mut port = RELAY_PORT;                       // 21117
if let Some(v) = common::get_arg_opt("PORT") {   // ← 注意：读的是环境变量
    let v: i32 = v.parse().unwrap_or_default();
    if v > 0 { port = v + 1; }                   // ← +1 ！
}
```

这是给官方 docker-compose 的遗留设计：一个 `.env` 里写 `PORT=21116`，hbbs 用 21116、hbbr 自动变成 21117。

对本仓库的影响：

- **只有 `-p` / `--port` 命令行参数是字面值**，`PORT`/`HBBR_PORT` 环境变量会触发 +1；
- 本仓库的 compose 里 hbbr 用的是 `command: hbbr`，没传 `PORT`，所以走默认 21117，**没问题**；
- 但你**千万别**为了"统一端口变量"给 hbbr 加 `- PORT=21117` —— 它会去监听 21118，正好撞上 hbbs 的 WebSocket 端口。

### 坑 3：改主端口要连带改 4 个地方

端口不是只改服务端就好。完整清单：

| # | 位置 | 改什么 |
|---|---|---|
| 1 | `server/.env.example` → `.env` | `HBBS_NAT` / `HBBS_TCP` / `HBBS_UDP` / `HBBR_TCP` / `RELAY_PORT` |
| 2 | `server/docker-compose.yml` | `\`hbbs -r ...\` 里的 relay 端口；`ports:` 容器侧 |
| 3 | 服务端命令行 | `hbbs -p P`、`hbbr -p R` |
| 4 | **客户端 `build/custom.toml`** | `rendezvous_host = "wedesk.alsoapp.com:P"`、`relay_host = "wedesk.alsoapp.com:R"`，**然后重新编译客户端** |

第 4 点最容易漏：客户端碰到不带端口的域名时，兜底用 `RENDEZVOUS_PORT`(21116) 和 `RELAY_PORT`(21117)。
服务端改了端口而客户端没改，现象是"ID 一直转圈，最后超时"，看日志还以为是鉴别失败。

---

## 4. 推荐规划方案

### 方案 A：内网 / 专线 —— 什么都不改（推荐起点）

域名 `wedesk.alsoapp.com` 在内网 DNS 指向 hbbs/hbbr 所在机器，端口保持默认。
客户端 `rendezvous_host = "wedesk.alsoapp.com"`、`relay_host = "wedesk.alsoapp.com:21117"`。

**这是当前 `build/custom.toml` 里已经配好的状态。**

优点：零变量；出问题能直接对照网上绝大多数排查经验；Day-2 换人维护不用解释"为什么端口不是默认的"。
缺点：默认端口会被互联网上的自动化脚本批量扫到 —— 但本方案只暴露在内网/专线，
或经安全组限制来源 IP，因此不构成问题。

### 方案 B：公网部署 —— 换成非默认端口段 + 来源限制

如果 `wedesk.alsoapp.com` 要解析到公网 IP，建议整体平移到一个统一端口段：
既错开自动化脚本对默认端口段的扫描，也便于集中写防火墙规则。

```
hbbs 主端口 P = 9100   → 开放：UDP 9100、TCP 9100、TCP 9099
                        → 隐藏：TCP 9102 (WS)

hbbr 主端口 R = 9101   → 开放：TCP 9101
                        → 隐藏：TCP 9103 (WS)
```

注意这里刻意**没有**沿用"hbbs 与 hbbr 端口相邻"的直觉：9100 的派生端口是 9099 和 9102，
与 hbbr 的 9101、9103 交错在同一个小段里。排布端口时请先按第 2.1 节的公式
把**全部派生端口**列出来再定主端口，避免"看着没冲突、实际派生号撞车"。

与之配套：

```env
# server/.env
HBBS_NAT=9099
HBBS_TCP=9100
HBBS_UDP=9100
HBBR_TCP=9101
RELAY_PORT=9101
```

```toml
# build/custom.toml —— 改完必须重新编译客户端
rendezvous_host = "wedesk.alsoapp.com:9100"
relay_host      = "wedesk.alsoapp.com:9101"
```

### 方案 C：hbbs 与 hbbr 分机

中继流量大时可以拆开两台机器。这时 `hbbs` 启动时要用 `-r` 明确告诉客户端中继在哪：

```bash
hbbs -p 21116 -r hbbr.internal:21117     # 或写成公网域名 wedesk.alsoapp.com:21117
```

并在 hbbs 侧放行 UDP 21116 + TCP 21115/21116，hbbr 侧只放行 TCP 21117。
客户端配置不变（仍然指向 hbbs 的域名）。

---

## 5. 客户端侧的网络要求

| 方向 | 要求 | 说明 |
|---|---|---|
| 出站 | TCP 21115 / TCP 21116 / UDP 21116 → `wedesk.alsoapp.com` | 注册、心跳、打洞协调 |
| 出站 | TCP 21117 → `wedesk.alsoapp.com` | P2P 失败时走中继 |
| 入站 | **无固定端口** | P2P 用的是随机临时端口，NAT 会话自动放行 |
| P2P 直连 | 依赖对方 NAT 类型 | 对称 NAT ↔ 对称 NAT 必然走中继，这是协议限制而非配置问题 |

如果员工网络只允许访问白名单域名，只需在出口白名单里放行 `wedesk.alsoapp.com` 的上述三个端口
—— 客户端**不会**再访问任何 rustdesk 官方域名（见 [05 定制与断网](05-customize.md)）。

> **不建议**在 hbbs 前面套通用 CDN / HTTP 反向代理：21116 的 **UDP** 无法被普通七层代理转发，
> 而它是 ID 注册与心跳的生命线。非要隐藏端口就用 DNAT / 端口转发，别用 HTTP 代理。

---

## 6. 验证

服务端二进制自带排障器，会从客户端视角逐个探测端口：

```bash
rustdesk-utils doctor wedesk.alsoapp.com
```

它依次检查 `rustdesk-utils/src/utils.rs` 里那张表：API(21114) → NAT(21115) → hbbs(21116) → hbbr(21117) → WS(21118/21119)。
**21114 报失败是正常的**（OSS 版不提供 API），其余四项应全部 OK。

人工核对外是否被扫到 / 是否被误 publish：

```bash
nmap -sU -sT -p 21115-21119 wedesk.alsoapp.com
# 期望：21115/tcp open、21116/tcp open、21116/udp open|filtered、21117/tcp open
#       21118、21119 必须 closed / filtered
```

---

## 7. 相关文档

- [04 部署指南](04-deploy.md) — 端口放行的部署上下文、`.env` 完整说明
- [09 服务端自编译](09-server-build.md) — 二进制、镜像怎么来
- [05 定制与断网](05-customize.md) — 客户端兜底地址与关闭官方回连
