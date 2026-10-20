# 11 · 服务端运维与客户端接入

> 目标：搞懂两个容器各自在干什么、客户端怎么连上来、日常怎么运维、
> 想加功能往哪加、哪些坑一踩就出事。
>
> 排障定位请看 [07-faq.md](07-faq.md)，端口细节看 [10-ports.md](10-ports.md)，
> 本文只讲"运维视角怎么理解这一套"。

---

## 一、心智模型：一次连接里有几个参与者

先把最容易搞错的一件事说清楚：**服务端不传输画面**。

| 参与者 | 在哪 | 干什么 | 不干什么 |
|---|---|---|---|
| **rd-hbbs** | 我们的容器 | 登记 `ID → 当前外网地址`，撮合双方，协调打洞 | **不转发任何画面数据** |
| **rd-hbbr** | 我们的容器 | 打洞失败时，把双方的流量来回搬 | 不参与 ID 分配、不存配置 |
| **客户端 A** | 员工电脑 | 发起连接（控制端） | — |
| **客户端 B** | 员工电脑 | 接受连接（被控端），同一个二进制 | — |

所以：

- **服务器带宽压力取决于打洞成功率，不是在线人数。** 内网同网段打洞几乎必成，
  这时 hbbr 是闲着的；跨对称 NAT（企业网/4G/校园网）大量走中继时，带宽才起来。
- **hbbs 挂了的后果**：新客户端拿不到 ID、已注册的客户端连不上对方（无法撮合），
  但**已经建立起来的 P2P 连接不受影响**。
- **hbbr 挂了的后果**：只有"本来要走中继"的连接会失败，P2P 直连照常。

---

## 二、容器逐个拆解

`docker-compose.yml` 里就两个业务容器 + 一个网络 + 一个卷。

### 2.1 `rd-hbbs` —— ID 注册与打洞协调

```yaml
command: hbbs -p 5187 -r ${RELAY_HOST}:5188
```

| 参数 | 含义 | 为什么这么写 |
|---|---|---|
| `-p 5187` | 主端口 | 由它**推导**出另外两个端口，见下 |
| `-r host:5188` | 告诉客户端"中继在哪" | **必须带端口**。漏了端口 hbbs 会按上游默认的 21117 拼，客户端被指到一个没人监听的端口，表现为"能注册但连不上桌面" |

`-p 5187` 会派生三个端口（源码 `src/rendezvous_server.rs`）：

```
5186 (TCP)  ← 5187 - 1   NAT 类型探测
5187 (TCP)  ← 主端口     打洞协调 / 连接请求
5187 (UDP)  ← 主端口     注册 + 心跳（同一端口的另一种协议）
5189 (TCP)  ← 5187 + 2   WebSocket 注册通道（已 publish）
```

`compose` 里 UDP 必须单独写一条 `/udp`，`"5187:5187"` 只映射 TCP。
**少了 UDP 那条，客户端注册会一直超时。**

启动时它会做这些事（按顺序）：

1. 读 `./id_ed25519`（没有就生成一对 ed25519 密钥）—— **只在首次生成，之后永远复用**
2. 打开 SQLite 数据库 `DB_URL`
3. 在三个端口上开始监听
4. 开始接受客户端的注册/心跳

### 2.2 `rd-hbbr` —— 中继转发

```yaml
command: hbbr -p 5188
```

```
5188 (TCP)  ← 主端口      中继数据
5190 (TCP)  ← 5188 + 2   WebSocket 中继（已 publish）
```

hbbr 是**近乎无状态**的：不读密钥、不认客户端身份，
它只负责把一条 TCP 连接上的字节流搬到另一条上。
所以它随时可以重启、扩容、甚至起多个实例，不影响在线连接以外的任何东西。

唯一的例外是**名单**：配了数据库时，黑名单 / 阻断名单落在
`relay_access_list` 表（见 6.2 节），并周期性重载 —— 这是多台中继
能共享同一份封禁的原因。这个状态是"可读可丢"的：库连不上时 hbbr
退回文件模式继续转发，不会因为一张名单拒绝启动。

