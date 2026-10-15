# 私有远程桌面套件（基于 RustDesk 二次开发）

一套面向**团队内部使用**的远程桌面私有化方案：自建服务器保证数据不外流，定制客户端让员工免配置直连。

---

## 一、这套东西解决什么问题

直接用官方 RustDesk 客户端，团队会遇到三个麻烦：

| 问题 | 后果 | 本方案的解法 |
|---|---|---|
| 默认连官方服务器 `rs-ny.rustdesk.com` | 员工忘记配置 → 流量走公网，违反数据不出内网要求 | 编译期把服务器改成你的，忘记配置也不会漏出去 |
| 每个员工手动填服务器地址+公钥 | 几十台机器配置成本极高，还容易填错 | 客户端开箱即连，界面里什么都不用填 |
| 品牌是 RustDesk、链接指向官网 | 内部推广时像是第三方工具 | 改成你自己的产品名与官网 |

**关键在于：改动的是"兜底默认值"，不是硬编码锁定。** RustDesk 解析服务器地址的优先级是：

```
命令行参数 > 用户界面设置的 custom-rendezvous-server > 编译注入 > 本地配置 > 兜底常量 ← 我们改这里
```

好处是员工（或你）仍可在界面里临时切服务器做调试，但默认值永远指向你的私有服务器。

---

## 二、目录说明

这是一个**自包含仓库**：`git clone` 一次即可构建，不需要 `--recurse-submodules`，
也不依赖 GitHub。上游 RustDesk 源码以 **vendored** 方式进仓库，按端分成两大块：
客户端在 `client/`，服务端在 `server/src/`。

