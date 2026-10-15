# 03 · 本地构建指南（Windows）

> 文档索引：[`docs/README.md`](README.md) ｜ 上一篇：[02 运行原理](02-runtime.md)

> 只想拿 exe？**建议直接用 GitHub Actions 云编译**，见 [03b · CI 云构建](03b-ci.md)。
> 本机配齐工具链要吃掉约 18 GB 磁盘和数小时，且每一步都可能卡住。
> 只有当你需要频繁改 Rust/Flutter 源码做调试时，本地构建才划算。

---

## 0. 先体检

```bat
cd <repo>\build
check-env.bat
```

输出示例（**这是本机 2026-10-15 的真实状态**）：

```
  [+] MSVC 工具链                 18/Insiders (MSVC 14.52.37025)
  [+] Windows SDK              10.0.26100.0
  [+] Rust                     cargo 1.75.0 (1d8b05cdd 2023-11-20)
  [+] Flutter                  3.24.5  [未进 PATH: C:\flutter\bin\flutter.bat]
  [x] vcpkg                    VCPKG_ROOT 未设置
  [x] LLVM/Clang               未安装（需 15.0.6）
```

`[x]` 是阻塞项，`[!]` 是可选项。**别跳过这步** —— 少一个组件会让构建在半小时后才失败，
而失败信息往往完全指不到真正的原因。

---

## 1. 依赖清单

| 组件 | 要求 | 作用 | 缺失时先搞定谁 |
|---|---|---|---|
| MSVC 工具链 | VS Build Tools，含「使用 C++ 的桌面开发」 | `link.exe`、C/C++ 编译器 | **最高优先级**，没有它一切免谈 |
| Windows SDK | 10.0.x | 头文件与导入库 | 随 MSVC 一起装 |
| Rust | **1.75**（不能更高） | 编译 Rust 核心库 | `fix-rust.py` |
| Flutter | **3.24.5** | 编译 UI | `apply-rustdesk-engine.bat` |
| vcpkg + 静态依赖 | `x64-windows-static` | libvpx/libyuv/opus/aom/ffmpeg | `install-vcpkg-deps.bat` |
| LLVM/Clang | **15.0.6** | bindgen 解析 C 头文件 | `winget install LLVM.LLVM --version 15.0.6` |

磁盘预算：

| 组件 | 占用 | 首次耗时 |
|---|---|---|
| Visual Studio Build Tools | ~8 GB | 20–40 min |
| vcpkg + C++ 依赖 | ~5 GB | 30–120 min |
| Rust 1.75 | ~1.5 GB | 5 min |
| Flutter 3.24.5 | ~2.5 GB | 5 min |
| LLVM 15 | ~2 GB | 3 min |
| 构建产物 | ~10 GB | 20–40 min |

### 为什么 Rust 必须锁 1.75

