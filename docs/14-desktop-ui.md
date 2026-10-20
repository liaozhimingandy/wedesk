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
「允许控制本设备 → 设备代码卡 → 远程控制设备」。

改造后（文件 → 职责）：

| 文件 | 职责 |
|---|---|
| `desktop/widgets/side_menu.dart` | **新增**。左侧导航栏（宽 208）：Logo、「远程控制 / 设备列表」两个导航项（选中态品牌软底色 + 品牌字色）、底部「高级设置」入口（复用 `DesktopTabPage.onAddSetting`） |
| `desktop/widgets/local_device_card.dart` | 本机设备区：**「允许控制本设备」开关**（映射 `stop-service` 选项）+ 服务状态 pill；下方浅灰卡片左列「设备代码」（12 位 4 位一组大号 + 复制）、右列「连接模式」（临时密码 + 显示/隐藏/复制/刷新/修改），中间竖分隔线 |
| `desktop/widgets/connect_panel.dart` | 连接区：标题行「远程控制设备」+ 就绪状态 pill、46px 搜索框（搜索图标 + 自动补全 + 一键清除）+ 蓝色主按钮「连接」、**连接模式单选**（远程控制 / 文件传输，Enter 与按钮跟随所选模式）、四张功能卡片（文件传输 / 摄像头 / 终端 / TCP 隧道） |
| `desktop/pages/connection_page.dart` | 承接 `navIndex`（0=远程控制页，1=设备列表页），内部 `IndexedStack` 切换、两页保活；`header` 参数用于在连接区上方注入设备卡/帮助卡；底部状态栏保留 |
| `desktop/pages/desktop_home_page.dart` | 普通模式：`Row[WeSideMenu, Expanded(ConnectionPage)]`；`incoming-only` 小窗保留旧紧凑布局（设备卡在左栏），`header` 置空避免重复渲染；帮助卡（安装/macOS 权限警告）移入主内容滚动区 |

**用户友好度的实际增量**（都是原来要靠猜的操作）：

- 「允许控制本设备」**开关直接放在主界面**（原来只能去设置里停/启服务）；
- 设备码、临时密码都有**显式复制按钮**（原来只有双击）；
- 临时密码可**显示/隐藏**（原来永远明文且无法核验）；
- 输入框有**清除按钮**和搜索图标，聚焦时高亮品牌色；
- 连接前可用**单选**切换「远程控制 / 文件传输」，二/三类入口升级为卡片；
- 「设备列表」成为独立页面，最近会话/收藏/地址簿不再挤在连接区下面；
- 状态栏能看到**当前有几个会话**。

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

- 尺寸常量 `kMainWindowFixedSize = Size(1100, 680)`（`consts.dart`），
  在 `main.dart` 的 `runMainApp` 里 `setSize + center`，然后
  `setResizable(false)` + `setMaximizable(false)`；
- 只保留**位置**记忆，不再恢复保存的尺寸/最大化状态
  （`restoreWindowPosition` 在固定模式下被跳过）；
- 标题栏是隐藏的，窗口控制按钮在 `connection_page.dart` 的
  `_buildWindowBar`（顶部拖动条 + 最小化/关闭，**故意没有最大化按钮**）；
- incoming-only / outgoing-only 特殊构建不走固定逻辑，行为不变；
- 远程会话等子窗口不受影响，照常可调整大小。

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
`nethelper`），MSI 打包（`res/msi/preprocess.py --app-name`）必须传同名参数，
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
| `Connection mode` | 连接模式 | 密码列标签 |
| `Remote control device` | 远程控制设备 | 连接区标题 |
| `Ready to connect` | 已准备好连接 | 连接区状态 pill |
| `Device list` | 设备列表 | 侧边导航项 |
| `Remote control` | 远程控制 | 侧边导航项 / 模式单选 |
| `Copy` | 复制 | 设备码/密码复制按钮 |
| `Show` / `Hide` | 显示 / 隐藏 | 密码显隐 |
| `Active sessions` | 活动会话 | 状态栏会话计数 |

其余语言（en 等）没有对应条目时会**回退显示英文 key**，这是上游 `translate()`
的既有行为；要补齐就往对应 `src/lang/*.rs` 里加同样的 key。

---

## 五、还没做的部分（下一步）

按优先级排，**不要一口气全改**，桌面端主框架之外的这几块仍是旧样式：

1. **设备卡片本体**（`common/widgets/peer_card.dart` 的网格视图）
   —— 现在还是「彩色底 + 平台大图标」，与 ToDesk 的白底卡片差距最大。
   但它被移动端共用，改造时必须按 `isDesktop` 分流。
2. **远程会话窗口工具栏**（`desktop/widgets/remote_toolbar.dart`，3800 行）
   —— 体积大、状态多，建议单独一次改动。
3. **设置页**（`desktop/pages/desktop_setting_page.dart`）
   —— 仍是上游 `settings_ui` 风格。
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