```
wedesk/                          ← 最终仓库，自主可控
│
├── ── 客户端（上游 rustdesk，v1.5.1，已注入定制）──────────
├── client/
│   ├── src/                         Rust 侧：被控端 / 控制端
│   ├── libs/                        核心库，hbb_common 已实体化（不再是 submodule）
│   ├── flutter/                     Flutter UI
│   ├── res/                         图标、logo 等品牌资源
│   ├── build.py  Cargo.toml        构建入口与 workspace 定义（上游原样，未改）
│   ├── LICENCE                      AGPL-3.0，随仓库保留以满足合规
│   └── .cargo/  examples/  fastlane/  flatpak/  appimage/  vcpkg.json
│       Dockerfile                  注意：这是【客户端】的 Linux 构建镜像，与服务端无关
│
├── ── 服务端（上游 rustdesk-server，已 vendor）────────────
├── server/
│   ├── src/                     ★ 服务端源码（hbbs/hbbr），独立 Cargo workspace
│   │   ├── src/                 hbbs、hbbr、rustdesk-utils 三个 bin
│   │   ├── libs/hbb_common/     服务端自己那份（勿与客户端合并，锁定 commit 不同）
│   │   └── systemd/             裸机部署的 service 文件
│   ├── deploy/                  部署资产：怎么把 src/ 编出来的二进制跑起来
│   │   ├── Dockerfile           容器内从源码编译 → 自有镜像
│   │   ├── Dockerfile.prebuilt   CI 用：直接装已编译二进制
│   │   ├── build-musl.sh        ★ 编译脚本 —— Dockerfile 与 CI 共用这一份
│   │   ├── docker-compose.yml   hbbs(ID/注册) + hbbr(中继)，默认本地构建
│   │   ├── .env.example         配置项模板，复制为 .env 后填写
│   │   ├── rdctl.py             跨平台管理工具（构建/启停/端口/查公钥/备份）
│   │   └── check-bins.sh        构建期自检：三个二进制必须真能执行
│   ├── .dockerignore            把 data/ 私钥挡在镜像之外（必须在 context 根）
│   └── data/                    运行时：id_ed25519 私钥 + SQLite（gitignore，勿提交）
│
├── ── 我们自己的工具 ──────────────────────────────
├── build/                       构建与定制脚本，按服务对象分三段
│   ├── client/                  只服务客户端
│   │   ├── custom.toml          ★ 唯一需要你改的配置文件
│   │   ├── customize.py         注入器，可反复运行（改配置后重跑即可）
│   │   ├── generate-bridge.py   生成 Rust↔Dart 胶水代码（改 flutter_ffi.rs 后必跑）
│   │   ├── check-env.py/.bat    环境体检
│   │   ├── install-vcpkg-deps.bat  vcpkg + C++ 依赖（30–120 min）
│   │   ├── apply-rustdesk-engine.bat Flutter SDK + 替换自研 engine
│   │   ├── build-local.bat      一键构建客户端
│   │   └── fix-rust.py  patch-ci.py  branding/  .tools/
│   ├── server/                  只服务服务端
│   │   ├── vendor-server.py     追上游服务端版本并重新导出源码
│   │   ├── server-vendor.json   记录上游 commit
│   │   ├── build-server.bat     编译服务端 Windows exe
│   │   └── build-server-image.bat  docker 构建服务端镜像（本机无需 Rust）
│   └── common/                  跨端共用
│       ├── check-workflow.py    工作流静态审计（推送前跑）
│       ├── get-version.py       从 Cargo.toml 取版本号
│       └── tag-release.bat      打 v* 标签 → CI 自动发布
│
├── upstream/                    上游基线与升级
│   ├── UPSTREAM.md              版本信息、改了什么、升级流程
│   ├── custom.patch             客户端定制改动的可重放补丁
│   ├── server.patch             服务端定制改动（关闭官方版本检查）
│   └── verify_patch.py          验证补丁仍能重建当前定制状态
│
├── docs/                        文档（见下方「文档导航」）
│   ├── README.md                索引 —— 不知道看哪篇就从这里进
│   ├── 00-repo-layout.md        ★ 仓库结构导览 + 「改 X 动哪里」速查表
│   ├── 01-architecture.md       架构总览：分层、目录职责、构建产物关系
│   ├── 02-runtime.md            运行原理：ID 注册、打洞、中继、会话数据流
│   ├── 03-build.md              本地构建（Windows）
│   ├── 03b-ci.md                GitHub Actions 云构建
│   ├── 04-deploy.md             部署：服务端上线、密钥管理、客户端分发
│   ├── 05-customize.md          定制：换品牌、改服务器、裁剪功能
│   ├── 06-maintenance.md        维护：新增功能 SOP、升级上游
│   ├── 07-faq.md                故障排查
│   ├── 08-performance.md        性能：硬件编解码、画质取舍、资源占用
│   ├── 09-server-build.md       服务端自编译：源码位置、三条产出路径、裸机部署
│   ├── 10-ports.md              端口与网络规划
│   ├── 11-server-ops.md         服务端运维与客户端接入
│   ├── upstream/                上游自带文档（README、AGENTS.md 等）
│   └── upstream-i18n/           上游 60 份多语言文档（留档，与本项目无关）
│
├── README.md                    本文件
├── .gitignore                   见下方「为什么要自己写 .gitignore」
└── .gitattributes               换行策略：*.bat 强制 CRLF，其余 LF
```

> **不知道某个文件是干什么的，或想改 X 不知道动哪里** → 看
> [`docs/00-repo-layout.md`](docs/00-repo-layout.md)，里面有完整的
> 「改 X 动哪里」速查表。

### 为什么把上游源码直接放进仓库

| | vendored（当前） | submodule | subtree |
| --- | --- | --- | --- |
| clone 后直接构建 | ✅ 一次 clone 就够 | ❌ 漏参数就缺 hbb_common | ✅ |
| 依赖 GitHub | ❌ 完全不依赖 | ✅ | ❌ |
| 保留上游历史 | ❌ | ✅ | ✅ |

选 vendored 的理由：这是最终交付仓库，第一诉求是"clone 下来就能构建"。
历史丢失用 `upstream/custom.patch` + `UPSTREAM.md` 补偿。

> ⚠️ **`client/libs/hbb_common` 已被实体化**（删掉了它的 `.git` 指针和 `.gitmodules`）。
> 服务器地址、公钥、APP_NAME 都定义在这里。若误恢复成 submodule，定制会静默失效。

### 为什么要自己维护 `.gitignore`（而不是沿用上游的）

