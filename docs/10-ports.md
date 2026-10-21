# 10 · 端口与网络规划

> 面向：**要把 wedesk 服务暴露给不同网络、或者反感"默认端口被扫"的部署场景**。  
> 只想照默认值跑起来的人，看完第 1 节就够了。



---

## 1. 一句话结论

**端口是固定的一套，全部写死在 `server/deploy/docker-compose.yml` 里，不做任何 +1 / +2 推导。**

服务端对外需要的端口（全部在 hbbs + hbbr 所在的那台机器上）：

| 端口       | 协议  | 进程   | 用途                         | 对外           |
| -------- | --- | ---- | -------------------------- | ------------ |
| **5186** | TCP | hbbs | NAT 类型探测                   | ✅ 必须         |
| **5187** | TCP | hbbs | 注册 / TCP 打洞与连接             | ✅ 必须         |
| **5187** | UDP | hbbs | 注册 + 心跳 + UDP 打洞           | ✅ 必须（单独一条映射） |
| **5188** | TCP | hbbr | 中继转发                       | ✅ 必须         |
| **5189** | TCP | hbbs | WebSocket 注册通道（UDP 不通时走这里） | ✅ 必须         |
| **5190** | TCP | hbbr | WebSocket 中继               | ✅ 必须         |
| 5191     | TCP | hbbs | 健康检查 `GET /health`         | 建议（外部监控用）    |
| 5192     | TCP | hbbr | 健康检查 `GET /health`         | 建议（外部监控用）    |

防火墙放行清单（可直接照抄）：

```
TCP 5186, 5187, 5188, 5189, 5190, 5191, 5192
UDP 5187
```

> **2026-10-20 排障结论：5189/5190 由"刻意不 publish"改为默认 publish。**  
> 客户端在 UDP 不通、或用户在设置里勾了"使用 WebSocket"时，会改用 `P+2` 这个  
> WS 端口注册。该端口外部不可达时，客户端**不会**退回 UDP（只有"用户主动勾选  
> WS"这一条路径才有回退，见 `client/src/rendezvous_mediator.rs` 的 `start()`），  
> 于是永远卡在"正在接入网络"，日志循环打印：
>
> ```
> start tcp: ws://192.168.0.105:5189
> rendezvous mediator error: deadline has elapsed
> ```
>
> 可用性损失远大于伪造 IP 的风险（后者的前提是你前面挂了反向代理，见坑 1）。

还有一个端口是**客户端自己推导出来的**：

| 端口   | 协议  | 谁在用                                        | 对外？       |
| ---- | --- | ------------------------------------------ | --------- |
| 5185 | TCP | 管理后台 admin（FastAPI）：客户端按 `P-2` 推导或由 `api_server` 编译注入（心跳 / 设备台账 / 地址簿 / 登录 / 审计） | ✅ 已由 compose 的 admin 服务提供 |

自建部署现在**有**这份 API 服务：`server/deploy/docker-compose.yml` 的 `admin`
服务监听 5185，对外地址 `admin.wedesk.alsoapp.com:5185`（DNS 解析到该机器）。
定制客户端把 api-server 编译为该地址（`build/client/custom.toml` 的
`server.api_server`）。若 admin 服务未部署，客户端日志仍会出现
`Heartbeat failed: ... TimedOut`，连续 3 次后自动停搏。

客户端不需要任何固定的入站端口。员工机器只需要能**出站**访问上面这些端口。

`server/deploy/docker-compose.yml` 里这 8 条映射全部写死成数字，直接用即可，不用改。

---

## 2. 端口全景与源码里的硬编码关系

> ⚠️ **写代码/改配置时不要用"主端口 +1 / +2"去推端口。** 部署用的那套数字是固定的  
> （第 1 节那张表），compose 里也是写死的。下面这一节讲的是**源码内部**的硬编码关系，  
> 只在你想整体平移端口、或排查"为什么多占了端口"时才需要看。

### 2.1 源码内部的派生关系（理解用，不是配置方式）

设 `P` = hbbs 主端口（本仓库取 **5187**），`R` = hbbr 主端口（本仓库取 **5188**）：

