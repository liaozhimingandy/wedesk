# 13 · 服务端部署与运维手册

> 文档索引：[`docs/README.md`](README.md) ｜ 上一篇：[12 PostgreSQL 后端](12-postgres-backend.md)

> **这篇是什么**：把散在 04 / 09 / 10 / 11 / 12 里的服务端内容收敛成**一条部署主线**——
> 从零起服务 → 原理架构 → 日常使用 → 排障。第一次部署只需读这一篇；
> 需要深挖某个主题时，文中每个链接都指向对应的专篇。

> **适用范围**：服务端（hbbs + hbbr）部署、运维、客户端接入。
> 客户端构建见 [03 本地构建](03-build.md) / [03b 云构建](03b-ci.md)，定制见 [05 定制指南](05-customize.md)。

---

## 一、五分钟快速部署

适合内网团队的第一台服务器。深坑与变体在第三章展开。

```bash
# 1. 准备一台 Linux 服务器（Docker + Compose 已装），放行
#    TCP 5186 / 5187 / 5188 / 5189 / 5190 / 5191 / 5192 + UDP 5187
git clone <你的仓库> && cd wedesk/server/deploy

# 2. 配置
copy .env.example .env        # Windows；Linux/macOS 用 cp
#    编辑 .env：把 RELAY_HOST 改成这台服务器的内网 IP 或域名（hbbr 注册用）

# 3. 启动（首次自动从源码构建镜像，约 15-25 分钟）
python rdctl.py start

# 4. 拿公钥 —— 客户端定制要用，务必记下来并备份
python rdctl.py key

# 5. 体检（可选但建议）：查"配置看着对、实际没生效"的静默失败
python rdctl.py doctor
```

启动成功后服务端就两个容器、一个数据目录：

```
server/deploy/
├── .env                 # 配置（gitignore，不进仓库）
├── docker-compose.yml   # 编排定义（compose 路线）
├── rdctl.py             # compose 路线管理工具（零依赖，标准库）
├── rdrun.sh             # docker run 路线一键脚本（分机/无 compose 时用）
└── ../data/             # 全部运行状态（gitignore）
    ├── id_ed25519       # 服务端私钥 —— 丢了 = 全部客户端重配
    ├── id_ed25519.pub   # 公钥（rdctl.py key / rdrun.sh key 打印的就是它）
    └── db_v2.sqlite3    # peer 注册表（仅 SQLite 后端时，默认是 PostgreSQL）
```

> **容器里的二进制是本仓库源码编译的**，不是 `rustdesk/rustdesk-server` 官方镜像。
> 这一点决定了：服务端定制补丁（关闭版本检查、PG 后端）只在本地镜像上生效。

