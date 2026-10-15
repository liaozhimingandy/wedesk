# 05 · 定制指南

> 文档索引：[`docs/README.md`](README.md) ｜ 上一篇：[04 部署指南](04-deploy.md) ｜ 下一篇：[06 维护手册](06-maintenance.md)

> 目标：换品牌、改服务器、裁剪功能。
> 所有行号与代码片段均对 **RustDesk master @ v1.5.1** 实测核对过。

---

## 一、定制的三个层次

改动按"侵入性"分三层，**优先用最上层**，越往下越难维护：

| 层次 | 手段 | 改在哪 | 升级上游时 |
|---|---|---|---|
| ① 配置注入 | 改 `build/custom.toml` 再跑 `customize.py` | 8 个文件的字符串 | ✅ 重跑脚本即可 |
| ② 资源替换 | 直接换文件 | 图标、安装包素材 | ✅ 不受影响（除非上游也改了同名文件） |
| ③ 源码直改 | 手写 Rust/Dart 逻辑 | 任意文件 | ⚠️ **必须手工重做**，且要重建补丁 |

**判断标准**：能用"字符串替换"表达的改动 → 加进 `custom.toml` / `customize.py` 规则表；
带 `if` 判断、新增函数、改控制流的 → 只能源码直改，并登记到 `upstream/UPSTREAM.md`。

---

## 二、第①层：配置注入

### 2.1 唯一需要手改的文件

`build/custom.toml`：

```toml
[server]
rendezvous_host = "192.168.2.114"                                 # hbbs 地址
key             = "bMwHjGjrhpNvD+lyhx2daVmrhmq94hyDfHNisznZTl0="  # RS_PUB_KEY
relay_host      = "192.168.2.114:21117"                           # hbbr 地址:端口

[brand]
app_name     = "企业网管助手"
exe_name     = "nethelper"
company_name = "贵公司名称"           # ⚠️ 占位值，务必替换
website      = "https://example.com"  # ⚠️ 占位值，务必替换
```

**`key` 从哪来**：服务端 `python rdctl.py key` 的输出。两边必须一致，否则客户端拒绝连接。

### 2.2 注入命令

```bash
python build/customize.py --dry-run    # 先预览，看命中几项
python build/customize.py              # 确认无误后写入
python build/customize.py --restore    # 后悔了，还原到定制前
```

**脚本是幂等的**，可以反复跑。改了 `custom.toml` 后重跑即可，不用担心重复注入。

**命令行临时覆盖**（CI 或一次性构建用）：

```bash
python build/customize.py --set brand.company_name="某某科技有限公司"
```

### 2.3 为什么需要 `.custom-state.json`

脚本精确匹配"上游原始值"来定位替换点。但你已经把公司名改成 A 之后，
脚本还在找上游的 `Purslane Tech Pte. Ltd.`，就**静默不命中**了。

`.custom-state.json` 记录"上次注入了什么"，脚本依次尝试：
**上游原始值 → 上次注入值**，两个都匹配不上才报未命中。

> 这个文件要跟着仓库走（已入库）。删掉它会导致改配置失效。

---

## 三、定制点原理

### 3.1 服务器地址是怎么选出来的

`libs/hbb_common/src/config.rs` 的解析链（优先级从高到低）：

```rust
pub fn get_rendezvous_server(...) -> String {
    let mut s = <传入参数>;                                              // ① 命令行/API
    if s.is_empty() { s = Self::get_option("custom-rendezvous-server"); } // ② 界面里用户填的
    if s.is_empty() { s = PROD_RENDEZVOUS_SERVER.read()...; }            // ③ 编译期注入
    if s.is_empty() { s = CONFIG2.read().rendezvous_server...; }         // ④ 本地配置文件
    if s.is_empty() { s = RENDEZVOUS_SERVERS.iter().next()...; }         // ⑤ 兜底常量 ← 我们改这层
    ...
}
```

**改兜底而不是硬锁定**的好处：员工仍可在设置里临时改服务器调试，
但出厂默认值永远指向我们自己的服务器。

> 上游旧版支持 `RENDEZVOUS_SERVER` / `RS_PUB_KEY` 编译期环境变量注入，
> 但 **1.5.1 的 `flutter-build.yml` 里已经搜不到这些变量了**，该机制已移除。
> 所以现在唯一可靠的做法是文本替换源码。

### 3.2 应用名只改一处就够了

```rust
// libs/hbb_common/src/config.rs:72
pub static ref APP_NAME: RwLock<String> = RwLock::new("企业网管助手".to_owned());
```

**Flutter 侧不写死应用名**，而是运行时从 Rust 读：

```dart
// flutter/lib/common.dart
String get appName {
  if (_appName.isEmpty) {
    _appName = bind.mainGetAppNameSync();   // ← 从 Rust 的 APP_NAME 取
  }
  return _appName;
}
```

**只需改 Rust 那一行，整个 UI 的标题、窗口名、关于页全部跟随。**

