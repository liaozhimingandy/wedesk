# 12 · 服务端数据统一存 PostgreSQL

> **先说结论**：PostgreSQL 是**默认后端**。
> 服务端除客户端之外的持久化数据全部落在这一个库里，一共三张表：
>
> | 表 | 内容 |
> |---|---|
> | `peer` | ID / 设备台账（ID、uuid、公钥、备注、注册信息、状态） |
> | `server_config` | 服务端自身状态 —— 托管 `id_ed25519` 密钥对 |
> | `connection_log` | 连接审计 —— 谁连了谁、时间、走中继还是 P2P |
>
> 新建一个部署什么都不用改；只有想退回 SQLite 或接外部 Postgres 才需要读这一篇。

---

## 一、为什么必须改源码，光配环境变量没用

hbbs 的 ID 存储原本**写死是 SQLite**：

- 上游 `src/database.rs` 用 `sqlx 0.6`，只开了 `sqlite` 特性；
- `DbPool` 直接硬编码 `SqliteConnectOptions`；
- 用了 `sqlx::query!` / `query_as!` **编译期宏**——编译阶段就要连一个真实库校验 SQL。

官方 OSS 也明确不做外部数据库。所以**改环境变量不会让它连上 PostgreSQL**，
必须改服务端源码。源码就在本仓库 `server/src/`，改完重新编译镜像即可
（构建流程见 [09 · 服务端自编译](09-server-build.md)）。

---

## 二、为什么抽象成 trait，而不是写两套函数

一个绕不开的约束：**sqlx 的 `query!` 宏在编译期绑定单一数据库类型**。
同一个二进制不可能用宏同时支持 SQLite 和 PostgreSQL —— 编译期的
`DATABASE_URL` 只能指向一种库。

所以做法是：

```
        Database (facade，对 peer.rs 暴露原 API)
                    │
        ┌───────────┴───────────┐
   SqliteBackend            PgBackend
   （保留原 query! 宏）      （运行时 query()，不需要编译期连库）
```

- **`Database` 是门面**，`Arc<dyn DbBackend>`，保持原有 `new / insert_peer /
  update_pk / get_peer` 签名。
- **SQLite 侧保留 `query!` 宏**：它已被 CI 的编译期校验覆盖，改动风险最低。
- **PostgreSQL 侧只用运行时 `query()`**：编译时不要求有一台活的 PostgreSQL，
  镜像构建不依赖数据库可用性。

同理，**2026-10 新增的两张表（`server_config` / `connection_log`）一律不用
`query!` 宏**。宏会在编译期拿构建机上的模板库 `server/src/db_v2.sqlite3`
（里面只有 `peer` 表）去校验表名与列，新表一旦写进宏就必然编译失败。

---

## 三、DB_TYPE 是唯一权威，DB_URL 可以省

这是最容易出错、也是**唯一需要记住**的一条规则。

部署层有三处会分别写 `DB_TYPE` 与 `DB_URL`（`.env` / `docker-compose.yml` /
`rdrun.sh`）。只要漏改一处，就会出现"类型说 Postgres、地址却指向 SQLite 文件"
这种**半通配置**：服务起得来，但数据落错了地方，日志里完全看不出来。

所以代码里改成了：**先由 `DB_TYPE` 定后端，再按这个类型挑地址**。

| `DB_TYPE` | `DB_URL` 的处理 |
|---|---|
| `postgres`（**默认**） | 是 `postgres://` 连接串 → 用它；否则由 `PG_USER` / `PG_PASSWORD` / `PG_HOST` / `PG_DB` 拼一个并 WARN |
| `sqlite` | 是文件路径 → 用它；给的是 `postgres://` → ERROR 日志后退回默认 `./db_v2.sqlite3` |
| 未设置 | 默认 `postgres`；**但若 `DB_URL` 明显是 SQLite 文件，则判定为旧部署，回退 SQLite 并 WARN** |

最后一行是唯一的例外，为的是不让老环境因为升版而起不来。回退会打日志，
并且一定会被 `rdctl.py doctor` 报出来 —— 不存在"悄悄退化"。

