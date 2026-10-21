# 14 · 桌面端 UI（WeDesign）

> 文档索引：[`docs/README.md`](README.md) ｜ 上一篇：[13 服务端部署与运维手册](13-server-deployment-handbook.md)

> **先说结论**：桌面端主界面已按 ToDesk 的视觉语法重做一遍，
> 所有样式收进一个设计系统文件 `client/flutter/lib/common/widgets/wedesign.dart`。
> **改配色/圆角/间距只需改这一个文件**，不要再在各页面里硬编码颜色。

---

## 一、WeDesign 是什么

`common/widgets/wedesign.dart` 是唯一的样式来源，提供三层东西：

| 层 | 内容 | 典型用法 |
|---|---|---|
| Token | `WeColors` / `WeRadii` / `WeSpace` / `WeShadow` | 取色、取圆角、取间距 |
| 基础组件 | `WeCard` / `WeIconButton` / `WePrimaryButton` / `WeOutlineButton` / `WeStatusPill` / `WeSectionTitle` | 直接拼页面 |
| 小工具 | `groupId()`（ID 4 位分组） | 展示设备码 |

几个必须知道的规则：

1. **颜色一律走 `WeColors.of(context)`**，它按 `Theme.of(context).brightness`
   自动返回亮/暗配色。直接写 `Colors.xxx` 或 `Theme.of(context)` 的地方，
   在改主题时一定会漏。
2. **圆角只有 4 档**：`WeRadii.sm(6) / md(10) / lg(14) / xl(20)`。
   卡片用 `lg`，输入框和按钮用 `md`，小标签用 `sm`。
3. **间距只有 6 档**：`WeSpace.xs(4) / s(8) / m(12) / l(16) / xl(20) / xxl(24)`。
4. `WeColors.brandGradient(context)` 是主按钮和本机卡头的渐变，
   改它会同时影响这两处 —— 这是刻意的，品牌感来自一致性。

| Token | 亮色 | 暗色 |
|---|---|---|
| `brand`（主蓝） | `#1E71FF` | `#3E86FF` |
| `brandEnd`（渐变尾） | `#4C9BFF` | `#1B5FE0` |
| `success` | `#17B26A` | `#2BCF80` |
| `warn` | `#F79009` | `#FFA733` |
| `danger` | `#F04438` | `#FF5C50` |
| `surface` / `surfaceAlt` | 白 / `#F5F7FA` | `#1E2026` / `#26282F` |

---

## 二、主界面改成了什么

布局参考 ToDesk：**左侧竖排导航 + 右侧主内容**，主内容从上到下是
「允许控制本设备 → 设备代码卡 → 远程控制设备」；整窗最上面是一条
**ToDesk 风格功能栏**（品牌 + 快捷操作 + 最小化/关闭）。

改造后（文件 → 职责）：

