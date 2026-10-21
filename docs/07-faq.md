# 07 · 故障排查

> 文档索引：[`docs/README.md`](README.md) ｜ 上一篇：[06 维护手册](06-maintenance.md)

> 按症状查表。**构建类问题先跑 `python build/client/check-env.py`**，它会告诉你缺什么。

---

## 一、构建类

### `file not found for module bridge_generated`

**原因**：`client/src/bridge_generated.rs` 不存在 —— bridge 胶水代码没生成。
这是**本地构建最容易漏的一步**，官方 CI 是单独 job 生成的，本地脚本早期没覆盖。

```bash
python build/client/generate-bridge.py     # 首次约 10-25 分钟（要编译 codegen）
```

生成成功后会产出 4 个文件：

| 文件 | 用途 |
|---|---|
| `client/src/bridge_generated.rs` | Rust 侧桥（**构建必需**） |
| `client/src/bridge_generated.io.rs` | Rust 侧 IO 桥 |
| `client/flutter/lib/generated_bridge.dart` | Dart 侧桥（**构建必需**） |
| `client/flutter/lib/generated_bridge.freezed.dart` | Dart 冻结类 |

用 `--check` 可以快速确认是否就绪（秒级）：

```bash
python build/client/generate-bridge.py --check
```

---

### `cargo: command not found` / `rustc` 报奇怪的错

**原因**：`~/.cargo/bin` 下的转发壳被截断成 0 字节。
通常发生在**并发跑 rustup 或中途 kill 掉 rustup** 的时候。

```bash
python build/client/fix-rust.py --check    # 诊断
python build/client/fix-rust.py            # 修复
```

**修复时的两条硬规则**：

1. **一次只跑一个 rustup**，跑完再干别的
2. **中途绝对不要 kill**

脚本内部用的是 `rustup toolchain install --force` 让 rustup 自行恢复，
**而不是** `rm -rf` 删 toolchain 目录 —— 后者会触发批量删除保护而失败。

---

### `check-env.py` 报 MSVC / Windows SDK 未安装，但我明明装了

