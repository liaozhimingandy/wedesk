# 00 · 仓库结构导览

**这篇回答一个问题：这个仓库里都有什么，我想改 X 的时候该动哪个文件。**

如果你是第一次接触这个仓库，读完这篇再去看其它文档；如果你已经熟悉，
把它当速查表用——第二节就是「改 X 动哪里」。

---

## 一、一张图看懂全仓

```
wedesk/
│
├── client/                        # ★ 客户端（被控端 + 控制端，Flutter + Rust）
│   ├── Cargo.toml  Cargo.lock     # 客户端 Rust workspace 定义
│   ├── build.py  build.rs         # 构建入口（build.py 是主入口）
│   ├── src/                       # Rust 侧业务代码
│   │   ├── flutter_ffi.rs         #   ← 改这里必须重跑 generate-bridge.py
│   │   ├── client.rs  server.rs   #   控制端 / 被控端
│   │   └── common.rs  ui/ ...
│   ├── libs/                      # 上游拆出来的 Rust 库（scrap / hbb_common / enigo ...）
│   │   └── hbb_common/src/config.rs   # ← 服务器地址、公钥、APP_NAME 都在这
│   ├── flutter/                   # Dart/Flutter UI 层
│   │   └── lib/generated_bridge.dart   # 代码生成产物，仓库里没有
│   ├── res/                       # 图标、安装器模板、桌面文件
│   ├── Dockerfile  entrypoint.sh  # 客户端的 Linux 构建镜像（与服务端无关！）
│   └── .cargo/  vcpkg.json        # 编译参数与 C++ 依赖清单
│
├── server/                        # ★ 服务端（hbbs + hbbr）
│   ├── src/                       # 源码：vendored 自上游 rustdesk-server
│   │   ├── src/                   #   hbbs / hbbr / rustdesk-utils 三个 bin
│   │   │                          #   （server/src/src/ 不是笔误，Cargo 惯例）
│   │   ├── libs/hbb_common/       #   服务端自己那份 hbb_common（勿与客户端合并）
│   │   └── systemd/               #   裸机部署用的 service 文件
│   ├── deploy/                    # 部署资产：怎么把 src/ 编出来的二进制跑起来
│   │   ├── Dockerfile             #   容器内从源码编译（本机不用装 Rust）
│   │   ├── Dockerfile.prebuilt     #   直接装已编译二进制（CI 用）
│   │   ├── docker-compose.yml     #   hbbs + hbbr 两个容器 + 端口映射
│   │   ├── build-musl.sh          #   真正的编译命令（Dockerfile 与 CI 共用）
│   │   ├── check-bins.sh          #   构建期自检：三个二进制必须真能执行
│   │   ├── rdctl.py               #   运维入口（start/stop/logs/backup/ports/key/build）
│   │   └── .env.example           #   端口与中继地址模板（复制为 .env 后填写）
│   ├── .dockerignore              # 把 data/ 私钥挡在镜像外（必须在 context 根）
│   └── data/                      # 运行时数据（gitignore，切勿提交）
│       ├── id_ed25519             #   服务器私钥 —— 丢了所有客户端要重配
│       └── db_v2.sqlite3          #   仅 SQLite 后端时使用，只有 peer 一张表
│
├── build/                         # ★ 构建与定制脚本（按服务对象分三段）
│   ├── client/                    # 只服务客户端
│   │   ├── customize.py  custom.toml   # 品牌/服务器注入器 + 唯一配置真源
│   │   ├── generate-bridge.py     #   生成 Rust↔Dart 胶水代码
│   │   ├── check-env.py  check-env.bat # 构建环境体检
│   │   ├── build-local.bat        #   Windows 一键构建
│   │   ├── install-vcpkg-deps.bat #   C++ 依赖（ffmpeg 等，最慢的一步）
│   │   ├── apply-rustdesk-engine.bat  # Flutter + RustDesk 定制引擎
│   │   ├── fix-rust.py  patch-ci.py   # 工具链修复 / 官方 CI 改造（备用）
│   │   └── branding/  .tools/     #   品牌资产占位 / codegen 二进制缓存
│   ├── server/                    # 只服务服务端
│   │   ├── vendor-server.py  server-vendor.json   # 追上游时重新导出源码
│   │   ├── build-server.bat       #   本机编 Windows 版 hbbs/hbbr exe
│   │   └── build-server-image.bat #   本机构建 Linux 服务端 Docker 镜像
│   └── common/                    # 跨端共用
│       ├── check-workflow.py      #   工作流静态审计（推送前跑）
│       ├── get-version.py         #   从 client/Cargo.toml 取版本号
│       └── tag-release.bat        #   打 v* 标签 → 触发 CI 发布
│
├── docs/                          # 文档（00 是本篇，其余按 01→11 顺序读）
├── upstream/                      # 上游基线与定制补丁登记
│   ├── UPSTREAM.md                #   升级上游前必读
│   ├── custom.patch  server.patch #   两端各自的定制 diff
│   └── verify_patch.py            #   验证补丁仍可完整重建定制状态
└── .github/workflows/build-windows.yml   # 唯一在用的 CI
                                   #   （Windows/Android/iOS 三端 + 服务端 + 镜像 + Release）
```