| 文件 | 职责 |
|---|---|
| `desktop/widgets/window_top_bar.dart` | **新增**。窗口顶部功能栏（高 46）：左侧 Logo + 产品名，右侧「快速上手 / 帮助 / 设置 / 最小化 / 关闭」（关闭悬停变红）。整条栏即拖动区 |
| `desktop/widgets/side_menu.dart` | 左侧导航栏（宽 208）：Logo、**三个导航项**「远程控制 / 设备列表 / 高级设置」（选中态品牌软底色 + 品牌字色）。选「高级设置」直接把右栏换成设置页（`weHomeNavIndex`），不再开新 Tab。栏身也是拖动区 |
| `desktop/widgets/local_device_card.dart` | 本机设备区：**「允许控制本设备」开关**（映射 `stop-service` 选项）+ 服务状态 pill；下方浅灰卡片左列「设备代码」（12 位 4 位一组大号 + 复制）、右列「连接模式」（**下拉切换一次性/固定/两者都用** + 临时密码的显示/隐藏/复制/刷新 + 固定密码的设置入口），中间竖分隔线 |
| `desktop/widgets/connect_panel.dart` | 连接区：标题行「远程控制设备」+ 就绪状态 pill、46px 搜索框（搜索图标 + 自动补全 + 一键清除）+ 蓝色主按钮「连接」、**连接模式单选**（远程控制 / 文件传输，Enter 与按钮跟随所选模式）、四张功能卡片（文件传输 / 摄像头 / 终端 / TCP 隧道） |
| `desktop/pages/connection_page.dart` | 承接 `navIndex`（0=远程控制页，1=设备列表页），内部 `IndexedStack` 切换、两页保活；`header` 参数用于在连接区上方注入设备卡/帮助卡；底部状态栏保留。远程控制页用 **fill-or-center**（`LayoutBuilder + ConstrainedBox(minHeight)` + `mainAxisAlignment.center`）：内容比窗口矮时垂直居中，不再出现「往下拉一片空白」 |
| `desktop/pages/desktop_home_page.dart` | 普通模式：`Column[WeWindowTopBar, Expanded(Row[WeSideMenu, 右栏])]`，右栏按 `weHomeNavIndex` 在 `ConnectionPage` / `DesktopSettingPage` 间切换；`incoming-only` 小窗保留旧紧凑布局（设备卡在左栏），`header` 置空避免重复渲染；帮助卡（安装/macOS 权限警告）移入主内容滚动区 |

**用户友好度的实际增量**（都是原来要靠猜的操作）：

- 「允许控制本设备」**开关直接放在主界面**（原来只能去设置里停/启服务）；
- 设备码、临时密码都有**显式复制按钮**（原来只有双击）；
- 临时密码可**显示/隐藏**（原来永远明文且无法核验）；
- 密码区**下拉直接切换连接模式**（一次性/固定/两者都用），不用进设置页；
- 输入框有**清除按钮**和搜索图标，聚焦时高亮品牌色；
- 连接前可用**单选**切换「远程控制 / 文件传输」，二/三类入口升级为卡片；
- 「设备列表」成为独立页面，最近会话/收藏/地址簿不再挤在连接区下面；
- 状态栏能看到**当前有几个会话**。

### 窗口框架：原生标题栏已彻底移除

`TitleBarStyle.hidden` **只缩边框、保留原生标题按钮**（截图里顶部那条
「— □ ✕」就是它）。真正去掉要调 `windowManager.setAsFrameless()`
（仅 Windows；macOS/Linux 的 `hidden` 本身就是全 frameless）。
主窗口在 `runMainApp` 的固定尺寸分支里调用，代价是失去原生边框和阴影，
用 Flutter 侧 1px 描边 + 顶栏分隔线补回层次。拖动窗口有两个区域：
顶部功能栏、左侧导航栏（都是 `onPanStart → windowManager.startDragging()`）。

### 高级设置（ToDesk 化）

设置页**内嵌在主窗口右栏**：侧栏选「高级设置」（`weHomeNavIndex = 2`）
时 `desktop_home_page.dart` 把右栏从 `ConnectionPage` 换成
`DesktopSettingPage`；`DesktopTabPage.onAddSetting` 在新 UI 路径下
也只是切这个导航（macOS / 兼容模式 / incoming-only 仍走旧的开 Tab 逻辑）。

`desktop_setting_page.dart` 从「左侧竖 Tab + 右 PageView」改为
**顶部横向 Tab 条**（`_horizontalTabs`：图标 + 文字，选中态品牌色 +
2px 下划线，标签溢出可横向滚动），顺序对齐 ToDesk：
基本设置 / 显示设置 / 安全设置 / 网络设置 / 账户设置 / 打印设置 / 关于，
桌面端不再渲染大标题（省一截纵向空间）。
安全页顶部是**琥珀色建议横幅**（⚠ + 「设置并开启」按钮，跳设置固定密码），
下面是通栏圆角卡片（`_Card` 不再是 540px 定宽）。
设置项本身的逻辑没动，只重排。

