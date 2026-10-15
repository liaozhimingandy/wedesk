# build/ —— 构建与定制脚本

这个目录放**不属于客户端也不属于服务端**的工具：它们操作前两者，但自身不是产物的一部分。
按服务对象分三段，找脚本时先看它在哪一段。

```
build/
├── client/    只服务客户端（定制注入、bridge 生成、环境体检、一键构建）
├── server/    只服务服务端（追上游导出源码、本机构建镜像/exe）
└── common/    跨端共用（工作流审计、版本号提取、打标签发版）
```

---

## client/ —— 客户端专属

| 脚本 | 干什么 | 什么时候跑 |
|---|---|---|
| `customize.py` | 把 `custom.toml` 里的品牌信息、服务器地址与公钥注入客户端源码。**幂等**，重复跑不会产生脏 diff | 改 `custom.toml` 后；每次 CI 构建前 |
| `custom.toml` | 品牌与服务器的**唯一配置真源**（软件名、公司、官网、rendezvous/relay 地址、公钥） | 改配置就改这里，别去改源码 |
| `generate-bridge.py` | 生成 Rust↔Dart 胶水代码（`src/bridge_generated.rs` 与 `flutter/lib/generated_bridge.dart`）。这两个文件被 gitignore，仓库里没有 | **改了 `src/flutter_ffi.rs` 之后必须跑**；首次构建前 |
| `check-env.py` / `check-env.bat` | 构建环境体检：VS Build Tools、LLVM、Flutter、vcpkg、Rust 版本一次查全 | 构建失败时第一个跑；新机器配环境时 |
| `build-local.bat` | Windows 一键构建客户端 | 日常本机构建 |
| `install-vcpkg-deps.bat` | 装 vcpkg 并编译 C++ 依赖（ffmpeg/libvpx/aom...）。**首次 60-150 分钟** | 一次性；换 vcpkg 版本时 |
| `apply-rustdesk-engine.bat` | 装 Flutter 3.24.5 并换上 RustDesk 定制引擎。跳过它窗口行为会坏 | 一次性 |
| `fix-rust.py` | 修复被中断的 rustup 导致的工具链损坏（0 字节转发壳等） | cargo 突然不见/报怪错时 |
| `patch-ci.py` | 把定制脚本部署进 `.github/`，让**官方** workflow 也产出定制客户端 | 备用。本仓库 CI 用自己的 `build-windows.yml`，用不到它 |
| `branding/` | 品牌资产占位目录（图标等） | 需要放自有素材时 |
| `.tools/` | `cargo install` 的 codegen 二进制缓存（数百 MB，gitignore） | 自动生成，不用管 |

### 最常用的两条

```bash
# 看品牌注入会改什么（不写盘）
python build/client/customize.py --source client --config build/client/custom.toml --dry-run

# 确认 bridge 是否就绪
python build/client/generate-bridge.py --check
```

---

## server/ —— 服务端专属

| 脚本 | 干什么 | 什么时候跑 |
|---|---|---|
| `vendor-server.py` | 从上游 `rustdesk-server` 重新导出源码到 `server/src/`，并套上我们的补丁 | **追上游新版本时**，见 `upstream/UPSTREAM.md` |
| `server-vendor.json` | 记录上游 commit（服务端 + hbb_common 各自的版本） | 由 `vendor-server.py` 维护 |
| `build-server.bat` | 本机编译 **Windows 原生** hbbs/hbbr/rustdesk-utils exe | 想在 Windows 上直接跑服务端时 |
| `build-server-image.bat` | 本机构建 **Linux** 服务端 Docker 镜像（容器内编译 musl 静态二进制） | 不想装 Rust/musl 但要自己的镜像时 |

```bash
# 本机构建服务端镜像（约 15-25 分钟，本机不需要 Rust）
build\server\build-server-image.bat
```

---

## common/ —— 跨端共用

| 脚本 | 干什么 | 什么时候跑 |
|---|---|---|
| `check-workflow.py` | 静态审计 `.github/workflows/*.yml`：GH 表达式语法、`env`/`steps.outputs` 引用是否可解析、用到哪些 secrets | **改了工作流之后、推送之前** |
| `get-version.py` | 从 `client/Cargo.toml` 提取版本号输出到 stdout | 被 `tag-release.bat` 调用 |
| `tag-release.bat` | 校验版本一致性 → 打 `v*` 标签 → 推送，触发 CI 发布客户端 + 服务端 + 镜像 | 发版时。`--dry` 可预演 |

```bash
# 推送前必跑
python build/common/check-workflow.py

# 预演发版
build\common\tag-release.bat --dry
```

---

## 为什么脚本要按端分

`build/` 下的脚本天然属于三类：只服务客户端、只服务服务端、跨端共用。
分开之后，**想知道"改 X 要跑哪个脚本"时，先看它在哪一段**，不用逐个读脚本头。

反过来说，如果某个脚本同时被两端使用，它就该在 `common/`——
放在任一一端都会让另一端的人找不到它。