```
hbbs  →  UDP  P           ID 注册、心跳、UDP 打洞
         TCP  P           TCP 打洞协调 / rendezvous 主连接
         TCP  P - 1       NAT 类型探测（额外端口，无法关闭）
         TCP  P + 2       WebSocket 监听（自动开，无开关）
         TCP  127.0.0.1:P-1    管理 console（仅当 bind 到指定 IP 时）

hbbr  →  TCP  R           中继主端口
         TCP  R + 2       WebSocket 中继（自动开，无开关）
         TCP  127.0.0.1:R      管理 console（仅当 bind 到指定 IP 时）
```

源码依据（vendored 在 `server/src/`）：

- `src/rendezvous_server.rs` 的 `start_with_bind()` — `nat_port = port - 1` / `ws_port = port + 2`
- `src/relay_server.rs` 的 `start_with_bind()` — `let port2 = port + 2`
- `src/common.rs:console_addr()` — 判断要不要额外绑一个 loopback console

**推论**：把 hbbs 主端口改成 `-p 9000`，它实际占用 **UDP 9000 + TCP 9000 + TCP 8999 + TCP 9002** 共四个端口。  
规划防火墙时只算 9000 的话，客户端会卡在"连接中"然后超时。

### 2.2 完整清单（含健康检查与那个"不存在的"端口）

| 端口   | 协议  | 进程        | 状态           | 用途                                 | 是否对外         |
| ---- | --- | --------- | ------------ | ---------------------------------- | ------------ |
| 5186 | TCP | hbbs      | 监听 + publish | NAT 类型探测                           | ✅ 是          |
| 5187 | TCP | hbbs      | 监听 + publish | 注册 / TCP 打洞与连接                     | ✅ 是          |
| 5187 | UDP | hbbs      | 监听 + publish | 注册 + 心跳 + UDP 打洞                   | ✅ 是          |
| 5188 | TCP | hbbr      | 监听 + publish | 中继转发画面/键鼠数据                        | ✅ 是          |
| 5189 | TCP | hbbs      | 监听 + publish | WebSocket 注册通道                     | ✅ 是（见坑 1 前提） |
| 5190 | TCP | hbbr      | 监听 + publish | WebSocket 中继                       | ✅ 是（见坑 1 前提） |
| 5191 | TCP | hbbs      | 监听 + publish | 健康检查 `GET /health`                 | 建议           |
| 5192 | TCP | hbbr      | 监听 + publish | 健康检查 `GET /health`                 | 建议           |
| 5185 | TCP | admin（FastAPI） | 监听 + publish | 管理后台 API：心跳 / 设备台账 / 地址簿 / 登录 / 审计 | ✅ 是 |

> 5189/5190 是 hbbs/hbbr **进程无条件监听**的端口（没有开关能关）。  
> `docker exec rd-hbbs ss -ltnp` 能看到它们在容器里听着。  
> 5185 由独立的 admin 容器提供（compose 的 `admin` 服务），与 hbbs/hbbr 互不相干。

> **客户端自己还有一个端口：21118。**  
> 它来自 `client/libs/hbb_common/src/config.rs` 的 `RENDEZVOUS_PORT + 2`，是员工电脑上"IP 直连"  
> 功能的监听端口。**我们没有改这个常量** —— 改它会连带把服务器的 5189 也挤走。  
> 它与服务端端口互不相干，UI 里那个 `21118` 的示例数字是对的，不用跟着改成 5189。

---

## 3. 为什么恰好是 5187 / 5188 这一组

这几个端口号不是挑出来的，是三个约束挤出来的唯一解（**只在你想整体平移端口时才需要看**）：

**约束一：hbbs 的派生端口必须落在 5186–5189 里。**

```
NAT = P-1 ≥ 5186   →   P ≥ 5187
WS  = P+2 ≤ 5189   →   P ≤ 5187          ⟹  P = 5187
```

于是 5186（NAT）、5187（主端口）、5189（WS）自动就位，hbbr 只剩 5188 可用。

**约束二：hbbr 必须等于 hbbs + 1 —— 这不是审美，是客户端的兜底公式。**

客户端里 `get_relay_server()`（`client/src/rendezvous_mediator.rs`）的顺序是：

```rust
Config::get_option("relay-server")            // 用户手动指定
    → hbbs 握手时下发的 relay 地址             // 正常路径
    → increase_port(&self.host, 1)            // 兜底：hbbs 端口 + 1
```