### 网络设置页：只保留代理（2026-10-21 起）

「网络设置」Tab 原先有四个入口：**ID/Relay Server**（自定义服务器地址）、
**Socks5/Http(s) Proxy**、**Use WebSocket** 开关，以及自建服务器才出现的
**Allow insecure TLS fallback** / **Disable UDP**。现在**只剩代理一项**。

原因：中继节点清单改由服务端维护 —— 每台 hbbr 启动后自动向 hbbs 注册上报，
P2P 打洞失败时由 hbbs 按 权重 / 负载 / 时延 统一下发。客户端再保存一份
服务器数据，只会出现"服务端换了节点、客户端还连老的"这类最难排查的问题，
所以"ID/Relay Server"整个入口被移除（`showServerSettings` 调用点删掉），
`get_relay_server()` 也不再读 `relay-server` 配置项。

三个开关一并移除的理由：它们都属运行期调优，日常用户不需要，且误开
**Disable UDP** 会直接把连接拖进"正在接入网络"卡死的分支
（`force_tcp` 路径，WS 失败也不会回退 UDP，见 docs/10-ports.md 坑 4）。
确有需要时仍可通过配置文件改对应 option：

```
allow-websocket = Y      # 等价于原 "Use WebSocket" 开关
disable-udp = Y          # 等价于原 "Disable UDP" 开关
```

或让运维在 `build/client/custom.toml` 里调整默认值后重新构建。
移动端设置页只移除了"ID/Relay Server"一项，本地传输类开关保留 ——
运营商网络下 UDP 常被限制，"Use WebSocket"是移动端唯一的自救手段。

### 关于我们：检查更新（2026-10-21 起）

「关于」Tab 在版本信息上方新增两块（版式对齐 ToDesk）：

- **自动更新版本**（勾选框）：勾上后客户端启动约 8 秒会自动检查一次更新，
  发现新版本时弹全局对话框引导下载。存的是 `allow-auto-update` 配置项，
  与 Rust 侧同名 option 同一个键。
- **当前版本 Vx.y.z + 检查更新按钮**：手动检查，进行中按钮变成
  "正在检查..."并禁用连点。

检查逻辑在 `client/flutter/lib/desktop/widgets/update_check.dart`（纯 Dart，
不动 Rust 侧 updater.rs——那条链路是给官方 api.rustdesk.com + GitHub Release
设计的，私有部署客户端不允许访问外网更新源，见 common.rs 的
`do_check_software_update` 对 custom client 的直接早退）。

**更新数据源**：`https://www.alsoapp.com/static/wedesk/update.json`，
发新版本时由运维更新这个文件即可，客户端不用重新构建。**线上实际格式**
（packages 数组，每平台一条，2026-10-21 起客户端按此解析）：

```json
{
  "app_id": "wedesk",
  "publish_time": "2026-10-21T14:30:00+08:00",
  "global_release_note": "本次迭代概要",
  "packages": [
    {
      "platform": "windows", "arch": "x64",
      "version": "1.2.5", "version_code": 125,
      "min_supported_version_code": 100,
      "is_force": false, "update_type": "full", "enabled": true,
      "update_title": "WeDesk 新版本可用",
      "update_desc": "✅ 优化内网P2P\n✅ 修复WSL2网络场景",
      "download_url": "https://www.alsoapp.com/static/wedesk/wedesk-1.2.5-setup.exe",
      "sha256": "...", "size": 12582912
    },
    { "platform": "android", "arch": "arm64-v8a", "version": "1.2.5", "is_force": true, "...": "..." },
    { "platform": "linux",  "arch": "x64",   "version": "1.2.5", "...": "..." },
    { "platform": "macos",  "arch": "arm64", "version": "1.2.5", "...": "..." }
  ],
  "gray_release": { "enable": false, "percent": 0.2, "device_white_list": [] },
  "metadata": { "sign": "", "cache_ttl": 300 }
}
```