**原因**：VS 装在了非默认盘符（如 `D:\Program Files\Microsoft Visual Studio\18\Insiders`），
而旧版探测逻辑只扫 `C:\Program Files\...\2022\`。

新版脚本已改成「盘符 × 程序目录 × 任意 VS 版本 × 任意发行版」的全扫描，能识别：
VS 17/18/2022/Insiders、Community/Professional/Enterprise/BuildTools，装在 C/D/E 任意盘。

若仍报未安装，手动确认这两个路径存在：

```bat
dir "D:\Program Files\Microsoft Visual Studio\18\Insiders\VC\Auxiliary\Build\vcvarsall.bat"
dir "D:\Windows Kits\10\Include"
```

---

### `flutter: command not found`

Flutter 装好了但不在 PATH。脚本支持环境变量覆盖，也可以直接指定：

```bat
set FLUTTER_ROOT=C:\flutter
build\client\build-local.bat
```

---

### vcpkg 依赖报错 / 缺 libvpx

```bat
set VCPKG_ROOT=D:\vcpkg
build\client\install-vcpkg-deps.bat   :: 首次 30-120 分钟
```

> ⚠️ `build\client\install-vcpkg-deps.bat` 与 `build\client\build-local.bat` 的 `VCPKG_ROOT` **必须指向同一个目录**，
> 否则装到 A 盘、构建去 B 盘找，必然失败。

---

### 窗口行为异常 / 下拉菜单不对

**原因**：没换成 RustDesk 自研 Flutter engine。官方 engine 会有各种细微异常。

```bat
build\client\apply-rustdesk-engine.bat
```

---

### `Cannot open include file: 'libavcodec/avcodec.h'` / `'libavutil/pixfmt.h'`

**原因**：启用了 `--hwcodec`，但 vcpkg 没装 FFmpeg。

hwcodec 要用 `cc` 编译 `cpp/mfx/mfx_encode.cpp`，需要 FFmpeg 头文件。
`vcpkg.json` 里 ffmpeg 是 `host: true` 且 platform 条件含 `& static`；
**host triplet 默认是 `x64-windows`（动态 CRT），不满足条件 → FFmpeg 被静默跳过，不报错**。

```bash
# 本地排查：看头文件到底在不在
ls "$VCPKG_ROOT/installed/x64-windows-static/include/libavcodec/avcodec.h"
```

解法二选一：

| 想要 | 做法 |
|---|---|
| **要硬件编解码**（默认） | 重跑 `install-vcpkg-deps.bat`——它现在是 manifest 模式，会自动带 FFmpeg 与 mfx-dispatch。首次源码编译 60–150 分钟 |
| **先跑通** | `set NO_HWCODEC=1` 再跑。hwcodec 是 `optional = true` 依赖，不开就不编译，软件编解码 VP9/AV1 完全可用 |

CI 上对应的是 `Run workflow` 时的 **hwcodec 勾选框**，默认**开启**。

> 如果是自己手写 `vcpkg install libvpx libyuv opus aom` 装的依赖，就会踩到这个坑
> —— 手写清单会漏掉 FFmpeg。`install-vcpkg-deps.bat` 已改为读 `vcpkg.json`，别再手写。

> 顺带说一句：`localtime` / `sprintf` 的 C4996 弃用警告、
> 以及 `client/res/vcpkg/libyuv` 那条"用 MSVC 编译 libyuv 会很慢"的提示，
> 都**只是警告**，不是构建失败的原因。真正致命的只有 C1083。

### 构建很慢 / 卡住不动

先判断是真在编译还是卡死：

```bat
tasklist /FI "IMAGENAME eq rustc.exe"
```

**隔 25 秒看两次，如果 PID 和内存完全不变** → 真卡住了（常见于沙箱/受限环境下
cargo 的子进程被挂起），杀掉重跑。若在变 → 只是慢，等着。

`cargo-expand` 那个 crate 依赖特别多，首次编译 10-25 分钟**是正常的**。

---

## 二、定制类

### 改了 `custom.toml` 但重跑脚本没生效

**原因**：脚本按"上游原始值"精确匹配。你已经改成 A 之后，脚本还在找上游的原始值。

`.custom-state.json` 就是解决这个的 —— 它记录上次注入值，脚本依次尝试
「上游原始值 → 上次注入值」。

**检查**：

```bash
python build/client/customize.py --dry-run    # 看命中数与未命中项
```

若显示未命中，确认 `build/.custom-state.json` 还在（它已入库，别删）。

---

### 客户端标题还是 "RustDesk" / 图标没换

按顺序排查：

1. `python build/client/customize.py --dry-run` —— 是否全命中
2. `grep -n APP_NAME client/libs/hbb_common/src/config.rs` —— 值是否落地
3. **构建前是否重新注入过**（上游升级/还原代码后必须重跑）
4. **Windows 图标缓存**：删 `%LOCALAPPDATA%\IconCache.db` 或重启资源管理器

> 注意：不要全局 sed 替换 "RustDesk" 字符串。应用名的正确改法只改 `APP_NAME` 一处，
> Flutter 侧运行时从 Rust 读。见 [05-customize.md](05-customize.md#32-应用名只改一处就够了)。

---

### 每次跑脚本都生成一堆 `.rd.bak` 文件，但没改任何东西

已修复。脚本用 `originals` 字典比对，**只在文件真正变化时才写备份**。
如果还出现，说明文件内容的换行符或编码不一致，检查 `.gitattributes` 是否被上游覆盖过。

---

## 三、连接类

| 现象 | 原因 | 排查 |
|---|---|---|
| 显示"未就绪" / ID 出不来 | 连不上 hbbs | 端口/防火墙/地址写错。`rdctl.py status` 看服务端是否健康 |
| ID 正常但连不上对方 | 打洞失败 且 hbbr 未通 | 检查 TCP 5188 是否放行 |
| 能连但极卡 | 走了中继 | 确认 `ALWAYS_USE_RELAY` 与 NAT 类型 |
| 提示公钥不匹配 | `RS_PUB_KEY` 与服务端密钥不配对 | `rdctl.py key` 重新取，改 `custom.toml` 后**重新构建** |
| 连上立刻断开 | 认证超时 / 服务进程未运行 | 需要无人值守时确认已装系统服务 |

### 怎么判断当前是 P2P 还是中继

看连接质量指示器，或直接看服务端 5188 端口流量 ——
**P2P 成功时 hbbr 几乎没有流量**。

#### 客户端日志在哪

桌面端：**设置 → 关于 → 日志目录**（点击路径直接打开文件夹，最近 5 个日志文件
可单独打开）。Rust 侧日志默认就是 debug 级、按天/16 MB 轮转保留 90 份，
界面上能看到的只有"连没连上"，**为什么走中继只能看日志**。

各平台落盘位置（`Config::log_path()`）：

| 平台 | 目录 |
|---|---|
| Windows | 配置目录下 `log\` |
| macOS | `~/Library/Logs/<AppName>/` |
| Linux | `~/.local/share/logs/<AppName>/` |
| Android | 应用主目录下 `<AppName>/Logs/` |

排查连接问题时在日志里搜这些关键字：

| 关键字 | 含义 |
|---|---|
| `lan discovery listener started` / `discover ping` | 局域网发现是否在工作 |
| `test nat` / `UDP NAT test` | NAT 类型探测是否成功（失败会反复重试） |
| `Hole Punched` | 打洞成功 |
| `Nothing connected to the hole punched to` | 洞打通了但没人来连 —— 典型的 hairpin/NAT 回环失败 |
| `failed to listen on ... after punching` | 打洞后监听失败 |

#### 如果内网环境下（同一 WiFi）却大量走中继

按概率排序，**逐条排除**：

1. **局域网发现没生效**（最常见）
   客户端的局域网发现是**固定端口的 UDP 广播**：`RENDEZVOUS_PORT + 3`，
   `RENDEZVOUS_PORT` 是编译期常量（`client/libs/hbb_common/src/config.rs`），
   与自定义服务器端口**无关**。所以：
   - Windows 防火墙必须允许本程序**入站 UDP** —— 装的时候如果点了"拒绝"，
     局域网发现会**静默**失效（不报错，只是永远发现不了）；
   - 路由器开了 **AP 隔离 / 客户端隔离 / 访客网络** → 广播不转发，必然走中继；
   - 设置里 `Deny LAN discovery` 是否被勾上。

2. **NAT 类型探测失败**（自建服务端很容易踩）
   NAT 类型是拿**主端口**和**主端口 - 1** 两个 TCP 端口探测的
   （`src/common.rs: test_nat_type_`），也就是默认的 **5187 + 5186**。
   **只 publish 了 5187 而漏了 5186**，探测就永远失败，`nat_type` 停在 UNKNOWN，
   打洞窗口被缩短，表现为"能连但常常走中继"。
   自查：`docker ps` 看 5186 是否在端口映射里，或
   `Test-NetConnection <服务器> -Port 5186`。

3. **路由器不支持 hairpin（NAT 回环）**
   两台设备在同一个路由器下时，打洞用的是**各自的公网映射地址**，需要路由器把
   发往自己公网 IP 的包转回内网。家用路由器相当一部分不支持，日志里表现为
   `Nothing connected to the hole punched to`。这一步失败就只能走中继。

4. 其它：`ALWAYS_USE_RELAY` 是否被设成 `Y`；是否跨 VLAN；是否走了 VPN
   （VPN 会改变 NAT 行为）；客户端是否指定了 socks 代理 / 强制走 WebSocket
   （`use_ws()` 为真时会**直接把 NAT 类型标成 SYMMETRIC**，等于放弃打洞）。

---

### 服务端密钥丢了

**代价很大**：必须重新生成 → 改 `RS_PUB_KEY` → **重新构建并分发所有客户端**。

所以：

```bash
cd server && python rdctl.py backup    # 加进计划任务，异地存一份
```

---

## 四、服务端类

### `rdctl.py start` 失败

```bash
docker ps              # Docker 是否在运行
cd server && cat .env  # RELAY_HOST / RELAY_PUBLIC_ADDR 是否填了实际 IP
python rdctl.py logs   # 看容器日志
```

### 端口冲突

整体平移 `.env` 里的三个变量即可，例如改成 9100 段：

```env
HBBS_PORT=9100
HBBS_NAT_PORT=9099      # 必须 = HBBS_PORT - 1
HBBR_PORT=9101          # 建议 = HBBS_PORT + 1（客户端兜底推导依赖这个关系）
```

`HBBS_PORT` / `HBBR_PORT` 同时决定**主机侧映射**与**容器里的 `-p` 参数**，
所以两侧永远一致，不会出现"端口开着但没人监听"。

> 改完之后 **`build/client/custom.toml` 的 `rendezvous_host` 必须跟着改到新端口并重新编译客户端**，
> 否则客户端还在按旧端口去连。`python rdctl.py ports` 可以随时打印当前规划做对照。

### 审计日志里来源 IP 全是网关地址

**原因**：端口映射模式下 NAT 会把来源改写成网关 IP。

**解法**（仅 Linux）：`docker-compose.yml` 里换成 `network_mode: "host"`，能拿到真实来源 IP。

### 重启 hbbr 之后黑名单失效了

**原因**：只有当 hbbr **没连上数据库**时才会这样 —— 那种情况下名单退回
文件模式，而上游的文件模式只在启动时读一次。

**排查**：

```bash
docker compose logs rd-hbbr | grep -E "名单|数据库"
#   连接数据库失败，名单退回文件模式 → 修 DB_URL / PG 连通性
#   未配置数据库 → 补 DB_TYPE / DB_URL 或接受文件模式
```

**确认已落库**：

```bash
docker compose exec -T postgres psql -U rustdesk -d rustdesk \
  -c "select kind, ip, note, created_at from relay_access_list order by created_at;"
