# 定制点原理图

想改更多东西（换图标、加审计、去掉某些功能）时，这份文档是入口。
以下所有行号与代码片段均对 **RustDesk master @ v1.5.1** 实测核对过。

---

## 1. 服务器地址是怎么被选出来的

这是整套定制的核心。`libs/hbb_common/src/config.rs:912` 起的解析链：

```rust
pub fn get_rendezvous_server(...) -> String {
    let mut rendezvous_server = <传入参数>;                  // ① 命令行/API 传入
    if rendezvous_server.is_empty() {
        rendezvous_server = Self::get_option("custom-rendezvous-server");  // ② 界面里用户填的
    }
    if rendezvous_server.is_empty() {
        rendezvous_server = PROD_RENDEZVOUS_SERVER.read().unwrap().clone(); // ③ 编译期注入
    }
    if rendezvous_server.is_empty() {
        rendezvous_server = CONFIG2.read().unwrap().rendezvous_server.clone(); // ④ 本地配置
    }
    if rendezvous_server.is_empty() {
        rendezvous_server = RENDEZVOUS_SERVERS.iter().next()...;  // ⑤ 兜底常量 ← 我们改这里
    }
    if !rendezvous_server.contains(':') {
        rendezvous_server = format!("{rendezvous_server}:{RENDEZVOUS_PORT}");
    }
    rendezvous_server
}
```

`src/common.rs:1206` 的 `get_custom_rendezvous_server()` 另有 Windows 特例——
**exe 文件名里可以嵌入 license 信息**（`get_license_from_exe_name()`），优先级最高。
这条路可以把服务器信息编进文件名做分发，但文件名会被改，不如改常量稳妥。

### 为什么改兜底常量而不是直接用 option_env

官方旧版本支持 `RENDEZVOUS_SERVER` / `RS_PUB_KEY` 等编译期环境变量注入，
但 **master（1.5.1）的 `flutter-build.yml` 里已经搜不到这些变量了**，该机制已移除。
所以现在唯一可靠的做法是文本替换源码。

---

## 2. 服务端口号

`libs/hbb_common/src/config.rs:117-123`

```rust
pub const RENDEZVOUS_SERVERS: &[&str] = &["rs-ny.rustdesk.com"];  // ← 改成你的地址
pub const RS_PUB_KEY: &str = "OeVuKk5nlHiXp+APNn0Y3pC1Iwpwn44JGqrQCsWqmBw="; // ← 改成你的公钥

pub const RENDEZVOUS_PORT: i32 = 21116;   // ID 注册/心跳(UDP) + TCP打洞(TCP)
pub const RELAY_PORT: i32 = 21117;        // 中继
pub const WS_RENDEZVOUS_PORT: i32 = 21118; // Web 客户端（默认不启用）
pub const WS_RELAY_PORT: i32 = 21119;     // Web 客户端中继（默认不启用）
```

改端口属于"有成套要做的事"（compose 映射、防火墙、客户端三处都要同步），一般不建议动。

---

## 3. 应用名只改一处就够了

```rust
// libs/hbb_common/src/config.rs:72
pub static ref APP_NAME: RwLock<String> = RwLock::new("RustDesk".to_owned());
```

**Flutter 侧并不写死应用名**，而是运行时从 Rust 读取：

```dart
// flutter/lib/common.dart:4188
String get appName {
  if (_appName.isEmpty) {
    _appName = bind.mainGetAppNameSync();   // ← 从 Rust 的 APP_NAME 取
  }
  return _appName;
}
```

所以**只需改 Rust 那一行，整个 UI 的标题、窗口名、关于页全部跟随**。
这是最容易被浪费的一个便利——很多第三方定制方案还在用 sed 全局替换 "RustDesk"
字符串，那样既容易漏，也可能误伤 `https://rustdesk.com` 之类的正常链接。

---

## 4. 断开发送：API 兜底域名

```rust
// src/common.rs:1259
fn get_api_server_(...) -> String {
    ...
    "https://admin.rustdesk.com".to_owned()   // ← 官方兜底
}
```

定制客户端用不上官方 API。若不处理，客户端在某些路径下仍会向官方域名发请求。
`customize.py` 默认把它置为 `""`，这是"数据不出内网"合规的关键一环。

---

## 5. 其余品牌锚点

| 位置 | 作用 | 当前值 |
|---|---|---|
| `Cargo.toml:4` | 作者署名 | `authors = ["rustdesk <info@rustdesk.com>"]` |
| `Cargo.toml:259` | 版权 | `Copyright © 2026 Purslane Tech Pte. Ltd.` |
| `flutter/windows/runner/Runner.rc:92-98` | Windows 属性（公司名/产品名/文件名/版权） | `CompanyName`, `ProductName`, `FileDescription`, `OriginalFilename`, `InternalName`, `LegalCopyright` |
| `flutter/android/app/src/main/res/values/strings.xml:2` | Android 应用名 | `<string name="app_name">RustDesk</string>` |
| `flutter/lib/common.dart:3741` | 帮助菜单官网 | `launchUrl(Uri.parse('https://rustdesk.com'))` |
| `flutter/lib/desktop/pages/desktop_setting_page.dart:2599,2607,2625` | 关于页链接与版权 | `rustdesk.com/privacy.html` 等 |
| `flutter/lib/desktop/pages/install_page.dart:216,218` | 安装页隐私政策 | `https://rustdesk.com/privacy.html` |

> 注意公司名是 **`Purslane Tech Pte. Ltd.`**，网上流传的旧教程常写作 "Purslane Ltd"，
> 那是过期信息，按旧值匹配会失败。

---

## 6. 想继续扩展的话

| 想做的事 | 入手点 |
|---|---|
| 换图标 | `res/icon.png`、`res/icon.ico`、`res/tray-icon.ico`；Windows 还需 `res/msi/banner.bmp` 等安装包素材 |
| 隐藏"设置服务器"入口，防止员工误改 | `flutter/lib/desktop/pages/desktop_setting_page.dart` 中的网络设置区块 |
| 预设固定密码做无人值守 | 搜索 `permanent password` / `approve-mode` 相关逻辑 |
| 去掉文件传输（防泄密） | `src/server/` 下的文件服务模块 + Flutter 侧对应 UI |
| 加连接审计 | 服务端 SQLite（`server/data/db_v2.sqlite3`），配合 `rdctl.py backup` 归档 |
| 禁止连接外部 ID | 服务端层面限制更彻底，比改客户端可靠 |

---

## 7. 关于 CI 为什么必须改官方 workflow

`flutter-build.yml` 顶层是 `on: workflow_call`，由别的 workflow 通过
`uses: ./.github/workflows/flutter-build.yml` 调用。

**陷阱**：它的内部第一步就是 `actions/checkout`。
如果你在自己的 job 里先跑定制、再调用它，它的 checkout 会把修改全部冲掉，
编译出来的还是原版。

所以正确做法是把定制步骤插进它的**内部**，位置在 checkout 之后、`Build rustdesk` 之前。
`patch-ci.py` 干的就是这件事，并且保持了幂等（重复执行不会重复插入）。

完整的官方 Windows x64 构建链路供参考：

```
checkout(recursive) → download bridge artifact → LLVM 15.0.6
→ Flutter 3.24.5 → 替换为 rustdesk 自研 engine → 打 dropdown 补丁
→ Rust 1.75 → vcpkg(commit 9e59...) → ★我们的定制步骤★
→ python build.py --portable --flutter --hwcodec --vram → 产出
```

这套链路里任何一步版本不匹配都会导致构建失败，这也是推荐直接改官方 workflow、
而不是自己重写 CI 的原因。