> ⚠️ `hbbr` 的环境变量 **`PORT` 会 +1 之后才使用**（历史包袱，见 `src/hbbr.rs`）。
> 写 `PORT=5188` 它就去监听 **5189**，正好撞上 hbbs 的 WebSocket 端口。
> 所以端口一律用命令行 `-p`，别用 `PORT` 环境变量。

### 2.3 `rd-net` —— 容器网络

一个普通的 bridge 网络。两个容器通过**服务名**互相访问（`hbbs` ↔ `hbbr`）。
这就是为什么 `command` 里 `-r` 可以写 `${RELAY_HOST}` 而 hbbs 日志里显示
`relay-servers=["127.0.0.1:5188"]` —— 这里 127.0.0.1 是**容器视角**的 127.0.0.1
还是宿主机，取决于 `RELAY_HOST` 填什么，见 [10-ports.md](10-ports.md) 方案 C。

### 2.4 `./data:/root` —— 唯一需要备份的卷

```
server/data/
├── id_ed25519        # 私钥
├── id_ed25519.pub    # 公钥（rdctl.py key 打印的就是它）
├── db_v2.sqlite3     # peer 注册表（仅 SQLite 后端时，默认是 PostgreSQL）
├── blacklist.txt     # hbbr 的黑名单（可选，没有就不创建）
└── blocklist.txt     # hbbr 的封禁名单（可选）
```

`WORKDIR /root` 且 `HOME=/root`，所以容器内所有相对路径都落在这一卷上。

> 默认后端是 PostgreSQL，数据实际落在 `server/deploy/pgdata/` 这个卷里，
> 所以**两个卷都要备份**：`rdctl.py backup` 会同时处理（密钥文件 + `pg_dump`）。
> 详见 [12 · 服务端数据统一存 PostgreSQL](12-postgres-backend.md)。

**这一卷就是服务端的全部状态。** 删了它 = 换一台新服务器。

### 2.5 资源上限在限制什么

| 容器 | mem_limit | cpus | 说明 |
|---|---|---|---|
| rd-hbbs | 256m | 0.50 | 只撮合不转发，常驻 10-40 MB。上限是"异常时快速重启"用的，不是性能预估 |
| rd-hbbr | 512m | 1.00 | **内存不是它的瓶颈，出网带宽才是**。每路中继约几 MB 缓冲 |

给 hbbr 加到 1 GB 不会让中继更快——瓶颈在网卡，不在内存。

---

## 三、拿你的启动日志逐行读

下面用你实际跑起来的日志对着源码讲一遍。**看懂这份日志，80% 的服务端问题能自己定位。**

### 3.1 rd-hbbr 的日志

```
INFO [src/relay_server.rs:65] #blacklist(blacklist.txt): 0
INFO [src/relay_server.rs:80] #blocklist(blocklist.txt): 0
```

启动时从**当前工作目录**读 `blacklist.txt` / `blocklist.txt`，每行一个 IP。
`0` 表示文件不存在（或为空）。**文件不存在是正常状态，不是错误。**

```
INFO [src/relay_server.rs:86] Listening on tcp :5188
INFO [src/relay_server.rs:88] Listening on websocket :5190
```

`5190 = 5188 + 2`，由源码写死，没有开关可关。
**容器内始终在听，只是 compose 没有 publish 它**——别以为没开。

```
INFO [src/relay_server.rs:91] Start
INFO [src/relay_server.rs:115] DOWNGRADE_THRESHOLD: 0.66
INFO [src/relay_server.rs:125] DOWNGRADE_START_CHECK: 1800s
INFO [src/relay_server.rs:135] LIMIT_SPEED: 32Mb/s
INFO [src/relay_server.rs:146] TOTAL_BANDWIDTH: 1024Mb/s
INFO [src/relay_server.rs:156] SINGLE_BANDWIDTH: 128Mb/s
```

这几行全是 `check_params()` 从**环境变量**读出来的限流参数（见 6.1 节）。
打印出来是好事：**看一眼就知道限流有没有生效**。