上游 `.gitignore` 第 1 行是 `/build`。在它自己的仓库里这条规则无害
（上游根本没有顶层 `build/` 目录，它的产物在 `client/flutter/build/` 和 `build-pkg/`），
但本仓库的 `build/` 放的是**构建脚本**，照搬就会把整个脚本目录忽略掉。

本仓库的 `.gitignore` 是三套规则合并的结果，特别注意：

- **没有 `/build`** —— 这是刻意的，别"修复"它
- 上游的 `*png / *svg / *jpg` 会吞掉 122 个 UI 图标
  （在上游仓库因文件早已 tracked 而失效，问题被掩盖；
  到新仓库后所有文件是 untracked，规则立刻生效）
  因此**末尾有解禁白名单，必须留在文件最后**才生效
- 上游产物目录 `client/flutter/build/` 由 `client/flutter/.gitignore` 管，`build-pkg/` 在此处管

---

## 二·五、文档导航

本文件是"怎么做"的速查手册。**想理解原理、想改代码，请进 `docs/`**：

| 想干什么 | 读哪篇 |
|---|---|
| **搞懂这套东西怎么跑的** | [01 架构总览](docs/01-architecture.md) → [02 运行原理](docs/02-runtime.md) |
| **拿到一个 exe** | [03b GitHub Actions 云构建](docs/03b-ci.md)（推荐）或 [03 本地构建](docs/03-build.md) |
| **发一个正式版本** | `build\common\tag-release.bat` —— 打标签即自动构建并发布 Release（见 [03b](docs/03b-ci.md)）<br>一次产出：客户端便携版 / MSI + **服务端 hbbs/hbbr** + Docker Hub 镜像 |
| **上线给用户用** | [04 部署指南](docs/04-deploy.md) |
| **端口怎么规划 / 上公网怎么配防火墙** | [10 端口与网络规划](docs/10-ports.md)<br>对外只要 3 个：TCP 5186/5187 + UDP 5187 + TCP 5188 |
| **不想用官方服务端镜像，要自己编译 hbbs/hbbr** | [09 服务端自编译](docs/09-server-build.md) |
| **换 Logo、改服务器、砍功能** | [05 定制指南](docs/05-customize.md) |
| **加一个新功能** | [06 维护手册](docs/06-maintenance.md) |
| **出问题了** | [07 故障排查](docs/07-faq.md) |
| **让它跑得更快更省** | [08 性能调优](docs/08-performance.md) |
| **升级上游版本** | [`upstream/UPSTREAM.md`](upstream/UPSTREAM.md) |

索引入口：[`docs/README.md`](docs/README.md)

### 改代码前必须知道的三条

1. **改了 `client/src/flutter_ffi.rs` 必须重跑 `python build/client/generate-bridge.py`**
2. **改了 `customize.py` 覆盖的 13 个文件，必须重跑 `python upstream/verify_patch.py --regen`**
3. **带逻辑判断的源码改动要登记到 `upstream/UPSTREAM.md`**（升级时脚本不会帮你重建）

---

## 三、快速开始（推荐路线：GitHub Actions 云编译）

你本机没有 Rust / Flutter / vcpkg 工具链，**在 Windows 上配齐这套环境要数小时且极易失败**。
用 GitHub Actions 免费云编译是更靠谱的选择——浏览器点几下就能拿到 exe。

### 第 1 步：起服务器（`server/`）

```bash
cd server\deploy
copy .env.example .env         # 编辑 .env，把 RELAY_HOST 改成你的 IP/域名
python rdctl.py start
```

首次启动成功后会打印**服务器公钥**，类似这样：

```
[OK] 服务器就绪。以下是你的服务器公钥，客户端定制时需要写入：

    GbRXxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxS8=
```

> **⚠️ 端口与防火墙**：必须在服务器/防火墙放行
> `TCP 5186, 5187, 5188` 和 `UDP 5187`。
> 只开 TCP 不开 UDP 会导致"ID 能注册但连不上"——这是最常见的坑。

### 第 2 步：改配置（`build/client/custom.toml`）

