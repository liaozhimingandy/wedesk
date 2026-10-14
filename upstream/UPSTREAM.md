# 上游基线

记录 wedesk 是从哪个上游版本长出来的，以及我们改了什么。
**上游升级时先看这里。**

## 基线信息

| 项目 | 值 |
| --- | --- |
| 上游项目 | [rustdesk/rustdesk](https://github.com/rustdesk/rustdesk) |
| 上游版本 | v1.5.1 |
| 主仓 commit | `29a160f` |
| hbb_common commit | `229b904` |
| 许可证 | AGPL-3.0 |
| 整合日期 | 2026-10-14 |

## 整合方式：vendored + 扁平化

上游源码**扁平化到仓库根**——`Cargo.toml`、`build.py`、`libs/`、`src/`、`flutter/`
都在最外层，**不是 submodule，不是 subtree，也没有 `rustdesk/` 子目录**。

源码根即仓库根的好处：`build.py`、官方 CI 脚本、各类教程里写的相对路径
都能直接对上，不用再做一层目录换算。

这样做的取舍：

| | vendored（当前选择） | git submodule | git subtree |
| --- | --- | --- | --- |
| clone 后能否直接构建 | ✅ 一次 `git clone` 就够 | ❌ 忘了 `--recurse-submodules` 就缺 hbb_common | ✅ |
| 依赖 GitHub | ❌ 完全不依赖 | ✅ 拉取要走 GitHub | ❌ |
| 保留上游历史 | ❌ 只有我们的提交 | ✅ | ✅ |
| 升级上游 | 手动覆盖 + 重放补丁 | `git submodule update` | `git subtree pull` |

**选 vendored 的理由**：wedesk 是最终交付仓库，第一诉求是"clone 下来就能构建"，
不希望 CI 或同事的机器因为网络、子模块配置翻车。上游历史丢失用 `custom.patch` + 本文件补偿。

### 关键改动：hbb_common 已实体化

上游 `libs/hbb_common` 是 submodule。整合时已：
- 删除 `libs/hbb_common/.git`（原本是指向 `.git/modules/...` 的指针文件）
- 删除 `.gitmodules`

**结果**：hbb_common 现在是普通目录，跟随主仓一起提交。
后续 `git clone` **不需要** `--recurse-submodules`。

> ⚠️ 注意：上游的服务器地址、公钥、APP_NAME 都定义在 hbb_common 里。
> 如果哪天有人误把它恢复成 submodule，所有定制会静默失效。

## 我们改了什么

定制由 `build/customize.py` 按 `build/custom.toml` 注入，覆盖 8 个文件：

| 文件 | 改了什么 |
| --- | --- |
| `libs/hbb_common/src/config.rs` | **核心三值**：`RENDEZVOUS_SERVERS`、`RS_PUB_KEY`、`APP_NAME` |
| `src/common.rs` | 清空官方 API 兜底地址，避免流量外泄到公网 |
| `Cargo.toml` | 作者、描述、公司名、`LegalCopyright` |
| `flutter/windows/runner/Runner.rc` | exe 文件属性：公司名、产品名、exe 名 |
| `flutter/android/app/src/main/res/values/strings.xml` | 安卓端 app 名 |
| `flutter/lib/common.dart` | "Powered by" 链接指向自家官网 |
| `flutter/lib/desktop/pages/install_page.dart` | 安装页隐私政策链接 |
| `flutter/lib/desktop/pages/desktop_setting_page.dart` | "关于"页：官网、隐私政策、版权行 |

改"兜底默认值"而不是硬锁定，用户仍可在设置里改服务器；
但出厂默认值一定指向我们自己的服务器。

> 单元测试 `#[cfg(test)]` 里还有 2 处官方地址，**故意没改** —— 改了会破坏测试意图。

## 补丁与验证

- `custom.patch` —— 以上 8 个文件的完整 diff，可从干净上游重放
- `verify_patch.py` —— 验证补丁仍能完整重建当前定制状态

```bash
python upstream/verify_patch.py --upstream D:/Git/rustdesk
```

退出码 0 = 通过。**上游升级后务必跑一次。**

## 上游升级流程

假设要从 v1.5.1 升到 v1.5.2：

```bash
# 1. 当前状态已提交的前提下建一个升级分支
git checkout -b upgrade-1.5.2

# 2. 拿到干净的新版本源码
git clone --recurse-submodules -b 1.5.2 https://github.com/rustdesk/rustdesk /tmp/rd-1.5.2

# 3. 覆盖上游部分：把新版本的这些条目搬到仓库根，覆盖同名项
#    libs/ src/ flutter/ res/ examples/ fastlane/ flatpak/ appimage/
#    .github/ .cargo/ Cargo.toml Cargo.lock build.py build.rs
#    Dockerfile entrypoint.sh vcpkg.json LICENCE
#    docs/ 下的上游文档 → docs/upstream/
#
#    我们自己的东西（build/ server/ upstream/ README.md .gitignore
#    .gitattributes docs/ARCHITECTURE.md 等）**一律不要动**。
#
#    注意：hbb_common 要连同子模块内容一起复制，并删掉它的 .git 指针文件

# 4. 重新注入定制（推荐，比打补丁更能容忍上游改动）
python build/customize.py --dry-run
python build/customize.py

# 4'. 或者重放补丁（上游改动大时容易冲突，按需选择）
#     git apply upstream/custom.patch

# 5. 验证
python upstream/verify_patch.py

# 6. 更新本文件的基线信息，提交
```

### 升级时最容易踩的坑

1. **忘带 `--recurse-submodules`** —— 新版本 hbb_common 缺失，定制静默失效
2. **hbb_common 的 `.git` 没删** —— 它会被当成 submodule，定制丢失
3. **把上游 `/build` 规则带回 `.gitignore`** —— 会忽略掉我们的 `build/` 脚本目录。
   本仓库的 `.gitignore` 是自有文件，上游更新不该覆盖它
4. **把上游 `.gitattributes` 带回来** —— 会覆盖我们的换行策略，
   导致 `*.bat` 变 LF 而在 CMD 下解析异常
5. **上游的 AI 指令文件落在仓库根** —— `AGENTS.md` / `CLAUDE.md` / `GEMINI.md`
   应放进 `docs/upstream/`，否则会干扰后续会话
6. **Flutter engine 没换** —— 必须用 RustDesk 自研 engine，
   官方 engine 会有窗口行为异常
