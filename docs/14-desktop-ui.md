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

改造前：左栏是「标题 + 一条蓝竖线 + 只读输入框」的朴素排版，
ID 只能**双击**复制，Tab 是纯图标，状态栏一行小字。

改造后（文件 → 职责）：

| 文件 | 改动 |
|---|---|
| `desktop/widgets/local_device_card.dart` | **新增**。本机设备卡：蓝色渐变卡头放 12 位设备码（4 位一组）+ 复制按钮 + 服务状态点与「启动服务」；下方临时密码行带**显示/隐藏、复制、刷新、修改**四个可见按钮 |
| `desktop/widgets/connect_panel.dart` | **新增**。连接区：46px 高搜索框（搜索图标 + 自动补全 + 一键清除）+ 蓝色主按钮「远程控制」+「更多」下拉（文件传输/摄像头/终端/TCP 隧道）+ 快捷入口 |
| `desktop/pages/desktop_home_page.dart` | 左栏宽 200 → **260**（12 位 ID 需要空间）；删除旧的 `buildIDBoard` / `buildPasswordBoard` / `buildPopupMenu`；提示文案降为次要说明；帮助卡换成圆角卡片，警告用琥珀色、其余用品牌蓝（原来是粉红渐变） |
| `desktop/pages/connection_page.dart` | 接入 `ConnectPanel`；底部 `OnlineStatusWidget` 重做为状态条：状态点 + 文案 + 「启动服务」+ 自建服务器提示 + 活动会话数 |
| `common/widgets/peer_tab_page.dart` | 桌面端 Tab 从纯图标改为**带文字的胶囊**（选中态品牌渐变），保留横向拖拽排序与右键显隐菜单；移动端维持原图标条 |
| `common/widgets/peers_view.dart` | 桌面端空状态改为中性设备图标 + 说明 + 引导语；移动端不变 |
| `common/widgets/peer_card.dart` | 平铺视图圆角 5 → 10，悬浮边框改用 `WeColors.brand` |

**用户友好度的实际增量**（都是原来要靠猜的操作）：

- 设备码、临时密码都有**显式复制按钮**（原来只有双击）；
- 临时密码可**显示/隐藏**（原来永远明文且无法核验）；
- 服务没起时卡头直接给「启动服务」按钮（原来要点进设置）；
- 输入框有**清除按钮**和搜索图标，聚焦时高亮品牌色；
- Tab 有文字，不再靠图标猜「最近 / 收藏 / 发现」；
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

---

## 四、新增文案

界面新增了 6 条文案，已同时进 `client/src/lang/cn.rs` 与 `tw.rs`：

| key | 中文 | 用途 |
|---|---|---|
| `Remote control` | 远程控制 | 连接区主按钮 |
| `Copy` | 复制 | 设备码/密码复制按钮 |
| `Show` / `Hide` | 显示 / 隐藏 | 密码显隐 |
| `Active sessions` | 活动会话 | 状态栏会话计数 |
| `remote_control_hint` | 输入对方的设备码即可发起远程协助… | 连接区副标题、空状态引导 |

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

本机没有 Flutter/Dart SDK，**改完无法 `flutter analyze`**。目前可用的校验手段：

1. 括号配平：去掉注释与字符串后统计 `{} () []` 是否成对（本次改动已全量跑过）；
2. 符号核对：确认引用的 `WeColors.*` / `WeSpace.*` / `WeRadii.*` 在
   `wedesign.dart` 里确有定义；
3. 真编译交由 CI 的 `build-windows.yml` 完成 —— **推分支前确保这条流水线能过**。
