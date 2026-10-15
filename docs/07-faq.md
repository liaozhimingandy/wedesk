# 07 · 故障排查

> 文档索引：[`docs/README.md`](README.md) ｜ 上一篇：[06 维护手册](06-maintenance.md)

> 按症状查表。**构建类问题先跑 `python build/check-env.py`**，它会告诉你缺什么。

---

## 一、构建类

### `file not found for module bridge_generated`

**原因**：`src/bridge_generated.rs` 不存在 —— bridge 胶水代码没生成。
这是**本地构建最容易漏的一步**，官方 CI 是单独 job 生成的，本地脚本早期没覆盖。

```bash
python build/generate-bridge.py     # 首次约 10-25 分钟（要编译 codegen）
```

生成成功后会产出 4 个文件：

| 文件 | 用途 |
|---|---|
| `src/bridge_generated.rs` | Rust 侧桥（**构建必需**） |
| `src/bridge_generated.io.rs` | Rust 侧 IO 桥 |
| `flutter/lib/generated_bridge.dart` | Dart 侧桥（**构建必需**） |
| `flutter/lib/generated_bridge.freezed.dart` | Dart 冻结类 |

用 `--check` 可以快速确认是否就绪（秒级）：

```bash
python build/generate-bridge.py --check
```

---

### `cargo: command not found` / `rustc` 报奇怪的错

**原因**：`~/.cargo/bin` 下的转发壳被截断成 0 字节。
通常发生在**并发跑 rustup 或中途 kill 掉 rustup** 的时候。

```bash
python build/fix-rust.py --check    # 诊断
python build/fix-rust.py            # 修复
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
build\build-local.bat
```

---

### vcpkg 依赖报错 / 缺 libvpx

```bat
set VCPKG_ROOT=D:\vcpkg
build\install-vcpkg-deps.bat      :: 首次 30-120 分钟
```

> ⚠️ `install-vcpkg-deps.bat` 与 `build-local.bat` 的 `VCPKG_ROOT` **必须指向同一个目录**，
> 否则装到 A 盘、构建去 B 盘找，必然失败。

---

### 窗口行为异常 / 下拉菜单不对

**原因**：没换成 RustDesk 自研 Flutter engine。官方 engine 会有各种细微异常。

```bat
build\apply-rustdesk-engine.bat
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
> 以及 `res/vcpkg/libyuv` 那条"用 MSVC 编译 libyuv 会很慢"的提示，
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
python build/customize.py --dry-run    # 看命中数与未命中项
```

若显示未命中，确认 `build/.custom-state.json` 还在（它已入库，别删）。

---

### 客户端标题还是 "RustDesk" / 图标没换

按顺序排查：

1. `python build/customize.py --dry-run` —— 是否全命中
2. `grep -n APP_NAME libs/hbb_common/src/config.rs` —— 值是否落地
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
| ID 正常但连不上对方 | 打洞失败 且 hbbr 未通 | 检查 21117 是否放行 |
| 能连但极卡 | 走了中继 | 确认 `ALWAYS_USE_RELAY` 与 NAT 类型 |
| 提示公钥不匹配 | `RS_PUB_KEY` 与服务端密钥不配对 | `rdctl.py key` 重新取，改 `custom.toml` 后**重新构建** |
| 连上立刻断开 | 认证超时 / 服务进程未运行 | 需要无人值守时确认已装系统服务 |

### 怎么判断当前是 P2P 还是中继

看连接质量指示器，或直接看服务端 21117 端口流量 ——
**P2P 成功时 hbbr 几乎没有流量**。

如果内网环境下大量走中继，检查：

- `ALWAYS_USE_RELAY` 是否被设成了 `Y`
- 是否跨了 VLAN（不同 VLAN 之间打洞成功率下降）
- 客户端是否走了 VPN（VPN 会改变 NAT 行为）

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
cd server && cat .env  # RELAY_HOST 是否填了实际 IP
python rdctl.py logs   # 看容器日志
```

### 端口冲突

改 `.env` 里的 `HBBS_TCP` / `HBBS_UDP` / `HBBR_TCP` 映射到别的主机端口。

> 改的是**主机侧**端口，容器内仍是 21115/21116/21117。客户端配置不用跟着改。

### 审计日志里来源 IP 全是网关地址

**原因**：端口映射模式下 NAT 会把来源改写成网关 IP。

**解法**（仅 Linux）：`docker-compose.yml` 里换成 `network_mode: "host"`，能拿到真实来源 IP。

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
| **ID** | 客户端自己 | `libs/hbb_common/src/config.rs` → `get_auto_id()`：取本机 MAC 地址后 4 字节，拼成 u32 后 `& 0x1FFFFFFF` |
| **随机密码** | 客户端自己 | `libs/hbb_common/src/password_security.rs` → `TEMPORARY_PASSWORD`：进程启动时用 PRNG 生成 6 位串 |

它们走的是同一条路径：**生成 → 存本地配置 → 再上报给 `custom.toml` 里写的那台 hbbs**。
所以无论你把服务器指向内网还是公网，ID 与随机密码都会照常出现；
反过来，断网状态下它们也照样有 —— 只是没有意义的"离线 ID"。

两个衍生问题：

**ID 会重复吗？** 空间只有 29 位（约 5.4 亿个取值），理论上会撞。真正常见的撞法是
**虚拟机模板克隆 / 虚拟网卡 MAC 相同** —— MAC 一样，算出来的 ID 必然一模一样，
后上线的那台会把前面那台顶掉，表现为"A 机器连过去其实是 B 机器的桌面"。
真撞了就改用主机名作为 ID（设置 → 安全 → 允许使用主机名作为 ID，
对应 `OPTION_ALLOW_HOSTNAME_AS_ID`），或手工改 `config/` 下的 id。

> 更稳的做法：不用 ID，用**固定永久密码 + 地址簿**。参见下一条。

**随机密码每次不一样，不方便远程。怎么固定？**

```bash
# 方式一：启动时指定固定密码（适合批量部署）
nethelper.exe --password YourFixedPwd

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

---

## 六、终极排查手段

**断网测试**：断开公网，两台内网机器仍能互连 ——
证明定制完整、完全不依赖官方服务器。

这是判断"私有化是否真的做到位"的唯一可靠方法。