### 三个"最容易看错"的地方

| 看起来像 | 实际是 |
|---|---|
| 根目录没有客户端代码 | 客户端整体在 `client/` 下，根目录只剩四样：`client/ server/ build/ docs/` |
| `server/src/src/` 像笔误 | 不是。`server/src/` 是 vendored 仓库根，它的 `src/` 才是 Rust 源码目录 |
| 根目录曾有个 `Dockerfile` | 那是**客户端**的 Linux 构建镜像，已移到 `client/Dockerfile`，与服务端无关 |

---

## 二、速查表：我想改 X，该动哪里

> 「要不要重编」一栏：**镜像** = 服务端要 `docker compose build --no-cache`；
> **bridge** = 必须重跑 `python build/client/generate-bridge.py`；
> **注入** = 必须重跑 `python build/client/customize.py`。

### 品牌与外观

| 我想… | 改这里 | 重编 |
|---|---|---|
| 改软件名 / 公司名 / 官网 | `build/client/custom.toml` 的 `[brand]` | 注入 + 客户端 |
| 改窗口图标、安装器图标 | `client/res/`（替换后跑注入） | 注入 + 客户端 |
| 改 UI 文案、界面布局 | `client/flutter/lib/` | bridge + 客户端 |
| 改 Windows 安装器界面 | `client/res/msi/` | 客户端 |

### 服务器与连接

| 我想… | 改这里 | 重编 |
|---|---|---|
| 换自建服务器地址 / 端口 | `build/client/custom.toml` 的 `[server]` | 注入 + 客户端 |
| 换服务器密钥对 | `server/data/` 换 `id_ed25519`，公钥填进 `custom.toml` | 注入 + 重启服务端 |
| 改对外端口 | `server/deploy/.env`（`HBBS_PORT` 等） | 重启服务端 |
| 改中继带宽 / 强制中继 | `server/deploy/.env` 或 compose 的 `environment` | 重启服务端 |

### 行为与功能

| 我想… | 改这里 | 重编 |
|---|---|---|
| 加一个客户端功能 | `client/src/` + `client/flutter/lib/` | bridge + 客户端 |
| 改 Rust↔Dart 接口 | `client/src/flutter_ffi.rs` | **必须** bridge |
| 改服务端业务逻辑 | `server/src/src/` | 镜像 |
| 加服务端启动参数 | `server/deploy/docker-compose.yml` 的 `command` | 重启服务端 |
| 关掉/开启某类日志 | compose 里的 `RUST_LOG` | 重启服务端 |

### 构建与发布

| 我想… | 改这里 | 说明 |
|---|---|---|
| 本机编客户端 | `build/client/build-local.bat` | 先跑 `check-env.bat` |
| 云端编客户端 + 服务端 + 镜像 | 打 `v*` 标签（`build/common/tag-release.bat`） | 见 `docs/03b-ci.md` |
| 本机构建服务端镜像 | `build/server/build-server-image.bat` | 容器内编译，本机不用装 Rust |
| 改 CI 流程 | `.github/workflows/build-windows.yml` | **推送前跑 `build/common/check-workflow.py`** |
| 追上游新版本 | `build/server/vendor-server.py` + `upstream/UPSTREAM.md` | 升级前必读 UPSTREAM.md |

---

## 三、新人上手路径

按这个顺序，两小时内能从零跑到「自己的客户端连上自己的服务器」：

```bash
# 1. 看懂结构（10 分钟）
#    读本文件 + docs/README.md 的索引

# 2. 起服务端（30 分钟，首次构建慢）
cd server/deploy
copy .env.example .env          # 然后编辑 .env，把 RELAY_HOST 改成服务器 IP
python rdctl.py build           # 容器内从源码编译，约 15-25 分钟
python rdctl.py start           # 起来后 python rdctl.py status 看状态
python rdctl.py ports           # 核对端口映射是否符合预期

# 3. 确认密钥（1 分钟）
python rdctl.py key             # 打印公钥，待会儿要填进客户端配置

# 4. 编客户端（40 分钟起）
cd ../../build/client
check-env.bat                   # 环境体检，缺什么一次说清
build-local.bat                 # 一键构建

# 5. 验证连通
#    把编出来的 exe 拷到另一台机器，输入服务端上某台被控端的 ID
```