### 3.2 rd-hbbs 的日志

```
INFO [src/common.rs:366] Private key comes from id_ed25519
```

密钥来自文件，**不是本次新生成的**。如果看到的是"生成新密钥"而不是这一行，
说明 `data/id_ed25519` 不在——所有已分发客户端都会公钥不匹配。

```
INFO [src/rendezvous_server.rs:1292] Key: bMwHjGjrhpNvD+lyhx2daVmrhmq94hyDfHNisznZTl0=
```

服务端公钥。**这一串必须和客户端 `custom.toml` 里的 `RS_PUB_KEY` 完全一致**，
否则客户端拒绝连接（这是防中间人的安全特性，不是 bug）。
用 `python rdctl.py key` 可以随时再打印一次。

```
INFO [src/peer.rs:75] DB_URL=postgres://rustdesk:******@postgres:5432/rustdesk
INFO [src/database.rs:xxx] Database backend = postgres (DB_URL=postgres://rustdesk:******@postgres:5432/rustdesk)
INFO [src/rendezvous_server.rs:xxx] 服务端密钥来源: 数据库 server_config
INFO [src/rendezvous_server.rs:137] serial=0
```

数据库连接串 —— **密码位已脱敏成 `******`**，用户名/主机/库名保留，够排障用
（服务端刻意这样做：连接串含明文口令，直接打日志等于把密码写进日志文件）。
紧接着一行会告诉你**钥匙这次是从哪来的**，这条最值钱：
出现 `本次新生成` 就意味着数据目录被换掉、或密钥托管没生效，客户端 ID 会漂。
`serial` 是给已废弃的配置更新机制用的，恒为 0，忽略即可。

```
INFO [src/common.rs:105] rendezvous-servers=[]
INFO [src/rendezvous_server.rs:173] mask: None
INFO [src/rendezvous_server.rs:174] local-ip: ""
INFO [src/common.rs:105] relay-servers=["127.0.0.1:5188"]
```

- `rendezvous-servers=[]`：我们没有配多级 hbbs 集群，单节点，正常
- `mask: None`：没配"哪些网段算内网"，因此 hbbs 无法判定连接来自 LAN，
  **它会把所有连接都当作外网处理**（影响的是"是否尝试内网直连"的判断，不影响连通性）
- `local-ip: ""`：没指定对外宣告的 IP，用默认路由
- `relay-servers=[...]`：**这一行是客户端拿到的中继地址**。它来自 `command` 里的 `-r`。
  如果这里显示的不是客户端能访问到的地址，中继必然连不上

```
INFO [src/rendezvous_server.rs:181] Listening on tcp/udp [::]:5187
INFO [src/rendezvous_server.rs:182] Listening on tcp [::]:5186, extra port for NAT test
INFO [src/rendezvous_server.rs:186] Listening on websocket [::]:5189
INFO [src/rendezvous_server.rs:191] ALWAYS_USE_RELAY=N
INFO [src/rendezvous_server.rs:223] Start
```

`[::]` 表示监听所有网卡（IPv6 any，通常同时接受 IPv4）。
到这一步服务端就绪了。

### 3.3 日志里的异常信号

| 日志现象 | 含义 |
|---|---|
| 完全没有 `Listening on tcp/udp` 那三行 | 端口被占或参数解析失败，容器会退出 |
| `Private key comes from id_ed25519` 缺失 | 密钥没找到，正在重新生成 → **所有客户端要重配** |
| `relay-servers` 是空或不可达地址 | 中继不可用，打洞失败的场景全都会连不上 |
| 长时间一行日志都没有 | 正常。默认 `RUST_LOG=warn`，心跳和注册不落日志 |
| `ERROR` / `WARN` 频繁出现 | 值得看，`RUST_LOG=info` 可以调高 |

---

## 四、客户端怎么连上服务端

### 4.1 服务器地址的五层优先级

客户端不是只有一个地方能指定服务器。`client/libs/hbb_common/src/config.rs` 的
解析链（**从高到低**）：

