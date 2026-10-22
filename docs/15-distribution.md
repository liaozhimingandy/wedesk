# 15 · 构建分发指南

> 文档索引：[`docs/README.md`](README.md) ｜ 相关：[07 故障排查](07-faq.md)（输入类问题看第七章）、[03 构建](03-build.md)

> 本文回答：**各平台的安装包从哪来、怎么触发、下载后怎么用、正式分发要补什么签名**。
> 结论先行：**所有平台的构建都走 GitHub Actions**（`build-windows.yml` 实际是全平台大流水线），
> 本地只需要在三种例外情况下才出手（见第六节）。

---

## 一、产物总览（CI 一次构建产出什么）

| 平台 | 产物 | 签名状态 | 能直接用来 |
|---|---|---|---|
| Windows x64 | `wedesk-<V>-windows-x64-portable.zip` | 无代码签名 | 测试 ✅ |
| Windows x64 | `wedesk-<V>-windows-x64.msi`（push/标签默认构建，手动触发可取消） | 无代码签名 | 测试 / 域控批量部署 ✅ |
| Android | `wedesk-<V>-android-<abi>.apk` ×3（arm64-v8a / armeabi-v7a / x86_64） | **debug 签名** | 直接安装 ✅（不能上架） |
| Android | `wedesk-<V>-android.aab` | debug 签名 | 仅上传 Play 用 |
| iOS | 未签名 IPA | 未签名 | 需开发者证书重签 |
| macOS | `.app` | 未签名 / ad-hoc | 本机测试 ✅（分发需公证） |
| Linux | x64 bundle（含 deb/AppImage，视开关） | 无 | 测试 ✅ |
| 服务端 | `dist/server/*.zip` + docker 镜像 | — | 部署 |

> **版本号** `V` 取自 `client/Cargo.toml` 的 `version`，由流水线自动读出，
> 不要在 workflow 里硬编码。

---

## 二、触发构建的两种方式

### 方式 A：推送即构建（日常）

`push` 到 `main` 自动触发全平台构建（PR 不触发，省机时）：

```bash
git push origin main
```

### 方式 B：手动触发（可裁剪平台，省时间）

GitHub 仓库页 → **Actions → Build Wedesk (Client + Server) → Run workflow**，
可调输入：

| 输入 | 说明 |
|---|---|
| `hwcodec` | 硬件编解码（NVENC/QSV/AMF）。关掉省 40+ 分钟，但被控端只能软件编码、CPU 显著变高。**测试画质/延迟就开着** |
| `build_args` | 追加给 `client/build.py` 的参数，默认 `--vram`（显存编码） |
| `build_msi` | 构建 MSI 安装包。**push 与标签默认构建**（慢，有失败保护：MSI 挂了不阻断便携版产物，但摘要会写"失败"并发 error 注解）；仅手动触发时取消勾选可跳过 |
| `build_android` / `build_ios` / `build_macos` / `build_linux` | 按需裁剪。**只测 Windows+安卓就关掉 iOS/macOS/Linux**，macOS runner 分钟数是 Linux 的 10 倍 |
| `company_name` / `website` | 临时覆盖 `build/client/custom.toml` 里的品牌值，留空则沿用 |

### 方式 C：打 Tag 发正式 Release

```bash
cd build/common
tag-release.bat --dry    # 预演
tag-release.bat          # 打 v<版本> 标签并推送 → CI 自动发布 Release
```

产物以 Release 附件形式挂到版本页，适合发给外部测试者。

---

## 三、下载产物

| 来源 | 保留期 | 适合 |
|---|---|---|
| Actions → 对应 run → Artifacts | **30 天** | 日常自测 |
| Releases → 版本页附件 | 永久 | 分发 / 归档 |

每个 run 末尾会打印 `SHA256SUMS.txt` 与文件清单，下载后建议核对：

```bash
sha256sum -c SHA256SUMS.txt
```

---

## 四、测试期各平台注意事项

### Windows