RustDesk 的 sciter/bridge 代码依赖 1.78 之前的行为。1.78 引入了 i128 布局变更
（[官方公告](https://blog.rust-lang.org/2024/03/30/i128-layout-update.html)），
会导致 ABI 不兼容。上游 CI 也锁的是这个值。

### 为什么必须替换 Flutter engine

RustDesk 用的是**自研 Flutter engine**（`windows-x64-release`）。官方 Flutter 的 engine
能编译过，但成品窗口行为是坏的。这一步由 `apply-rustdesk-engine.bat` 负责，
也是最常见的本地构建失败原因之一。

---

## 2. 执行顺序

```bat
cd <repo>\build

REM ① 环境体检，先确认没有阻塞项
check-env.bat

REM ② C++ 依赖（最慢，一次性）
install-vcpkg-deps.bat

REM ③ Flutter SDK + 替换自研 engine
apply-rustdesk-engine.bat

REM ④ 最终构建（内含 bridge 生成检查）
build-local.bat
```

工具位置都可用环境变量覆盖，默认值：

| 变量 | 默认 | 说明 |
|---|---|---|
| `FLUTTER_ROOT` | `c:\flutter` | Flutter SDK |
| `VCPKG_ROOT` | `d:\vcpkg` | 放 D 盘，build tree 数 GB，C 盘会吃紧 |

所有路径都从脚本自身位置推导，仓库放哪个盘都能跑，脚本里没有硬编码盘符。

---

## 3. 关于 bridge（最容易漏的一步）

`src/bridge_generated.rs` 和 `flutter/lib/generated_bridge.dart` 是
**flutter-rust-bridge 生成的**，被 `.gitignore` 排除，仓库里没有。少了它们：

```
cargo build   -> error[E0583]: file not found for module `bridge_generated`
flutter build -> Target of URI hasn't been generated: 'generated_bridge.dart'
```

`build-local.bat` 会自动检查并在缺失时生成，你也可以单独跑：

```bat
python build\generate-bridge.py --check     REM 只看是否就绪
python build\generate-bridge.py             REM 生成
python build\generate-bridge.py --force     REM 改了 src/flutter_ffi.rs 后强制重生成
```

生成器本身是从源码 `cargo install` 的，首次约 5–15 分钟。
**中途别 kill** —— 半途中断会留下损坏的二进制，只能删掉 `build\.tools\frb` 重来。

---

## 4. 品牌注入

```bat
python build\customize.py --dry-run    REM 先预览，不写盘
python build\customize.py              REM 再写入
```

脚本是**幂等**的：源码已是目标值时什么都不会做，也没有脏 diff，可以放心反复跑。
改了 `build\custom.toml` 后重跑即可生效。临时改某一项不用编辑文件：

```bat
python build\customize.py --set brand.company_name="某某科技"
```

撤销（依赖 `*.rd.bak` 备份）：

```bat
python build\customize.py --restore
```

---

## 5. 已知坑

### 5.1 Rust 转发壳被截断成 0 字节

**症状**：`rustc -V` 报 "command not found"，但 `~/.rustup/toolchains/1.75-*` 目录明明在。

**成因**：并发跑多个 rustup，或在下载途中 `taskkill`，会把 `~/.cargo/bin` 下的转发壳
截断成 0 字节——它们是 rustup.exe 的硬链接，rustup 靠 `argv[0]` 判断自己该扮演谁，
所以 0 字节就等于全废。

**修复**（`build\fix-rust.py`，已内置全部要点）：

```bat
python build\fix-rust.py --check    REM 体检
python build\fix-rust.py            REM 修复
```

关键两点，脚本里都有注释说明为什么：

1. **不要用 `rmtree` 删 toolchain 目录**。一来上千文件会触发批量删除保护，
   二来 rustup 自带损伤恢复——`toolchain install --force` 会打印
   `recovering from a partially installed toolchain` 并自行清理，比外部删目录安全。
2. **补建转发壳**。受限制的终端里 rustup 建硬链接可能静默失败（安装成功但
   `toolchain unchanged`，`~/.cargo/bin` 里仍然没有可执行文件）。
   脚本会用 `shutil.copy2` 从 rustup.exe 复制一份同名代理兜底。

### 5.2 MSVC / SDK 没装在 C 盘

体检脚本曾经一律报「MSVC 未安装」，实际是装在 `D:\Program Files\Microsoft Visual Studio\18\Insiders`。
原因是它只扫了 `C:\Program Files\Microsoft Visual Studio\2022|2019`。

现在改成「盘符 × 程序目录 × 任意 VS 版本 × 任意发行版」扫描，
2026 这类新版本号以及 `Insiders` 这类发行通道都能识别到。

### 5.3 Flutter 装好了但不在 PATH

体检脚本现在会主动探测 `C:\flutter`、`C:\src\flutter` 等常见位置，
找到后标注 `[未进 PATH: ...]`，不会误判成未安装。

### 5.4 VS 2026 + MSVC v145

本机是 VS 18 (Insiders) / MSVC 14.52，而上游 CI 用 VS 2022 / MSVC v143。
遇到诡异的编译错误时值得怀疑这一点——**这是未经上游验证的组合**。
真出问题最快的验证方式是改用 VS 2022 Build Tools 重试。
