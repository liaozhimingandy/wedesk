#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
customize.py —— RustDesk 源码品牌与服务器定制注入器

把 custom.toml 里的品牌信息、自建服务器地址与公钥，注入到 RustDesk 源码中，
产出一份"开箱即连你自己服务器"的定制源码树。

为什么需要它
------------
RustDesk 默认连官方服务器 rs-ny.rustdesk.com。团队私有化部署必须让客户端默认
连接自建服务器，否则：
  1. 每个员工都要手动填服务器+公钥，体验极差且容易填错；
  2. 忘记配置的用户流量会走到官方公共服务器，违反数据不出内网的合规要求。

本脚本修改的是"兜底默认值"，位于服务器地址解析优先级链的最末端：
    命令行参数 > 用户界面设置的 custom-rendezvous-server > 编译注入 > 本地配置 > 兜底常量
改兜底常量的好处：用户仍可在界面里临时改服务器，不会影响调试，但默认值永远是你的服务器。

核心定制点（均已对 2026-10 master 分支实测验证）
------------------------------------------------
  libs/hbb_common/src/config.rs
      RENDEZVOUS_SERVERS  服务器兜底地址
      RS_PUB_KEY          服务器公钥
      APP_NAME            应用名 ← Flutter UI 通过 mainGetAppNameSync() 动态读取，改一处全局生效
      ORG                 macOS 配置目录组织段（com.alsoapp）
  src/common.rs
      get_api_server_ 的官方兜底域名 admin.rustdesk.com（置空以杜绝外发请求）
  Cargo.toml / Runner.rc / strings.xml / desktop_setting_page.dart / common.dart / install_page.dart
      公司名、版权、产品名、官网与隐私政策链接
  brand.package_name（全平台包名/Bundle ID）
      Android build.gradle + 3 份 AndroidManifest + kotlin 包声明/import；
      iOS/macOS pbxproj + Info.plist + xcconfig + exportOptions；
      Linux CMakeLists；Cargo bundle identifier；macOS launchd 标签前缀与
      privileges_scripts 模板；manual_update 常量与测试。
      kotlin 源码目录名随包名迁移（com/carriez/flutter_hbb → com/alsoapp/wedesk），
      目录迁移后 glob 规则仍能命中。

用法
----
    python customize.py --source <rustdesk源码目录> --config custom.toml --dry-run
    python customize.py --source <rustdesk源码目录> --config custom.toml
    python customize.py --source <rustdesk源码目录> --restore      # 还原备份

零第三方依赖，Python 3.7+ 可运行。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ImportError:
    tomllib = None

BAK_SUFFIX = ".rd.bak"

# 记录"上次注入进源码的值"，让脚本可以反复调整配置而不是一次性工具。
# 随仓库一起提交，团队成员拿到的 state 与源码状态一致。
STATE_FILE = Path(__file__).resolve().parent / ".custom-state.json"


# ===========================================================================
# 极简 TOML 读取器
# ---------------------------------------------------------------------------
# 仅支持本项目所需子集：[section] / key = "value" / key = 'value' / # 注释 /
# 布尔与整数字面量。有 tomllib 时优先用它，无则回退到这里，做到零依赖且跨版本兼容。
# ===========================================================================
def _parse_value(raw: str):
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"'):
        return _unescape(raw[1:-1])
    if raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1]
    if raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    try:
        return int(raw)
    except ValueError:
        return raw


def _unescape(s: str) -> str:
    return (
        s.replace('\\"', '"')
        .replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace("\\\\", "\\")
    )


