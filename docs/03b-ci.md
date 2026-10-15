# 03b · GitHub Actions 云构建

> 文档索引：[`docs/README.md`](README.md) ｜ 上一篇：[02 运行原理](02-runtime.md) ｜ 下一篇：[04 部署指南](04-deploy.md)

在完全不用配置本地工具链的情况下产出 Windows 客户端。**推荐路线**。

---

## 一、流水线概览

`.github/workflows/build-windows.yml` 一条龙，三个 job：

```
preflight (10s)  ─┐
                  ├─→  generate-bridge (~10min)  ──→  build-windows (~40-60min)  ──→  artifact
```

| Job | Runner | 做什么 | 耗时 |
|---|---|---|---|
| `preflight` | ubuntu-latest | 校验子模块已实体化、品牌已注入、服务器地址已改 | ~10 s |
| `generate-bridge` | ubuntu-22.04 | 生成 flutter-rust-bridge 胶水代码 | ~10 min |
| `build-windows` | windows-2022 | vcpkg 依赖 → 品牌注入 → 编译 → 打包 | 40–60 min |

产物：

- `wedesk-windows-x64` —— 便携版目录，解压即用（保存 14 天）
- `wedesk-windows-x64-msi` —— MSI 安装包（可选，需手动触发时勾选）

### 为什么拆成两个 job

bridge 产物平台无关，生成器依赖 `cargo install` + `dart pub get`，跑起来不便宜，
单独一个 job 做完再传给 Windows job，失败时可以单独重跑，不必连带重编 C++ 依赖。
更实际的原因是：**这两步的失败模式完全不同**，分开后看日志一眼就知道挂在哪。

### 为什么 PR 不跑全量构建

```yaml
if: ${{ github.event_name != 'pull_request' }}
```

不是用 cancellation，而是直接不触发——vcpkg 的二进制缓存有读写竞争，
频繁取消/重跑反而容易把缓存写坏。PR 只跑 10 秒的 preflight 就够拦住低级错误了。

---

## 二、怎么跑

### 自动触发

推送到 `main` 即触发。`docs/**`、`upstream/**`、`*.md` 的改动被 `paths-ignore` 排除——
改文档不该烧 40 分钟机器时间。

```bash
git add build/custom.toml
git commit -m "chore: 更新服务器地址"
git push
```

### 手动触发

仓库 → **Actions** → `Build Windows Client` → **Run workflow**，可填：

| 输入项 | 说明 |
|---|---|
| `company_name` | 覆盖 `build/custom.toml` 的 `brand.company_name` |
| `website` | 覆盖 `brand.website` |
| `hwcodec` | **硬件编解码开关，默认开启**（见下方说明） |
| `build_args` | 传给 `build.py` 的额外参数，默认 `--vram`（显存编码，进一步降低延迟与 CPU） |
| `skip_customize` | 跳过品牌注入（抓 CI 本身的问题时用） |
| `build_msi` | 额外产出 MSI 安装包 |

手动输 CI 的意义在于：**临时换个品牌试一次，不必改源码也不用留提交**。

### 关于 hwcodec 开关

`--hwcodec` 启用 NVENC / Intel QSV / AMD AMF 硬件编解码，能显著降低远程桌面的 CPU 占用。
但它要求 **vcpkg 从源码编译 FFmpeg**（含 nasm、AMF、nvcodec、qsv 一堆补丁），
在 GitHub Runner 上动辄 40 分钟以上，**首次无缓存时是整个流水线最慢的一步**。

默认开，理由是构建慢一次性、运行省 CPU 是永久的：

| | 默认（开） | 取消勾选 `hwcodec` |
|---|---|---|
| FFmpeg | vcpkg 源码编译 | 跳过 |
| 构建耗时 | +40~90 分钟（首次） | 较快 |
| 编解码 | 显卡硬编 + 软件兜底 | 软件 VP9 / AV1，**完全可用** |
| 运行 CPU | 个位数百分比 | 数十个百分点 |
| 适用场景 | 正式分发、追求流畅度 | 只想快速验证链路能否跑通 |

第二次之后 vcpkg 的 `x-gha` 二进制缓存会生效，耗时大幅下降。

> 想临时验证"CI 本身通不通"而不想等 FFmpeg，取消勾选 `hwcodec` 即可。
> 长时间迭代调试时可以这样省机时，出正式版时记得勾回来。
> 各参数的完整取舍见 [08 性能调优](./08-performance.md)。

---

## 三、关键设计点（改之前请读）

### 1. checkout 不要 submodule

```yaml
submodules: false
```

`libs/hbb_common` 等子模块已实体化（vendored），仓库是自包含的。
这是本仓库与上游 CI 最本质的差异——上游每次都要 `--recursive`，
也因此常遇到 submodule 拉不下来导致整条流水线红。

`preflight` 里有一条硬校验：一旦发现 `.gitmodules` 就报 error，
防止有人不小心把子模块关系引进来。

### 2. Flutter engine 用推导路径，不写死

上游直接写 `C:/hostedtoolcache/windows/flutter/stable-3.24.5-x64/...`。
这里改成：

```bash
FLUTTER_ROOT="$(dirname "$(dirname "$(readlink -f "$(command -v flutter)")")")"
```

写死路径的问题是：`subosito/flutter-action` 换安装根目录就 404，
而且报错信息是"复制失败"，完全看不出是路径变了。