客户端解析规则（`update_check.dart`）：

- 只看 `platform` == 当前平台 且 `enabled != false` 的条目；`arch` 与本机
  ABI 一致的优先（windows `x64` / android `arm64-v8a` / macos `arm64` 等写法
  都会归一化后再比），同组里取 `version` 最高的。
- 当前平台没有任何条目 → 按"已是最新"处理（平台未发布不算故障）。
- `version` 三段式比较，**大于**客户端当前版本才提示更新；容忍 `v` 前缀。
- `is_force: true` → 强制更新：弹窗不可关闭，"稍后"隐藏，只剩"立即更新"。
- 说明文案取 `update_desc`，为空回退 `update_title`，再回退顶层
  `global_release_note`；下载地址用 `download_url`。
- `min_supported_version_code` / `gray_release` / `metadata` 暂未使用
  （灰度留作后续扩展）。

早期设计的顶层平铺格式（`version` + `platforms.<平台>.url` + `notes` +
`mandatory`）客户端仍然兼容，作为 packages 数组缺失时的回退。

交互分支：有新版本 → 弹窗（当前版本 → 新版本 + 更新说明 + 立即更新/稍后，
"立即更新"用系统浏览器打开下载地址）；已是最新 → toast"已是最新版本"；
网络失败 → 弹窗显示错误原因（不阻塞正常使用）。请求超时 10 秒。

### 主窗口旧标签栏已隐藏

`DesktopTab` 加了实例级参数 `showTabBar`（默认 true，不影响远程会话窗口）。
`DesktopTabPage` 在新 UI 路径（非 macOS、非兼容模式、非 incoming-only）
下传 `showTabBar: false`——旧的那条「Logo + Tab + ≡ — □ ✕」整条消失，
窗口按钮由顶部功能栏（`WeWindowTopBar`）承担，固定窗口没有最大化。

---

## 三、常见改法

**换主色**（比如改成企业蓝）：只改 `wedesign.dart` 里的
`WeColors.brandLight` / `brandDark` / `brandEndLight` / `brandEndDark` 四个常量。
本机卡头、主按钮、Tab 选中态、输入框聚焦态、链接色会一起变。

**加一个新卡片**：用 `WeCard`，不要自己写 `Container + BoxDecoration`：

```dart
WeCard(
  padding: const EdgeInsets.all(WeSpace.l),
  child: Column(children: [/* ... */]),
);
```

**加一个主操作按钮**：`WePrimaryButton(text: ..., icon: ..., onPressed: ...)`，
它自带渐变、悬浮阴影和禁用态。次要操作用 `WeOutlineButton`。

**改 ID 分组**：不要自己写分隔逻辑，`groupId(id)` 复用
`common/formatter/id_formatter.dart` 的 `formatID`，保证卡头与输入框永远一致
（`kIdGroupSize` 当前为 4，即 `1234 5678 9012`）。

**主窗口是固定尺寸的**（2026-10-20 起）：

- 尺寸常量 `kMainWindowFixedSize = Size(960, 560)`（`consts.dart`），
  在 `main.dart` 的 `runMainApp` 里 `setSize + center`，然后
  `setResizable(false)` + `setMaximizable(false)`；
- 只保留**位置**记忆，不再恢复保存的尺寸/最大化状态
  （`restoreWindowPosition` 在固定模式下被跳过）；
- **窗口最顶部是自绘功能栏**（`WeWindowTopBar`：品牌 + 快捷操作 +
  最小化/关闭），原生标题栏连同其按钮已彻底移除。拖动窗口有两个区域：
  顶部功能栏、左侧导航栏（都是 `onPanStart → windowManager.startDragging()`）；
- incoming-only / outgoing-only 特殊构建不走固定逻辑，行为不变；
- 远程会话等子窗口不受影响，照常可调整大小。