```toml
[server]
# 端口必须对得上：`rendezvous_host` 是 5187（hbbs），relay 是 5188（hbbr）。
# 客户端内置的兜底端口是 21116，所以这里不写端口就等于没改。
rendezvous_host = "你的域名或IP:5187"   # 客户端真正能连到的地址
relay_host      = "你的域名或IP:5188"
key             = "上一步拿到的公钥"     # 必须完全一致，否则连不上

[brand]
app_name  = "企业网管助手"
exe_name  = "nethelper"
company_name = "你的公司"
website   = "https://你的内网地址"
```

### 第 3 步：提交并触发构建

**不需要再 fork RustDesk —— 上游源码已经在仓库里了**（`client/libs/hbb_common` 等子模块已
实体化，clone 下来就能编译）。直接：

```bash
git add build/client/custom.toml && git commit -m "chore: 更新服务器地址与品牌信息"
git push
```

推送会自动触发 `Build Wedesk (Client + Server)` 工作流。也可以手动触发：
仓库 → **Actions** → `Build Wedesk (Client + Server)` → **Run workflow**。

手动触发时可以临时覆盖品牌而不改源码：

| 输入项 | 说明 |
|---|---|
| `company_name` / `website` | 覆盖 `build/client/custom.toml` 里的值 |
| `build_args` | 传给 `build.py` 的参数，默认 `--hwcodec --vram` |
| `skip_customize` | 跳过品牌注入（调试 CI 用） |
| `build_msi` | 额外产出 MSI 安装包 |

> **为什么 IP 变了必须重新构建**：服务器地址是**编译期注入**的。
> 换成新服务器后要改 `build/client/custom.toml` 里的 `rendezvous_host` 和 `key`，再重新构建。
> （用户自己仍可以在客户端界面临时改服务器——我们改的是兜底默认值，不是硬锁定。）

### 第 4 步：下载产物

构建约 30–60 分钟（首次更久，之后有缓存），完成后在 Artifacts 下载 `wedesk-windows-x64`。

### 第 5 步：分发

把整个 `rustdesk` 目录打包发给员工即可（便携版，无需安装）。

> **注意**：开源仓库请先设为 **Private**。RustDesk 是 AGPL-3.0，对外分发改版客户端
> 必须提供完整源码；设为私有后仅内部使用，不触发 §13 的源码提供义务。

---

## 四、本地构建（当前主线）

源码已整合到本仓库的 `client/` 下（`Cargo.toml`、`build.py` 都在其中），定制也已注入完成。

**2026-10-15 实测进度**：MSVC ✅ / Windows SDK ✅ / Rust 1.75 ✅（已修）/ Flutter 3.24.5 ✅
—— 还差 **vcpkg** 和 **LLVM 15**，见下方体检表。

完整步骤、磁盘预算和已知坑见 [`docs/03-build.md`](docs/03-build.md)（比本节详细得多）。

### 4.1 必须先体检

```bat
cd <repo>\build\client
check-env.bat
```

它会列出每一项依赖的状态，并明确告诉你哪些缺失、怎么装。
**别跳过这步**——少一个组件会让构建在半小时后才失败。

### 4.2 需要你亲自做的两件事

我当前是非管理员会话，下面两件必须由你完成：

**① 安装 Visual Studio Build Tools（管理员权限，必需）**

这是 Windows 上唯一的硬阻塞项。Rust 编译到 `x86_64-pc-windows-msvc`
依赖 `link.exe` 和 Windows SDK，没有 MSVC 就完全编不了。

- 下载：https://visualstudio.microsoft.com/zh-hans/downloads/#build-tools-for-visual-studio-2022
- 安装时务必勾选：**「使用 C++ 的桌面开发」**（会自带 Windows 10/11 SDK）
- 约 20–40 分钟，装完重启终端

> 不需要完整 Visual Studio IDE，只要 Build Tools。

**②（可选）确认代理开启**

本机 7890 端口有代理在用。GitHub/Google 下载走它会快很多，
之前 Docker 拉镜像失败就是因为代理没开。

### 4.3 一条命令装完剩下的

装好 MSVC 后，按顺序执行（都是幂等的，重跑安全）：