推导用的 `PG_HOST` / `PG_PORT` 默认 `postgres:5432`，即 compose 里的服务名。

---

## 四、最容易踩的坑：构建期和运行期是两套变量

| | 变量 | 作用 | 值 |
|---|---|---|---|
| 构建期 | `DATABASE_URL` | 给 sqlx 宏做编译期校验 | **必须仍是 sqlite** |
| 运行期 | `DB_TYPE` + `DB_URL` | 决定实际用哪个后端 | 按需求 |

两者**不要混**。构建期 `DATABASE_URL` 由 `server/deploy/build-musl.sh` 设置，
指向仓库里的 `server/src/db_v2.sqlite3`（那是 sqlx 的校验模板库，不是运行数据）。
若把它改成 `postgres://`，编译会直接失败。

---

## 五、类型映射（SQLite → PostgreSQL）

| SQLite | PostgreSQL | 说明 |
|---|---|---|
| `blob` | `bytea` | guid / uuid / pk / user 都是二进制 |
| `tinyint` | `smallint` | status |
| `datetime` | `timestamptz` | created_at |
| `WITHOUT ROWID` | **去掉** | SQLite 专有语法 |
| `?` 占位符 | `$1` | 绑定参数写法不同 |
| `user` 列名 | `"user"` | PG 里是保留字，必须加引号 |

另外一个 PG 特有的坑：**PG 的 PREPARE 不支持一次执行多条语句**，
所以建表与建索引必须逐条执行，不能像 SQLite 那样一把梭。

DDL 见 `server/deploy/postgres-init.sql`（可选，仅用于"想预先受管 schema"的场景；
正常部署时 hbbs 启动会自动建表，幂等）。

---

## 六、密钥托管（`server_config`）

### 问题

上游把 `id_ed25519` 当成**进程工作目录下的两个文件**：存在就用，不存在就生成。
单机没问题，但下面两个场景会出事：

1. **多实例 / 横向扩容**：每台 hbbs 各生成一把私钥，客户端拿到的服务端公钥来回变，
   表现为"时好时坏、偶尔报密钥不匹配"——极难定位。
2. **无状态容器**：数据目录只读或每次重建，密钥活不过重启，
   每重启一次全团队客户端就得重配一次。

### 做法

钥匙存进 `server_config` 表，所有实例读同一行。解析顺序（先命中先用）：

| 优先级 | 来源 | 行为 |
|---|---|---|
| 1 | 命令行 `-k/--key` 给的 64 字节私钥 | 最高优先级，且**覆盖**库中旧值（运维显式指定，意图明确） |
| 2 | 本地文件 `id_ed25519` | 兼容既有部署，保证老 ID 不漂移；顺手回填数据库 |
| 3 | 数据库 `server_config` | 多实例共享的关键路径；补写本地副本让既有脚本照常工作 |
| 4 | 都没有 | 就地生成，同时写文件与数据库 |

两个细节：

- **第 3、4 步会回读"赢家"**：并发启动时多台可能同时各自生成一把钥匙，
  必须回读数据库里真正落地的那把，否则本实例会拿着一把没人认的孤儿钥匙 ——
  表现为"同一个客户端在不同实例上 ID 不一致"。
- **回写文件走 `create_new`，已有私钥绝不覆盖**，所以整个流程幂等，
  重启多少次都不会把正在用的钥匙换掉。

### ⚠️ 安全权衡（说直白些）

**私钥进库意味着：任何能读这个库的人都能拿到服务端私钥**，
进而可以签发任意客户端密钥。所以一旦启用托管，PostgreSQL 的访问控制、
账号权限、备份文件密级都必须按"服务端私钥"来对待，而不是普通业务库。

接受不了就设 `DB_STORE_KEY=N` 退回纯文件模式 —— 代价是多实例无法共享钥匙。

---

## 七、连接审计（`connection_log`）

### 问题

上游 hbbs **完全不落任何连接记录** —— 谁连了谁、连了多久、走中继还是 P2P，
服务端一概不写库，只能从容器日志里翻到零星几行。要做合规审计，
过去的结论只能是"自己在前面加反向代理或流量镜像"。这里补成了原生能力。

### 做法：异步批量落库