```

> 已经配好库的话，`blacklist.txt` 里的老条目会在启动时自动迁移进库一次
> （只在库为空时执行），不需要手工导。

### hbbr 日志一直刷「连接 hbbs 失败」，中继时断时续

**原因**：这是**节点注册**没成功（不影响已经在转发的连接，但 hbbs 的
多节点调度会失效，客户端回落到"主端口+1"推导）。按顺序查三样：

```bash
docker compose logs rd-hbbr | grep -E "注册|hbbs"
#   连接 hbbs(hbbs:5187)失败        → HBBS_HOST 填错 / hbbs 没起来 / 5187 不通
#   未配置 RELAY_PUBLIC_ADDR         → 补上（Docker 网络下来源 IP 是网桥地址）
#   hbbs 拒绝中继节点注册            → 填了环回地址（127.0.0.1/localhost）
```

**确认注册成功**（hbbs 那台机器上执行）：

```bash
docker compose exec -T rd-hbbs sh -c 'echo rrg | nc 127.0.0.1 5187'
#   输出一张表：host / region / capacity / conns / seen_s
#   (no relay registered) = 一台都没注册上
```

`seen_s` 是"最近一次上报距现在多少秒"，超过 210 秒的节点会被 hbbs 自动剔除。
分机部署（hbbr 不在 hbbs 那台机器）时，`HBBS_HOST` 必须指向 hbbs 的公网/内网
可达地址，光通容器名是不够的。

---

## 五、运行时类

### 改了配置但不生效

UI 进程和服务进程**各持有一份内存配置**。改完设置需要**重启服务**才同步：

```bash
cd server && python rdctl.py restart     # 服务端
# 客户端侧：退出托盘图标里的程序再启动
```

### 客户端一打开就有一个 ID 和一串随机密码，是在连官方服务器吗

**不是。这两样都是本机算出来的，完全跟服务器无关。**

| 现象 | 谁生成的 | 源码位置 |
|---|---|---|
| **ID** | 客户端自己 | `client/libs/hbb_common/src/config.rs` → `gen_auto_id()`：PRNG 取 `[10^11, 10^12)`，恒为 **12 位纯数字** |
| **随机密码** | 客户端自己 | `client/libs/hbb_common/src/password_security.rs` → `TEMPORARY_PASSWORD`：进程启动时生成 **6 位纯数字** |

它们走的是同一条路径：**生成 → 存本地配置 → 再上报给 `custom.toml` 里写的那台 hbbs**。
所以无论你把服务器指向内网还是公网，ID 与随机密码都会照常出现；
反过来，断网状态下它们也照样有 —— 只是没有意义的"离线 ID"。

#### ID 为什么统一成 12 位

上游的 ID 长度是**按平台分叉**的：桌面端取 MAC 地址后 4 字节再 `& 0x1FFFFFFF`，
最多 9 位（还常常更短）；手机端是 `gen_range(1e9..2e9)`，恒 10 位。
同一套服务端上两种位数并存，通讯录、白名单、审计都得按平台分别处理。

现在统一成 12 位纯数字，判据只有一个：`Config::is_unified_id()`。
**旧 ID 会在 `Config::get_id()` 里被自动升级**（桌面 9 位 / 手机 10 位 / 主机名 ID 都算旧），
升级会打一条 INFO 日志。因为读写 ID 的入口散落在主进程 / 安装的服务 / Android 后台服务
三处，只有挂在这个所有人都调用的函数上，才能保证没有一路能拿到不合规范的 ID。

> ⚠️ 升级意味着**对方通讯录里存的旧 ID 会失效**，需要重新告知对方。
> 这是"统一"必须付的代价，也是本次改动唯一的破坏性影响。

> ⚠️ **桌面端主进程与安装的服务各有自己的配置文件**（Windows 上服务跑在
> SYSTEM 账户下，Linux 上跑在 root 下），所以升级后第一次启动时两边会各自
> 生成一个 12 位 ID，再通过 IPC 收敛 —— 最终以服务那边的为准
> （`ipc.rs` 的 `get_id()` 会采纳服务返回的 ID）。
> 收敛期间日志里可能看到两个 ID 各注册一次，属正常，之后就稳定了。
> 上游用 MAC 推导时两边天然算出同一个值，换成随机数后这个前提没了，
> 只能靠 IPC 同步 —— 这也是"要不要为了稳定性改用机器指纹派生"的权衡点。

12 位 = 约 9000 亿个取值，随机碰撞可以忽略。真撞了（服务端返回 `ID_EXISTS`）
客户端会自动 `Config::update_id()` 换一个重试，无需人工干预。

> 更稳的做法：不用 ID，用**固定永久密码 + 地址簿**。参见下一条。

#### 一次性密码为什么钉死成 6 位数字

上游把它拆成两个可选项：`temporary-password-length`（6/8/10）与
`allow-numeric-one-time-password`（数字 or 含字母）。前者让不同设备密码位数不一致，
后者让默认密码混进大小写字母 —— 口头报密码时极易听错。
现在固定为 6 位纯数字（`TEMPORARY_PASSWORD_LENGTH`），两个选项都不再生效，
对应的 UI 入口（桌面端"One-time password length"、移动端同名菜单项）已移除。

**随机密码每次不一样，不方便远程。怎么固定？**

```bash
# 方式一：启动时指定固定密码（适合批量部署）
wedesk.exe --password YourFixedPwd