**密码区可以直接切连接模式**（2026-10-20 起）：本机设备卡「连接模式」
列下方就是下拉框（`local_device_card.dart` 复用 `common.dart` 的
`ComboBox`），选项写的是 `kOptionVerificationMethod`——与设置页
「安全 → 密码」是**同一个选项**，两边永远一致：

| 选项 | 界面表现 |
|---|---|
| 使用一次性密码 | 一次性密码行（显隐/复制/刷新） |
| 使用固定密码 | 固定密码行（已设置/未设置 + 设置入口） |
| 同时使用 | 两行都显示 |

规则照搬设置页：选到固定密码但还没设过时，先弹「设置固定密码」框，
设完才真正切过去。固定密码**只能写不能读**（Rust 侧只暴露
`permanent-password-set` 布尔），所以显示 `••••••`/「未设置」而不显示明文。

**品牌名**：窗口标题、托盘、About 页读的都是 `hbb_common` 的 `APP_NAME`
（经 `mainGetAppNameSync()` 动态取）。改显示名要动四处，缺一处就会出现
「有的地方新名字、有的地方旧名字」：

| 位置 | 内容 |
|---|---|
| `build/client/custom.toml` 的 `[brand] app_name` | 注入源头 |
| `client/libs/hbb_common/src/config.rs` 的 `APP_NAME` | 运行时全局 |
| `client/flutter/windows/runner/Runner.rc` | Windows 文件属性 |
| `client/flutter/android/.../values/strings.xml`、`ios/Runner/Info.plist` | 移动端显示名 |

改完 `custom.toml` 后同步 `build/client/.custom-state.json` 里对应条目
（customize.py 靠它做新旧值替换）。**exe 文件名**在
`client/flutter/windows/CMakeLists.txt` 的 `BINARY_NAME`（当前
`wedesk`），MSI 打包（`res/msi/preprocess.py --app-name`）必须传同名参数，
否则 WiX 按 `$(var.Product).exe` 找不到主程序。

**关于页/托盘等 UI 文案**不要写死 "RustDesk"：用
`'${translate('About')} ${bind.mainGetAppNameSync()}'` 这种组合；
新增翻译时中/繁的**值**里也不应出现 RustDesk（key 里的不算，key 是代码引用）。

---

## 四、新增文案

界面新增了 12 条文案，已同时进 `client/src/lang/cn.rs` 与 `tw.rs`：

| key | 中文 | 用途 |
|---|---|---|
| `Allow remote control` | 允许控制本设备 | 设备卡开关行 |
| `Device ID` | 设备代码 | 设备码标签 |
| `Connection mode` | 连接模式 | 密码列标签 / 模式下拉 |
| `Remote control device` | 远程控制设备 | 连接区标题 |
| `Ready to connect` | 已准备好连接 | 连接区状态 pill |
| `Device list` | 设备列表 | 侧边导航项 |
| `Remote control` | 远程控制 | 侧边导航项 / 模式单选 |
| `Copy` | 复制 | 设备码/密码复制按钮 |
| `Show` / `Hide` | 显示 / 隐藏 | 密码显隐 |
| `Active sessions` | 活动会话 | 状态栏会话计数 |
| `Permanent password` / `Not set` | 固定密码 / 未设置 | 固定密码行 |
| `Quick start` | 快速上手 | 顶部功能栏 |
| `Basic settings` 等 7 条 | 基本设置 / 显示设置 / 安全设置 / 网络设置 / 账户设置 / 打印设置 | 设置页横向 Tab（ToDesk 顺序） |
| `Security verification` | 安全验证 | 安全设置卡标题 |
| `Verification method when controlling this device` | 控制本设备时的验证方式 | 安全设置卡标题 |
| `Set up and enable` / `banner_2fa_title` / `banner_2fa_desc` | 设置并开启 / 建议设置主设备… | 安全设置顶部横幅 |
| `Auto update version` 等 12 条 | 自动更新版本 / 检查更新 / 正在检查更新... / 发现新版本 / 已是最新版本 / 检查更新失败 / 立即更新 / 稍后 / 更新说明 / 暂无更新说明 / 当前版本 / 该平台暂未提供下载地址 | 关于页检查更新（2026-10-21 新增） |

