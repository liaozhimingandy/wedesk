# 12 · ID 数据改存 PostgreSQL

> **先说结论**：默认不动就是 SQLite，行为与官方完全一致。
> 这一篇只在你确实想把 ID / 设备台账统一存进 PostgreSQL 时才需要读。

---

## 一、这不是改个环境变量就能解决的

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
  update_pk / get_peer` 签名 —— 因此 **`peer.rs` 零改动**。
- **SQLite 侧保留 `query!` 宏**：它已被 CI 的编译期校验覆盖，改动风险最低。
- **PostgreSQL 侧只用运行时 `query()`**：这样编译时不要求有一台活的
  PostgreSQL，镜像构建不依赖数据库可用性。

后端由 `DB_TYPE` 决定（`sqlite` / `postgres`），**默认 `sqlite`。**

---

## 三、最容易踩的坑：构建期和运行期是两套变量

| | 变量 | 作用 | 值 |
|---|---|---|---|
| 构建期 | `DATABASE_URL` | 给 sqlx 宏做编译期校验 | **必须仍是 sqlite** |
| 运行期 | `DB_TYPE` + `DB_URL` | 决定实际用哪个后端 | 按需求 |

两者**不要混**。构建期 `DATABASE_URL` 由 `server/deploy/build-musl.sh` 设置，
指向仓库里的 `server/src/db_v2.sqlite3`（那是 sqlx 的校验模板库，
不是运行数据）。若把它改成 `postgres://`，编译会直接失败。

---

## 四、类型映射（SQLite → PostgreSQL）

| SQLite | PostgreSQL | 说明 |
|---|---|---|
| `blob` | `bytea` | guid / uuid / pk / user 都是二进制 |
| `tinyint` | `smallint` | status |
| `datetime` | `timestamptz` | created_at |
| `WITHOUT ROWID` | **去掉** | SQLite 专有语法 |
| `?` 占位符 | `$1` | 绑定参数写法不同 |
| `user` 列名 | `"user"` | PG 里是保留字，必须加引号 |

另外一个 PG 特有的坑：**PG 的 PREPARE 不支持一次执行多条语句**，
所以索引必须逐条创建，不能像 SQLite 那样一把梭。

DDL 见 `server/deploy/postgres-init.sql`。

---

## 五、怎么启用

### 继续用 SQLite（默认）

什么都不用改。`docker compose up -d` 即可。

### 切到 PostgreSQL

编辑 `server/deploy/.env`，三处**必须一起改**，缺一个就会静默不生效：

```bash
DB_TYPE=postgres
DB_URL=postgres://rustdesk:<密码>@postgres:5432/rustdesk
PG_PASSWORD=<一个够强的口令>
```

然后带 profile 启动（不带 `pg` 的话 postgres 服务根本不会被创建）：

```bash
cd server/deploy
docker compose --profile pg up -d
```

### ⚠️ 切完务必体检一次

```bash
python rdctl.py doctor
```

这一步不是可选项。上述配置里最容易出错的是**改了 `DB_TYPE` 却还在用官方镜像**
—— 官方镜像不认这个变量，会**静默退回 SQLite**：不报错、日志也看不出来，
等你发现 ID 没进 PostgreSQL 时已经攒了一堆数据。

`doctor` 不只比对配置，还会**实证数据落在哪**（查 PG 的 `peer` 表有没有、
本地 SQLite 有没有数据），因此能分辨下面几种情况：

| 现象 | doctor 结论 |
|---|---|
| `RD_IMAGE` 仍是官方镜像 | 提醒「镜像不认识 DB_TYPE，数据仍会写进 SQLite」 |
| PG 查不到、本地 SQLite 有数据 | 提醒「实际仍在用 SQLite」，提示检查 `--profile pg` 与 `DB_URL` |
| 两边都有数据 | 正常显示 PG 行数，并提示本地还有 N 行历史数据可迁移 |

### 两个新加的保护（2026-10 补）

| 保护 | 解决什么 |
|---|---|
| `DB_TYPE=postgres` 但 `DB_URL` 不是 `postgres://` 连接串 → **启动即硬失败**并指明该填什么 | 只改 `DB_TYPE`、忘了改 `DB_URL` 时，`DB_URL` 会退回 `db_v2.sqlite3` 这个文件路径，sqlx 抛出的错误跟 PostgreSQL 毫无关系，排查方向直接跑偏 |
| 首次连库失败自动重试（默认 10 次 × 3 秒，可用 `DB_CONNECT_RETRIES` 调，设 1 即恢复"立刻失败"） | 容器并发启动时 hbbs 常常先于 PG 可用；`depends_on: service_healthy` 只覆盖首次编排，PG 自身重启、宿主机重启两者同时拉起时依然会撞上。一次连不上就退出 → CrashLoop，日志里只有一行看不懂的连接错误 |

顺带把连接串做了**脱敏**：`peer.rs` 与 `database.rs` 的启动日志里，
`DB_URL` 的密码位会被替换成 `******`（用户名、主机、库名保留，不影响排障）。
PG 连接串含明文口令，直接 log 等于把数据库密码写进日志文件。

`rdctl.py doctor` 也会挡第一种情况，且给出可复制的修复命令 —— 不用等容器起来再猜。

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

## 六、验证状态

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

## 七、已知限制

- **hbbr 不碰数据库**。只有 hbbs 读写 `peer` 表，hbbr 只做中继转发。
- **hbbs 仍然不记录连接审计**。换 PostgreSQL 解决的是"ID 台账统一管理"，
  **不是**"谁连了谁"——后者服务端一概不落库，只能在前面加反向代理或流量镜像。
- **PG 后端是单实例设计**。hbbs 本身没有多副本协调机制，
  上 PostgreSQL 不等于获得了高可用。
- 若切回 SQLite，PG 里的数据**不会**自动回来，需要用 `pg_dump` 导出后自行合并。

---

## 八、相关文件

| 文件 | 作用 |
|---|---|
| `server/src/src/database.rs` | 双后端实现（`DbBackend` trait + SQLite / PG 两个实现） |
| `server/deploy/postgres-init.sql` | PG 版 `peer` 表 DDL 与查询样例 |
| `server/deploy/migrate-sqlite-to-pg.py` | 存量 SQLite 数据迁到 PG |
| `server/deploy/rdctl.py` | `doctor` 体检 / `upgrade` 升级 / `backup` 备份（含 pg_dump） |
| `server/deploy/docker-compose.yml` | `postgres` 服务（profile `pg`）与 `DB_TYPE` |