```bat
cd <repo>\build

REM 0. Rust 转发壳损坏时的专用修复（并发 rustup / 中途 kill 会导致 0 字节）
python fix-rust.py

REM 1. C++ 依赖，最慢 30-120 分钟
install-vcpkg-deps.bat

REM 2. Flutter SDK + 替换官方自研 engine
apply-rustdesk-engine.bat

REM 3. 最终构建，20-40 分钟（内部会自动检查并生成 bridge 胶水代码）
build-local.bat
```

> **LLVM 15.0.6 需要手动装**（本项目没有对应脚本）：
> `winget install LLVM.LLVM --version 15.0.6`
> bindgen 用它解析 C 头文件，缺了会在编译依赖时报一堆莫名的符号错误。

三个脚本的路径都基于脚本自身位置推导，仓库放在哪个盘都能跑。
工具位置可用环境变量覆盖，默认值：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `FLUTTER_ROOT` | `c:\flutter` | Flutter SDK（已装好） |
| `VCPKG_ROOT` | `d:\vcpkg` | vcpkg 放 D 盘，编译产物数 GB，C 盘吃紧 |

### 4.4 为什么必须替换 Flutter engine

RustDesk 用的是**自研 Flutter engine**（`windows-x64-release`）。
直接用官方 Flutter 的 engine 能编译过，但成品窗口行为是坏的。
官方 CI 也做这个替换——这是最常见的本地构建失败原因之一。

| 组件 | 磁盘占用 | 耗时（首次） |
|---|---|---|
| Rust 1.75 | ~1.5 GB | 5 min |
| Flutter 3.24.5 | ~2.5 GB | 5 min |
| vcpkg + C++ 依赖 | ~5 GB | 30–120 min |
| Visual Studio Build Tools | ~8 GB | 20–40 min |
| 构建产物 | ~10 GB | 20–40 min |

**注意**：确认 C 盘空间充足（当前 60 GB 可用会比较紧张，建议清理或把
Flutter/vcpkg 装到 D 盘 —— 改两个脚本里的路径即可）。

### 4.5 手动执行路线

不想用 bat 的话，等效的原始命令（脚本会自动定位源码和配置，无需带参数）：

```bash
# 1. 注入定制（可反复运行，改了 custom.toml 后重跑即可）
python <repo>\build\client\customize.py --dry-run   # 先预览
python <repo>\build\client\customize.py             # 再写入

# 2. 生成 flutter-rust-bridge 胶水代码（首次约 5-15 分钟，build-local.bat 会自动做）
python <repo>\build\client\generate-bridge.py --check    # 先看是否就绪
python <repo>\build\client\generate-bridge.py            # 缺了就生成

# 3. 构建（在 client/ 下执行，源码整棵树都在里面）
cd <repo>\client
python .\build.py --portable --flutter --skip-portable-pack --hwcodec --vram
```

产物在 `<repo>\client\flutter\build\windows\x64\runner\Release`。

想撤销定制（还原到注入前，依赖 `*.rd.bak` 备份）：

```bash
python <repo>\build\client\customize.py --restore
```

> ⚠️ `--restore` 是回到**最初的上游原始值**（RustDesk / 官方服务器），
> 不是回到上一次的定制值。想改公司名或服务器，直接改 `custom.toml` 后重跑即可，
> 脚本认得"上次注入的值"，能正确迁移。

---

## 五、⚠️ 许可证提醒（务必先看）

**RustDesk 使用 AGPL-3.0 许可证**，这一点直接影响你的使用方式：

| 场景 | 是否合规 |
|---|---|
| 公司内部使用，不对外分发 | ✅ 完全没问题，AGPL 不限制内部使用 |
| 改了代码，只在内部用，不外传 | ✅ 无需开源 |
| **把修改后的客户端发给公司以外的人** | ⚠️ 属于"分发"，**必须向接收者提供完整源码** |
| 作为商业产品卖给客户 | ⚠️ 需要遵守 AGPL 或向官方购买商业授权 |

如果合规要求严格且涉及对外分发，建议考虑 RustDesk 官方商业授权，
或改用 Apache/MIT 许可的替代方案。

---

## 六、已知问题与排查

