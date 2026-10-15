# 03b · GitHub Actions 云构建

> 文档索引：[`docs/README.md`](README.md) ｜ 上一篇：[02 运行原理](02-runtime.md) ｜ 下一篇：[04 部署指南](04-deploy.md)

在完全不用配置本地工具链的情况下产出 Windows 客户端。**推荐路线**。

---

## 一、流水线概览

`.github/workflows/build-windows.yml` 一条龙，四个 job：

```
preflight (10s)  ─┐
                  ├─→  generate-bridge (~10min)  ──→  build-windows (~40-60min)  ──→  artifact
                                                                                        │
                                                                       （仅 v* 标签）  ↓
                                                                                    release (2min)
```

| Job | Runner | 做什么 | 耗时 |
|---|---|---|---|
| `preflight` | ubuntu-latest | 判断是否需要构建、校验子模块已实体化、品牌已注入 | ~10 s |
| `generate-bridge` | ubuntu-22.04 | 生成 flutter-rust-bridge 胶水代码 | ~10 min |
| `build-windows` | windows-2022 | vcpkg 依赖 → 品牌注入 → 编译 → 打包 | 40–60 min |
| `build-server` | ubuntu-latest | **容器内 musl 本机编译** hbbs/hbbr/rustdesk-utils | 15–25 min |
| `server-image` | ubuntu-latest | 把上一步的二进制装成镜像推到 Docker Hub | ~3 min |
| `release` | ubuntu-latest | **仅 `v*` 标签**：打包 zip、算校验和、发布 Release | ~2 min |

产物：

- `wedesk-windows-x64` —— 便携版目录，解压即用（Artifact，保存 14 天）
- `wedesk-windows-x64-msi` —— MSI 安装包（手动触发时勾选，或打标签时自动构建）
- `wedesk-server-linux-x64` —— hbbs / hbbr / rustdesk-utils + systemd 单元 + zip
- `liaozhiming/wedesk-server` —— 服务端镜像（tag / 手动触发 / 推送到 main 时推送）
- GitHub Release —— 打 `v*` 标签时发布，长期保存，适合分发给员工

### 为什么拆成多个 job

bridge 产物平台无关，生成器依赖 `cargo install` + `dart pub get`，跑起来不便宜，
单独一个 job 做完再传给 Windows job，失败时可以单独重跑，不必连带重编 C++ 依赖。
更实际的原因是：**这几步的失败模式完全不同**，分开后看日志一眼就知道挂在哪。

release 单独成 job 则是为了**权限收敛**：发布需要 `contents: write`，
而构建环节只有 `contents: read`。构建机上的第三方 action 因此碰不到发布能力。

### 为什么 PR 不跑全量构建

```yaml
if: ${{ github.event_name != 'pull_request' && needs.preflight.outputs.should_build == 'true' }}
```

不是用 cancellation，而是直接不触发——vcpkg 的二进制缓存有读写竞争，
频繁取消/重跑反而容易把缓存写坏。PR 只跑 10 秒的 preflight 就够拦住低级错误了。

---

## 二、怎么跑

### 一次性配置：仓库 secrets

服务端镜像要推到 Docker Hub，需要两个 secrets
（Settings → Secrets and variables → Actions → New repository secret）：

| Secret | 内容 |
|---|---|
| `DOCKER_USERNAME` | Docker Hub 用户名（即 `liaozhiming`） |
| `DOCKER_PASSWORD` | Docker Hub 的 **Personal Access Token** |

> **用 Access Token，不要用账号密码**：账号密码无法做细粒度权限控制，
> 且账号一旦开启 2FA，`docker login` 会直接失败。
> 令牌在 Docker Hub → Account Settings → Personal access tokens 生成，
> 权限给 `Read, Write, Delete` 即可。

没配这两个 secret 时：日常 push 只构建镜像做验证（作业不会红）；
**但打标签发布时会硬失败**——该发版却发不出去，必须让人看见，不能静默降级成"没镜像的 Release"。

