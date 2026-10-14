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
  src/common.rs
      get_api_server_ 的官方兜底域名 admin.rustdesk.com（置空以杜绝外发请求）
  Cargo.toml / Runner.rc / strings.xml / desktop_setting_page.dart / common.dart / install_page.dart
      公司名、版权、产品名、官网与隐私政策链接

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

    def __init__(self, note, relpath, old, new, required=False, count=None):
        self.note = note
        self.relpath = relpath
        self.old = old
        self.new = new
        self.required = required  # True 时不命中按失败处理
        self.count = count  # 期望替换次数

    @staticmethod
    def regex(note, relpath, pattern, repl, required=False):
        r = Rule(note, relpath, pattern, repl, required)
        r.is_regex = True
        return r

    is_regex = False


def build_rules(c: dict) -> "list[Rule]":
    srv = c.get("server", {})
    brand = c.get("brand", {})
    adv = c.get("advanced", {})

    host = srv.get("rendezvous_host", "").strip()
    key = srv.get("key", "").strip()
    app_name = brand.get("app_name", "RemoteDesk").strip()
    company = brand.get("company_name", "").strip()
    website = brand.get("website", "").strip()
    exe = brand.get("exe_name", app_name.lower()).strip() or "remotedesk"

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
            Rule(
                "关于页 - 版权归属",
                "flutter/lib/desktop/pages/desktop_setting_page.dart",
                "Purslane Tech Pte. Ltd.",
                company,
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
    """
    prev_state = prev_state or {}
    hit, miss = [], []
    new_state: dict[str, str] = {}
    cache: dict[Path, str] = {}       # 应用规则后的内容
    originals: dict[Path, str] = {}   # 磁盘上的原样，用来判断到底变没变

    for r in rules:
        fp = root / r.relpath
        if not fp.exists():
            miss.append((r, "文件不存在"))
            continue
        if r.new == r.old:
            continue

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

        # 只要本条规则有明确目标值就记入状态，包括"值已在源码里、本次没动"
        # 的情况 —— 这样下次改配置时仍能认出它。
        if r.new:
            new_state[key] = r.new

        # 正则规则会"匹配成功但替换后内容一字未变"——例如版权行的年份和
        # 公司名都没动。这时应算已命中，否则每次都假报"待修改 N 项"，
        # 真实运行还会白白生成 .rd.bak 备份并重写文件。
        if n and new_text == text:
            hit.append((r, 0))
            continue

        if n == 0:
            # 幂等保护：匹配不到旧值，但新值已经在了 —— 说明这条之前注入过。
            # 这种情况不算失败，否则重跑脚本（比如只改公司名）会满屏报"未命中"，
            # 而且会误触发下面的 required 失败退出。
            if not r.is_regex and r.new and r.new in text:
                hit.append((r, 0))
            else:
                miss.append((r, "未找到目标字符串（源码版本可能已变更）"))
            continue

        cache[fp] = new_text
        hit.append((r, n))

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
    if not srv.get("rendezvous_host"):
        problems.append("server.rendezvous_host 未填写 —— 客户端将仍连官方服务器")
    key = srv.get("key", "").strip()
    if not key:
        problems.append("server.key 未填写 —— 公钥不匹配会导致无法连接")
    elif key == "OeVuKk5nlHiXp+APNn0Y3pC1Iwpwn44JGqrQCsWqmBw=":
        problems.append("server.key 仍是官方默认值 —— 必须换成你自己的 hbbs 公钥")
    return problems


def main():
    _init_color()
    ap = argparse.ArgumentParser(
        description="RustDesk 源码品牌/服务器定制注入器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--source",
        default=None,
        help="rustdesk 源码根目录（省略则自动探测本仓库的 rustdesk/）",
    )
    ap.add_argument(
        "--config",
        default=None,
        help="定制配置文件（省略则用与本脚本同级的 custom.toml）",
    )
    ap.add_argument("--dry-run", action="store_true", help="只预览不写入")
    ap.add_argument("--restore", action="store_true", help="还原到定制前状态")
    args = ap.parse_args()

    # 路径一律以本脚本位置为基准，这样从任何目录调用都能工作，
    # 不必先 cd 到 build/ 再执行。
    # 上游源码已扁平化到仓库根（与 build/ 同级），不再有 rustdesk/ 子目录。
    here = Path(__file__).resolve().parent
    root = Path(args.source).resolve() if args.source else here.parent
    cfg_path = Path(args.config).resolve() if args.config else (here / "custom.toml")

    if not root.is_dir():
        print(f"{RED}[x]{RESET} 源码目录不存在: {root}")
        sys.exit(1)
    if not cfg_path.is_file():
        print(f"{RED}[x]{RESET} 配置文件不存在: {cfg_path}")
        sys.exit(1)

    cfg = load_config(cfg_path)
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