**Docker 拉镜像失败：`connecting to 127.0.0.1:7890 ... actively refused`**

你的 Docker Desktop 配置了代理，它转发的 7890 端口（通常是科学上网客户端）当前没运行。
三种解法任选其一：
1. 启动你的代理软件；
2. Docker Desktop → Settings → Resources → Proxies → 关闭代理；
3. 给 Docker daemon 单独配置一个可用的镜像加速器。

**客户端连不上，提示 key 不匹配**
`server.key` 与服务器上 `data/id_ed25519.pub` 必须逐字节一致。用 `python rdctl.py key` 重新取。

**ID 能注册但连接失败**
99% 是 UDP 5187 没放行，或 relay 地址配错，检查 `server/deploy/.env` 的 `RELAY_HOST`。

**定制没生效**
检查 `git clone` 是否带了 `--recursive`；再看 CI 日志里是否有定制步骤的输出。

---

## 七、验证状态

定制锚点已对 RustDesk v1.5.1 源码**实测验证**（非占位、非推测）：

| 项目 | 结果 |
| --- | --- |
| 定制规则命中 | **18 / 18**，0 失败 |
| 幂等性（连跑两次） | 第二次 0 修改、0 未命中 |
| 改配置后迁移旧值 | ✅ 公司名 A→B→A 双向迁移正确 |
| 补丁可重放性 | ✅ 干净上游 + `custom.patch` 逐字节重建 13 个文件 |
| 图标资源入库 | ✅ 122 个 png/svg 已解除上游 `.gitignore` 的误伤 |
| 子模块实体化 | ✅ hbb_common 已是普通目录，clone 无需 `--recursive` |
| `docker-compose.yml` | 通过 `docker compose config` 校验，服务器已真实运行 |

若某条规则未命中，`customize.py` 会明确报出**哪一项失败及原因**，不会静默放过。

---

## 八、上游升级

上游信息、改了哪些文件、完整升级步骤见 **`upstream/UPSTREAM.md`**。

核心思路：定制改动以补丁形式留存，升级 = 换源码 + 重放定制。

```bash
# 升级后务必跑一次，确认定制仍能完整重建
python upstream/verify_patch.py
```

**最容易踩的坑**：`.gitignore` 是我们自己维护的，上游更新**不会**覆盖它
（它在仓库根，而上游源码里那份在合并时已丢弃）。
但要留意别在升级时把上游的 `/build` 规则带回来——那会忽略掉我们的脚本目录。

---

## 九、当前待办

以下仍是**占位值**，需要你补齐后重新注入并构建：

| 位置 | 当前占位 | 需要改成 |
| --- | --- | --- |
| `build/client/custom.toml` → `company_name` | `贵公司名称` | 你的真实公司名 |
| `build/client/custom.toml` → `website` | `https://example.com` | 你的内网帮助站点 |
| `build/client/branding/` | 空 | 你的图标、logo（替换客户端图标） |

改完执行：

```bash
python build\client\customize.py --dry-run   # 预览
python build\client\customize.py             # 写入
```

客户端图标目前仍是 RustDesk 原版。替换图标需要覆盖 `client/res/` 与
`client/flutter/assets/` 下的对应文件，并重新构建。主要几个：

| 用途 | 文件 |
| --- | --- |
| Windows exe / 任务栏图标 | `client/res/icon.ico`、`client/res/tray-icon.ico` |
| 安装包与 About 图标 | `client/res/32x32.png`、`client/res/128x128.png` |
| Flutter UI 内嵌 | `client/flutter/assets/`、各端 `Runner/Assets` |

### 阻塞项

- **MSVC 未安装**（正在安装）：Windows 编译 `x86_64-pc-windows-msvc` 必需，
  需 VS Build Tools 2022 + 「使用 C++ 的桌面开发」
- **Rust 工具链待修复**：`~/.cargo/bin` 下的转发壳此前被并发安装进程截断为 0 字节，
  需串行重跑 `rustup`（一次只跑一个，严禁中途 kill）
- **C 盘空间**：当前约 60 GB 可用，vcpkg 已安排在 D 盘，但仍建议留意