```
① 命令行参数 / API 传入
② 界面里用户手填的 custom-rendezvous-server
③ 编译期注入（customize.py 改的 PROD_RENDEZVOUS_SERVER）
④ 本地配置文件 RustDesk.toml
⑤ 源码兜底常量 RENDEZVOUS_SERVERS  ← 我们也改了这一层
```

**我们的做法是同时改 ③ 和 ⑤**（见 [05-customize.md](05-customize.md)）。
好处是出厂默认永远指向自家服务器，同时员工临时改服务器调试也不会被写死。

> 这五层是"或"的关系：**高优先级为空才往下走**。
> 所以如果员工在界面里手填过一次，那一台的默认值就被覆盖了 —— 批量部署时要注意。

### 4.2 三步接入

**第一步：确认服务端在跑、端口通**

```bash
cd server && python rdctl.py status     # 看 rd-hbbs / rd-hbbr 是否 Up
python rdctl.py ports                   # 打印端口规划，对照防火墙
```

从**客户端所在的网络**验证端口（不是从服务器本机）：

```bash
# 在员工电脑上执行
Test-NetConnection -ComputerName <服务器IP> -Port 5187
Test-NetConnection -ComputerName <服务器IP> -Port 5186
Test-NetConnection -ComputerName <服务器IP> -Port 5188
# UDP 没有 PowerShell 内置探测，看客户端能否出现 ID 即可
```

**第二步：客户端拿到服务器地址**

定制构建出来的客户端**已经内置了** `wedesk.alsoapp.com:5187`（或你改的值），
正常情况下**什么都不用填**，启动后 ID 会自动出现。

如果要用别的服务器（调试/多环境），在界面里填：

> 设置 → 网络 → 定制服务器（custom-rendezvous-server）

或启动时用命令行指定：

```bat
wedesk.exe --custom-rendezvous-server 192.168.1.10:5187
```

**第三步：验证连的是自家服务器**

最可靠的办法是**断公网测试**：拔掉网线（或禁用外网网卡），
两台内网机器仍能互连 —— 证明完全不依赖官方服务器。

辅助判断：

```bash
python rdctl.py key     # 服务端公钥
# 客户端日志里搜 "Key:" 或连接失败提示"公钥不匹配"
```

### 4.3 连接建立时发生了什么（简版）

```
控制端                 hbbs :5187                hbbr :5188              被控端
  │ ① 我要连 ID 12345  │                          │                       │
  │────────────────────>│                          │                       │
  │                     │ ② 有人要连你，请打洞      │                       │
  │                     │─────────────────────────────────────────────────>│
  │ ③ 对方的外网地址     │                          │                       │
  │<────────────────────│                          │                       │
  │                     │                          │                       │
  │ ④a 同时向对方发包 ─────────────────────────────────────────────────────>│
  │<───────────────────────────────────────────────────── ④b 同时发包      │
  │                     │                          │                       │
  ├─ 打洞成功：直连，数据不经过服务器 ✅ 零服务器带宽
  └─ 打洞失败：双方连 hbbr，由它转发 ⚠️ 吃服务器出网带宽
```

**判断当前是 P2P 还是中继**：连接质量指示器，或直接看 5188 端口流量——
**P2P 成功时 hbbr 几乎没有流量**。

### 4.4 无人值守与批量部署

要让"员工不在电脑前 / 没登录时也能被连"，必须装系统服务：

```bat
wedesk.exe --silent-install        :: 静默安装服务
```

装上之后客户端随系统启动，登录屏和 UAC 界面也能操作。

**批量预配置**可以写进 `RustDesk2.toml`（与 `RustDesk.toml` 同目录）：

```toml
verification-method = "use-permanent-password"   # 只认永久密码，忽略每次变的临时密码
```

配合固定密码，员工机器重启、换人都能连：

```bat
wedesk.exe --password YourFixedPwd
```

> **比 ID 更稳的做法**：不用随机 ID，改用**固定永久密码 + 地址簿**。
> ID 空间只有 29 位，虚拟机模板克隆 / 虚拟网卡 MAC 相同会算出完全一样的 ID，
> 后上线的那台会把前面那台顶掉。详见 [07-faq.md](07-faq.md)。