# 方式二：UI 里"设置 → 安全 → 密码"里设一个永久密码，
#         并把"验证方式"改成 仅使用永久密码
```

对应的选项是 `verification-method = use-permanent-password`，也可写进 `RustDesk2.toml` 预置。

### 配置文件在哪

| 平台 | 路径 |
|---|---|
| Windows 便携版 | exe 同目录下的 `config/` |
| Windows 安装版 | `%APPDATA%\<AppName>\config\` |
| Linux | `~/.config/<appname>/` |
| macOS | `~/Library/Preferences/<appname>/` |

### 覆盖升级后 ID 丢了

ID 在 `config/` 目录里。**覆盖 exe 不会丢，但把整个目录删了会丢。**
便携版升级时保留 `config/` 目录。

### 检查更新失败 / 总提示"已是最新版本"

客户端的更新检查走 `https://www.alsoapp.com/static/wedesk/update.json`
（设置 → 关于 → 检查更新；勾了"自动更新版本"则启动 8 秒后自动查一次）。
失败时弹窗会带具体原因，按原因对号入座：

| 弹窗里的原因 | 含义 | 处理 |
|---|---|---|
| `HTTP 404` / `HTTP 5xx` | 官网静态目录里没有这个文件，或临时故障 | 找运维确认 `update.json` 已部署到 `www.alsoapp.com/static/wedesk/` |
| `Connection timed out` / `SocketException` | 客户端到不了外网 | 私有内网机器本来就出不去，属预期；有代理的话在 设置 → 网络 配 Socks5/Http(s) |
| `bad json` / `no version` | 文件内容不是约定的格式 | 对照 docs/14-desktop-ui.md 的格式说明修 JSON |
| 显示"已是最新版本"但官网已有新版 | `update.json` 的 `version` 没比客户端大 | 版本号是**三段式数字**按段比较，`v1.2.3` 这种带 `v` 或四段号都不会被判定为更新 |

> ⚠️ 这条链路与 Rust 侧官方更新器是**故意分开**的：私有部署客户端不允许访问
> api.rustdesk.com（`common.rs` 的 `do_check_software_update` 对 custom client
> 直接早退），所以主页那张"new version"卡片对定制客户端永远不显示，
> 更新提示只来自这里。

---

## 六、终极排查手段

**断网测试**：断开公网，两台内网机器仍能互连 ——
证明定制完整、完全不依赖官方服务器。

这是判断"私有化是否真的做到位"的唯一可靠方法。