打洞请求在连接建立的**热路径**上，一次同步 INSERT 会把网络往返与事务开销
直接加进连接时延 —— 为了审计拖慢每个用户的连接不划算。所以是
「内存环形缓冲 + 后台任务批量刷盘」：

```
    热路径 record() ──push──▶ VecDeque（有上限）
                                  │
    后台任务每 N 秒 ──drain───────┘──▶ 一个事务批量 INSERT
```

三个刻意的取舍：

1. `record()` 用 `try_lock`：拿不到锁（后台任务正在 drain）就丢弃这条记录。
   审计是可观测性数据，不该反过来拖住主流程。
2. 落库失败时把事件放回缓冲等下一轮重试，但总量封顶（`AUDIT_MAX_BUFFER`），
   避免数据库长时间不可用时内存无限增长。
3. 任何错误只记日志，绝不 panic —— 数据库挂掉应由 hbbs 自身的健康检查体现，
   而不是被审计模块放大成崩溃。

### 字段与取值

`event` 取值：

| 值 | 含义 |
|---|---|
| `register_pk` | 客户端注册 / 更新公钥（`detail` 为 `insert` 或 `update`） |
| `punch_hole` | A 请求连 B，已转发给 B（`via_relay` 表示是否强制走中继） |
| `punch_hole_failed` | 请求被拒绝（`detail` 为 `license_mismatch` / `offline` / `id_not_exist`） |

`from_id` 在打洞事件里通常是空的 —— 协议里 `PunchHoleRequest` 只带被连接方的 id，
发起方此刻只有一个 socket 地址。要还原"哪个 ID 连了哪个 ID"，
用 `postgres-init.sql` 里那段把 IP 反查成 ID 的关联查询。

### 开关

| 变量 | 默认 | 说明 |
|---|---|---|
| `AUDIT_LOG` | `Y` | 设为 `N` 完全关闭（连缓冲都不进，热路径零开销） |
| `AUDIT_FLUSH_SECS` | `5` | 刷盘间隔（秒） |
| `AUDIT_MAX_BUFFER` | `20000` | 缓冲上限 |

---

## 八、怎么部署 / 怎么切

### 用默认（PostgreSQL）

什么都不用改：

```bash
cd server/deploy
cp .env.example .env      # 填上 RELAY_HOST，并改掉 PG_PASSWORD
docker compose up -d      # 会自动连带拉起 rd-postgres
```

### 接外部 / 托管 Postgres（RDS 等）

只改 `.env` 里的 `DB_URL` 即可（`DB_TYPE` 保持 `postgres`）：

```bash
DB_TYPE=postgres
DB_URL=postgres://user:pwd@your-db-host:5432/rustdesk
```

本地那个 Postgres 容器就多余了，停掉即可（数据卷 `./pgdata` 不会丢）：

```bash
docker compose stop postgres
# 或 docker compose up -d --scale postgres=0
```

> 注意：`hbbs` 的 `depends_on` 要求 postgres 处于 healthy，所以**不要**用
> `profiles:` 把它摘掉 —— 那会让 hbbs 启动时等一个不存在的服务而卡住。

### 退回 SQLite

`DB_TYPE=sqlite` 且 `DB_URL=/root/db_v2.sqlite3` 两行一起改。
此时只有 `peer` 一张表，不托管密钥、不落审计，与官方行为完全一致。

### ⚠️ 切完务必体检一次

```bash
python rdctl.py doctor
```

这一步不是可选项。最容易出错的是**改了 `DB_TYPE` 却还在用官方镜像** ——
官方镜像不认这个变量，也不建后两张表，会**静默退回 SQLite**：
不报错、日志也看不出来，等你发现 ID 没进 PostgreSQL 时已经攒了一堆数据。

`doctor` 不只比对配置，还会**实证数据落在哪**：连进 PG 查
`information_schema.tables`，逐张核对 `peer` / `server_config` /
`connection_log` 是否齐全，再看本地 SQLite 有没有残留数据。因此能分辨：