也就是说，即使 hbbs 忘了 `-r` 参数，客户端也会去试 `hbbs端口 + 1` = 5188 —— 恰好是 hbbr。  
**把 hbbr 放到别的端口（比如 5190）而不给 hbbs 加 `-r`，中继握手必然失败**，  
且现象很迷惑：ID 正常、能注册，就是连不进桌面。

> 这正是 5187/5188 这对数字的价值：它保留了上游 `21116/21117` 原有的 "+1" 关系，  
> 让最常见的部署少一个配置点。换成 5187/5190 之类的组合，就必须处处记得加 `-r`。

**约束三：反过来的冲突要避开**：hbbr 的 WS 端口是 R+2 = 5190，它落在 5186–5189 **之外**。  
这是好事 —— 说明 5189 不会被 hbbr 的 WS 抢走，两个 WS 端口各自独立。

**日常部署不用改任何端口变量**：compose 里 8 条映射与两处 `-p` 全部写死。  
只有宿主端口冲突、需要整体平移时，才走 `rdrun.sh`（它接受变量并负责校验）：

```env
HBBS_PORT=5187
HBBS_NAT_PORT=5186     # 必须 = HBBS_PORT - 1
HBBR_PORT=5188         # 建议 = HBBS_PORT + 1
HBBS_WS_PORT=5189      # 必须 = HBBS_PORT + 2
HBBR_WS_PORT=5190      # 必须 = HBBR_PORT + 2
```

`rdctl.py start` 会校验 `HBBS_NAT_PORT == HBBS_PORT - 1` 与两个 WS 端口的 `+2` 关系，  
不满足直接拒绝启动。`rdctl.py ports` 打印当前这套固定端口表：

```
$ python rdctl.py ports
      5186  TCP    hbbs NAT 类型探测
      5187  TCP    hbbs 注册 / TCP 打洞与连接
      5187  UDP    hbbs 注册 + 心跳 + UDP 打洞
      5188  TCP    hbbr 中继数据
      5189  TCP    hbbs WebSocket 注册通道
      5190  TCP    hbbr WebSocket 中继
      5191  TCP    hbbs 健康检查 /health
      5192  TCP    hbbr 健康检查 /health
```

---

## 4. 四个必须先知道的坑

### 坑 1：WebSocket 端口受 trust 反向代理头 —— 默认全 trust

`src/relay_server.rs` 的 WebSocket 分支会读 `X-Real-IP` / `X-Forwarded-For`，**并且直接信任、不做任何来源校验**。  
任何能直连该端口的人都能伪造 IP，从而：

- 绕过基于 IP 的限流与封禁；
- 把你审计日志里的来源 IP 写成别人。

所以（按优先级）：

- **默认 publish 5189/5190**（compose 当前行为）。直连场景下没有代理插手，客户端要伪造  
  IP 得自己在 WS 握手头里塞 `X-Real-IP`，收益低、门槛高；而"注册不上"是 100% 发生的可用性故障。  
  两害相权，先保证能用。
- **如果你在前面挂了 nginx 之类的反向代理**：让代理**覆写**这两个头（`proxy_set_header X-Real-IP $remote_addr;`），  
  并用防火墙只允许代理 IP 访问容器端口。做不到就把 compose 里那两行 `5189/5190` 注释掉。
- **一旦改用 `network_mode: host`**，容器内外网络栈合一，5189/5190 直接对外 ——  
  确认客户端都能走 UDP 5187 后，在 iptables / 安全组 DROP 掉这两个端口。  
  `docker-compose.yml` 末尾注释里已经补了这条。

### 坑 2：`hbbr` 的环境变量 `PORT` 会 +1

```rust
// server/src/src/hbbr.rs
let mut port = RELAY_PORT;                       // 常量 21117（服务端那份 hbb_common）
if let Some(v) = common::get_arg_opt("PORT") {   // ← 注意：读的是环境变量
    let v: i32 = v.parse().unwrap_or_default();
    if v > 0 { port = v + 1; }                   // ← +1 ！
}
```

这是给官方 docker-compose 的遗留设计：一个 `.env` 里写 `PORT=21116`，hbbs 用 21116、hbbr 自动变成 21117。

对本仓库的影响：