### 自动触发

推送到 `main` 即触发。仅改动 `docs/`、`upstream/`、`*.md` 时会在 preflight 里判定为
**不需要构建**而跳过——改文档不该烧 40 分钟机器时间。

```bash
git add build/client/custom.toml
git commit -m "chore: 更新服务器地址"
git push
```

> 这里的"跳过"是 preflight 里的 `should_build` 门禁实现的，**不是** `paths-ignore`。
> 原因见 [关键设计点 8](#8-没有用-paths-ignore)。

### 打标签发布 Release（推荐的分发方式）

推送 `v*` 标签 → 自动构建 → 自动发布 Release：

```bat
build\tag-release.bat
```

脚本会做四件事，任何一件不通过就停下：

1. 从 `Cargo.toml` 读取版本号（**不手工输入**，避免与 CI 校验冲突）
2. 检查工作区干净、且与 `origin/main` 一致
3. 检查标签未重复
4. 打标签并推送，最后打印 Actions 与 Release 页面链接

先 `build\tag-release.bat --dry` 看一遍将要做什么，确认无误再真跑。

Release 里会有：

| 文件 | 说明 |
|---|---|
| `wedesk-<版本>-windows-x64-portable.zip` | 便携版，解压即用 |
| `wedesk-<版本>-windows-x64.msi` | 安装包，适合域控/组策略批量部署 |
| `SHA256SUMS.txt` | 校验和，分发前核对 |
| Source code (zip/tar.gz) | GitHub 自动生成，即 AGPL 要求的完整源码 |

带 `-` 的标签（如 `v1.5.2-rc1`）会自动标记为**预发布**，不会抢占 `latest`。

### 手动触发

仓库 → **Actions** → `Build Wedesk (Client + Server)` → **Run workflow**，可填：

| 输入项 | 说明 |
|---|---|
| `company_name` | 覆盖 `build/client/custom.toml` 的 `brand.company_name` |
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

### 坑：push 触发时 input 的默认值不生效

`inputs` context **只在手动触发（`workflow_dispatch`）时才有值**。
push 触发时 `inputs.hwcodec` 是空的，而 `if: ${{ inputs.hwcodec }}` 对空值判 false
—— 于是 input 里写的 `default: true` **完全不生效**。

后果远不止"几个步骤被跳过"，而是一整条静默降级链：

| 位置 | push 触发时的实际行为 |
|---|---|
| Verify FFmpeg headers | 跳过，缺失不会被发现 |
| vcpkg host triplet | 退回 `x64-windows` → **FFmpeg 被静默跳过** |
| `build.py` | 拿不到 `--hwcodec` → **产物是软件编码版** |
| 流水线状态 | **依然全绿** |

构建成功但产物不是想要的——这比构建失败更难发现。

解决办法是 job 级 env 显式兜底，让非手动触发时按设计意图走：

```yaml
env:
  ENABLE_HWCODEC: ${{ github.event_name != 'workflow_dispatch' || inputs.hwcodec == true }}
  BUILD_MSI: ${{ github.event_name == 'workflow_dispatch' && inputs.build_msi == true }}
```

**还有第二个坑**：env 里存的是**字符串** `"true"` / `"false"`，
而 Actions 中非空字符串一律 truthy —— **`"false"` 也是真**。
所以判断必须写成：

```yaml
if: ${{ env.ENABLE_HWCODEC == 'true' }}    # 对
if: ${{ env.ENABLE_HWCODEC }}              # 错：值为 "false" 时仍然执行
```

另外，step 自己定义的 `env` 在它自己的 `if` 里**取不到**，
所以兜底变量必须放在 job 级。

---

## 三、关键设计点（改之前请读）

### 1. checkout 不要 submodule

```yaml
submodules: false
```

`client/libs/hbb_common` 等子模块已实体化（vendored），仓库是自包含的。
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
python build/client/customize.py --source client --config build/client/custom.toml
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

### 8. 没有用 `paths-ignore`

最初用了 `on.push.paths-ignore` 排除 `docs/**`、`upstream/**`、`*.md`。
加上标签发布后必须去掉，因为 **paths 过滤器对 tag push 同样生效**：
GitHub 会拿标签指向的提交与其父提交做 diff 来判断是否匹配。

于是这个序列会静默失效：

```
最后一次提交只改了文档 → 打 v1.5.1 标签 → 推送
→ diff 全是 .md，被 paths-ignore 过滤 → 流水线不触发 → Release 不发布
```

没有任何失败提示，只是"版本没发出来"，排查时会怀疑标签、怀疑权限、怀疑 action 版本，
很难想到是文档过滤规则干的。

现在改成 preflight 里的 `should_build` 门禁：同样能省掉只改文档时的 40 分钟机时，
但**标签一定放行**。

### 9. 标签版本必须与 `Cargo.toml` 一致

`release` job 第一件事就是比对，不一致直接失败：

```
::error::标签 v1.5.2 与 Cargo.toml 的 1.5.1 不一致。
```

为什么卡这么死：版本号写错在构建阶段没有任何症状，客户端照常编译通过，
只有发布后才会发现 Release 上写的版本和实际二进制对不上。
而发出去的东西回收成本很高——已分发给员工的链接会失效，
别人也无从判断自己装的是哪一版。

所以版本号**只从 `Cargo.toml` 取**，用 `build/common/tag-release.bat` 打标签，不要手敲。

### 10. 发布权限收敛在单个 job

```yaml
permissions:
  contents: read      # 顶层：构建只需要读

jobs:
  release:
    permissions:
      contents: write  # 只有发布这一步能写
```

构建环节跑着一堆第三方 action（vcpkg、flutter、LLVM、msbuild），
它们都不应该具备发布能力。把 `write` 收在最后一个 job 里，
万一上游 action 出问题，影响范围也止于构建。

### 11. `run:` 脚本里的 `${{ }}` 也要过语法校验

GitHub 在**编译期**（job 还没开始跑）就会把工作流里所有 `${{ }}` 解析一遍验语法，
范围不止 `if:` / `env:` / `with:`，**`run:` 脚本正文，包括它的 shell 注释，一样算**。

于是这句本来想当示例的中文注释，会让整个文件直接失效：

```yaml
- name: 计算是否推送镜像
  run: |
    # 放 shell 里判而不是写 ${{ }} —— GH 表达式的 ! 取反与 && 优先级
    latest=false
```

GitHub 报的是：

```
无效的工作流文件: .github/workflows/build-windows.yml#L1
（第 813 行，第 14 列）：预期出现表达式
```

注意 **813 行是 `run: |` 那一行，第 14 列正好是块标量的那个 `|`**——
它回指的是脚本文本的起点，而不是出错的那句注释。
只按行列去找，会在一条完全无辜的 `run: |` 上打转。

两条规则记牢：

1. 工作流文件里（含 `run:` 脚本的注释）**不要出现空的 `${{ }}`**；
   要写示例代码就用文字描述，或写成 `$` + `{{ }}` 拆开。
2. 拿到"第 N 行第 M 列"的报错，若那一列落在 `|` / `>` 上，
   往下找该块标量内部最近的 `${{ }}`。

`build/common/check-workflow.py` 已把这条固化：它会解析全部 `${{ }}` 的语法，
空表达式、括号/引号不闭合、多余内容、未知函数、未知上下文根都会报出来，并给出所在 job / step。

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
| preflight 报"服务器地址仍是官方默认值" | `build/client/custom.toml` 没改 | 填真实 `rendezvous_host` |
| preflight 报"仍残留上游公司名" | 忘了跑 customize.py | `python build/client/customize.py` |
| vcpkg 步骤卡住超 60 min | x-gha 缓存没生效 | 确认已导出 `ACTIONS_CACHE_URL`；或首次本来就会慢 |
| `Patch flutter` 报 already applied | 换了 Flutter 版本 | 此补丁只适用于 3.24.5，脚本已做版本判断跳过 |
| 构建成功但客户端连不上 | 公钥不匹配 | 核对 `build/client/custom.toml` 的 `key` 与 hbbs 的 `id_ed25519.pub` |
| 首次构建后想改服务器 IP | 地址是编译期注入的 | 改 `custom.toml` 后重新构建（用户仍可在界面临时改服务器） |
| 打了标签但没发 Release | 标签名不是 `v` 开头 | release job 的条件是 `startsWith(github.ref_name, 'v')` |
| 打了标签但流水线没跑 | 见[设计点 8](#8-没有用-paths-ignore) | 若自行加回了 `paths-ignore`，只改文档的提交上打的标签会被过滤掉 |
| release job 报版本不一致 | 标签与 `Cargo.toml` 版本不同 | 先改 `Cargo.toml` 的 `version` 再打标签；用 `build/common/tag-release.bat` 不会出这问题 |
| Release 里没有 MSI | MSI 构建失败 | 该步骤有 `continue-on-error` 保护，看构建日志；便携版不受影响 |
| 想重发某个版本 | 标签已存在 | 先改版本号再发；或删标签重发（已下载旧版的人不会自动更新，慎做） |
| GitHub 提示"无效的工作流文件：…（第 N 行，第 M 列）：预期出现表达式" | 工作流里有空的或语法错的 `${{ }}` | 见[设计点 11](#11-run-脚本里的--也要过语法校验)；用 `python build/common/check-workflow.py` 定位 |

---

## 六、工作流自检脚本

改动工作流后，推送前先跑：

```bash
python build/common/check-workflow.py                  # 检查 .github/workflows/*.yml
python build/common/check-workflow.py <file.yml>       # 检查指定文件
```

退出码 `0` = 没问题，`1` = 发现错误。它检查四类问题：

| 类别 | 例子 | 为什么值得查 |
|---|---|---|
| `env.X` 无定义 | 删了 `env.SERVER_TARGET`，打包步骤还在引用 | 不会报错，只会静默变成空串，表现为 `cp: cannot stat 'target//release/hbbs'` 这种"路径空了一截" |
| `steps.X.outputs.Y` 不存在 | step id 写错，或从没 `echo "k=v" >> "$GITHUB_OUTPUT"` | 同样静默为空，让后续 `if:` 条件永远为假 |
| 表达式语法错误 | `${{ }}` 空表达式、括号/引号不闭合、用了双引号字符串 | **整个 workflow 直接失效**，所有 job 都不跑 |
| 未知函数 / 上下文根 | `notAFunction()`、`foo.bar` | 运行期才是错误，编译期未必拦得住 |

另外会把用到的仓库 secrets 列出来，提醒去 Settings 配置。

局限：`uses:` 型 action 的 outputs 由 action 自己定义，静态枚举不出来，只校验 step id 存在；`run:` 里自己 `VAR=xxx` 再 `$VAR` 的 shell 局部变量也识别不了——这两类靠人工。

---

## 七、与本地构建的一致性

两边用的是同一份版本锁定表：

| 项 | 值 | 定义位置 |
|---|---|---|
| Rust | 1.75 | 工作流 `env.RUST_VERSION` |
| Flutter | 3.24.5 | 工作流 `env.FLUTTER_VERSION` |
| bridge generator | 1.80.1 | 工作流 `env.FLUTTER_RUST_BRIDGE_VERSION` / `generate-bridge.py` |
| LLVM | 15.0.6 | 工作流 `env.LLVM_VERSION` |
| vcpkg | commit `9e593bb1` | 工作流 `env.VCPKG_COMMIT_ID` |

改任何一个，记得**两边同步**——这也是为什么它们都写在工作流顶部的 `env:` 里集中管理。