| 现象 | doctor 结论 |
|---|---|
| `RD_IMAGE` 仍是官方镜像 | 提醒「镜像不认识 DB_TYPE，数据仍会写进 SQLite」 |
| `DB_TYPE=postgres` 但 `DB_URL` 不是连接串 | **错误**，并给出可复制的修复命令 |
| PG 少表 | 逐张报「缺少表 X」→ 镜像是旧版本，或 `DB_URL` 指向了别的库 |
| PG 查不到、本地 SQLite 有数据 | 提醒「实际仍在用 SQLite」 |
| 两边都有数据 | 正常显示 PG 行数，并提示本地还有 N 行历史数据可迁移 |

密钥托管与审计也会一并体检：开了 `DB_STORE_KEY` 却查不到私钥行 → 提醒
「多实例下每台会各生成一把，客户端 ID 会来回变」。

### 迁移存量数据

`data/db_v2.sqlite3` 里已有的 ID 必须搬过去，否则旧客户端会连不上或被分配新 ID：

```bash
cd server/deploy
python migrate-sqlite-to-pg.py ../data/db_v2.sqlite3 --out migration.sql
docker compose exec -T postgres psql -U rustdesk -d rustdesk < migration.sql
```

脚本要点：零第三方依赖（只需标准库 `sqlite3` + 容器里的 `psql`）；
blobs 用 PG 的 bytea 十六进制字面量 `'\x...'`，二进制安全；
`ON CONFLICT DO NOTHING` 幂等，重复执行不会污染数据。

> 迁移前先 `python rdctl.py backup` 备份一份。

---

## 九、验证状态

已在真实环境实测（PostgreSQL 18 + Rust 1.99）：

| 项目 | 结果 |
|---|---|
| `cargo check --locked` | ✅ 通过，无新增告警（告警集与上游基线逐条一致） |
| `mask_password` / `is_pg_url` 单测 | ✅ 通过（含"密码不得出现在输出里""SQLite 文件路径不得误判为 PG"） |
| `peer` 表 DDL 被 PostgreSQL 接受 | ✅ CREATE TABLE + 4 个索引 |
| SQLite → PostgreSQL 数据迁移 | ✅ 记录迁移成功，guid / pk / uuid 三个 blob 字段**逐字节比对一致** |
| `PgBackend` 往返（insert / get / update_pk） | ✅ 真跑 PostgreSQL 通过 |
| SQLite 后端运行时测试 | ✅ 上游 `test_insert` 通过（10000 并发写 + 10000 并发读） |

---

## 十、已知限制

- **hbbr 不碰数据库**。只有 hbbs 读写库，hbbr 只做中继转发。
- **审计只覆盖打洞与注册**，不记录会话时长：hbbs 只负责撮合，
  真正的连接建立后数据不经过 hbbs，因此"连了多久"服务端无从得知。
  要精确时长需要在 hbbr 侧统计。
- **PG 后端不等于高可用**。hbbs 本身没有多副本协调机制，唯一被解决的是
  **多实例共享同一把钥匙**（否则客户端 ID 会漂移）。
- 若切回 SQLite，PG 里的数据**不会**自动回来，需要用 `pg_dump` 导出后自行合并。

---

## 十一、相关文件

| 文件 | 作用 |
|---|---|
| `server/src/src/database.rs` | 双后端实现（`DbBackend` trait + SQLite / PG）+ 地址解析 `resolve_db_url()` |
| `server/src/src/server_key.rs` | 密钥解析与数据库托管 |
| `server/src/src/audit.rs` | 连接审计缓冲与后台刷盘 |
| `server/src/src/rendezvous_server.rs` | 打洞路径上的审计钩子与密钥装配 |
| `server/deploy/postgres-init.sql` | 三张表的 PG DDL 与常用查询样例 |
| `server/deploy/migrate-sqlite-to-pg.py` | 存量 SQLite 数据迁到 PG |
| `server/deploy/rdctl.py` | `doctor` 体检 / `upgrade` 升级 / `backup` 备份（含 pg_dump） |
| `server/deploy/docker-compose.yml` | `postgres` 服务（默认启用）与 `DB_TYPE` |
| `server/deploy/rdrun.sh` | docker run 路线：自动拼 `DB_URL` 并拉起 PG 容器 |