def load_config(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if tomllib is not None:
        return tomllib.loads(text)

    cfg: dict = {}
    section = cfg
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            section = cfg.setdefault(name, {})
            continue
        if "=" not in line:
            raise ValueError(f"{path}:{lineno} 无法解析的行: {line!r}")
        key, val = line.split("=", 1)
        section[key.strip()] = _parse_value(val)
    return cfg


# ===========================================================================
# 终端着色（Windows 传统控制台自动禁用）
# ===========================================================================
def _init_color():
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleMode(
                ctypes.windll.kernel32.GetStdHandle(-11), 7
            )
        except Exception:
            pass


GREEN, RED, YELLOW, BLUE, DIM, RESET = "", "", "", "", "", ""
if os.environ.get("NO_COLOR") is None:
    GREEN, RED, YELLOW, BLUE, DIM, RESET = (
        "\033[32m",
        "\033[31m",
        "\033[33m",
        "\033[34m",
        "\033[2m",
        "\033[0m",
    )


# ===========================================================================
# 替换规则
# ===========================================================================
class Rule:
    """一条替换规则"""

    def __init__(self, note, relpath, old, new, required=False, count=None, residual=None, relglob=False):
        self.note = note
        self.relpath = relpath
        self.old = old
        self.new = new
        self.required = required  # True 时不命中按失败处理
        self.count = count  # 期望替换次数
        # residual：这条规则"要消灭的东西"。
        # 正则规则第二次跑必然匹配不到（旧值已被换掉），但它不像精确规则那样能用
        # `new in text` 判断"早已生效"，于是每次都假报未命中。
        # 填了本字段后，n==0 时改查它还在不在：不在 → 按已命中处理；
        # 还在却没匹配上 → 才是真正的没命中（比如源码格式变了）。
        self.residual = residual
        # relglob：relpath 按 glob 模式展开，对每个命中文件套用同一条规则。
        # 包名定制要覆盖整个 kotlin 源码目录，而目录名本身随包名变化
        # （com/carriez/flutter_hbb → com/alsoapp/wedesk），写死路径的话，
        # 在迁移过的树里会全部落空报"文件不存在"。
        self.relglob = relglob

    @staticmethod
    def regex(note, relpath, pattern, repl, required=False, residual=None, relglob=False):
        r = Rule(note, relpath, pattern, repl, required, residual=residual, relglob=relglob)
        r.is_regex = True
        return r

    is_regex = False


def build_rules(c: dict) -> "list[Rule]":
    srv = c.get("server", {})
    brand = c.get("brand", {})
    adv = c.get("advanced", {})

    host = srv.get("rendezvous_host", "").strip()
    key = srv.get("key", "").strip()
    api_server = srv.get("api_server", "").strip()
    app_name = brand.get("app_name", "RemoteDesk").strip()
    product = brand.get("product_name", "RemoteDesk").strip()
    company = brand.get("company_name", "").strip()
    website = brand.get("website", "").strip()
    exe = brand.get("exe_name", app_name.lower()).strip() or "remotedesk"
    pkg = brand.get("package_name", "").strip()

    rules: list[Rule] = []

    # ---------------------------------------------------------------- core
    # 服务器兜底地址：注意保留切片语法 &[...]，这里是常量定义而非运行时变量
    if host:
        rules.append(
            Rule(
                "服务器地址（兜底默认值）",
                "libs/hbb_common/src/config.rs",
                'pub const RENDEZVOUS_SERVERS: &[&str] = &["rs-ny.rustdesk.com"];',
                'pub const RENDEZVOUS_SERVERS: &[&str] = &["%s"];' % host,
                required=True,
            )
        )
    if key:
        rules.append(
            Rule(
                "服务器公钥（校验核心，必须与你的 hbbs 一致）",
                "libs/hbb_common/src/config.rs",
                'pub const RS_PUB_KEY: &str = "OeVuKk5nlHiXp+APNn0Y3pC1Iwpwn44JGqrQCsWqmBw=";',
                'pub const RS_PUB_KEY: &str = "%s";' % key,
                required=True,
            )
        )
    if api_server:
        # 管理后台（admin FastAPI）地址：注入为编译期常量，get_api_server_
        # 优先采用它，不再按 rendezvous 端口-2 推导。
        #
        # 幂等设计（源码里可能已存在该常量，重复插入会 E0428）：
        #   规则 A（正则）：常量已存在 → 只更新值；
        #   规则 B（精确）：常量不存在 → 插入。锚点用"RS_PUB_KEY 行紧接
        #     RENDEZVOUS_PORT 行"——常量一旦存在，这个组合就不成立，
        #     两条规则天然互斥，不会重复插入。
        rules.append(
            Rule.regex(
                "管理后台地址（client 的 /api/* 上报目标，custom.toml server.api_server）",
                "libs/hbb_common/src/config.rs",
                r'pub const CUSTOM_API_SERVER: &str = "[^"]*";',
                'pub const CUSTOM_API_SERVER: &str = "%s";' % api_server,
            )
        )
        api_doc = (
            "/// 管理后台（admin FastAPI）地址。非空时 get_api_server_ 直接采用它，\n"
            "/// 不再按 rendezvous 端口-2 推导。由 build/client/customize.py 依据\n"
            "/// custom.toml 的 server.api_server 注入；留空则保持端口推导行为。\n"
        )
        rules.append(
            Rule(
                "管理后台地址插入（const 尚不存在时；已存在则由上面的规则只更新值）",
                "libs/hbb_common/src/config.rs",
                'pub const RS_PUB_KEY: &str = "%s";\n\npub const RENDEZVOUS_PORT: i32 = 21116;'
                % key,
                'pub const RS_PUB_KEY: &str = "%s";\n\n%spub const CUSTOM_API_SERVER: &str = "%s";\n\npub const RENDEZVOUS_PORT: i32 = 21116;'
                % (key, api_doc, api_server),
            )
        )
        # common.rs 的生效开关同样要做幂等：注入后旧锚点（api 检查紧接
        # let s0）不再成立，重复跑也只会命中"已是目标值"分支。
        rules.append(
            Rule(
                "管理后台地址生效（get_api_server_ 优先采用 CUSTOM_API_SERVER）",
                "src/common.rs",
                "    if !api.is_empty() {\n        return api.to_owned();\n    }\n    let s0 = get_custom_rendezvous_server(custom);",
                "    if !api.is_empty() {\n        return api.to_owned();\n    }\n"
                "    // 编译期注入的定制管理后台地址优先于端口推导\n"
                "    // （见 hbb_common config::CUSTOM_API_SERVER，由 customize.py 写入）。\n"
                "    if !config::CUSTOM_API_SERVER.is_empty() {\n"
                "        return config::CUSTOM_API_SERVER.to_owned();\n"
                "    }\n"
                "    let s0 = get_custom_rendezvous_server(custom);",
            )
        )
    if app_name:
        rules.append(
            Rule(
                "应用名（Flutter UI 经 mainGetAppNameSync 动态读取，改此处全局生效）",
                "libs/hbb_common/src/config.rs",
                'pub static ref APP_NAME: RwLock<String> = RwLock::new("RustDesk".to_owned());',
                'pub static ref APP_NAME: RwLock<String> = RwLock::new("%s".to_owned());'
                % app_name,
            )
        )
        rules.append(
            Rule.regex(
                "macOS .app 名（必须与 APP_NAME 一致：更新流程与 daemon/agent "
                "plist 都按 /Applications/{APP_NAME}.app 寻址，correct_app_name "
                "会把模板里的 RustDesk 改写成 APP_NAME）",
                "flutter/macos/Runner/Configs/AppInfo.xcconfig",
                r"PRODUCT_NAME = [^\n]*",
                "PRODUCT_NAME = %s" % app_name,
            )
        )
        # ---- 深链 scheme ---------------------------------------------------
        # 客户端生成分享链接用 get_uri_prefix() = "{APP_NAME 小写}://"，
        # 各平台的 scheme 注册必须与它一致，否则 wedesk:// 链接点了没反应。
        # 上游注册的是 rustdesk，属于"链接生成端已改名、接收端没改"的隐性断裂。
        rules += [
            Rule.regex(
                "Android 深链 scheme 注册",
                "flutter/android/app/src/main/AndroidManifest.xml",
                r'<data android:scheme="[^"]*" />',
                '<data android:scheme="%s" />' % app_name.lower(),
            ),
            Rule.regex(
                "iOS 深链 scheme 注册",
                "flutter/ios/Runner/Info.plist",
                r'(<key>CFBundleURLSchemes</key>\s*<array>\s*<string>)[^<]*(</string>)',
                r"\g<1>%s\g<2>" % app_name.lower(),
            ),
            Rule.regex(
                "macOS 深链 scheme 注册",
                "flutter/macos/Runner/Info.plist",
                r'(<key>CFBundleURLSchemes</key>\s*<array>\s*<string>)[^<]*(</string>)',
                r"\g<1>%s\g<2>" % app_name.lower(),
            ),
            Rule.regex(
                "Linux 桌面入口 scheme handler",
                "res/rustdesk-link.desktop",
                r"x-scheme-handler/[^;]*;",
                "x-scheme-handler/%s;" % app_name.lower(),
            ),
            # Dart 侧：web 初始链接规范化时拼的占位 scheme（urlLinkToCmdArgs
            # 只解析 authority/path，scheme 仅是让字符串可解析），以及两处
            # 日志/三处注释里的链接示例。proto 文件里的同名注释不动 ——
            # 那是协议文档，与上游保持一致更重要。
            Rule.regex(
                "Dart 深链占位 scheme（home_page 的 fakelink）",
                "flutter/lib/mobile/pages/home_page.dart",
                r'(fakelink = ")[a-zA-Z]+://',
                r"\g<1>%s://" % app_name.lower(),
            ),
            Rule.regex(
                "Dart 深链拒绝日志（config/password 两处）",
                "flutter/lib/common.dart",
                r"(Ignore )[a-z]+://(config|password)( because)",
                r"\g<1>%s://\g<2>\g<3>" % app_name.lower(),
            ),
            Rule.regex(
                "Dart 深链注释示例（connect-id 三种形态）",
                "flutter/lib/common.dart",
                r"(// )[a-z]+://(<connect-id>)",
                r"\g<1>%s://\g<2>" % app_name.lower(),
            ),
        ]

    # ------------------------------------------------------- 隐私：断开发送
    # get_api_server_ 的最后兜底是官方 admin.rustdesk.com。定制客户端没有官方
    # API 可用，置空可杜绝客户端向官方域名发起任何请求。
    if adv.get("remove_official_api_fallback", True):
        rules.append(
            Rule(
                "移除官方 API 兜底域名（避免向 admin.rustdesk.com 发起请求）",
                "src/common.rs",
                '"https://admin.rustdesk.com".to_owned()',
                '"".to_owned()',
            )
        )

    # ------------------------------------------------------------- 品牌
    if company:
        rules += [
            Rule(
                "Cargo 作者署名",
                "Cargo.toml",
                'authors = ["rustdesk <info@rustdesk.com>"]',
                'authors = ["%s"]' % company,
            ),
            Rule.regex(
                "Cargo 版权声明",
                "Cargo.toml",
                r'LegalCopyright = "Copyright © \d{4} [^"]*"',
                'LegalCopyright = "Copyright © %d %s. All rights reserved."'
                % (2026, company),
            ),
            Rule(
                "Windows 资源 - 公司名称",
                "flutter/windows/runner/Runner.rc",
                '"Purslane Tech Pte. Ltd."',
                '"%s"' % company,
            ),
            Rule.regex(
                "Windows 资源 - 版权行",
                "flutter/windows/runner/Runner.rc",
                r'"Copyright © \d{4} [^"]*"',
                '"Copyright © %d %s. All rights reserved."' % (2026, company),
            ),
            # 关于页的版权归属现在已经收口到 common.dart 的 kCopyrightCompany 常量
            # （见 desktop_setting_page.dart 的 _AboutState）。
            # 以前这条规则改的是 desktop_setting_page.dart 里的上游字面量
            # "Purslane Tech Pte. Ltd." —— 一旦某个版本把它换成别的写法（或早期
            # 注入留下的占位值"贵公司名称"），规则就静默落空，改 custom.toml 的
            # company_name 根本同步不到界面上。
            Rule.regex(
                "关于页 - 版权归属（common.dart kCopyrightCompany）",
                "flutter/lib/common.dart",
                r"(const String kCopyrightCompany = ')[^']*(';)",
                r"\g<1>%s\g<2>" % company,
                residual="kCopyrightCompany",
            ),
            # 老树兜底：还没迁移到常量的源码里仍有内联字面量，按同一样式改写，
            # 顺带清掉过去注入进去的占位值。
            Rule.regex(
                "关于页 - 版权归属（旧版内联字面量兜底）",
                "flutter/lib/desktop/pages/desktop_setting_page.dart",
                r"(\$\{DateTime\.now\(\)\.toString\(\)\.substring\(0, 4\)\} )[^\n']*?(\\n\$license')",
                r"\g<1>$kCopyrightCompany\g<2>",
                residual="Purslane",
            ),
            # macOS 原生 About 面板的版权行（Info.plist 的
            # NSHumanReadableCopyright 读 $(PRODUCT_COPYRIGHT)）。
            Rule.regex(
                "macOS About 面板 - 版权行",
                "flutter/macos/Runner/Configs/AppInfo.xcconfig",
                r"PRODUCT_COPYRIGHT = Copyright © \d{4} [^\n]*",
                "PRODUCT_COPYRIGHT = Copyright © %d %s. All rights reserved."
                % (2026, company),
            ),
            # 便携版打包器的版权行。
            Rule.regex(
                "便携版 winres - 版权行",
                "libs/portable/Cargo.toml",
                r'LegalCopyright = "Copyright © \d{4} [^"]*"',
                'LegalCopyright = "Copyright © %d %s. All rights reserved."'
                % (2026, company),
            ),
            # MSI 在"程序和功能"里显示的制造商。CI 只传 --app-name，
            # 不传 --manufacturer，默认值会原样进安装包。
            Rule.regex(
                "MSI - 制造商（--manufacturer 默认值）",
                "res/msi/preprocess.py",
                r'(default=")[^"]*(",\n\s*help="The app manufacturer)',
                r"\g<1>%s\g<2>" % company,
            ),
            # hbbs/hbbr --help 顶部的作者行（clap 的 .author()）。
            # server/ 在 --source（client/）之外，路径要跳一级；纯上游
            # 客户端树里这两个文件不存在，规则报"文件不存在"仅警告。
            Rule.regex(
                "服务端 hbbs --help 作者行",
                "../server/src/src/common.rs",
                r'\.author\("[^"]*"\)',
                '.author("%s")' % company,
            ),
            Rule.regex(
                "服务端 hbbr --help 作者行",
                "../server/src/src/hbbr.rs",
                r'\.author\("[^"]*"\)',
                '.author("%s")' % company,
            ),
        ]

    if app_name:
        rules += [
            Rule(
                "Windows 资源 - 产品名称",
                "flutter/windows/runner/Runner.rc",
                'VALUE "ProductName", "RustDesk" "\\0"',
                'VALUE "ProductName", "%s" "\\0"' % app_name,
            ),
            Rule(
                "Windows 资源 - 文件描述",
                "flutter/windows/runner/Runner.rc",
                'VALUE "FileDescription", "RustDesk Remote Desktop" "\\0"',
                'VALUE "FileDescription", "%s Remote Desktop" "\\0"' % app_name,
            ),
            Rule(
                "Windows 资源 - 可执行文件名",
                "flutter/windows/runner/Runner.rc",
                'VALUE "OriginalFilename", "rustdesk.exe" "\\0"',
                'VALUE "OriginalFilename", "%s.exe" "\\0"' % exe,
            ),
            Rule(
                "Windows 资源 - 内部名称",
                "flutter/windows/runner/Runner.rc",
                'VALUE "InternalName", "rustdesk" "\\0"',
                'VALUE "InternalName", "%s" "\\0"' % exe,
            ),
            Rule(
                "Android 应用名",
                "flutter/android/app/src/main/res/values/strings.xml",
                '<string name="app_name">RustDesk</string>',
                '<string name="app_name">%s</string>' % app_name,
            ),
            # 便携版打包器（client/libs/portable）自带的 Windows 版本资源，
            # 由它自己的 build.rs + winres 读取。CI 的便携产物就是它产的，
            # 不改则文件属性里仍是 RustDesk。
            Rule.regex(
                "便携版 winres - 产品名",
                "libs/portable/Cargo.toml",
                r'ProductName = "[^"]*"',
                'ProductName = "%s"' % app_name,
            ),
            Rule.regex(
                "便携版 winres - 文件描述",
                "libs/portable/Cargo.toml",
                r'FileDescription = "[^"]*"',
                'FileDescription = "%s Remote Desktop"' % app_name,
            ),
        ]

    if website:
        base = website.rstrip("/")
        rules += [
            Rule(
                "帮助菜单 - 官网链接",
                "flutter/lib/common.dart",
                "launchUrl(Uri.parse('https://rustdesk.com'));",
                "launchUrl(Uri.parse('%s'));" % base,
            ),
            Rule(
                "关于页 - 官网链接",
                "flutter/lib/desktop/pages/desktop_setting_page.dart",
                "launchUrlString('https://rustdesk.com');",
                "launchUrlString('%s');" % base,
            ),
            Rule(
                "顶栏 - 快速开始/帮助链接",
                "flutter/lib/desktop/widgets/window_top_bar.dart",
                "static const _website = 'https://rustdesk.com';",
                "static const _website = '%s';" % base,
            ),
            Rule(
                "关于页 - 隐私政策链接",
                "flutter/lib/desktop/pages/desktop_setting_page.dart",
                "launchUrlString('https://rustdesk.com/privacy.html');",
                "launchUrlString('%s/privacy.html');" % base,
            ),
            Rule(
                "安装页 - 隐私政策链接",
                "flutter/lib/desktop/pages/install_page.dart",
                "'https://rustdesk.com/privacy.html'",
                "'%s/privacy.html'" % base,
            ),
            # ---- 下面这些是 Rust 侧的文档链接常量 ---------------------------------
            # 它们不像上面的 Dart 链接那样"点一下才访问"，而是会随错误消息返回给
            # (server -> client) 的消息框。虽然 HELPER_URL 做过白名单校验，
            # 但默认指向 rustdesk.com 依然会让员工看到官方域名，一并换掉。
            Rule(
                "文档首页链接常量",
                "libs/hbb_common/src/config.rs",
                'pub const LINK_DOCS_HOME: &str = "https://rustdesk.com/docs/en/";',
                'pub const LINK_DOCS_HOME: &str = "%s";' % base,
            ),
            Rule(
                "X11 必要说明链接常量",
                "libs/hbb_common/src/config.rs",
                'pub const LINK_DOCS_X11_REQUIRED: &str = '
                '"https://rustdesk.com/docs/en/manual/linux/#x11-required";',
                'pub const LINK_DOCS_X11_REQUIRED: &str = "%s";' % base,
            ),
            Rule(
                "X11 参考链接（连接错误提示）",
                "src/client.rs",
                'pub const SCRAP_X11_REF_URL: &str = '
                '"https://rustdesk.com/docs/en/manual/linux/#x11-required";',
                'pub const SCRAP_X11_REF_URL: &str = "%s";' % base,
            ),
            Rule(
                "Wayland 登录屏错误提示链接",
                "src/client.rs",
                'link: "https://rustdesk.com/docs/en/manual/linux/#login-screen",',
                'link: "%s",' % base,
            ),
            # MSI 在"程序和功能"里显示的支持链接
            Rule(
                "MSI - 技术支持联系链接",
                "res/msi/preprocess.py",
                '"v": "https://github.com/rustdesk/rustdesk",',
                '"v": "%s",' % base,
            ),
            Rule(
                "MSI - 帮助在线链接",
                "res/msi/preprocess.py",
                '"v": "https://github.com/rustdesk/rustdesk/issues/",',
                '"v": "%s",' % base,
            ),
        ]

    # ------------------------------------------------------- 包名 / Bundle ID
    #
    # 上游把 com.carriez.*（RustDesk 作者的个人域名）写死在各平台工程文件里，
    # fork 之后必须全量换成自己的包名，否则：
    #   Android 应用市场 / iOS App Store 认 applicationId / bundle id 为唯一
    #   标识，包名不对 = 上架不了、更新不互通；
    #   macOS 的 ORG 决定配置目录（~/Library/Preferences/<org>.<app>），
    #   launchd 标签决定开机项与卸载残留，改不动等于权限对话框反复弹。
    #
    # 写法上一律用"匹配任意当前值"的正则而不是写死 com.carriez.*：这样
    # 反复跑、改配置重跑都幂等（命中但无变化 → 记"已是目标值"）。
    # 两个例外只能认旧值，用 residual 兜底：
    #   - kotlin 的 package 行：同目录下 KeyboardKeyEventMapper.kt 是
    #     package hbb（protobuf 生成类所在包），不能跟着改，所以正则锚定旧包名；
    #   - ffi.kt 的 import 行：同上。
    if pkg:
        # ORG = 包名去掉最后一段（com.alsoapp.wedesk -> com.alsoapp），
        # macOS ProjectDirs 用它拼配置目录；launchd 标签前缀也用它。
        org = pkg.rsplit(".", 1)[0] if "." in pkg else pkg
        rules += [
            # ---- Android ----------------------------------------------------
            Rule.regex(
                "Android applicationId / namespace（build.gradle 两处）",
                "flutter/android/app/build.gradle",
                r'(applicationId|namespace) "[^"]*"',
                r'\g<1> "%s"' % pkg,
            ),
            Rule.regex(
                "AndroidManifest package 属性（main）",
                "flutter/android/app/src/main/AndroidManifest.xml",
                r'package="[^"]*"',
                'package="%s"' % pkg,
            ),
            Rule.regex(
                "AndroidManifest package 属性（debug）",
                "flutter/android/app/src/debug/AndroidManifest.xml",
                r'package="[^"]*"',
                'package="%s"' % pkg,
            ),
            Rule.regex(
                "AndroidManifest package 属性（profile）",
                "flutter/android/app/src/profile/AndroidManifest.xml",
                r'package="[^"]*"',
                'package="%s"' % pkg,
            ),
            Rule.regex(
                "开机广播 action 名（与 BootReceiver.DEBUG_BOOT_COMPLETED 同值）",
                "flutter/android/app/src/main/AndroidManifest.xml",
                r'<action android:name="[^"]*DEBUG_BOOT_COMPLETED" />',
                '<action android:name="%s.DEBUG_BOOT_COMPLETED" />' % pkg,
            ),
            # kotlin 源码目录随包名迁移（com/carriez/flutter_hbb ->
            # com/alsoapp/wedesk），只有 glob 能在两种布局下都找到文件。
            # 目录名与 package 声明不一致 Kotlin 也能编译，但保持一致更稳。
            Rule.regex(
                "kotlin 包声明（glob 覆盖整个源码目录）",
                "flutter/android/app/src/main/kotlin/com/*/*/*.kt",
                r"(?m)^package com\.carriez\.flutter_hbb$",
                "package %s" % pkg,
                residual="com.carriez.flutter_hbb",
                relglob=True,
            ),
            Rule.regex(
                "BootReceiver 的 DEBUG_BOOT_COMPLETED 常量",
                "flutter/android/app/src/main/kotlin/com/*/*/BootReceiver.kt",
                r'(?m)^const val DEBUG_BOOT_COMPLETED = "[^"]*"$',
                'const val DEBUG_BOOT_COMPLETED = "%s.DEBUG_BOOT_COMPLETED"' % pkg,
                relglob=True,
            ),
            Rule.regex(
                "ffi.kt 对 RdClipboardManager 的 import",
                "flutter/android/app/src/main/kotlin/ffi.kt",
                r"(?m)^import com\.carriez\.flutter_hbb\.RdClipboardManager$",
                "import %s.RdClipboardManager" % pkg,
                residual="com.carriez.flutter_hbb",
            ),
            # ---- iOS ---------------------------------------------------------
            Rule.regex(
                "iOS PRODUCT_BUNDLE_IDENTIFIER（Debug/Release/Profile 三处）",
                "flutter/ios/Runner.xcodeproj/project.pbxproj",
                r"PRODUCT_BUNDLE_IDENTIFIER = [^;]*;",
                "PRODUCT_BUNDLE_IDENTIFIER = %s;" % pkg,
            ),
            Rule.regex(
                "iOS CFBundleURLName",
                "flutter/ios/Runner/Info.plist",
                r"(<key>CFBundleURLName</key>\s*<string>)[^<]*(</string>)",
                r"\g<1>%s\g<2>" % pkg,
            ),
            Rule.regex(
                "iOS exportOptions 的 bundle id 键（profile 名要在 Apple "
                "Developer 后台建同名的，脚本管不了）",
                "flutter/ios/exportOptions.plist",
                r"(<key>)com\.[A-Za-z0-9_.]+(</key>)",
                r"\g<1>%s\g<2>" % pkg,
            ),
            # ---- macOS -------------------------------------------------------
            Rule.regex(
                "macOS PRODUCT_BUNDLE_IDENTIFIER（Debug/Release/Profile 三处）",
                "flutter/macos/Runner.xcodeproj/project.pbxproj",
                r"PRODUCT_BUNDLE_IDENTIFIER = [^;]*;",
                "PRODUCT_BUNDLE_IDENTIFIER = %s;" % pkg,
            ),
            Rule.regex(
                "macOS CFBundleURLName",
                "flutter/macos/Runner/Info.plist",
                r"(<key>CFBundleURLName</key>\s*<string>)[^<]*(</string>)",
                r"\g<1>%s\g<2>" % pkg,
            ),
            Rule.regex(
                "macOS AppInfo.xcconfig 的 bundle id",
                "flutter/macos/Runner/Configs/AppInfo.xcconfig",
                r"PRODUCT_BUNDLE_IDENTIFIER = [^\n]*",
                "PRODUCT_BUNDLE_IDENTIFIER = %s" % pkg,
            ),
            # ---- Linux -------------------------------------------------------
            Rule.regex(
                "Linux GTK APPLICATION_ID（桌面入口/WMClass 之外的正式标识）",
                "flutter/linux/CMakeLists.txt",
                r'set\(APPLICATION_ID "[^"]*"\)',
                'set(APPLICATION_ID "%s")' % pkg,
            ),
            # ---- Rust --------------------------------------------------------
            Rule.regex(
                "Cargo bundle identifier",
                "Cargo.toml",
                r'identifier = "[^"]*"',
                'identifier = "%s"' % pkg,
            ),
            # cargo 自带 bin（rustdesk/naming/service）的 Windows 版本资源，
            # 由 build.rs 的 winres 读取；Flutter 那个 exe 用的是 Runner.rc，
            # 两处都要改，否则文件属性里仍是 RustDesk。
            # OriginalFilename 保持 rustdesk.exe 不动 —— 它就是 cargo bin 的真名。
            Rule.regex(
                "Cargo winres - 产品名",
                "Cargo.toml",
                r'ProductName = "[^"]*"',
                'ProductName = "%s"' % app_name,
            ),
            Rule.regex(
                "Cargo winres - 文件描述",
                "Cargo.toml",
                r'FileDescription = "[^"]*"',
                'FileDescription = "%s Remote Desktop"' % app_name,
            ),
            Rule.regex(
                "hbb_common ORG（macOS 配置目录，client 侧）",
                "libs/hbb_common/src/config.rs",
                r'pub static ref ORG: RwLock<String> = RwLock::new\("[^"]*"\.to_owned\(\)\);',
                'pub static ref ORG: RwLock<String> = RwLock::new("%s".to_owned());' % org,
            ),
            Rule.regex(
                # 本仓库 server/ 下有一份内联 fork 的 hbb_common（服务端自用），
                # 路径相对 --source（client/）要跳到仓库根；纯上游客户端树里
                # 这个文件不存在，规则会报"文件不存在"（非必需，仅警告）。
                "hbb_common ORG（macOS 配置目录，server 内联 fork 侧）",
                "../server/src/libs/hbb_common/src/config.rs",
                r'pub static ref ORG: RwLock<String> = RwLock::new\("[^"]*"\.to_owned\(\)\);',
                'pub static ref ORG: RwLock<String> = RwLock::new("%s".to_owned());' % org,
            ),
            Rule.regex(
                "correct_app_name 的模板 bundle id 替换源（与 plist 模板同值）",
                "src/platform/macos.rs",
                r's\.replace\("[^"]*", &bundleid\)',
                's.replace("%s", &bundleid)' % pkg,
            ),
            # launchd 标签前缀：write_plists / root-update / 回滚脚本里共 6 处
            # （daemon/agent × plist 路径 × 标签）。模板里的 RustDesk 字样由
            # correct_app_name 在运行时改写成 APP_NAME，这里只换组织段。
            # 注意 org 已含 "com." 前缀，替换串里不要再拼一次。
            Rule.regex(
                "launchd 标签/plist 路径前缀（com.<org>.{App}_{service,server}）",
                "src/platform/macos.rs",
                r"com\.[A-Za-z0-9_]+(\.\{\}_(?:service|server))",
                r"%s\g<1>" % org,
            ),
            Rule.regex(
                "macos.rs 单测：已安装 app 的 bundle id",
                "src/platform/macos.rs",
                r'info\("com\.[^"]*"\)',
                'info("%s")' % pkg,
            ),
            Rule.regex(
                "macos.rs 单测：(bundle_id, accepted) 用例",
                "src/platform/macos.rs",
                r'\("com\.[^"]*", true\)',
                '("%s", true)' % pkg,
            ),
            Rule.regex(
                "macos.rs 里 tccutil 注释的 bundle id",
                "src/platform/macos.rs",
                r"(tccutil reset All )com\.[A-Za-z0-9_.]+",
                r"\g<1>%s" % pkg,
            ),
            Rule.regex(
                "common.rs 注释里的 XPC 服务名",
                "src/common.rs",
                r"(XPC_SERVICE_NAME=)com\.[A-Za-z0-9_.]+",
                r"\g<1>%s.%s_server" % (org, app_name),
            ),
            Rule.regex(
                "daemon.plist 标签",
                "src/platform/privileges_scripts/daemon.plist",
                r"com\.[A-Za-z0-9_]+\.RustDesk_(service|server)",
                r"%s.RustDesk_\g<1>" % org,
            ),
            Rule.regex(
                "daemon.plist AssociatedBundleIdentifiers",
                "src/platform/privileges_scripts/daemon.plist",
                r"(<key>AssociatedBundleIdentifiers</key>\s*<string>)[^<]*(</string>)",
                r"\g<1>%s\g<2>" % pkg,
            ),
            Rule.regex(
                "agent.plist 标签",
                "src/platform/privileges_scripts/agent.plist",
                r"com\.[A-Za-z0-9_]+\.RustDesk_(service|server)",
                r"%s.RustDesk_\g<1>" % org,
            ),
            Rule.regex(
                "agent.plist AssociatedBundleIdentifiers",
                "src/platform/privileges_scripts/agent.plist",
                r"(<key>AssociatedBundleIdentifiers</key>\s*<string>)[^<]*(</string>)",
                r"\g<1>%s\g<2>" % pkg,
            ),
            Rule.regex(
                "install.scpt 的 launchd 路径与标签",
                "src/platform/privileges_scripts/install.scpt",
                r"com\.[A-Za-z0-9_]+\.RustDesk_(service|server)",
                r"%s.RustDesk_\g<1>" % org,
            ),
            Rule.regex(
                # 与 Label 规则同一风格：模板里保留 RustDesk 字样，运行时由
                # correct_app_name 改写成 APP_NAME（com.alsoapp.WeDesk/），
                # 与 ProjectDirs 实际 Preferences 目录一致。
                "install.scpt 的 Preferences 目录（须与 ProjectDirs 实际路径一致）",
                "src/platform/privileges_scripts/install.scpt",
                r"com\.[A-Za-z0-9_]+\.RustDesk/",
                r"%s.RustDesk/" % org,
            ),
            Rule.regex(
                "uninstall.scpt 的 launchd 路径",
                "src/platform/privileges_scripts/uninstall.scpt",
                r"com\.[A-Za-z0-9_]+\.RustDesk_(service|server)",
                r"%s.RustDesk_\g<1>" % org,
            ),
            Rule.regex(
                "update.scpt 的 launchd 路径与标签",
                "src/platform/privileges_scripts/update.scpt",
                r"com\.[A-Za-z0-9_]+\.RustDesk_(service|server)",
                r"%s.RustDesk_\g<1>" % org,
            ),
            Rule.regex(
                "manual_update 的公版 bundle id 常量（定制客户端走实际 bundle id）",
                "src/manual_update.rs",
                r'const PUBLIC_MACOS_BUNDLE_ID: &str = "[^"]*";',
                'const PUBLIC_MACOS_BUNDLE_ID: &str = "%s";' % pkg,
            ),
            # 单测 resolves_public_and_custom_update_package_identity 三类字面量：
            #   1. 公版用例是一对（期望值 + 实参），必须成对替换，只换一边断言就挂；
            #   2. 实参行单独一条，覆盖 (false, ...) 那个用例；
            #   3. com.example.* 是"任意自定义 id 原样返回"的对照用例，两条规则
            #      都用 (?!example) 负向断言避开 —— 早期版本没加，把对照用例
            #      改成了同一个 id，测试直接失败。
            Rule.regex(
                "manual_update 单测：公版用例的期望值 + 实参（成对）",
                "src/manual_update.rs",
                r'"com\.(?!example)[^"]*",(\s*\n\s*expected_update_package_id\(true, Some\(")com\.(?!example)[^"]*(")',
                r'"%s",\g<1>%s\g<2>' % (pkg, pkg),
            ),
            Rule.regex(
                "manual_update 单测：expected_update_package_id 的 bundle id 实参",
                "src/manual_update.rs",
                r'expected_update_package_id\((true|false), Some\("com\.(?!example)[^"]*"\.to_owned\(\)\)\)',
                r'expected_update_package_id(\g<1>, Some("%s".to_owned()))' % pkg,
            ),
            Rule.regex(
                "macOS 更新脚本测试的 bundle id",
                "res/update/test_macos_update_scripts.py",
                r'bundle_id = "[^"]*"',
                'bundle_id = "%s"' % pkg,
            ),
        ]

    # ------------------------------------------------- ASCII 品牌名（英文场合）
    #
    # app_name 是给用户看的产品名，可以是中文。但有几处必须是 ASCII 或文件名安全的：
    # MSI 字符串资源、Windows 服务显示名、防火墙规则名、Linux 桌面入口。
    # 把中文名填进去能编译，但 WiX 字符串表与部分注册表项会踩坑，所以单独设一项。
    if product:
        rules.append(
            Rule.regex(
                "MSI 界面文案（开始菜单/服务显示名/卸载项）",
                "res/msi/Package/Language/Package.en-us.wxl",
                r'(<String Id="[^"]+" Value="[^"]*?)RustDesk([^"]*?" />)',
                r"\1%s\2" % product,
                residual="RustDesk",
            )
        )
        rules += [
            Rule(
                "Linux 桌面入口 - 程序名",
                "res/rustdesk.desktop",
                "Name=RustDesk",
                "Name=%s" % product,
            ),
            Rule(
                "Linux 桌面入口 - 卸载项名",
                "res/rustdesk-link.desktop",
                "Name=RustDesk",
                "Name=%s" % product,
            ),
        ]

    return rules


# ===========================================================================
# 注入状态（支持反复调整配置）
# ===========================================================================
def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        # 状态文件损坏不值得让整个定制流程失败，退回空状态即可，
        # 代价是最多一次改不动旧值，脚本会给出明确提示。
        return {}


def save_state(state: dict) -> None:
    if not state:
        return
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


# ===========================================================================
# 执行引擎
# ===========================================================================
def apply_rules(root: Path, rules, dry_run=False, prev_state=None):
    """
    返回 (hit, miss, new_state)

    prev_state 是上一次注入时记录的值，形如 {rule_key: 当时写入的字符串}。
    它的作用是让脚本可以反复调整：每条精确匹配规则除了认上游原始值，
    也认"上次写进去的那个值"，于是把公司名从 A 改成 B 时能正确找到 A。
    没有它，第二次改配置就会满屏"未找到目标字符串"。

    relglob 规则的 relpath 是 glob 模式，对每个命中文件套用同一条规则
    （包名定制的 kotlin 目录迁移后路径会变，只有 glob 能找到文件）。
    """
    prev_state = prev_state or {}
    hit, miss = [], []
    new_state: dict[str, str] = {}
    cache: dict[Path, str] = {}       # 应用规则后的内容
    originals: dict[Path, str] = {}   # 磁盘上的原样，用来判断到底变没变

    for r in rules:
        if r.new == r.old:
            continue

        # 目标文件列表：glob 规则展开成多个，普通规则就是单个文件
        if r.relglob:
            fps = sorted(root.glob(r.relpath))
            if not fps:
                miss.append((r, "无匹配文件: %s" % r.relpath))
                continue
        else:
            fp = root / r.relpath
            if not fp.exists():
                miss.append((r, "文件不存在"))
                continue
            fps = [fp]

        changed = 0   # 真正让内容发生变化的替换次数
        matched = 0   # 匹配到旧值的次数（含"替换后一字未变"的幂等命中）
        for fp in fps:
            text = cache.get(fp)
            if text is None:
                text = fp.read_text(encoding="utf-8", errors="surrogateescape")
                cache[fp] = text
                originals[fp] = text

            key = "%s|%s" % (r.relpath, r.note)

            if r.is_regex:
                new_text, n = re.subn(r.old, r.new, text)
            else:
                # 依次尝试：上游原始值 -> 上次注入的值。
                candidates = [r.old]
                prev = prev_state.get(key)
                if prev and prev not in candidates:
                    candidates.append(prev)

                new_text, n = text, 0
                for cand in candidates:
                    c = text.count(cand)
                    if c:
                        new_text, n = text.replace(cand, r.new), c
                        break

            # 正则规则会"匹配成功但替换后内容一字未变"——例如版权行的年份和
            # 公司名都没动。这时应算已命中，否则每次都假报"待修改 N 项"，
            # 真实运行还会白白生成 .rd.bak 备份并重写文件。
            if n and new_text == text:
                matched += n
                continue

            if n == 0:
                # 幂等保护：匹配不到旧值，但新值已经在了 —— 说明这条之前注入过。
                # 这种情况不算失败，否则重跑脚本（比如只改公司名）会满屏报"未命中"，
                # 而且会误触发下面的 required 失败退出。
                if not r.is_regex and r.new and r.new in text:
                    matched += 1
                elif r.is_regex and r.residual and r.residual not in text:
                    # 正则匹配不到、且它要消灭的东西也找不到了 —— 同样是早已生效。
                    matched += 1
                else:
                    miss.append((r, "未找到目标字符串（源码版本可能已变更）"))
                    break
                continue

            cache[fp] = new_text
            changed += n
            matched += n

        # 只要本条规则有明确目标值就记入状态，包括"值已在源码里、本次没动"
        # 的情况 —— 这样下次改配置时仍能认出它。
        if r.new:
            new_state[key] = r.new
        if changed:
            hit.append((r, changed))
        elif matched:
            hit.append((r, 0))
        # changed==0 且 matched==0：所有目标文件都没命中，miss 已在上面记录

    if not dry_run:
        for fp, text in cache.items():
            # 只动真正发生变化的文件。否则"无需改动"的运行也会给每个文件
            # 生成一份 .rd.bak 并重写一遍，白白制造噪音和误备份。
            if text == originals.get(fp):
                continue
            bak = fp.with_suffix(fp.suffix + BAK_SUFFIX)
            if not bak.exists():
                shutil.copy2(fp, bak)
            fp.write_text(text, encoding="utf-8", errors="surrogateescape")

    return hit, miss, new_state


def restore(root: Path, rules):
    n = 0
    paths = {r.relpath for r in rules}
    for rel in paths:
        fp = root / rel
        bak = fp.with_suffix(fp.suffix + BAK_SUFFIX)
        if bak.exists():
            shutil.copy2(bak, fp)
            bak.unlink()
            n += 1
    return n


# ===========================================================================
def validate(c: dict):
    problems = []
    srv = c.get("server", {})
    brand = c.get("brand", {})
    host = srv.get("rendezvous_host", "").strip()
    relay = srv.get("relay_host", "").strip()
    if not host:
        problems.append("server.rendezvous_host 未填写 —— 客户端将仍连官方服务器")
    key = srv.get("key", "").strip()
    if not key:
        problems.append("server.key 未填写 —— 公钥不匹配会导致无法连接")
    elif key == "OeVuKk5nlHiXp+APNn0Y3pC1Iwpwn44JGqrQCsWqmBw=":
        problems.append("server.key 仍是官方默认值 —— 必须换成你自己的 hbbs 公钥")

    api_server = srv.get("api_server", "").strip()
    if api_server and not api_server.lower().startswith(("http://", "https://")):
        problems.append(
            'server.api_server 必须带 http:// 或 https:// 前缀，且端口写全，'
            '例如 "http://admin.wedesk.alsoapp.com:5185"'
        )

    pkg = brand.get("package_name", "").strip()
    if pkg and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+", pkg):
        problems.append(
            "brand.package_name 必须是反向域名格式（至少两段、每段字母/数字/下划线、"
            '以字母开头），例如 "com.alsoapp.wedesk"'
        )

    # ---- 端口检查 ----------------------------------------------------------
    # 客户端内置兜底端口是 21116（libs/hbb_common/src/config.rs 的 RENDEZVOUS_PORT，
    # 我们没改这个常量 —— 改了会连带把"IP 直连"监听端口抬高到 5189）。
    # 服务端跑在 5187，所以地址里必须显式带端口，否则客户端会去敲 21116。
    if host and ":" not in host.rsplit("]", 1)[-1]:
        problems.append(
            "server.rendezvous_host 没有写端口，客户端会按内置的 21116 去连 —— "
            "服务端不在 21116 时必须写成 \"域名:端口\""
        )

    # relay_host 已废弃（2026-10-21）：客户端不再保存中继节点数据。
    # 中继清单由 hbbs 维护 —— 每台 hbbr 启动后自动注册上报，hbbs 在 P2P
    # 失败时统一下发。客户端这里只按"主端口+1"做最后兜底推导。
    # 若老配置里还留着这个键，仅提示、不再校验。
    if relay:
        print(
            "注意：server.relay_host 已被忽略 —— 中继节点改由 hbbs 维护"
            "（hbbr 自动注册上报），客户端不再配置中继地址。"
            "维护 hbbr 容器上的 RELAY_PUBLIC_ADDR 即可，详见 docs/10-ports.md。"
        )
    return problems


def _port_of(addr: str):
    """取 "host:port" 里的 port（IPv6 用 [::1]:5187 形式，取最后一段）。"""
    tail = addr.rsplit("]", 1)[-1]
    return int(tail.rsplit(":", 1)[1]) if ":" in tail and tail.rsplit(":", 1)[1].isdigit() else None


def apply_overrides(c: dict, overrides):
    """把 --set section.key=value 合并进已加载的配置。

    只接受两级键（section.key）。空 VALUE 表示"不覆盖"而不是"清空"：
    CI 里常见写法是 `--set brand.company_name="$NAME"`，变量未设时 $NAME 展开为空串，
    此时若当成清空，会把仓库里已注入的公司名抹成空，很难排查。
    """
    for item in overrides:
        key, sep, val = item.partition("=")
        if not sep:
            raise ValueError(f"--set 需要 SECTION.KEY=VALUE 形式，收到: {item!r}")
        parts = key.strip().split(".")
        if len(parts) != 2:
            raise ValueError(f"--set 的键必须是 SECTION.KEY 两级形式，收到: {key!r}")
        sec, name = parts[0].strip(), parts[1].strip()
        val = val.strip()
        if not val:
            continue
        c.setdefault(sec, {})[name] = val
    return c


def _force_utf8_console() -> None:
    """Windows 的控制台默认是 ANSI 代码页（西文 cp1252 / 中文 cp936）。本脚本
    通篇中文输出，在 cp1252 下第一行 print 就是 UnicodeEncodeError，而且往往
    发生在"注入其实已经做完、正要打印结论"的时候 —— 看起来像注入失败，
    实际只是日志写不出。CI 里靠 workflow 的 PYTHONIOENCODING 兜底，这里是
    本地/其它调用方式下的第二道保险。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # Python < 3.7
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass


def main():
    _force_utf8_console()
    _init_color()
    ap = argparse.ArgumentParser(
        description="RustDesk 源码品牌/服务器定制注入器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--source",
        default=None,
        help="rustdesk 源码根目录（省略则用本仓库的 client/）",
    )
    ap.add_argument(
        "--config",
        default=None,
        help="定制配置文件（省略则用与本脚本同级的 custom.toml）",
    )
    ap.add_argument("--dry-run", action="store_true", help="只预览不写入")
    ap.add_argument("--restore", action="store_true", help="还原到定制前状态")
    ap.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="SECTION.KEY=VALUE",
        help=(
            "覆盖配置项的某个字段，可重复。用于 CI/命令行里临时改品牌，"
            "免去修改 custom.toml 再提交。例："
            "--set brand.company_name=\"某某科技\" --set brand.website=https://xx.com"
        ),
    )
    args = ap.parse_args()

    # 路径一律以本脚本位置为基准，这样从任何目录调用都能工作，
    # 不必先 cd 到 build/ 再执行。
    # 客户端源码在仓库的 client/ 下：脚本在 build/client/，向上两级到仓库根再进 client/。
    here = Path(__file__).resolve().parent
    root = Path(args.source).resolve() if args.source else here.parent.parent / "client"
    cfg_path = Path(args.config).resolve() if args.config else (here / "custom.toml")

    if not root.is_dir():
        print(f"{RED}[x]{RESET} 源码目录不存在: {root}")
        sys.exit(1)
    if not cfg_path.is_file():
        print(f"{RED}[x]{RESET} 配置文件不存在: {cfg_path}")
        sys.exit(1)

    cfg = load_config(cfg_path)
    if args.set:
        cfg = apply_overrides(cfg, args.set)
    rules = build_rules(cfg)

    if args.restore:
        n = restore(root, rules)
        print(f"{GREEN}[OK]{RESET} 已还原 {n} 个文件")
        return

    print(f"{BLUE}源码目录{RESET} {root}")
    print(f"{BLUE}配置文件{RESET} {cfg_path}\n")

    problems = validate(cfg)
    if problems:
        for p in problems:
            print(f"{YELLOW}[!]{RESET} {p}")
        print()

    prev_state = load_state()
    hit, miss, new_state = apply_rules(
        root, rules, dry_run=args.dry_run, prev_state=prev_state
    )
    if not args.dry_run:
        save_state(new_state)

    changed = [r for r, n in hit if n]
    alreadied = [r for r, n in hit if not n]

    for r, n in hit:
        if n:
            print(f"  {GREEN}✓{RESET} {r.note}  {DIM}({n} 处){RESET}")
        else:
            print(f"  {GREEN}✓{RESET} {r.note}  {DIM}(已是目标值，跳过){RESET}")
    for r, why in miss:
        flag = RED if r.required else YELLOW
        print(f"  {flag}✗{RESET} {r.note}: {why}")

    print()
    failed_required = [r for r, _ in miss if r.required]
    if args.dry_run:
        print(
            f"{BLUE}预演完成{RESET}: 将修改 {len(changed)} 项，"
            f"已是目标值 {len(alreadied)} 项，未命中 {len(miss)} 项（未写入磁盘）"
        )
    else:
        print(
            f"{GREEN}定制完成{RESET}: 本次修改 {len(changed)} 项，"
            f"已是目标值 {len(alreadied)} 项，未命中 {len(miss)} 项"
        )
        print(f"{DIM}原文件已备份为 *{BAK_SUFFIX}，可用 --restore 还原{RESET}")
    if not changed and not miss:
        print(f"{DIM}源码已是最新定制状态，无需改动。{RESET}")

    if failed_required:
        print()
        print(
            f"{RED}[x]{RESET} 有 {len(failed_required)} 项必需替换未命中，"
            f"定制可能不生效。请检查源码版本是否匹配。"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