- **只有 `-p` / `--port` 命令行参数是字面值**；`PORT` 环境变量会触发 +1。所以本仓库的 compose  
  给 hbbr 传的是 `command: hbbr -p 5188`，**不是** `- PORT=5188`；
- 谁要是给 hbbr 加了环境变量 `PORT=5188`，它会去监听 **5189**，正好撞上 hbbs 的 WebSocket 端口，  
  然后两个容器都起不来，报的是含糊的 `Address already in use`。

### 坑 3：改主端口要连带改的地方

| # | 位置                                    | 改什么                                                                                                      |
| - | ------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| 1 | `server/deploy/.env.example` → `.env` | `HBBS_PORT` / `HBBS_NAT_PORT` / `HBBR_PORT` / `HBBS_WS_PORT` / `HBBR_WS_PORT`，并保持 `NAT = P-1`、`WS = P+2` |
|   |                                       |                                                                                                          |
| 2 | `server/deploy/docker-compose.yml`    | 不用改 —— `ports:` 段与 `command` 都引用同一批变量，会自动跟着走                                                             |
| 3 | **客户端 `build/client/custom.toml`**    | `rendezvous_host = "id.wedesk.alsoapp.com:P"`，**然后重新编译客户端**（中继地址不用配，见方案 C）                |

第 3 点最容易漏：客户端碰到不带端口的域名时，兜底用 `RENDEZVOUS_PORT`(21116)。  
服务端改了端口而客户端没改，现象是"ID 一直转圈，最后超时"，看日志还以为是鉴权失败。

### 坑 4：客户端会去连 `P-2` API 端口（现在已有 admin 服务接住）

客户端 `common::get_api_server_()` 在只配了自定义 rendezvous server 时，会把 API 地址  
推导成 `http://<host>:P-2`（5187 → **5185**）。这个服务由 compose 的 `admin` 服务提供  
（FastAPI 管理后台：心跳、设备台账、地址簿同步、审计、登录）。

**admin 已部署时**：客户端请求正常，无噪音。若既不部署 admin 也没配 api-server，  
客户端日志每 15 秒一条

```
WARN [src/hbbs_http/sync.rs] Heartbeat failed: reqwest::Error { kind: Request,
      url: "http://192.168.0.105:5185/api/heartbeat", source: TimedOut }
```

这是**噪音不是故障**，不影响注册、打洞、中继。从 2026-10-20 起客户端连续失败 3 次后  
自动停搏，日志里只会看到 3 条 WARN + 一条说明性的 `info`。

想彻底消掉，三选一：

- 部署 compose 里的 `admin` 服务（`docker compose up -d admin`）；
- 客户端显式配一个 `api-server`（定制客户端经 `custom.toml` 的 `server.api_server` 注入）；
- 关闭设备注册：`OPTION_REGISTER_DEVICE`（`register-device`）= `N`，  
  此时 `get_api_server()` 直接返回空串，心跳根本不启动。

> `build/client/custom.toml` 里的 `relay_host` **不会被注入到源码**，它只是一致性记录：  
> 中继地址由 hbbs 下发、或按上文"+1"兜底。`customize.py` 会检查  
> `relay_host 端口 == rendezvous_host 端口 + 1`，不满足就告警提醒你去给 hbbs 加 `-r`。

---

## 5. 部署场景

### 方案 A：就在本仓库这套配置上跑（推荐起点）

域名 `id.wedesk.alsoapp.com` 解析到 hbbs/hbbr 所在机器，端口用 **5186 / 5187 / 5188**。  
客户端只配 `rendezvous_host = "id.wedesk.alsoapp.com:5187"`；中继由 hbbr 自动注册、hbbs 下发，客户端不存中继地址。

**这是当前 `build/client/custom.toml` 与 `docker-compose.yml` 里已经配好的状态，什么都不用改。**

### 方案 B：宿主端口冲突 —— 整体平移

把三个变量一起平移到另一段，例如：

```env
HBBS_PORT=9100
HBBS_NAT_PORT=9099      # = 9100 - 1
HBBR_PORT=9101          # = 9100 + 1，保住客户端兜底推导
```

```toml
rendezvous_host = "id.wedesk.alsoapp.com:9100"  # 改完必须重新编译客户端
# relay_host 已废弃：中继节点由 hbbr 自动注册，只需设 RELAY_PUBLIC_ADDR
```