- 便携版 zip 解压到**英文路径**直接运行 `wedesk.exe`，无需安装。
- **安装版（MSI）**：push 与标签构建默认产出，artifact 名 `wedesk-windows-x64-msi`，
  装后有开始菜单/桌面快捷方式与卸载入口，适合发给不想解压的人；
  若摘要里 MSI 一行写的是"失败"，展开 Build MSI installer 步骤看日志。
- 关于"wedesk.exe 图标空白"：在资源管理器里**先解压再看** —— 直接在 zip 预览
  界面里所有 exe 都显示通用图标，这不是图标丢失。CI 已内置资源级校验
  （exe 必须内嵌非空白图标，缺了直接让构建失败），不会再静默流出这种包。
  解压后仍显示旧图/空白，多半是 **Windows 图标缓存**：换个文件夹名再放一份，
  或执行 `ie4uinit.exe -show`（Win10/11）后重启资源管理器。CI 图标校验步骤
  会打印内嵌图标的 SHA256，可与之核对确认真的是品牌图标。
  快捷方式/托盘图标由 `build/client/gen_ico.py` 从 `app_icon.ico` 的 256px
  PNG 条目统一生成（`--check` 可校验现有文件），换图标时跑一次即可全量同步。
- 首次运行若报"缺少 VCRUNTIME/DLL"——换一台装过 VC++ 运行库的机器，
  或改用安装版；这是便携包的老问题，不是构建坏了。
- 想验证"锁屏可控/无法控"的行为，见 [07-faq 第七章](07-faq.md)：
  免安装默认锁屏不可输入，`wedesk.exe --install-service` 补装服务后可输入。

### Android

- 按设备架构选包：绝大多数手机/平板是 **arm64-v8a**；
  模拟器 x86_64 选 x86_64 包。装错 ABI 会直接解析失败。
- 允许"安装未知来源应用"。debug 签名可直接覆盖安装。
- 换包名定制后的首个版本（`com.alsoapp.wedesk`）与旧包**不兼容升级**，
  需先卸载旧版（ID 与配置不互通，属预期）。

### iOS / macOS

- CI 产出的是**未签名**包，只能用于：
  - macOS：本机 `xattr -d com.apple.quarantine` 后直接跑 `.app` 自测；
  - iOS：需用自己的开发者证书 + Provisioning Profile 重签名（见第五节）。
- 正式分发前 macOS 还需**公证（notarization）**，否则用户被 Gatekeeper 拦。

### Linux

- bundle 解压即用；deb 装后注意桌面入口与 scheme handler（`wedesk://`）。

---

## 五、正式分发的签名准备（测试期可跳过）

| 平台 | 需要什么 | 怎么配 |
|---|---|---|
| Android | 发布 keystore（.jks/.keystore） | 本地 `keytool -genkey -v -keystore wedesk.jks -keyalg RSA -keysize 2048 -validity 10000 -alias wedesk`；在 `client/flutter/android/key.properties` 配 `storeFile/storePassword/keyAlias/keyPassword`，并把 CI 里临时改 debug 签名的 `sed` 行去掉（或改 secrets 方案） |
| Windows | 代码签名证书（.pfx，EV 或 OV） | CI 加 signtool 步骤；无证书时便携版可发，但用户首次运行会弹 SmartScreen 警告 |
| iOS | Apple 开发者账号 + 分发证书 + Profile | Xcode 自动签名或 exportOptions.plist 指定 team；CI 需配 `APPLE_*` secrets |
| macOS | Developer ID Application 证书 + notarytool | 签名 + `xcrun notarytool submit` 公证两条 CI 步骤 |

> Android 重签已装过的 debug 包会因签名不一致安装失败 —— **先卸载再装**。

---

## 六、什么时候需要本机构建

| 场景 | 本机要备什么 |
|---|---|
| 改一行 Dart 想看效果 | Flutter 3.24.5 + Android SDK（`flutter build apk` 即可，不涉及 Rust） |
| 改 Rust 服务端/客户端代码 | Rust 1.75 + **Visual Studio 2022（C++ 桌面负载）** + Windows SDK；先跑 `python build/client/check-env.py` 自检 |
| 出 Windows 完整包 | 同上 + `python build/client/build-local.bat`（便携版打包脚本） |
| Android 完整包（含 Rust so） | 还需 Android NDK + cmake + vcpkg 依赖链（ffmpeg/aom/libvpx）—— **成本高，直接用 CI 产物** |