**遇到问题时的第一反应**：先跑 `python build/client/check-env.py`（客户端）或
`python server/deploy/rdctl.py status`（服务端），这两个会直接把结论说清楚，
比翻日志快得多。详细排错见 `docs/07-faq.md`。

---

## 四、为什么这么分（设计理由）

### 1. `client/` 与 `server/` 分开，不只是为了好看

两者是**独立的 Cargo workspace、独立的工具链、独立的发布节奏**：

| | 客户端 | 服务端 |
|---|---|---|
| Rust 版本 | 1.75（sciter/bridge 的 i128 ABI 限制） | ≥1.78（lockfile v4） |
| 构建产物 | exe / MSI（Windows） | musl 静态二进制（Linux 容器） |
| 依赖来源 | 上游 `rustdesk/rustdesk` | 上游 `rustdesk/rustdesk-server` |
| 能否互不影响地并行编译 | ✅ 同一条 CI 里两个 job 并行 | ✅ |

合在一起时，"这个 `Cargo.toml` 是哪边的"要凭记忆；分开后路径自己会说话。

### 2. `server/src/` 与 `server/deploy/` 分开

`src/` 是**编译输入**，会随上游版本更新而整体变动；
`deploy/` 是**我们自己的部署方式**，基本不动。

合在一起时，追上游的一次同步很容易把部署文件一起覆盖掉——
而那里面藏着端口规划、私钥排除规则、镜像自检，覆盖任何一个都要重新踩一遍坑。

### 3. `build/` 按服务对象分三段

`build/` 下的脚本天然属于三类：只服务客户端、只服务服务端、跨端共用。
分开之后，**想知道"改 X 要跑哪个脚本"时，先看它在哪一段**，不用逐个读脚本头。

### 4. `.gitignore` / `.gitattributes` 为什么只有一个

规则分散在多个文件时，「某类产物为什么没被忽略」要翻遍全仓才能回答；
而单文件加区块注释可以一眼看完，审查和补漏都更容易。
换行符策略同理——`.bat` 必须 CRLF、`.sh/.py/.md` 必须 LF、
`.env/.dockerignore` 必须 LF，写在一处才不会互相矛盾。

### 5. `data/` 为什么不入库

`server/data/id_ed25519` 是服务器私钥。一旦提交并推送，等同于服务器身份可被伪造，
且 Git 历史里很难彻底清除。因此 `.gitignore` 里 `server/data/` 与
`server/.dockerignore` 里 `data/` 是**双重保险**——后者还防它被烧进镜像层。

---

## 五、几条硬规则（违反会静默出错）

1. **改了 `client/src/flutter_ffi.rs` 必须重跑 `python build/client/generate-bridge.py`**
   Dart 侧看不到新函数，表现为"改了但没反应"。

2. **改了 `customize.py` 覆盖的那批文件，必须重跑 `python upstream/verify_patch.py --regen`**
   否则下次升级上游时改动会静默丢失。

3. **推送前跑 `python build/common/check-workflow.py`**
   GitHub 对工作流的校验比 YAML 解析严格得多——一句注释里的空 `${{ }}`
   就能让整个 workflow 失效，而报错位置会指到毫不相干的行。

4. **`docker compose up` 不带 `--build` 不会重建镜像**
   改了 Dockerfile 或源码后必须显式 `build`，否则静默复用旧镜像。

5. **`server/deploy/.env` 里 `RD_IMAGE` 一旦有值，本地 Dockerfile 就不参与构建**
   你以为在跑自己编的二进制，其实跑的是远端镜像。要"完全本地化"就注释掉它。

6. **不要在 `server/data/` 下放 `.env`**
   hbbs/hbbr 启动时会读工作目录的 `.env` 并把它当参数，`PORT=5188` 会让 hbbr
   改听 5190 撞上 hbbs 的 WebSocket 端口，全程不报错。

7. **移动任何脚本之前，先确认它的根目录推断还成立**
   构建脚本一律用 `Path(__file__)` 逐级向上推导仓库根 / 客户端根，不硬编码盘符。
   好处是仓库放哪都能跑，代价是**层数假设**：脚本从 `build/` 挪到 `build/client/`
   后，原来的 `parent.parent` 就少了一级，`ROOT` 会指向 `build/` 而不是仓库根。
   而这种错**不会立刻报错**——目录都存在、`is_dir()` 检查全过，直到某次
   "找不到源码 / 产物永远缺失"才暴露。三个脚本（`generate-bridge.py`、
   `customize.py`、`vendor-server.py`）和 `get-version.py` 都中过这一招，
   每个的修正处都写了注释说明该写几级。**移动脚本 = 必须重算层数。**