### 4.5 客户端侧的网络要求

| 角色 | 需要放行 |
|---|---|
| 被控端 | 入站不需要开（打洞是出站发起）；但要能**出站**到服务器的 5186/5187/5188 |
| 控制端 | 同上 |

**两端都不需要对公网开放入站端口**，这是打洞机制带来的好处。
但如果两侧都是对称 NAT，就只能靠 hbbr 中继，此时**服务器 5188 必须可达**。

---

## 五、日常运维

### 5.1 命令表

都在 `server/` 目录下执行：

| 命令 | 作用 | 说明 |
|---|---|---|
| `python rdctl.py start` | 启动 | 启动前会校验端口关系、检查镜像是否可用，**镜像损坏会自动重建** |
| `python rdctl.py stop` | 停止 | |
| `python rdctl.py restart` | 重启 | 改配置后用它 |
| `python rdctl.py status` | 状态 | 容器状态 + 数据库表行数 |
| `python rdctl.py logs [hbbs\|hbbr]` | 跟日志 | 不带参数跟全部；Ctrl+C 退出 |
| `python rdctl.py key` | 打印服务端公钥 | 与客户端 `RS_PUB_KEY` 对照 |
| `python rdctl.py ports` | 打印当前端口规划 | 改端口后用它核对 |
| `python rdctl.py build [--no-cache]` | 从源码构建镜像 | 首次 15-25 分钟；改过源码/Dockerfile 用 `--no-cache` |
| `python rdctl.py backup` | 备份密钥 + 数据库 | **必做，进计划任务** |
| `python rdctl.py uninstall` | 卸载容器 | 保留 `data/` |

原生命令也够用：

```bash
docker compose ps              # 容器状态
docker compose logs -f --tail 200 rd-hbbs
docker compose restart rd-hbbr
docker stats rd-hbbs rd-hbbr   # 真实资源占用（校准 compose 里的上限）
```

### 5.2 看日志的正确姿势

```bash
# 只看 hbbr 的实时日志
python rdctl.py logs hbbr

# 抓最近 500 行里的错误
docker compose logs --tail 500 rd-hbbs rd-hbbr | grep -iE "error|warn|panic"

# 调高日志级别（会变详细，磁盘写入也变多，排查完记得调回）
# 改 compose 里的 RUST_LOG=info，然后 restart
```

**默认 `RUST_LOG=warn` 是刻意为之**：hbbs 在 `info` 级别下每注册/心跳都落一行，
长期运行的磁盘写入与格式化开销是可观测的，而那些信息对运维没有价值。
排障时临时调高即可。

日志轮转已经在 compose 里配好（`max-size: 10m` × `max-file: 3`，
即每个容器最多约 30 MB），不会无限涨。

### 5.3 备份

**只备份 `server/data/` 就够**（密钥 + 数据库，全在里面）：

```bash
cd server && python rdctl.py backup
# 产物在 server/backup/<时间戳>/
```

建议：

```bash
# Linux 服务器上加 crontab，异地存一份
0 3 * * * cd /opt/wedesk/server && python3 rdctl.py backup
```

> **密钥丢失的代价**：重新生成 → 改 `RS_PUB_KEY` → **重新构建并分发所有客户端**。
> 这不是"建议备份"，是必需。

### 5.4 升级

分两种：

| 升级什么 | 怎么做 |
|---|---|
| **服务端**（hbbs/hbbr 本身） | 见 [09-server-build.md](09-server-build.md) 第五节，改动 commit sha → 重打补丁 → 重建镜像 |
| **客户端** | 不走官方更新通道（已封死），靠人工分发新 zip。**覆盖 exe 不会丢 ID**，因为配置在 `config/` 目录 |

升级服务端的顺序建议：先 `backup` → 再停容器 → 换镜像 → 起容器 → 用一台客户端验证。

### 5.5 该盯哪些指标