本机构建流程与常见坑见 [03 构建](03-build.md)；输入类"点了没反应"的排查见
[07-faq 第七章](07-faq.md)。

---

## 七、构建号（version code）与更新判断

- **构建号来源**：CI 每次运行把 `github.run_number`（同一工作流单调 +1）通过
  `--dart-define=VERSION_CODE=<n>` 注入 Flutter；Dart 侧常量
  `consts.dart::kVersionCode` 读取。本地开发未注入时为 0，按"未知构建号"处理。
- **界面展示**：关于页"当前版本"行显示 `V1.2.3 (123)`；构建号为 0 时只显示语义版本。
- **更新比较**（`update_check.dart`）：update.json 条目里**双方都有**有效构建号时
  （本地 >0 且条目 `"version_code": <int>` >0）按构建号比大小；任一方缺失则回退
  语义版本字符串比较 —— 所以旧的 update.json 不用改也不会坏。
- **运维发布清单**：更新 update.json 时给对应平台条目加上 `"version_code": <n>`，
  取值 = 该平台最近一次成功构建的 run_number（构建摘要里会打印）。**构建号必须
  递增**，否则已装高构建号的客户端收不到更新提示。

---

## 八、构建时间（BUILD_DATE）规范

产物里写死的"构建时间"来自编译期生成的 `BUILD_DATE`（关于页、`wedesk.exe --build-date`、
MSI 控制面板"安装日期"都读它），由两处生成：

- Rust 侧：`client/libs/hbb_common/src/lib.rs` 的 `gen_version()` → 写 `BUILD_DATE`；
- MSI 侧：`client/res/msi/preprocess.py` 的 `g_build_date` → 写 MSI `BuildDate`。

**约定（⚠️ 必须遵守）：构建时间一律由源码层按北京时间（UTC+8）生成，不要改回依赖 runner 时区。**

```rust
// client/libs/hbb_common/src/lib.rs —— gen_version()
let build_date = format!("{}", (chrono::Utc::now() + chrono::Duration::hours(8))
    .format("%Y-%m-%d %H:%M"));
```

```python
# client/res/msi/preprocess.py
g_build_date = (datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")
```

### 为什么不能靠"改 runner 时区"
- Linux / macOS runner：靠顶层 env `TZ=Asia/Shanghai` 即可让 `Local::now()` / `datetime.now()` 变北京时间。
- **Windows runner：忽略 `TZ` 环境变量**，必须 `tzutil /s "China Standard Time"` 改系统时区才生效。
- **致命点**：`BUILD_DATE` 不是生成一次就完——`client/build.rs` 在**每个平台 job 各自重新生成**
  （Windows 在 Windows runner，Android/iOS/Linux/macOS 在各自 runner）。靠 runner 时区就要求
  **每一个构建 job 都配对时区配置**，漏一个平台就会静默退回 UTC 且不会报错；以后新增 job 或换
  runner 镜像也会悄悄失效。
- 源码层 `Utc+8` 对所有平台一视同仁、自解释、不随配置漂移失效。中国不实行夏令时，`+8h` 恒正确。

### 配套项
- `build-windows.yml` 顶层 `env` 保留 `TZ: "Asia/Shanghai"`（仅让 Linux 日志 / `date` 显示北京时间，
  不影响构建时间本身；Windows 上它不生效，但留着无害且表意清晰）。
- 运行期时间（录屏文件名、连接时间戳等）保持机器本地时区，不在此列。

> 备注：曾短暂试过"改 Windows runner 时区（`tzutil` 步骤）+ 回退源码"的方案，因上述跨 job
> 漂移风险已放弃，回到源码层 `Utc+8`。后续若再看到构建时间变 UTC，先回看本条。