注意**别只看主端口**：9100 会派生出 9099 和 9102。排布前先按第 2.1 节的公式把  
**全部派生端口**列出来，确认没有和已有服务撞车 —— 5190/9102 这类"不起眼的 +2 端口"  
往往才是踩的内容。

### 方案 C：hbbs 与 hbbr 分机

中继流量大时可以拆开两台机器。**现在不需要在 hbbs 上配 `-r`**：B 机的 hbbr
启动后会自动向 hbbs 注册（60 秒一次），hbbs 维护 `relay_nodes` 表并在
P2P 失败时下发最优节点。B 机只需要两个环境变量：

```bash
docker run ... -e HBBS_HOST=<A机IP>:5187 -e RELAY_PUBLIC_ADDR=<B机IP或域名> ... hbbr -p 5188
```

放行策略随之分开：hbbs 那台放行 **TCP 5186 + TCP/UDP 5187**，hbbr 那台只放行 **TCP 5188**。  
客户端配置不变（仍然指向 hbbs 的域名），也不保存任何中继地址。

> 旧的 `-r <host>:5188` 仍受支持（兼容存量部署、临时固定一台中继），
> 与自动注册共用同一套健康检查与打分。验证注册是否成功（控制台只收
> 环回来源，端口映射下必须进容器里执行）：
> ```bash
> docker compose exec -T rd-hbbs sh -c 'echo rrg | nc 127.0.0.1 5187'
> ```

---

## 6. 客户端侧的网络要求


| 方向     | 要求                                                    | 说明                                 |
| ------ | ----------------------------------------------------- | ---------------------------------- |
| 出站     | TCP 5186 / TCP 5187 / UDP 5187 → `id.wedesk.alsoapp.com` | 注册、心跳、打洞协调                         |
| 出站     | TCP 5188 → `id.wedesk.alsoapp.com`                       | P2P 失败时走中继                         |
| 入站     | **无固定端口**                                             | P2P 用的是随机临时端口，NAT 会话自动放行           |
| IP 直连  | 本机监听 TCP 21118（可选功能）                                  | 见第 2.2 节的说明；不用这个功能就不用管             |
| P2P 直连 | 依赖对方 NAT 类型                                           | 对称 NAT ↔ 对称 NAT 必然走中继，这是协议限制而非配置问题 |

如果员工网络只允许访问白名单域名，只需在出口白名单里放行 `id.wedesk.alsoapp.com` 的上述端口  
—— 客户端**不会**再访问任何 rustdesk 官方域名（见 [05 定制与断网](05-customize.md)）。

> **不建议**在 hbbs 前面套通用 CDN / HTTP 反向代理：5187 的 **UDP** 无法被普通七层代理转发，  
> 而它是 ID 注册与心跳的生命线。非要隐藏端口就用 DNAT / 端口转发，别用 HTTP 代理。

---

## 7. 验证

容器起来后先看规划自洽性（会顺带检出 `.env` 里的废弃变量）：

```bash
python rdctl.py ports
```

从客户端视角逐个探测：

```bash
# 本机（二进制 in PATH 时）
rustdesk-utils doctor <服务器IP>
```

`rustdesk-utils doctor` 用的是**内置默认端口表**（21115/21116/21117/21118/21119），  
它不知道我们改了端口，所以给你的结论要人工折算：**把每一项换算到 5186/5187/5188 上再看**，  
或者直接看 result 里它自己打印的目标地址。真正等价于"客户端能否连上"的检查是：

```bash
# 外部 —— 期望：5186/tcp open、5187/tcp open、5187/udp open|filtered、5188/tcp open
#        5189、5190 必须 closed / filtered
nmap -sU -sT -p 5186-5192 id.wedesk.alsoapp.com
```

宿主机上的真实监听（确认没有把 WS 端口 publish 出去）：

```bash
docker compose ps                      # 看 PORTS 列
ss -ltnp | grep -E '518[6-9]|5190'
```

---

## 8. 相关文档

- [04 部署指南](04-deploy.md) — 端口放行的部署上下文、`.env` 完整说明
- [09 服务端自编译](09-server-build.md) — 二进制、镜像怎么来
- [05 定制与断网](05-customize.md) — 客户端兜底地址与关闭官方回连