| 指标 | 怎么看 | 异常意味着 |
|---|---|---|
| 容器是否 Up | `rdctl.py status` | 反复重启 → 看日志找原因（端口冲突/OOM） |
| hbbr 出网带宽 | `docker stats rd-hbbr` | 持续打满 → 大量走中继，查 `ALWAYS_USE_RELAY` 与 NAT 类型 |
| hbbs 内存 | 同上 | 常驻 10-40 MB，涨到上百 MB 说明 peer 表异常膨胀 |
| 5188 端口流量 | 几乎为 0 | 打洞成功率高，健康状态 |
| `data/` 目录大小 | `du -sh server/data` | db 只增不减（没有清理逻辑），长期不用管 |
| 磁盘 | `df -h` | 日志已轮转，主要看 data 卷 |

---

## 六、扩展与二次开发

### 6.1 服务端可调参数

改 `server/deploy/docker-compose.yml` 的 `environment:` 段，然后 `restart`。

**hbbr 的限流参数**（`src/relay_server.rs` 的 `check_params()`）：

| 环境变量 | 默认 | 单位 | 作用 |
|---|---|---|---|
| `DOWNGRADE_THRESHOLD` | 0.66 | 比例 | 带宽用到这个比例就开始降速 |
| `DOWNGRADE_START_CHECK` | 1800 | 秒 | 降速检查周期 |
| `LIMIT_SPEED` | 32 | Mb/s | 单连接限速 |
| `TOTAL_BANDWIDTH` | 1024 | Mb/s | 总带宽上限 |
| `SINGLE_BANDWIDTH` | 128 | Mb/s | 单会话带宽上限 |

**hbbs 的参数**：

| 环境变量 / 参数 | 作用 |
|---|---|
| `ALWAYS_USE_RELAY` | `Y` = 强制所有连接走中继。跨内网连通率低时开，代价是全部吃服务器带宽 |
| `MAX_PUNCH_REQUESTS` | 打洞请求频率上限（默认 100000） |
| `DB_URL` | SQLite 路径 |
| `RUST_LOG` | 日志级别 |
| `-k <KEY>` | 只允许携带相同 key 的客户端连接（额外的一道准入，默认不设） |

> 这些值改完**立即生效**（重启容器后），不需要重新编译。
> 打印在启动日志里，改完看一眼确认生效。

### 6.2 黑名单 / 封禁名单

两份名单，区别要分清：

| 名单 | 效果 |
|---|---|
| `blacklist`（黑名单） | 命中后被**限速**（走 `LIMIT_SPEED`），不拒绝连接 |
| `blocklist`（阻断名单） | 命中后**直接断开**，最严格 |

> ⚠️ 两者只在 **hbbr（中继路径）** 上生效。
> P2P 直连的流量根本不经过服务器，**名单对它无效**。

#### 推荐方式：控制台命令（改动立即生效，且会持久化）

```bash
docker compose exec hbbr /usr/bin/rustdesk-utils 2>/dev/null || true   # 端口 5188 也是控制台
# 标准做法：直接连控制台端口发一行命令
printf 'blacklist-add 1.2.3.4 暴力破解\n' | nc <server> 5188
printf 'blacklist 1.2.3.4\n'            | nc <server> 5188   # 查询是否存在
printf 'blacklist\n'                     | nc <server> 5188   # 列出全部（含备注与加入时间）
printf 'blacklist-remove 1.2.3.4\n'      | nc <server> 5188
printf 'blocklist-add 5.6.7.8\n'         | nc <server> 5188
```

#### 落盘位置

- **配了数据库（compose 默认）** → 落在 `relay_access_list` 表，重启不丢；
  启动时若库为空而 `blacklist.txt` 有内容，会自动迁移进库一次。
  多台 hbbr 会每 `ACCESS_RELOAD_SECS`（默认 60）秒重载一次，天然共享同一份名单。
- **没配数据库** → 落在 `server/data/blacklist.txt` / `blocklist.txt`
  （本次改动补上了写回；上游原本只在启动时读、运行期改动重启即丢）。