**另一条路：`docker run` 直接起**。下面这套 compose + `rdctl.py` 是"两台服务
一起管"的默认路线；如果你的情况是下面任一一种，直接跳到
[3.5 用 docker run 启动](#35-用-docker-run-启动hbbs--hbbr-可分离部署)：

- hbbs（ID 注册）与 hbbr（中继）要**分机部署**，或中继要**横向扩容**；
- 服务器上**只有 docker，没有 compose 插件**；
- 要把启动命令落到 systemd unit / k8s 里。

---

## 二、部署架构与原理

### 2.1 部署拓扑

```mermaid
flowchart TB
    subgraph STAFF["员工电脑（同一二进制：控制端/被控端）"]
        C1["控制端<br/>网管工位"]
        C2["被控端 A"]
        C3["被控端 B"]
    end

    subgraph SERVER["你的服务器（内网或云主机）"]
        HBBS["rd-hbbs 容器<br/>TCP 5186 · TCP+UDP 5187<br/>ID 注册 · 心跳 · 打洞协调"]
        HBBR["rd-hbbr 容器<br/>TCP 5188<br/>中继转发"]
        DATA[("server/data/<br/>id_ed25519 密钥对 + 黑/封名单")]
        HBBS --- DATA
        HBBR -.->|"黑/封名单"| DATA
    end

    subgraph DEF["默认：PostgreSQL 后端"]
        PG[("rd-postgres<br/>peer / server_config / connection_log")]
    end

    C1 -->|"① 注册/心跳（UDP 5187）"| HBBS
    C2 -->|"① 注册/心跳"| HBBS
    C3 -->|"① 注册/心跳"| HBBS
    C1 <-.->|"② P2P 直连（优先，不经过服务器）"| C2
    C1 <-.->|"② P2P 直连"| C3
    C1 -.->|"③ 打洞失败时走中继"| HBBR
    HBBS -->|"DB_TYPE=postgres（默认）"| PG
```

**三条最重要的结论**：

1. **服务端不传输画面**（除非打洞失败走中继）。hbbs 只传控制信令（每人几 KB/分钟）；
   hbbr 才传画面流量（每人 100 KB/s ~ 2 MB/s 视画质）。
2. **带宽压力取决于打洞成功率，不是在线人数。** 内网同网段打洞几乎必成，hbbr 近乎闲置。
3. **hbbs 挂**：新客户端拿不到 ID、无法撮合新连接，但已建立的 P2P 连接不受影响。
   **hbbr 挂**：只有本来要走中继的连接失败。

### 2.2 固定端口表（部署前照抄即可）

**端口是固定的一套，compose 里 8 条映射与两处 `-p` 全部写死数字，不做 +1 / +2 推导。**

| 端口 | 协议 | 进程 | 用途 |
|---|---|---|---|
| 5186 | TCP | hbbs | NAT 类型探测 |
| 5187 | TCP | hbbs | 注册 / TCP 打洞与连接 |
| 5187 | UDP | hbbs | 注册 + 心跳 + UDP 打洞 |
| 5188 | TCP | hbbr | 中继转发 |
| 5189 | TCP | hbbs | WebSocket 注册通道 |
| 5190 | TCP | hbbr | WebSocket 中继 |
| 5191 | TCP | hbbs | 健康检查 `GET /health` |
| 5192 | TCP | hbbr | 健康检查 `GET /health` |

```mermaid
flowchart LR
    subgraph HBBS_D["hbbs · 主端口 5187"]
        P1["TCP 5186<br/>NAT 类型探测"]
        P2["TCP+UDP 5187<br/>注册/心跳/打洞"]
        P3["TCP 5189<br/>WebSocket 注册通道"]
        P4["TCP 5191<br/>健康检查"]
    end
    subgraph HBBR_D["hbbr · 主端口 5188"]
        R1["TCP 5188<br/>中继数据"]
        R2["TCP 5190<br/>WebSocket 中继"]
        R3["TCP 5192<br/>健康检查"]
    end
    PS["（客户端还会自己推导一个<br/>api-server = TCP 5185，<br/>自建无此服务，日志噪音而已）"]
```

| 规则 | 说明 |
|---|---|
| 映射左右两侧是同一个数字 | 容器内端口由 `command: hbbs -p 5187` 决定，宿主映射写成 `9000:5187` 只会得到"端口开着但连不上" |
| 5187 的 TCP 与 UDP 是两条映射 | `"5187:5187"` 只发 TCP，UDP 必须单独写 `/udp`，少了它客户端注册永远超时 |
| **5189 必须开** | 客户端 UDP 不通或勾选了"使用 WebSocket"时用它注册；摸不到就**一直卡在"正在接入网络"**（日志：`start tcp: ws://host:5189` → `deadline has elapsed`）。上游"不要开"只针对**前面挂了反向代理**的场景（WS 分支无条件信任 `X-Real-IP`/`X-Forwarded-For`），有代理时必须让代理覆写该头，否则注释掉这条映射 |
| 源码里确实有"主端口−1 / +2"的硬编码 | hbbs 无条件额外监听 主端口−1 与 主端口+2，hbbr 额外监听 主端口+2，**没有开关**。这就是为什么上面这张表是"挤出来"的，也是平移端口时必须整体移动的原因 |

**防火墙放行**：`TCP 5186 / 5187 / 5188 / 5189 / 5190 / 5191 / 5192` + `UDP 5187`。
若确认所有客户端都能走 UDP 5187，5189/5190 可以关掉。

**日常部署不需要碰任何端口变量。** 只有宿主端口冲突时才整体平移 —— 用
`rdrun.sh --hbbs-port 9100 --nat-port 9099 --hbbr-port 9101 --hbbs-ws-port 9102 --hbbr-ws-port 9103`
（脚本会校验上面那些硬编码关系，错了直接拒绝启动），
并且客户端 `custom.toml` 的 `rendezvous_host` 必须同步改、**重新构建分发**。
详见 [10 端口与网络规划](10-ports.md)。

### 2.3 数据与密钥：整个服务端的全部状态

```
server/data/
├── id_ed25519        # 私钥 —— 重启不重新生成；丢了 = 重新生成 + 重新注入 RS_PUB_KEY + 重新分发所有客户端
├── id_ed25519.pub    # 公钥 —— rdctl.py key 打印的值，对应客户端 custom.toml 的 RS_PUB_KEY
├── db_v2.sqlite3     # peer 注册表（仅 SQLite 后端时）：只有一张 peer 表
├── blacklist.txt     # hbbr 黑名单（可选；配了库时以 relay_access_list 表为准）
└── blocklist.txt     # hbbr 封禁名单（可选，命中即拒绝）
```

**这张表里没有什么**：hbbs 只登记"哪个 ID 注册过"，
**不记录谁连了谁、连了多久、走 P2P 还是中继**。要做合规审计必须自己在前面加
反向代理或流量镜像，别指望这个库。详见 [11 服务端运维](11-server-ops.md) 第 6.3 节。

**备份就等于备份这一个目录**：

```bash
cd server/deploy && python rdctl.py backup     # 产物在 server/backup/<时间戳>/
```

### 2.4 数据库后端：SQLite（默认）与 PostgreSQL（可选）

```mermaid
flowchart TB
    subgraph HBBS_P["hbbs 进程"]
        FACADE["Database 门面<br/>Arc&lt;dyn DbBackend&gt;"]
    end
    FACADE -->|"DB_TYPE=postgres（默认）"| PG[("PostgreSQL<br/>运行时 query()<br/>编译期不连库")]
    FACADE -->|"DB_TYPE=sqlite"| SQLITE[("db_v2.sqlite3<br/>保留 sqlx query! 宏<br/>编译期校验 SQL")]
    SQLITE --> DATA[("./data:/root 卷")]
    PG --> PGDATA[("./pgdata 卷<br/>默认启用）")]
```

| | PostgreSQL（**默认**） | SQLite |
|---|---|---|
| 启用方式 | 什么都不用改 | `DB_TYPE=sqlite` + `DB_URL=/root/db_v2.sqlite3` 两行一起改 |
| 数据位置 | `server/deploy/pgdata/` | `server/data/db_v2.sqlite3` |
| 库内表 | `peer` + `connection_log` + `relay_nodes`（`server_config` 仅旧版升级残留，启动时自动清空） | 同上（审计仅 PG 生效；`relay_nodes` 两端都用，见 2.6） |
| 密钥 | 只存文件（见 2.7），不进数据库 | 同左 |
| 连接审计 | ✅ 异步批量落库 | ❌ 完全不记录 |
| 存量迁移 | 从 SQLite 迁过来：`migrate-sqlite-to-pg.py` | — |
| 编译期约束 | 运行时 `DB_URL` 才是 PG 连接串 | 构建期 `DATABASE_URL` 必须指向 sqlite 模板库，**两套变量不要混** |
| 常见静默失败 | 改了 `DB_TYPE` 但 `RD_IMAGE` 还是官方镜像 → 官方镜像不认这个变量，**不报错，继续写 SQLite**。跑 `rdctl.py doctor` 可实证数据落在哪 | — |

后端选择由 `DB_TYPE` 唯一决定，`DB_URL` 可以省略（省了就由 `PG_*` 推导）。
类型与地址不一致时的兜底规则见 [12 · 第三节](12-postgres-backend.md#三db_type-是唯一权威db_url-可以省)。

设计动机（sqlx `query!` 宏编译期绑定单一数据库类型，同一个二进制不能用宏同时支持两种库）
与完整说明见 [12 PostgreSQL 后端](12-postgres-backend.md)。

### 2.5 CI 构建与发布流水线

wedesk 的服务端镜像与三端客户端都由同一条 GitHub Actions 流水线产出
（`.github/workflows/build-windows.yml`，11 个 job）：

```mermaid
flowchart LR
    subgraph CI["GitHub Actions（push 到 main / 手动触发）"]
        PRE["preflight<br/>编译期必需文件在位"]
        BRIDGE["generate-bridge<br/>flutter_ffi → Dart/Rust 桥"]
        SRV["build-server<br/>musl 静态二进制"]
        WIN["build-windows<br/>exe + vcpkg 依赖"]
        AND["build-android ×3<br/>armeabi-v7a / x86_64 / arm64-v8a"]
        APK["android apk/aab"]
        IOS["build-ios<br/>未签名 IPA"]
        MAC["build-macos"]
        LIN["build-linux"]
        IMG["Push server image<br/>→ Docker Hub"]
    end
    PRE --> BRIDGE --> WIN
    PRE --> SRV --> IMG
    BRIDGE --> AND --> APK
    BRIDGE --> IOS
    BRIDGE --> MAC
    BRIDGE --> LIN
    IMG -->|"RD_IMAGE=liaozhiming/wedesk-server:latest"| DEPLOY["服务器拉取升级<br/>rdctl.py upgrade"]
    WIN & APK & IOS & MAC & LIN --> ART["Actions Artifacts<br/>保留 14 天"]
    TAG["推送 v* 标签"] --> REL["Publish Release<br/>四端 + 服务端打包发布"]
```

**两条使用方式**：

- **要官方源以外的镜像**：CI 会把镜像推到 Docker Hub，服务器 `.env` 设
  `RD_IMAGE=liaozhiming/wedesk-server:latest`，然后 `python rdctl.py upgrade`。
- **要带版本号的发布包**：推 `v*` 标签，`Publish Release` job 自动把四端产物
  与服务端二进制打成 GitHub Release（该 job 有 `v*` 门控，普通 push 不会误发版）。

> 流水线细节与排障见 [03b CI 云构建](03b-ci.md)。当前状态：11 个 job 全绿。

### 2.6 中继调度：多节点选择与混合探测

中继节点清单有两个来源：**hbbr 自动注册**（默认，推荐）与静态 `-r` 参数（兼容旧部署）。
两者进入同一套优选流程，对调度完全透明。

#### 节点来源：hbbr 自动注册

每台 hbbr 启动后每 60 秒向 hbbs 上报一次（`RelayRegister`，协议 oneof 32）：

```
hbbr --(host/port/region/capacity/connections/version)--> hbbs
hbbs --(ok / interval_sec / 认定的对外地址)--> hbbr
```

- hbbs 收登记后立即把节点并入调度列表，并落库 `relay_nodes` 表；
- **超过 210 秒（3 个周期）没再上报的节点自动剔除**——下线不需要人工操作；
- hbbr 侧只需要一个变量：`RELAY_PUBLIC_ADDR=<客户端可达的域名或IP>`
  （Docker 网络下 hbbs 看到的来源 IP 是网桥地址，不可用）；
- 环回 / 未指定地址会被 hbbs **拒绝登记**并在 hbbr 日志里说明原因；
- `region` / `capacity` 随注册上报，省掉手工打标；`connections` 是实时快照，
  让负载惩罚真正参与打分（v1 静态 `-r` 场景下它恒为 0）。

控制台查看：`relay-registered`（简写 `rrg`）列出当前在线的注册节点。

#### 优选：带权打分（默认生效，不需要客户端配合）

hbbs 每 3 秒（`CHECK_RELAY_TIMEOUT`）探测一次每个中继：

1. 对每个中继 TCP 连一次，记录 **RTT** 与是否存活；
2. 结果写回 `relay_nodes` 表（`latency_ms` / `healthy`）并更新内存缓存；
3. 有连接请求时按综合分挑最优：

   ```
   score = weight - (connections / capacity) × 200 - latency_ms × 0.5
   ```

   越大越优；`healthy=false` 或已满载的直接排除。探测结果还没回来（缓存为空）
   时退回原来的轮询，保证启动瞬间可用。

`region` / `weight` / `capacity` 是运维手动打标的静态权重，默认 `''` / `100` / `1000`。
在 hbbs 控制台（`check_cmd`）里维护：

```
relay-set-meta <host> <region> <weight> <capacity>   # 打标，简写 rsm
relay-list                                          # 列出全部中继及其实时健康，简写 rl
relay-health                                        # 打印内存缓存（实时时延/存活），简写 rh
```

> **region 是手动标签，不是 GeoIP**——刻意不引入 IP 库依赖。
> v1 里它参与展示与人工决策，"优先同区域"要等两端客户端上报自身区域
> （混合探测的下一步）才能真正自动生效。

#### v2（协议骨架，尚未闭环）：两端 UDP 探测 + BOW

"两端并行探测 → 上报 → 敲定唯一公共中继"的三个消息已经落进 `rendezvous.proto`
（`RelayProbeRequest` / `RelayProbeResult` / `RelaySelect`，oneof 编号 29/30/31），
hbbs 侧的会话状态机与 BOW 打分也已实现，但**客户端的 UDP 探测还没做**，故默认关闭：

> 已落地的注册消息：`RelayRegister` / `RelayRegisterResponse`（oneof 32/33），
> 仅 hbbr ↔ hbbs 之间使用，不涉及客户端，因此没有兼容性负担 ——
> 旧版 hbbr 只是不注册，行为退回 `-r` 静态配置。

```mermaid
sequenceDiagram
    participant A as 控制端
    participant B as 被控端
    participant S as hbbs
    S->>A: RelayProbeRequest(candidates 2~3 个, session_token)
    S->>B: RelayProbeRequest(同上)
    A-->>S: RelayProbeResult(session_token, 各候选 RTT/丢包)
    B-->>S: RelayProbeResult(同上)
    S->>A: RelaySelect(最终中继, session_token)
    S->>B: RelaySelect(同上)
```

打分用 **BOW（Best-of-Worst）**：只考虑两端都探过的候选，取
`max(控制端RTT, 被控端RTT)` 最小者；任一端不可达（RTT = -1）的候选直接排除。
这样选出来的是"两端都能用"的中继，而不是"对某一端很快、对另一端不通"的中继。

启用（等两端客户端都支持探测后再开）：

```bash
# .env
RELAY_PROBE=1        # 默认 0 = 关闭；关闭时行为与 v1 完全一致，旧客户端安全
```

未启用时 `punch_hole` 仍按 v1 直接下发最优中继，连接照常建立——
**协议骨架缺失不影响 v1 的可用性**。

### 2.7 密钥管理：只存文件（docker secrets 标准），不进数据库

**为什么改**（2026-10-20 起）：旧版本把私钥托管在 `server_config` 表里，
意味着任何能读到这个库的人（DBA、备份文件、拖库攻击）都能拿到服务端
私钥，进而伪造你的服务器签名。现改为 docker secrets 标准的文件挂载：

```bash
docker run ... \
  -v /d/Git/secrets/id_ed25519:/run/secrets/id_ed25519:ro
```

文件内容与上游 `id_ed25519` 完全一致（64 字节私钥的 base64 文本）。

**hbbs/hbbr 的私钥解析顺序**（先命中先用）：

| 优先级 | 来源 | 说明 |
|---|---|---|
| 1 | `-k <base64私钥>` | 命令行显式指定，仅本次生效，不落盘 |
| 2 | `KEY_FILE` 环境变量 | 显式指定任意容器内路径（文件或目录） |
| 3 | `/run/secrets/id_ed25519` | docker secrets 标准挂载点 |
| 3' | `/run/secrets/` 目录扫描 | 挂载名**不必叫 id_ed25519**：目录内任何"base64 解出 64 字节"的文件都认作私钥（按文件名排序取首个，如 `id`、`wedesk.key` 均可） |
| 4 | `./data/id_ed25519`（工作目录） | 上游行为，首次启动自动生成 |
| 5 | 旧库一次性迁移 | `server_config` 里还留着旧版托管的私钥时，自动写回文件并**删除库中该行** |
| 6 | 就地生成 | 以上都没有，只写文件（与上游一致） |

要点：

- **数据库中不再写入任何密钥材料**；`rdctl.py doctor` 会检查库里是否还有残留
- 多实例共享同一套 ID = 所有实例挂载**同一个 secret 文件**
- 公钥照旧用 `rdctl.py key` / `docker exec rd-hbbs cat /root/id_ed25519.pub` 查看
- `/run/secrets` 挂载是只读的：hbbs 不会往旁边写 `.pub`（数据目录里已有的
  `.pub` 不受影响），客户端要的 `RS_PUB_KEY` 用 `rdctl.py key` 取

### 2.8 健康检查接口

hbbs 与 hbbr 各自带一个轻量 HTTP 健康检查端点（零新依赖，容器内自实现）：

| 服务 | 默认端口 | 探测内容 |
|---|---|---|
| hbbs | `5191`（= 主端口+4） | 进程存活 + **数据库 `select 1` 探活** |
| hbbr | `5192`（= 中继端口+4） | 进程存活 |

```bash
curl http://<server>:5191/health
# 200
# {"service":"hbbs","status":"ok","version":"1.4.2","uptime_secs":86400,
#  "timestamp":1760000000,"database":{"backend":"postgres","ok":true}}
# 数据库探活失败 → 503，status 变 "degraded"
```

- 端口可用 `--health-port <N>` 或环境变量 `HEALTH_PORT` 覆盖，设 `0` 关闭
- compose 已内置 `healthcheck`（busybox wget 每 30s 自检），`docker compose ps`
  直接显示 `(healthy/unhealthy)`；`rd-hbbs` 异常时还会联动 `depends_on` 行为
- 外部监控（Prometheus blackbox / 云拨测 / 负载均衡）直接拨这两个端口即可
- 端口变量：`.env` 里 `HBBS_HEALTH_PORT` / `HBBR_HEALTH_PORT`；
  改 `HBBS_PORT`/`HBBR_PORT` 时记得同步

---

## 三、服务端部署详解

### 3.1 前置条件

| 项 | 要求 |
|---|---|
| Docker | 20.10+，`docker info` 能跑 |
| Docker Compose | v2（`docker compose version`） |
| Python | 3.8+（rdctl.py 只用标准库，**零第三方依赖**） |
| 网络 | 服务器能被所有客户端访问；防火墙放行 TCP 5186/5187/5188/5189/5190/5191/5192 + UDP 5187 |
| 磁盘 | 镜像构建约 2-3 GB，运行期 `data/` 通常 <100 MB |

### 3.2 `rdctl.py` 完整命令参考

在 `server/deploy/` 下执行。退出码：`doctor` 返回 0 = 无错误（可能有提醒），1 = 有需要处理的错误。

| 命令 | 作用 | 备注 |
|---|---|---|
| `python rdctl.py start` | 启动 hbbs + hbbr | 启动前校验端口关系；本地镜像损坏会自动重建；首次自动生成密钥对 |
| `python rdctl.py stop` | 停止并保留数据 | |
| `python rdctl.py restart` | 重启 | 改 `.env` / compose 后用它 |
| `python rdctl.py status` | 容器状态 + 已注册设备概况 | |
| `python rdctl.py ports` | 打印当前端口规划 | 对照防火墙/安全组时用 |
| `python rdctl.py key` | 打印服务端公钥 | 与客户端 `RS_PUB_KEY` 对照 |
| `python rdctl.py logs [hbbs\|hbbr]` | 实时日志 | 不带参数跟全部；Ctrl+C 退出 |
| `python rdctl.py build [--no-cache]` | 从本仓库源码构建镜像 | 首次 15-25 分钟；改了源码/Dockerfile 用 `--no-cache` |
| `python rdctl.py backup` | 备份密钥 + 数据库到 `server/backup/<时间戳>/` | **必做，进计划任务**；PG 后端时含 pg_dump |
| `python rdctl.py upgrade` | 拉远端镜像并重启 | 仅 `RD_IMAGE` 指向远端时可用。`--tag=<tag>` 指定版本；`--check` 只报告有无更新；`--no-backup` 跳过自动备份（不推荐） |
| `python rdctl.py doctor` | 部署体检 | 查"配置看着对、实际没生效"的静默失败，见下 |
| `python rdctl.py uninstall` | 停止并删除容器 | **保留 `data/`**；会二次确认 |

**`doctor` 查什么**（按顺序）：

```
环境    Docker 守护进程、compose 配置合法性
配置    .env 是否存在、RELAY_HOST 是不是 127.0.0.1/localhost
        废弃变量（老版本迁移）、server/data/.env 陷阱、RD_IMAGE 是否指向官方镜像
端口    P-1/P/P+2 与 R/R+2 推导关系自洽
镜像    本地 or 远端；本地镜像里有没有可执行的 hbbs/hbbr
数据    实证数据落在哪：PG 的 peer 表 vs 本地 SQLite（分辨"配了 PG 其实在写 SQLite"）
```

**原生命令兜底**（rdctl 覆盖不到的场景）：

```bash
docker compose ps                              # 容器状态
docker compose logs -f --tail 200 rd-hbbs      # 看日志
docker stats rd-hbbs rd-hbbr                   # 真实资源占用
docker compose up -d                           # 启动（含 PostgreSQL，默认后端）
```

> 上面都是 compose 玩法。**脱离 compose**（只有 docker、或 hbbs/hbbr 分机部署）
> 的场景用 `server/deploy/rdrun.sh` 或裸 `docker run` 命令，见
> [3.5 用 docker run 启动](#35-用-docker-run-启动hbbs--hbbr-可分离部署)。

### 3.3 `.env` 完整参数参考

按部署频率排序。**改任何值之后都要 `python rdctl.py restart`**。

| 变量 | 默认 | 说明 |
|---|---|---|
| `RELAY_HOST` | `127.0.0.1` | **必须改成实际 IP/域名**。它经 `RELAY_PUBLIC_ADDR` 传给 hbbr 做节点注册；`rdctl.py doctor` 会对 localhost 告警 |
| `RELAY_PUBLIC_ADDR` | 空（= 沿用 `RELAY_HOST`） | hbbr 注册时自报的**客户端可达地址**。Docker 部署必须填，否则 hbbs 登记到网桥地址 |
| `HBBS_HOST` | `hbbs:5187` | hbbr 的注册目标。中继单独一台机器时改成 hbbs 那台的 IP |
| `RELAY_REGION` / `RELAY_CAPACITY` | 空 | 可选：区域标签 / 并发容量，用于 hbbs 打分 |
| `ALWAYS_USE_RELAY` | `N` | `N`=优先 P2P（内网推荐）；`Y`=强制中继（跨公网提高连通率，全部吃服务器出网带宽） |
| `RUST_LOG` | `warn` | `error`/`warn`/`info`/`debug`/`trace`。默认 `warn` 是刻意的：info 下每次注册/心跳都落一行，长期运行磁盘写入可观。排障临时调高，**调完记得调回** |
| `RD_IMAGE` | 注释（= 本地构建） | 有值就直接用/拉那个镜像。`liaozhiming/wedesk-server:latest` = CI 推的远端镜像；`rustdesk/rustdesk-server:latest` = 官方镜像（**失去本地化补丁，且不认 `DB_TYPE`**） |
| `HBBS_PORT` / `HBBS_NAT_PORT` / `HBBR_PORT` | `5187` / `5186` / `5188` | **compose 不读**（它用写死的数字）。只有 `rdrun.sh`/`rdctl.py` 平移端口时才用，必须满足 `HBBS_NAT_PORT = HBBS_PORT - 1` |
| `HBBS_WS_PORT` / `HBBR_WS_PORT` | `5189` / `5190` | 同上，仅 `rdrun.sh` 平移时用，必须满足 `HBBS_WS_PORT = HBBS_PORT + 2`、`HBBR_WS_PORT = HBBR_PORT + 2`。要关掉 WS 通道，直接注释掉 compose 里 5189/5190 那两条映射 |
| `HBBS_MEM` / `HBBS_CPUS` | `256m` / `0.50` | hbbs 实测常驻 10-40 MB，上限是"异常时快速重启"用的，不是性能预估 |
| `HBBR_MEM` / `HBBR_CPUS` | `512m` / `1.00` | hbbr 内存随并发路数增长，但**瓶颈是出网带宽不是内存**，加到 1 GB 不会更快 |
| `LOG_MAX_SIZE` / `LOG_MAX_FILE` | `10m` / `3` | 每容器日志约 30 MB 封顶，不会写满磁盘 |
| `DB_TYPE` / `DB_URL` | `postgres` / 留空（由 `PG_*` 推导） | 见 2.4 节。`DB_URL` 的含义随后端变化：sqlite 是容器内文件路径，postgres 是连接串 |
| `MAX_DATABASE_CONNECTIONS` | 留空 | **刻意留空**：留空则 sqlite→1、postgres→8 各用合理默认；写死会迫使 SQLite 路径也用它，更容易撞 `database is locked` |
| `DB_CONNECT_RETRIES` | 留空（=10） | 仅 postgres 生效。容器并发启动时 hbbs 常先于 PG 可用，不重试会 CrashLoop。设 1 恢复"立刻失败" |
| `PG_IMAGE` / `PG_DB` / `PG_USER` / `PG_PASSWORD` | `postgres:16-alpine` / `rustdesk` / `rustdesk` / `changeme` | 默认生效（Postgres 容器随服务一起起）。**`PG_PASSWORD` 生产必须改** |
| `KEY_FILE` | 留空 | 显式指定私钥路径（容器内，文件或目录）。留空则按 `KEY_FILE → /run/secrets/id_ed25519 → /run/secrets/ 目录扫描 → ./data/id_ed25519` 顺序自动探测，目录内文件名不限（内容合法即可），见 2.7 |
| `HBBS_HEALTH_PORT` / `HBBR_HEALTH_PORT` | `5191` / `5192` | 健康检查 HTTP 端口（2.8 节），compose healthcheck 与外部监控都用它；设 0 关闭 |
| `AUDIT_LOG` / `AUDIT_FLUSH_SECS` / `AUDIT_MAX_BUFFER` | `Y` / `5` / `20000` | 连接审计开关 / 刷盘间隔（秒）/ 缓冲上限 |
| `RELAY_PROBE` | `0` | 混合探测开关（2.6 节）。`1` = hbbs 下发候选中继让两端 UDP 探测并回传。**需客户端支持，当前默认关闭**；设 0 时按服务端优选直接下发中继，旧客户端安全 |
| `PG_MEM` / `PG_CPUS` | `512m` / `1.00` | PG 容器资源上限 |

完整带注释的模板见 `server/deploy/.env.example`（每个值该不该改都写了）。

### 3.4 部署场景

#### 场景 A：内网团队（默认，推荐起点）

就是第一章的五分钟流程。保持 compose 默认的端口映射写法（跨平台都能跑），
防火墙放行 4 条规则。同网段 P2P 成功率极高，hbbr 几乎不耗带宽。

#### 场景 B：公网生产（host 网络 + 防火墙）

需要**真实来源 IP**（审计/限流）或更低延迟时，Linux 上切 host 网络：

```yaml
# docker-compose.yml：两个业务服务都加，并删掉各自的 ports: 与 networks: 段
network_mode: "host"
```

切之前**先想清楚 5189 / 5190 放不放行**——端口映射的隔离会一并消失，
容器里那两个 WebSocket 端口会原样暴露到宿主网络栈：

```bash
# 放行（推荐：UDP 受限网络的客户端要靠 5189 注册）
iptables -I INPUT -p tcp --dport 5189 -j ACCEPT
iptables -I INPUT -p tcp --dport 5190 -j ACCEPT

# 或：确认所有客户端都走 UDP 5187 时再拒绝
# iptables -I INPUT -p tcp --dport 5189 -j DROP
# iptables -I INPUT -p tcp --dport 5190 -j DROP
# 云平台还要在安全组做同样的事（放行 5186/5187/5188 + UDP 5187）
```

> `--net=host` **仅 Linux 生效**；Windows/macOS Docker Desktop 保持端口映射写法。
> 详细论证见 [10 端口与网络规划](10-ports.md) 第 4、5 节。

#### 场景 C：hbbs 与 hbbr 分机

中继带宽吃紧时，把 hbbr 单独搬到带宽更好的机器：

1. A 机跑 hbbs（含 `data/`），B 机跑 hbbr；
2. A 机 `.env`：`RELAY_HOST=<A 机对外 IP/域名>`（hbbr 注册用）；
   B 机 `docker run` hbbr 时带 `-e HBBS_HOST=<A机IP>:5187 -e RELAY_PUBLIC_ADDR=<B机IP>`；
3. hbbs 侧设 `ENFORCED_RELAY=N`（允许中继到非本机地址）；
4. 防火墙：A 机放行 5186/5187（TCP+UDP），B 机放行 5188。

> 更彻底的做法是**两台机器各跑一条独立的 `docker run`**，不用 compose 互相屏蔽，
> 见 [3.5 用 docker run 启动](#35-用-docker-run-启动hbbs--hbbr-可分离部署)。
> 中继要横向扩容（加多台 hbbr）也只能走那条路。

#### 场景 D：接外部 / 托管 PostgreSQL

默认已经自带 Postgres 容器，只有想把数据放到外部数据库（RDS 等）才需要这一步：

```bash
# .env 只改 DB_URL 一行（DB_TYPE 保持 postgres）
DB_TYPE=postgres
DB_URL=postgres://user:pwd@your-db-host:5432/rustdesk

cd server/deploy && docker compose up -d

# 本地那个 Postgres 容器就多余了，停掉即可（./pgdata 数据卷不会丢）
docker compose stop postgres
```

> 切完务必体检：`python rdctl.py doctor` —— 它会实证数据落在哪，
> 而不是只看写了什么配置。

存量 SQLite 数据搬进 PG：

```bash
python migrate-sqlite-to-pg.py ../data/db_v2.sqlite3 --out migration.sql
docker compose exec -T postgres psql -U rustdesk -d rustdesk < migration.sql
```

已知限制：hbbr 只读写名单那一张表（且可降级为文件）；换 PG 解决的是"ID 台账统一管理"，
**不是**"谁连了谁"的审计；PG 后端仍是单实例设计，不等于高可用。

### 3.5 用 `docker run` 启动（hbbs / hbbr 可分离部署）

Compose 文件是"两台服务一起起"的写法。实际要用 `docker run` 的场景有三个：

1. **分机部署**——中继带宽吃紧，要把 hbbr 单独搬到带宽更好的机器（或加多台中继）；
2. **机器上只有 docker**，没有 compose 插件（精简版 Docker Engine、部分国产化系统）；
3. 要落到 **systemd unit / k8s** 里——它们最终执行的也是这里的两条 `docker run`。

**先建立正确的心智模型**：hbbs 与 hbbr 是**彼此独立的两个进程，它们之间没有任何内部通信**。

```mermaid
flowchart LR
    subgraph LAN["客户端所在网络"]
        C1["被控端 A"]
        C2["控制端 B"]
    end
    subgraph S1["A 机 · ID 注册服务器"]
        H["hbbs<br/>TCP 5186 / TCP+UDP 5187<br/>TCP 5189（WebSocket 注册）"]
    end
    subgraph S2["B 机 · 中继服务器（可多台）"]
        R["hbbr<br/>TCP 5188<br/>TCP 5190（WebSocket 中继）"]
    end

    C1 -- "① 注册 / 心跳 / 打洞协调<br/>UDP 5187 + TCP 5186" --> H
    C2 -- "① 注册 / 心跳 / 打洞协调" --> H
    H -. "② hbbr 自动注册上报（60s）<br/>hbbs 维护 relay_nodes 表" .-> R
    H -. "③ 打洞失败时下发最优中继<br/>只告知，不转发数据" .-> C1
    H -. "③ 同上" .-> C2
    C1 -- "④ 打洞失败时走中继<br/>TCP 5188" --> R
    C2 -- "④ 中继数据" --> R
    C1 <-. "P2P 直连：优先，不经过任何服务器" .-> C2
```

由此得到三条部署规则（分机方案全靠它们）：

| 规则 | 含义 |
|---|---|
| hbbr **只需要知道** hbbs 在哪 | `HBBS_HOST=<A机IP>:5187`，用于自动注册上报节点信息 |
| hbbs **不需要**再用 `-r` 写死中继地址 | 节点由 hbbr 自动注册（60s 一次），hbbs 维护 `relay_nodes` 表并统一调度 |
| 客户端必须**同时能访问**两台机器 | hbbs 的 5186/5187（TCP+UDP）**和** hbbr 的 5188，缺一个就"能注册但连不上桌面" |

> 静态 `-r` 仍然支持（兼容旧部署与"临时指定一台中继"的场景），但 **compose 已不再使用它**。
> 两条链路并存时共用同一套健康检查与打分，`-r` 的节点与自动注册的节点对调度完全透明。

#### 3.5.1 准备镜像（两种来源，二选一）

```bash
# ① 从本仓库源码构建（在 server/ 目录下执行，首次 15-25 分钟）
cd server
docker build --platform linux/amd64 -t wedesk-server:latest -f deploy/Dockerfile .

# ② 直接拉 CI 推的远端镜像（不用装 Rust 工具链）
docker pull liaozhiming/wedesk-server:latest
```

> 给 Linux 服务器用务必带 `--platform linux/amd64`——在 Apple Silicon / ARM
> 机器上默认会编出 arm64 镜像，x86 服务器跑不起来。
> 镜像里同时有 `hbbs` 和 `hbbr` 两个二进制，靠 `docker run` 末尾的
> 命令参数决定这次当哪个用（Dockerfile 刻意没设 ENTRYPOINT）。

#### 3.5.2 同一台机器：hbbs + hbbr 一起起

```bash
# 0) 准备工作（只需一次）
docker network create rd-net
mkdir -p /srv/wedesk/data          # 密钥与 SQLite 落盘处，务必纳入备份

# 1) ID 注册服务器
docker run -d --name rd-hbbs --restart unless-stopped \
  --network rd-net \
  -p 5186:5186/tcp -p 5187:5187/tcp -p 5187:5187/udp -p 5189:5189/tcp \
  -v /srv/wedesk/data:/root \
  --memory 256m --cpus 0.50 --pids-limit 128 \
  --log-opt max-size=10m --log-opt max-file=3 \
  -e ALWAYS_USE_RELAY=N -e ENFORCED_RELAY=N \
  -e DB_TYPE=postgres -e DB_URL=postgres://rustdesk:changeme@postgres:5432/rustdesk \
  -e RUST_LOG=warn \
  wedesk-server:latest \
  hbbs -p 5187

# 2) 中继服务器（同一台机器；HBBS_HOST 默认就是 hbbs:5187，这里显式写出来）
docker run -d --name rd-hbbr --restart unless-stopped \
  --network rd-net \
  -p 5188:5188/tcp -p 5190:5190/tcp \
  -v /srv/wedesk/data:/root \
  --memory 512m --cpus 1.00 --pids-limit 256 \
  --log-opt max-size=10m --log-opt max-file=3 \
  -e RUST_LOG=warn \
  -e HBBS_HOST=hbbs:5187 \
  -e RELAY_PUBLIC_ADDR=<客户端可达的域名或IP> \
  wedesk-server:latest \
  hbbr -p 5188

# 3) 取公钥（客户端定制的 RS_PUB_KEY 要填它）
docker exec rd-hbbs cat /root/id_ed25519.pub
```

`RELAY_PUBLIC_ADDR` 必须换成客户端能访问到的地址（域名或 IP，**不带端口**）。
留空的话 hbbs 只能按连接来源 IP 登记 —— Docker 网络下那是网桥地址，
客户端连不到，中继退化成立即失败再回退 P2P。

#### 3.5.3 只跑 ID 注册服务器（hbbs）

分机时 A 机只跑这一条，中继由 B 机的 hbbr 自动注册上来：

```bash
docker run -d --name rd-hbbs --restart unless-stopped \
  -p 5186:5186/tcp -p 5187:5187/tcp -p 5187:5187/udp -p 5189:5189/tcp \
  -v /srv/wedesk/data:/root \
  --memory 256m --cpus 0.50 \
  -e ENFORCED_RELAY=N \
  -e DB_TYPE=postgres -e DB_URL=postgres://rustdesk:changeme@postgres:5432/rustdesk \
  -e RUST_LOG=warn \
  wedesk-server:latest \
  hbbs -p 5187
```

要点：

- 放行 **5186（TCP）+ 5187（TCP 和 UDP 两条）+ 5189（TCP，WebSocket 注册通道）**；
  **5187 的 UDP 那条最容易漏**，漏了的表现是客户端 ID 一直出不来；
  5189 漏了的表现是客户端卡在"正在接入网络"，日志刷 `ws://host:5189 → deadline has elapsed`；
- `ENFORCED_RELAY=N` 允许中继指向非本机地址（分机必需）；
- 默认就是 PG 后端，hbbs 会自动连内置容器；接外部库时才需要显式指定
  `DB_URL=postgres://user:pwd@host:5432/db`（见 3.4 场景 D）；
  并建议带 `-e DB_CONNECT_RETRIES=10`（hbbs 可能先于 PG 就绪）；
- 想看中继节点注册上来了没（控制台只收环回来源，端口映射下要进容器）：
  `docker compose exec -T rd-hbbs sh -c 'echo rrg | nc 127.0.0.1 5187'`。

#### 3.5.4 只跑中继服务器（hbbr）

分机时 B 机只跑这一条，**只需要知道 hbbs 在哪**：

```bash
docker run -d --name rd-hbbr --restart unless-stopped \
  -p 5188:5188/tcp -p 5190:5190/tcp \
  -v /srv/wedesk/data:/root \
  --memory 512m --cpus 1.00 \
  -e RUST_LOG=warn \
  -e HBBS_HOST=<A机IP>:5187 \
  -e RELAY_PUBLIC_ADDR=<B机客户端可达的IP或域名> \
  -e RELAY_REGION=<区域标签，可省> \
  wedesk-server:latest \
  hbbr -p 5188
```

要点：

- 放行 **5188（TCP）**，需要 WebSocket 中继再加 **5190（TCP）**；hbbr 只在
  "名单持久化"与"节点注册"两件事上用数据库（配了 `DB_TYPE`/`DB_URL` 才连，
  连不上就退回文件模式继续转发），那个 `-v` 只为与 compose 行为一致
  （密钥目录同源）；
- 端口用**命令行 `-p`**，不要用环境变量 `PORT`——源码里环境变量 `PORT`
  会被 +1 之后再使用，写 `PORT=5188` 它去监听 5189，正好撞上 hbbs 的 WebSocket 端口；
- `RELAY_PUBLIC_ADDR` 是**唯一必填**的新变量：它是客户端视角的本机地址。
  填错 / 不填的表现是中继"注册了但连不上"——hbbr 日志里
  `中继节点已注册：<地址>` 那行会把它实际登记的地址打出来，拿它对照一下就知道；
- 中继压力大的时候就**再加一台跑同样的命令**（改 `RELAY_PUBLIC_ADDR` 即可），
  无需改 hbbs 任何配置 —— hbbs 按实时时延 / 存活 / 权重挑最优的发给两端，
  超过 210 秒没上报的节点自动剔除
  （不是客户端依次试），这是本方案最简单的横向扩容方式，详见 2.6 节；
- hbbr 的瓶颈是**出网带宽**不是内存，加内存不会更快，该盯的是带宽与并发路数。

#### 3.5.5 一键脚本 `rdrun.sh`

上面每条命令都能复制就用；如果嫌参数长，`server/deploy/rdrun.sh`
把端口关系校验、数据目录创建、网络创建、公钥打印都包了进去（只依赖 docker + bash）：

```bash
cd server/deploy
./rdrun.sh both --relay-host 10.0.0.5          # 单机：注册 + 中继
./rdrun.sh hbbs --relay-host 203.0.113.10      # 分机 A：只做 ID 注册
./rdrun.sh hbbr                                # 分机 B：只做中继
./rdrun.sh hbbr --net host                     # 中继机走宿主网络（仅 Linux）
./rdrun.sh status                              # 容器状态 + 端口规划
./rdrun.sh key                                 # 打印公钥
./rdrun.sh restart                             # 改了参数后重建容器
./rdrun.sh stop                                # 停容器（数据保留）
```

常用选项：`--image`（默认取 `.env` 的 `RD_IMAGE`）、`--data-dir`、`--hbbs-port`
/`--nat-port`/`--hbbr-port`、`--net bridge|host`、`--db-type`/`--db-url`、
`--always-relay Y|N`、`--pull`（拉远端最新镜像）、`--force`（同名容器已存在时重建）、
`--dry-run`（只打印将要执行的命令）。

它替你守着三个容易错的约束：

1. `HBBS_NAT_PORT == HBBS_PORT - 1`（不满足直接拒绝启动）；
2. 没给 `--relay-host` 时自动探测本机 IP 并要求确认，填了 `127.0.0.1`/`localhost`
   会明确告警；
3. 同名容器默认拒绝覆盖（防止误删生产容器），改参数要重建须显式 `--force`。

#### 3.5.6 运维命令对照

| 你要做什么 | Compose 方式 | `docker run` 方式 |
|---|---|---|
| 看状态 | `docker compose ps` | `docker ps -a --filter name=rd-hb` |
| 看日志 | `rdctl.py logs` / `docker compose logs -f rd-hbbs` | `docker logs -f --tail 200 rd-hbbs` |
| 停（保留数据） | `rdctl.py stop` | `docker rm -f rd-hbbs rd-hbbr` |
| 改参数后重建 | `rdctl.py restart` | `docker rm -f rd-hbbs && 重新 docker run` 或 `rdrun.sh restart` |
| 备份 | `rdctl.py backup` | 直接打包 `-v` 那个数据目录（密钥 + SQLite） |
| 升级 | `rdctl.py build` / `upgrade` | 见下 |

**升级（重点，与 compose 最大的行为差异）**：`docker run` 启动的容器
**不会因为镜像更新而自动换二进制**。`docker restart rd-hbbs` 用的还是启动时那层镜像。
必须：

```bash
docker pull liaozhiming/wedesk-server:latest   # 或重新 docker build
docker rm -f rd-hbbs rd-hbbr                   # 只删容器，数据目录不动
# 然后用完全相同的命令重新 docker run（或 rdrun.sh restart）
```

#### 3.5.7 网络模式与三个平台坑

| 场景 | 选法 |
|---|---|
| 大多数部署 | 端口映射（上面的写法），跨平台都能跑；5189/5190 现在默认 publish（UDP 受限客户端要用），不需要就注释掉那两条 |
| Linux 生产要真实来源 IP / 更低延迟 | `--network host`，但要按场景 B 想清楚 5189/5190 放行还是拒绝；此时不用写 `-p` |
| Windows / macOS Docker Desktop | 只能用端口映射，`--net=host` 不生效 |

平台相关的三个坑：

- **SELinux 系统**（RHEL / CentOS / Rocky）：`-v` 要写成
  `-v /srv/wedesk/data:/root:z`，否则容器报 permission denied；
- **Windows 的 Git Bash / MSYS**：会把 `/root` 这种参数偷偷改写成 Windows 路径
  （实测变成 `C:/Program Files/.../PortableGit/root/db_v2.sqlite3`），
  hbbs 起来就崩 `unable to open database file`。用 `MSYS_NO_PATHCONV=1`
  并把数据目录写成 Windows 风格（如 `C:/wedesk/data`），
  `rdrun.sh` 已自动处理；生产请直接在 Linux 服务器或 WSL 里跑；
- **端口映射左右必须一致**：改了 `hbbs -p 6000` 就要把 `-p` 映射也改成 6000，
  写成 `-p 9000:5187` 只会得到"端口开着但连不上"（容器里根本没进程听 9000）。

### 3.6 升级

| 升级什么 | 做法 |
|---|---|
| **服务端二进制**（改了 `server/src` 或 Dockerfile） | `python rdctl.py build && python rdctl.py restart` |
| **拉 CI 镜像**（`RD_IMAGE` 指向远端） | `python rdctl.py upgrade`（自动先备份再拉取重启；`--check` 只看不升） |
| **数据库引擎小版本** | 改 `PG_IMAGE` → `up -d` 重建 PG 容器；先 `backup` |
| **`docker run` 部署的服务端** | 拉/编新镜像 → `docker rm -f rd-hbbs rd-hbbr` → 用**完全相同的命令**重新 `docker run`（或 `rdrun.sh restart`）。**只 `docker restart` 没用**：容器用的还是启动时那层镜像，见 3.5.6 |

升级顺序永远是：**先 `backup` → 停/换 → 起 → 用一台客户端验证**。
升级上游基线（改 `server/src` 对齐新 commit）的 SOP 见 [09 服务端自编译](09-server-build.md)。

### 3.7 部署后验证清单

```
服务端
□ rdctl.py doctor 退出码 0
□ rdctl.py status 两个容器都 Up
□ 从客户端所在网络（不是服务器本机）能连上 5187：
    Test-NetConnection -ComputerName <IP> -Port 5187
□ hbbs 启动日志有 "服务端私钥来自文件"（说明密钥是复用不是新生成）
□ hbbs 日志有「中继节点注册：<地址>:5188」，且该地址客户端能访问（`echo rrg \| nc 127.0.0.1 5187`）
□ 备份已进计划任务（crontab / 任务计划程序），异地存一份

客户端
□ 启动后 ID 自动出现（说明 UDP 5187 通）
□ 两台机器互连成功，且是 P2P（看连接质量指示器；5188 几乎无流量）
□ 断开公网后仍能互连（证明没连官方服务器）
□ 需要无人值守的机器已 --silent-install
```

---

## 四、常用使用

### 4.1 客户端获取与分发

CI 产物（Actions Artifacts，保留 14 天）或推 `v*` 标签后的 GitHub Release：

| 形态 | 做法 | 适用 |
|---|---|---|
| 便携版 | 解压即用，不写注册表 | 快速试用、U 盘携带 |
| 安装版 | MSI / Inno Setup 打包 | 正式分发 |
| 静默装服务 | `wedesk.exe --silent-install` | **网管无人值守接管** |

分发前确认（与 [04 部署指南](04-deploy.md) 第四节清单一致）：

```
□ custom.toml 的 rendezvous_host 是服务器实际地址
□ RS_PUB_KEY 与 rdctl.py key 输出一致
□ 试连两台机器成功
□ 断公网后仍能连接
□ 图标与 APP_NAME 已替换（还是 RustDesk 原样说明 customize.py 没跑成功）
```

### 4.2 日常连接使用

- **ID 与随机密码都是客户端本机算的**（MAC 后 4 字节 & 0x1FFFFFFF / 启动时 PRNG），
  跟服务器无关，断网也有——只是没有意义的"离线 ID"。
- **固定密码**（远程运维刚需）：`wedesk.exe --password YourFixedPwd`，
  或在 UI 设置 → 安全 → 密码里设永久密码，验证方式改成"仅使用永久密码"。
- **换服务器调试**：设置 → 网络 → 定制服务器，或
  `wedesk.exe --custom-rendezvous-server 192.168.1.10:5187`。
- **判断 P2P 还是中继**：连接质量指示器，或看服务器 5188 流量——
  P2P 成功时 hbbr 几乎没有流量。
- **配置文件位置**：Windows 便携版在 exe 同目录 `config/`，安装版在
  `%APPDATA%\<AppName>\config\`；Linux `~/.config/<appname>/`；
  macOS `~/Library/Preferences/<appname>/`。**覆盖升级 exe 不会丢 ID，删整个目录会。**

### 4.3 批量部署预配置

`RustDesk2.toml`（与 `RustDesk.toml` 同目录）预置，镜像/组策略一起推：

```toml
verification-method = "use-permanent-password"   # 只认永久密码，忽略每次变的临时密码
```

> **比 ID 更稳的做法**：固定永久密码 + 地址簿。ID 只有 29 位空间，
> 虚拟机模板克隆 / 虚拟网卡 MAC 相同会算出完全一样的 ID，后上线的顶掉先上线的，
> 而 hbbs 不会检测也不会告警。

### 4.4 服务端日常运维速查

```bash
python rdctl.py status                    # 每天一眼
python rdctl.py logs hbbs                 # 排障
python rdctl.py ports                     # 对照防火墙
python rdctl.py key                       # 核对客户端公钥
python rdctl.py backup                    # 进计划任务
python rdctl.py doctor                    # 改完配置 / 升级后
```

`docker run` 路线（分机 / 无 compose，见 3.5）：

```bash
docker ps -a --filter name=rd-hb                 # 容器状态
docker logs -f --tail 200 rd-hbbs                # 排障
docker exec rd-hbbs cat /root/id_ed25519.pub     # 公钥
tar czf wedesk-backup.tgz -C /srv/wedesk data    # 备份：打包数据目录即可
./rdrun.sh status | key | restart | stop         # 一键脚本
```

该盯的指标：容器是否 Up（反复重启 → 看日志找端口冲突/OOM）、
hbbr 出网带宽（持续打满 → 大量走中继，查 `ALWAYS_USE_RELAY`）、
5188 流量（几乎为 0 = 打洞成功率高，健康）、`data/` 大小（db 只增不减，无清理逻辑，长期不用管）。

---

## 五、常见问题（FAQ）

按"部署时最先撞上"排序。构建类问题（vcpkg / bridge / E0658 等）见 [07 故障排查](07-faq.md)。

### 5.1 部署启动类

**Q：`rdctl.py start` 失败 / 容器起来又退出**

```bash
docker ps                  # Docker 在跑吗
cat .env                   # RELAY_HOST / RELAY_PUBLIC_ADDR 填了吗
python rdctl.py logs       # 容器日志
```

**Q：提示端口关系不满足**

`HBBS_NAT_PORT` 必须 = `HBBS_PORT - 1`（源码硬编码）。这是**刻意拒绝启动**——
宁可不启动，也不要半通的配置。要平移就三个变量一起改。

**Q：端口开着但连不上 / 能注册但连不上桌面**

| 原因 | 查法 |
|---|---|
| 映射左右两侧不一致（如 `-p 9000:5187`） | `python rdctl.py ports` 对照；容器里根本没进程听 9000 |
| 少了 UDP 5187 那条映射 | 必须写两条：一条 TCP、一条带 `/udp`。缺了 ID 就出不来 |
| `-r` 漏了端口，客户端被指到默认 21117 | hbbs 日志 `relay-servers=[...]` 是否带 `:5188` |
| 分机部署时客户端到 hbbr 的 5188 不通 | 在**客户端机器**上测：`Test-NetConnection <中继IP> -Port 5188` |
| `server/data/.env` 陷阱：把 compose 的 `.env` 误拷进 data/，键被静默变成服务端参数（`PORT=5188` 会让 hbbr 改听 5189） | `ls server/data/`，两个 `.env` 永远分开 |

**Q：hbbs 一起就崩，日志 `unable to open database file`**

三种原因，都指向"容器内 `/root` 不可写或不是你以为的那个目录"：

| 原因 | 查法 |
|---|---|
| 忘了 `-v <数据目录>:/root` | `docker inspect rd-hbbs --format '{{json .Mounts}}'` |
| Windows Git Bash 把 `/root` 改写成 Windows 路径（日志里 `DB_URL=C:/...PortableGit/root/...`） | 见 3.5.7：`MSYS_NO_PATHCONV=1` + Windows 风格数据目录 |
| SELinux 系统（RHEL/Rocky）没加 `:z` | 挂载写成 `-v /srv/wedesk/data:/root:z` |

**Q：容器秒退，日志只有 `exec format error` / `exec: "hbbr": executable file not found`**

前者是**镜像架构不对**（在 ARM 机器上编了镜像给 x86 服务器用）——
构建时显式带 `--platform linux/amd64`。后者是镜像里没有真正可执行的二进制，
见 [09 服务端自编译](09-server-build.md)（Dockerfile 的 cache mount 陷阱）。

**Q：镜像更新了，服务端却还是旧版本**

`docker restart` 不会换二进制——容器用的还是启动那一刻的镜像层。
必须删容器重建（数据目录不动）：

```bash
docker pull liaozhiming/wedesk-server:latest    # 或重新 docker build
docker rm -f rd-hbbs rd-hbbr                    # 只删容器
# 用完全相同的命令重新 docker run，或：rdrun.sh restart
```

compose 路线同理：`rdctl.py build --no-cache && rdctl.py restart`。

**Q：只有 docker、没有 compose，或者要把 hbbs 和 hbbr 分到两台机器**

走 [3.5 用 docker run 启动](#35-用-docker-run-启动hbbs--hbbr-可分离部署)：
两条 `docker run` 各自独立，或用 `rdrun.sh hbbs` / `rdrun.sh hbbr`。
分机时记住：hbbr 用 `HBBS_HOST=<A机IP>:5187` 找到 hbbs 并自动注册，
`RELAY_PUBLIC_ADDR=<B机IP>` 告诉 hbbs"客户端怎么找到我"；
客户端**同时**要能访问两台机器（A 机 5186/5187，B 机 5188）。

**Q：分机部署各要放行哪些端口**

| 机器 | 放行 | 不放行 |
|---|---|---|
| hbbs（ID 注册） | TCP 5186、TCP 5187、**UDP 5187**、TCP 5189（WS 注册通道） | 5191（健康检查，按需） |
| hbbr（中继） | TCP 5188、TCP 5190（WS 中继） | 5192（健康检查，按需） |

**Q：能加多台中继分担压力吗**

能，而且现在**不用改 hbbs 任何配置**：再找台机器跑一条同样的 `hbbr` 命令
（带上它自己的 `RELAY_PUBLIC_ADDR`），它会在 60 秒内自动注册到 hbbs 并
立即参与调度。想下线就停掉容器 —— 超过 210 秒没上报，hbbs 自动把它剔除。

（旧的 `-r a:5188,b:5188` 逗号列表仍然有效，用于"临时固定一台中继"或
兼容还没升级的部署；两种来源共用同一套健康检查与打分。）

**hbbs 会自己做优选，不再是"客户端依次试"**：它每 3 秒探测一次每个中继，
记录 RTT 与存活状态，然后按 `weight - 负载惩罚 - 时延惩罚` 挑最优的那个
下发给两端（详见 [2.6 节](#26-中继调度多节点选择与混合探测)）。
想手工干预某个中继的优先级，在控制台打标：

```
relay-set-meta 2.2.2.2:5188 cn-west 80 500    # region / weight / capacity
relay-list                                    # 看每个中继的实时时延与存活
```

注意三点：

- **它不是四层负载均衡**：hbbr 之间没有流量调度，hbbs 只是"挑一个告诉两端"。
  真正的按连接数分摊要自己在前面放四层代理；
- **静态 `-r` 节点没有 connections 上报**，负载惩罚对它们不参与打分，
  选优主要靠时延与存活；自动注册的节点两项都参与；
- 想手工干预某个中继优先级，用 `relay-set-meta` 打标（会覆盖注册上报的值，
  下次注册上报再同步刷新 region / capacity）。

**Q：端口都放行了，客户端还是连不上**

按顺序查四层，每层都可能单独挡住：

1. **云安全组**（阿里云/腾讯云/AWS 控制台）——最容易只配了 TCP 忘配 UDP；
2. **系统防火墙** `iptables -L -n` / `firewall-cmd --list-ports`；
3. **Docker 自己的链**：Docker 会在 `DOCKER-USER` 链里插规则，
   外部访问容器端口要过它，`iptables -L DOCKER-USER -n` 看有没有被 DROP；
4. **容器内真的在听吗**：`docker exec rd-hbbs sh -c 'netstat -lntup'`
   （alpine 镜像里是 `netstat -lntup`，没有就 `apk add net-tools`）。

**Q：审计日志里来源 IP 全是网关地址**

端口映射模式下 NAT 改写来源。Linux 可切 `network_mode: "host"`（先决定 5189/5190 放行还是拒绝），见场景 B。

### 5.2 连接类

| 现象 | 原因 | 排查 |
|---|---|---|
| ID 出不来 / "未就绪" | 连不上 hbbs | 从**客户端所在网络**测 5187；`rdctl.py status` 看服务端 |
| ID 正常但连不上对方 | 打洞失败且 hbbr 未通 | TCP 5188 是否放行 |
| 能连但极卡 | 走了中继 | `ALWAYS_USE_RELAY` 是否被设成 Y；是否跨 VLAN / 走了 VPN |
| 公钥不匹配 | `RS_PUB_KEY` 与服务端密钥不配对 | `rdctl.py key` 重新取，改 `custom.toml` 后**重新构建** |
| 连上立刻断开 | 认证超时 / 服务进程未运行 | 无人值守确认已 `--silent-install` |

**Q：A 机器连过去其实是 B 机器的桌面**

ID 撞了（虚拟机模板克隆 / 虚拟网卡 MAC 相同）。改用主机名作为 ID
（设置 → 安全 → 允许使用主机名作为 ID），或固定永久密码 + 地址簿。

### 5.3 密钥与数据类

**Q：密钥丢了怎么办**

代价很大：重新生成 → 改 `RS_PUB_KEY` → **重新构建并分发所有客户端**。
所以 `rdctl.py backup` 不是建议，是必需，进计划任务、异地存一份。

**Q：重启后所有客户端公钥不匹配**

私钥文件不在（被删/没挂载卷，`KEY_FILE`、`/run/secrets/id_ed25519`、
`data/id_ed25519` 都没有）。hbbs 日志里看不到
"服务端私钥来自文件" 就是在重新生成。从备份恢复 `data/` 即可。

**Q：数据库能直接看吗**

```bash
sqlite3 server/data/db_v2.sqlite3 "select id, created_at, status from peer order by created_at desc limit 20;"
```

只有一张 `peer` 表，**没有连接/审计日志**——服务端一概不记，别指望它做合规。

### 5.4 数据库类（PostgreSQL）

**Q：配了 `DB_TYPE=postgres`，数据却没进 PG**

几种静默不生效，`rdctl.py doctor` 都能实证分辨：

| 现象 | 原因 |
|---|---|
| `RD_IMAGE` 还是官方镜像 | 官方镜像不认 `DB_TYPE`，也不建后两张表，继续写 SQLite，不报错 |
| 服务还没启动过 | 三张表由 hbbs 首次启动时自动创建 |
| `DB_URL` 指向了另一个库 | 表建在别处了。`rdctl.py doctor` 会逐张报「缺少表 X」 |
| `DB_TYPE=sqlite` 但 `DB_URL` 是连接串 | 类型与地址打架，`rdctl.py doctor` 会报；两者必须自洽 |

**Q：容器并发启动 CrashLoop，日志只有一行连接错误**

hbbs 先于 PG 可用。默认 10 次 × 3 秒自动重试（`DB_CONNECT_RETRIES` 可调，设 1 恢复立刻失败）。

**Q：PG 连接串的密码会进日志吗**

不会。启动日志里密码位已脱敏成 `******`（用户名/主机/库名保留）。

**Q：多台 hbbs 怎么共享同一套 ID**

让所有实例挂载**同一个私钥文件**（docker secrets 标准方式，见 2.7）：

```
-v /d/Git/secrets/id_ed25519:/run/secrets/id_ed25519:ro
```

所有实例读同一把私钥，客户端拿到的服务端公钥始终一致。
2026-10-20 前的老版本靠 `server_config` 表共享；新版启动时会把库里的
私钥自动迁出成文件并删除该行——**不要再依赖数据库共享密钥**。

**Q：我 docker run 跑的是 hbbr，为什么日志提示要配 PostgreSQL**

镜像的默认命令是 **hbbs**（`CMD ["hbbs"]`）——`docker run` 结尾不带命令
时启动的是 hbbs，它需要数据库，日志会出现
`DB_URL 未设置，按 PG_* 推导为 …` / `DB_TYPE=postgres，但 DB_URL 不是
Postgres 连接串…` 之类的提示。跑中继必须在命令**末尾显式写 hbbr**：

```bash
docker run ... <镜像> hbbr -p 5188
```

反过来验证：hbbr（中继）**只用数据库存名单**（`relay_access_list` 一张表），
不会去写 ID 台账与审计表。挂着 `DB_*`/`PG_*` 配置时它会打一行
"hbbr 只用它持久化黑名单 / 阻断名单"的 info 然后正常跑；没配这些变量则
完全不连库。如果你在一个容器里看到"ID 注册服务器"或 peer 表相关的日志，
那它跑的一定是 hbbs，不是 hbbr。

**Q：`connection_log` 会一直涨吗**

会。审计表没有任何自动清理逻辑，请自行按 `ts` 定期归档清理。
不想落审计就设 `AUDIT_LOG=N`（热路径零开销，连缓冲都不进）。

**Q：能查到"谁连了谁"吗**

能，但要关联一次：协议里 `PunchHoleRequest` 只带被连接方的 id，
发起方此刻只有一个 IP，所以 `from_id` 在打洞事件里是空的。
用 `postgres-init.sql` 里那段把 IP 反查成 ID 的查询即可还原。
注意"连了多久"查不到 —— 见上一条限制。

### 5.5 升级与 CI 类

**Q：`docker compose up` 不生效 / 跑了旧二进制**

本地已有同 tag 镜像时**静默复用**。改了源码/Dockerfile 后：

```bash
python rdctl.py build --no-cache && python rdctl.py restart
```

（`rdctl.py start` 会检查镜像里有没有可执行的 hbbs/hbbr，损坏自动重建；
手动 `docker compose up` 没这层保护。）

**Q：CI 挂了看什么**

按顺序：① Actions 页 Annotations（`emit-error-lines.sh` 会把 rustc 报错的
`-->` 源码路径与 `= note:` 最低 Rust 版本提示刷进去）；② `ci-logs` 分支的
历史归档（存在时）；③ [07 故障排查](07-faq.md) 构建类。
历史高频问题：桌面 job 的 vcpkg/opus 缺失、Linux 缺 gstreamer dev 包、
flutter 补丁热缓存幂等、钉 git 依赖 rev 时漏改 workspace 其他成员的声明。

### 5.6 安全与合规

**Q：5189/5190 到底该开还是不该开**

**默认开（compose 当前行为）**。客户端 UDP 不通或勾了"使用 WebSocket"时要用
5189 注册，摸不到就永远卡在"正在接入网络"。

风险只在**前面挂了反向代理**时才成立：上游 WS 分支无条件信任 `X-Real-IP`/
`X-Forwarded-For`，有代理时客户端可伪造来源 IP，绕过限流/封禁并污染审计。
那种情况下要么让代理**覆写**这两个头，要么注释掉 compose 里的 5189/5190。

**Q：RustDesk 是 AGPL-3.0，我们商用要注意什么**

| 场景 | 是否触发开源义务 |
|---|---|
| 公司内部使用 | ❌ 不触发（未"分发"给外部） |
| 只把 exe 发给外部客户、不给源码 | ⚠️ **违反 AGPL §13** |
| 仓库公开 | ✅ 天然满足 |

`client/LICENCE` 就是 AGPL-3.0，不要删也不要改。

---

## 六、相关文档

| 想看什么 | 去 |
|---|---|
| 仓库结构、改 X 动哪里 | [00 仓库结构导览](00-repo-layout.md) |
| 客户端三层架构、构建产物拼接 | [01 架构总览](01-architecture.md) |
| 一次连接的完整时序（打洞/中继细节） | [02 运行原理](02-runtime.md) |
| 本地编译 exe / 云编译 | [03 本地构建](03-build.md) / [03b CI 云构建](03b-ci.md) |
| 客户端分发三种形态、上线检查表 | [04 部署指南](04-deploy.md) |
| 服务端自编译、升上游 | [09 服务端自编译](09-server-build.md) |
| 端口推导、网络方案、三个坑 | [10 端口与网络规划](10-ports.md) |
| 容器逐个拆解、启动日志逐行读、运维指标 | [11 服务端运维与客户端接入](11-server-ops.md) |
| PostgreSQL 后端设计与迁移 | [12 PostgreSQL 后端](12-postgres-backend.md) |
| 构建/连接/定制类故障 | [07 故障排查](07-faq.md) |