> 很多第三方定制方案还在用 `sed` 全局替换 `"RustDesk"` 字符串 ——
> 那样既容易漏，也会误伤 `https://rustdesk.com` 之类的正常链接。**别那么干。**

### 3.3 端口号

```rust
pub const RENDEZVOUS_PORT:     i32 = 21116;  // ID 注册/心跳(UDP) + TCP 打洞(TCP)
pub const RELAY_PORT:          i32 = 21117;  // 中继
pub const WS_RENDEZVOUS_PORT:  i32 = 21118;  // Web 客户端（默认不启用）
pub const WS_RELAY_PORT:       i32 = 21119;  // Web 客户端中继（默认不启用）
```

改端口属于"有成套要做的事"（compose 映射、防火墙、客户端同步），**一般不建议动**。

### 3.4 官方 API 兜底域名

```rust
// src/common.rs
fn get_api_server_(...) -> String {
    ...
    "https://admin.rustdesk.com".to_owned()   // ← 官方兜底
}
```

`customize.py` 默认把它置为 `""`，这是**"数据不出内网"合规的关键一环**。
配套还要改 `do_check_software_update()` —— 见 [02-runtime.md 第六节](02-runtime.md)。

---

## 四、品牌锚点清单

`customize.py` 覆盖这 8 个文件：

| 文件 | 改了什么 |
|---|---|
| `libs/hbb_common/src/config.rs` | **核心三值**：`RENDEZVOUS_SERVERS`、`RS_PUB_KEY`、`APP_NAME` |
| `src/common.rs` | 清空官方 API 兜底地址 |
| `Cargo.toml` | 作者、描述、公司名、`LegalCopyright` |
| `flutter/windows/runner/Runner.rc` | exe 文件属性：公司名、产品名、exe 名 |
| `flutter/android/app/src/main/res/values/strings.xml` | 安卓端 app 名 |
| `flutter/lib/common.dart` | "Powered by" 链接指向自家官网 |
| `flutter/lib/desktop/pages/install_page.dart` | 安装页隐私政策链接 |
| `flutter/lib/desktop/pages/desktop_setting_page.dart` | "关于"页：官网、隐私政策、版权行 |

> 公司名原文是 **`Purslane Tech Pte. Ltd.`**。网上流传的旧教程常写作 "Purslane Ltd"，
> 那是过期信息，按旧值匹配会失败。

> 单元测试 `#[cfg(test)]` 里还有 2 处官方地址，**故意没改** —— 改了会破坏测试意图。

---

## 五、第②层：资源替换（换 Logo）

| 想换的东西 | 文件 |
|---|---|
| 应用图标 | `res/icon.png`、`res/icon.ico` |
| 托盘图标 | `res/tray-icon.ico` |
| 各尺寸 | `res/32x32.png`、`res/64x64.png`、`res/128x128.png`、`res/128x128@2x.png` |
| Windows 安装包素材 | `res/msi/banner.bmp` 等 |
| macOS | `flutter/macos/Runner/Assets.xcassets/AppIcon.appiconset/` |
| Android | `flutter/android/app/src/main/res/mipmap-*/ic_launcher.png` |

`build/branding/` 目录目前是空的 —— 把你的素材放进去占位，替换时从那里拷。

> 图标改了但 exe 还显示旧的：Windows 有图标缓存，删 `%LOCALAPPDATA%\IconCache.db` 或重启资源管理器。

---

## 六、第③层：功能裁剪

| 想做的事 | 入手点 |
|---|---|
| 隐藏"设置服务器"入口，防员工误改 | `flutter/lib/desktop/pages/desktop_setting_page.dart` 的网络设置区块 |
| 预设固定密码做无人值守 | 搜索 `permanent password` / `approve-mode` |
| **去掉文件传输（防泄密）** | `src/server/` 文件服务模块 **+ Flutter 侧对应 UI**，两端都要改 |
| 加连接审计 | 服务端 SQLite（`server/data/db_v2.sqlite3`），配合 `rdctl.py backup` 归档 |
| 禁止连接外部 ID | **服务端层面限制更彻底**，比改客户端可靠 |
| 禁用剪贴板同步 | `src/server/clipboard_service.rs` + 设置项 |

> ⚠️ **只隐藏 UI 是假禁用。** 协议还在，对面用别的客户端照样能触发。
> 真禁用必须 Rust 侧不提供服务。

---

## 七、验证定制成功

```bash
# 1. 看注入命中数（应该全命中，无未命中项）
python build/customize.py --dry-run

# 2. 确认核心三值落地
grep -nE 'APP_NAME|RENDEZVOUS_SERVERS|RS_PUB_KEY' libs/hbb_common/src/config.rs

# 3. 确认没有残留官方兜底
grep -rn "admin.rustdesk.com" src/ | grep -v "^src/bridge_generated"

# 4. 补丁仍可重放（改了那 8 个文件后必做）
python upstream/verify_patch.py --regen
python upstream/verify_patch.py
```

**运行时终极验证**：断开公网，两台内网机器仍能互连 → 说明完全没依赖官方服务器。