启动日志里 `#blacklist: N` / `#blocklist: N` 会告诉你加载了多少条。

> 数据库连不上时 hbbr **不会**拒绝启动，而是退回文件模式并打一条 error：
> 中继的可用性优先于名单的持久化，绝不能为一张名单把整条转发链路拖住。

### 6.3 数据库里到底有什么

只有一张表 `peer`（`src/database.rs`）：

| 字段 | 内容 |
|---|---|
| `id` | 客户端 ID |
| `uuid` / `pk` | 客户端公钥 |
| `user` / `note` | 备注 |
| `info` | 注册时上报的网络信息 |
| `created_at` | 注册时间 |
| `status` | 在线状态 |

可以直接查（PostgreSQL 是默认后端）：

```bash
cd server/deploy
docker compose exec -T postgres psql -U rustdesk -d rustdesk \
  -c "select id, created_at, status from peer order by created_at desc limit 20;"
```

SQLite 后端下则查本地文件：

```bash
sqlite3 server/data/db_v2.sqlite3 "select id, created_at, status from peer order by created_at desc limit 20;"
```

**连接审计**记在 `connection_log` 表里（仅 PostgreSQL 后端）：

```bash
docker compose exec -T postgres psql -U rustdesk -d rustdesk \
  -c "select ts, event, from_ip, to_id, via_relay, detail
      from connection_log order by ts desc limit 50;"
```

> 审计只覆盖"注册"与"打洞撮合"两个环节，**不记录会话时长**：
> 真正的连接建立后数据不经过 hbbs，服务端无从得知连了多久，
> 要精确时长得在 hbbr 侧统计。另外 `connection_log` 会持续增长，
> 请自行按 `ts` 定期归档清理。

### 6.4 想加功能时的落点

| 你想做的事 | 落点 | 要不要重新编译 |
|---|---|---|
| 改限流 / 改日志级别 / 改强制中继 | `docker-compose.yml` 的 `environment` | 否，重启即可 |
| 封禁某些 IP | `data/blacklist.txt` | 否，重启 hbbr |
| 改端口 | `.env` + `custom.toml` + 防火墙 | **是**，客户端要重新构建 |
| 改服务端业务逻辑 | `server/src/src/` | 是，重编镜像 |
| 改客户端 UI / 加功能 | 见 [06-maintenance.md](06-maintenance.md) 的 SOP | 是 |
| 接自己的认证 / 审计 | 上游没有现成钩子，需要改源码或外挂代理 | 是，且最重 |

**服务端二次开发的入口**：

```
server/src/
├── src/main.rs               # hbbs 入口（命令行参数在这定义）
├── src/hbbr.rs               # hbbr 入口
├── src/rendezvous_server.rs  # hbbs 主逻辑：注册、心跳、打洞协调
├── src/relay_server.rs       # hbbr 主逻辑：转发、限流
├── src/peer.rs               # peer 表读写、DB_URL 处理
├── src/database.rs           # SQLite 表结构（只有 peer 一张）
└── src/common.rs             # 参数解析（含 .env 加载，见 7.1）、工具函数
```

> 服务端是**独立 Cargo workspace**，与客户端（Rust 1.75）隔离，
> 用 ≥1.78 工具链编译，互不影响。构建方式见 [09-server-build.md](09-server-build.md)。

---

## 七、必须知道的坑

按"踩了之后多难查"排序。

### 7.1 `server/data/` 下不要放 `.env` —— 会被当成配置文件读

两个二进制启动时都会尝试读**工作目录**下的 `.env`，并把里面的键当成参数
（`src/common.rs` 的 `init_args()`、`src/hbbr.rs`）：

```rust
if let Ok(v) = Ini::load_from_file(".env") { /* set_arg(k, v) */ }
```

而容器的 `WORKDIR` 是 `/root`，正是挂载 `./data:/root` 的地方。
**所以只要 `server/data/.env` 存在，它的键就会被静默变成服务端参数。**