其余语言（en 等）没有对应条目时会**回退显示英文 key**，这是上游 `translate()`
的既有行为；要补齐就往对应 `src/lang/*.rs` 里加同样的 key。

### 连接过程的阶段提示（2026-10 新增）

连接时那个"正在连接..."的加载弹窗，文案会**随阶段实时变化**：

| 阶段（Rust 侧 `stage`） | 界面显示 | detail 补充 |
|---|---|---|
| `rendezvous` | 正在接入注册服务器... | 注册服务器地址 |
| `punching` | 正在尝试 P2P 直连...(UDP+TCP) | 本次打洞用到的传输 |
| `relay` | 正在通过中继服务器连接... | 中继服务器地址 |
| `handshake` | 正在建立加密通道... | 链路类型 |
| `connected` | 连接已建立 | 链路类型 |

链路打通之后，画面左上角的 `ConnectionTypeBadge` 会常驻显示
**P2P 直连** / **中继转发**（绿点 / 琥珀点，未加密再加一个红色断锁图标）。

改动涉及三处，别只改一处：

1. Rust 埋点：`client/src/client.rs` 的 `set_connection_stage(...)`
   （`Interface` trait 里有默认空实现，`Session<T>` 里显式转发给 handler）；
2. 事件分发：`flutter/lib/models/model.dart` 处理 `connection_stage`；
3. 文案：`flutter/lib/common.dart` 的 `connectionStageText()` +
   `src/lang/cn.rs` / `tw.rs` 的对应词条。

> ⚠️ 加载弹窗那段用了 `Obx` 才能跟着事件刷新，而 **Obx 在 build 期间一个
> observable 都没读到会直接抛异常**（release 下是整窗灰白）。所以取状态
> 必须走 `connectionProgressOf(id)` —— 它保证一定返回一个 Rx（没有就惰性建），
> 千万不要图省事写成"没注册就直接 return 文案"。

---

## 五、还没做的部分（下一步）

按优先级排，**不要一口气全改**，桌面端主框架之外的这几块仍是旧样式：

1. **设备卡片本体**（`common/widgets/peer_card.dart` 的网格视图）
   —— 现在还是「彩色底 + 平台大图标」，与 ToDesk 的白底卡片差距最大。
   但它被移动端共用，改造时必须按 `isDesktop` 分流。
2. **远程会话窗口工具栏**（`desktop/widgets/remote_toolbar.dart`，3800 行）
   —— 体积大、状态多，建议单独一次改动。
3. **设置页其余分页** —— 外壳（横向 Tab + 安全页）已 ToDesk 化，
   基本/网络/显示/账户等分页内部仍是上游 `settings_ui` 风格，
   可逐页换成 `WeCard`。
4. **移动端** —— 本次完全没动，按计划单独做。

---

## 六、验证

本机装了 Flutter SDK（`C:\flutter`，3.24.5 / Dart 3.5.4，与 CI 的
`FLUTTER_VERSION` 一致），**改完可以在本地 `dart analyze`**：

1. `cd client/flutter && flutter pub get`（会改 `pubspec.lock`，用完
   `git checkout -- pubspec.lock` 还原）；
2. `lib/generated_bridge.dart` 本机生成不了（codegen 要解析整个 Rust
   workspace，webm 子模块指向 chromium.googlesource.com 被拦），放一个
   `noSuchMethod` 桩顶替，分析时过滤掉 `RustdeskImpl` 的
   `undefined_method` 噪音，用完删除；
3. `dart analyze <改动的文件>`，git 必须在 PATH 里。

仍要由 CI 的 `build-windows.yml` 做最终真编译确认。