### 3. codegen 装到独立目录再缓存

```yaml
path: .tools/frb
```

`flutter_rust_bridge_codegen` 要 `cargo install`（编译 10 分钟），必须缓存。
但如果缓存 `~/.cargo/bin`，会把 `rustc`/`cargo` 本身也卷进来，
两者互相踩（你缓存的是 tool A 的产物，里面躺着 tool B 的二进制）。
装到独立根目录就绕开了这个耦合。

### 4. vcpkg 二进制缓存不能省

```yaml
VCPKG_BINARY_SOURCES: "clear;x-gha,readwrite"
```

少了这行，每次都要从源码重编 `aom` / `libvpx` / `ffmpeg`，单次直接 40 分钟以上。
配套还需要在 Windows job 开头导出 `ACTIONS_CACHE_URL` / `ACTIONS_RUNTIME_TOKEN`——
不导出的话 vcpkg 会**静默退化**成不用缓存，不报错、只是慢，很难排查。

### 5. 品牌注入是幂等的

```bash
python build/customize.py --source . --config build/custom.toml
```

源码已是目标值时什么都不做，所以重复构建不会产生脏 diff。
这也是为什么 CI 可以无条件跑这一步。

### 6. 可选步骤不该炸掉主产物

虚拟显示器驱动之外，**打印机驱动**是从第三方下载再做 sha256 校验的，
网络抖动属于常态。这类步骤统一 `continue-on-error: true`：
缺了它客户端照样能连、能控、能传文件，只是少个功能。

MSI 构建同理（依赖 WiX/nuget，失败不影响便携版交付）。

### 7. `VCPKG_DEFAULT_HOST_TRIPLET` 不能删

这是本流水线踩过的最隐蔽的一个坑，删掉它构建会以很费解的方式失败。

`vcpkg.json` 里 `ffmpeg` 声明为 `host: true`，且 platform 条件是 `((windows | ...) & static)`。
而 host triplet **默认是 `x64-windows`（动态 CRT）**，不满足 `static` —— 于是 vcpkg
**静默跳过 FFmpeg，不报错**。等到 cargo 编译 hwcodec 的 `mfx_encode.cpp` 才炸：

```
fatal error C1083: Cannot open include file: 'libavcodec/avcodec.h': No such file or directory
fatal error C1083: Cannot open include file: 'libavutil/pixfmt.h': No such file or directory
```

上游 `flutter-build.yml` 靠这个变量让 host triplet 与 target triplet 一致，从而装上 FFmpeg。

这里的处理是**条件设置**：只在勾选 `hwcodec` 时才设成 `x64-windows-static`，
否则保持默认 `x64-windows` 让 vcpkg 跳过 FFmpeg（默认构建因此快很多）。

改这个值前请确认 `vcpkg.json` 里 ffmpeg 条目的 platform 条件仍然要求 `static`。

---

## 四、上游工作流都去哪了

原本有 11 条 `.github/workflows/*.yml`（Android / iOS / macOS / Linux / Flatpak /
F-Droid / nightly / Release 发布等等），现在**全部移到 `.github/workflows-upstream/`**。

GitHub 只加载 `workflows/*.yml`，所以移过去 = 停用，纯做存档参照。
理由见 [`.github/workflows-upstream/README.md`](../.github/workflows-upstream/README.md)——
简单说它们依赖一堆我们不打算配的 `secrets.*`（Apple 开发者账号、Android keystore、
签名服务器），留着只有红叉。

单一需求（编译 Windows 客户端）就该有一条干净的流水线，而不是 11 条注定失败的。

---

## 五、排错

| 现象 | 原因 | 处理 |
|---|---|---|
| preflight 报"服务器地址仍是官方默认值" | `build/custom.toml` 没改 | 填真实 `rendezvous_host` |
| preflight 报"仍残留上游公司名" | 忘了跑 customize.py | `python build/customize.py` |
| vcpkg 步骤卡住超 60 min | x-gha 缓存没生效 | 确认已导出 `ACTIONS_CACHE_URL`；或首次本来就会慢 |
| `Patch flutter` 报 already applied | 换了 Flutter 版本 | 此补丁只适用于 3.24.5，脚本已做版本判断跳过 |
| 构建成功但客户端连不上 | 公钥不匹配 | 核对 `build/custom.toml` 的 `key` 与 hbbs 的 `id_ed25519.pub` |
| 首次构建后想改服务器 IP | 地址是编译期注入的 | 改 `custom.toml` 后重新构建（用户仍可在界面临时改服务器） |

---

## 六、与本地构建的一致性

两边用的是同一份版本锁定表：

| 项 | 值 | 定义位置 |
|---|---|---|
| Rust | 1.75 | 工作流 `env.RUST_VERSION` |
| Flutter | 3.24.5 | 工作流 `env.FLUTTER_VERSION` |
| bridge generator | 1.80.1 | 工作流 `env.FLUTTER_RUST_BRIDGE_VERSION` / `generate-bridge.py` |
| LLVM | 15.0.6 | 工作流 `env.LLVM_VERSION` |
| vcpkg | commit `9e593bb1` | 工作流 `env.VCPKG_COMMIT_ID` |

改任何一个，记得**两边同步**——这也是为什么它们都写在工作流顶部的 `env:` 里集中管理。