典型事故：把 compose 用的 `server/.env` 误拷进 `data/`，里面的
`HBBS_PORT=5187` 无害（不是已知参数会被忽略），但 `PORT=5188` 会让
hbbr 改听 **5189**（PORT 会 +1），或者 `RELAY_HOST` 里的值覆盖掉
`-r` 参数 —— 症状全是"端口开着但连不上"，且启动日志里看不出异常。

**优先级**：`.env` 文件 < `-c` 指定的配置文件 < 命令行参数。
所以命令行 `-p 5188` 能覆盖 `.env`，但 `.env` 能覆盖默认行为。

**防范**：`server/.env` 和 `server/data/` 永远分开，别互拷。

### 7.2 改端口要连带改客户端

`HBBS_PORT` / `HBBS_NAT_PORT` / `HBBR_PORT` 同时决定**宿主机映射**和**容器内 `-p`**，
两侧永远一致。但改完之后：

- `build/client/custom.toml` 的 `rendezvous_host` 必须跟着改
- **客户端必须重新构建分发**，否则还在按旧端口连

`HBBS_NAT_PORT` 必须等于 `HBBS_PORT - 1`（源码 `nat_port = port - 1`，没有开关）。

### 7.3 WebSocket 端口（5189 / 5190）不要 publish

它们无条件信任 `X-Real-IP` / `X-Forwarded-For` 头且不校验来源，
**任何人直连都能伪造来源 IP**，绕过限流/封禁并污染日志。
只有前面挂了反向代理、由代理统一覆写该头时才可开放，且必须防火墙限制仅代理 IP 可达。

详见 [10-ports.md](10-ports.md) 坑 1。

### 7.4 `docker compose up` 不会重新构建

本地已有同 tag 镜像时**静默复用**。改过源码或 Dockerfile 后：

```bash
docker compose build --no-cache && docker compose up -d
```

`rdctl.py start` 会在启动前检查镜像里有没有可执行的 hbbs/hbbr，
发现损坏会自动重建 —— 但如果你手动 `docker compose up`，这层保护不生效。

### 7.5 `.env` 的 `RD_IMAGE` 一旦有值，本地 Dockerfile 就不参与

`image: ${RD_IMAGE:-wedesk-server:latest}` + `build:` 的组合：
`RD_IMAGE` 有值就**直接用（或拉取）那个镜像**。
你以为在跑自己编的二进制，其实跑的是远端的，也不会带我们关掉的版本检查补丁。

要"完全本地化"就把它注释掉。

### 7.6 中继带宽是唯一真正的性能瓶颈

`ALWAYS_USE_RELAY=Y` 会让**所有**连接走中继。10 路 1080p 中继轻松吃掉
几十 Mb/s 出网带宽。内网同网段务必保持默认的 `N`，让机器走 P2P 直连。

### 7.7 端口映射模式下，服务端看到的来源 IP 全是网关

NAT 会把来源改写成网关 IP。需要真实来源 IP（审计/限流）时，
Linux 上可以换 `network_mode: "host"`，但**必须先确认防火墙挡住了 5189/5190**，
否则 7.3 的保护直接消失。

### 7.8 ID 会重复，且没有任何服务端防护

ID 由客户端本机算（MAC 后 4 字节 `& 0x1FFFFFFF`），29 位空间。
虚拟机模板克隆 / 虚拟网卡 MAC 相同 → ID 必然相同 → 后上线的顶掉先上线的。
hbbs 不会检测也不会告警。稳的做法是固定永久密码 + 地址簿，别依赖随机 ID。

---

## 八、相关文档

| 想看什么 | 去 |
|---|---|
| 连接建立的完整时序 | [02-runtime.md](02-runtime.md) |
| 端口推导规则与网络方案 | [10-ports.md](10-ports.md) |
| 服务端怎么自编译 / 报错 | [09-server-build.md](09-server-build.md) |
| 客户端定制（品牌、服务器地址） | [05-customize.md](05-customize.md) |
| 具体故障排查 | [07-faq.md](07-faq.md) |
| 客户端加功能 SOP | [06-maintenance.md](06-maintenance.md) |
