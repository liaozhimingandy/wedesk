#!/usr/bin/env python3
# =============================================================================
# 检查 GitHub Actions 工作流：表达式语法 + 引用可解析性
# =============================================================================
# 【为什么需要它】
#   工作流里有两类错误不会让 YAML 解析失败，也不会让 action 报错：
#
#   A. 引用类：某个变量悄悄变成空字符串 —— 然后你会看到诸如
#          cp: cannot stat 'target//release/hbbs'
#      这种"路径中间空了一截"的怪现象，以及一段把人往错误方向引的报错文案。
#      本仓库就真实踩过一次：删掉 workflow 层的 env.SERVER_TARGET 之后，
#      打包步骤里 ${{ env.SERVER_TARGET }} 静默变成空串，
#      而编译脚本自己还有一份同名默认值 —— 两个真源，各走各的。
#
#   B. 语法类：GitHub 在【编译期】就会解析并校验工作流里所有 ${{ }} 的语法，
#      不只是 if: / env:，连 run: 脚本正文里的也算。
#      所以你在 shell 注释里随手写一句"别写成 ${{ }}"当示例，
#      整个 workflow 会直接变成"无效的工作流文件"，而且报错位置
#      回指的是 run: 块标量的起点（形如 `run: |` 那一行的 `|`），
#      从位置上看不出到底是哪一句有问题。本仓库同样真实踩过一次。
#
# 【检查什么】
#   1. ${{ env.FOO }}          —— FOO 在 workflow / job / step 任何一层都没定义
#   2. ${{ steps.X.outputs.Y }} —— 该 job 里没有 id: X 的 step，或 X 从不输出 Y
#   3. ${{ secrets.X }} 统计    —— 只列出用到哪些 secret，提醒去仓库配置。
#      secrets 是否真的配了，静态检查看不到，只能靠运行时兜底
#      （本仓库的做法：用到前先判空，缺了就硬失败并指名道姓）
#   4. 所有 ${{ }} 的【语法】：空表达式、括号/引号不闭合、多余内容、
#      未知函数名、未知上下文根名（含 run: 脚本和注释里的）
#   5. if: 的裸表达式（GitHub 允许 if: 不写 ${{ }}，此时整行按表达式解析）
#
# 【不检查什么】
#   - env: 里定义的 shell 局部变量（如 run 里自己 VAR=xxx 再 $VAR）——
#     脚本识别不了，属于已知噪声，靠人工
#   - 表达式的类型正确性与运行时求值结果
#
# 用法：
#   python build/common/check-workflow.py               # 检查 .github/workflows/*.yml
#   python build/common/check-workflow.py <file.yml> [...]    # 检查指定文件
#
# 退出码：0 = 没问题；1 = 发现语法错误或无法解析的引用
# =============================================================================

from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("需要 pyyaml：pip install pyyaml")

def _find_repo_root(start: Path) -> Path:
    """从脚本所在位置向上查找仓库根（第一个含 .github/workflows 的目录）。

    为什么不能写死 parent.parent：本脚本原先在 build/ 下，parent.parent
    正好是仓库根；重构时移到 build/common/ 后层数假设立刻失效，ROOT 变成
    build/，于是"没有找到工作流文件"——而它当时返回的是 exit 0。
    一个静态检查工具在【什么都没检查】的情况下报告"没问题"，
    比直接报错危险得多：会把没验证当成已验证。
    """
    for cand in [start.parent, *start.parents]:
        if (cand / ".github" / "workflows").is_dir():
            return cand
    return start.parent.parent  # 兜底：保持旧行为


ROOT = _find_repo_root(Path(__file__).resolve())
DEFAULT_DIR = ROOT / ".github" / "workflows"

# GitHub 自动注入的环境变量（节选），引用它们不算未定义
BUILTIN_ENV = {
    "CI", "HOME", "PWD", "USER", "PATH", "TMPDIR", "LANG",
    "GITHUB_OUTPUT", "GITHUB_ENV", "GITHUB_PATH", "GITHUB_STEP_SUMMARY",
    "GITHUB_WORKSPACE", "GITHUB_WORKFLOW", "GITHUB_ACTION", "GITHUB_ACTOR",
    "GITHUB_REPOSITORY", "GITHUB_REPOSITORY_OWNER", "GITHUB_REF",
    "GITHUB_REF_NAME", "GITHUB_REF_TYPE", "GITHUB_SHA", "GITHUB_EVENT_NAME",
    "GITHUB_JOB", "GITHUB_RUN_ID", "GITHUB_RUN_NUMBER", "GITHUB_SERVER_URL",
    "RUNNER_OS", "RUNNER_ARCH", "RUNNER_TEMP", "RUNNER_NAME",
}

ENV_REF = re.compile(r"\$\{\{\s*env\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
STEP_OUT_REF = re.compile(
    r"\$\{\{\s*steps\.([A-Za-z0-9_-]+)\.outputs\.([A-Za-z0-9_-]+)\s*\}\}"
)
SECRET_REF = re.compile(r"\$\{\{\s*secrets\.([A-Za-z0-9_.-]+)\s*\}\}")
# 匹配 echo "key=$VAL" >> "$GITHUB_OUTPUT"
# 注意 =" 与 = 的区别：值部分在 = 与收尾引号之间，写成 =" 会永远匹配不上
OUT_KEY = re.compile(
    r'echo\s+"([A-Za-z_][A-Za-z0-9_-]*)=[^"]*"\s*>>\s*"\$GITHUB_OUTPUT"'
)

# =============================================================================
# GitHub 表达式语法校验
# =============================================================================
# 语法（与 actions/runner 的 ExpressionParser 对齐，只判语法不求值）：
#   or   := and  ( '||' and )*
#   and  := eq   ( '&&' eq )*
#   eq   := rel  ( ('==' | '!=') rel )*
#   rel  := unary ( ('>=' | '<=' | '>' | '<') unary )*
#   unary := '!' unary | primary
#   primary := '(' or ')' | 数字 | '单引号字符串' | true/false/null
#            | 函数调用 | 上下文引用（可跟 .属性 / [索引] / .*）

EXPR_RE = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
NUM_RE = re.compile(r"-?(?:0[xX][0-9a-fA-F]+|[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)")
# GitHub 表达式里字符串只能用单引号，内部单引号写成两个
STR_RE = re.compile(r"'(?:[^']|'')*'")

FUNCTIONS = {
    "contains", "startswith", "endswith", "format", "join",
    "tojson", "fromjson", "hashfiles", "success", "always",
    "cancelled", "failure",
}
CONTEXTS = {
    "github", "env", "vars", "job", "jobs", "steps", "runner",
    "secrets", "strategy", "matrix", "needs", "inputs",
}
LITERALS = {"true", "false", "null"}


class ExpressionError(Exception):
    """表达式语法错误。"""


class _ExprParser:
    def __init__(self, text: str) -> None:
        self.t = text
        self.i = 0

    # --- 基础工具 --------------------------------------------------------
    def _ws(self) -> None:
        while self.i < len(self.t) and self.t[self.i] in " \t\r\n":
            self.i += 1

    def _peek(self) -> str:
        self._ws()
        return self.t[self.i] if self.i < len(self.t) else ""

    def _eat(self, s: str) -> bool:
        self._ws()
        if self.t.startswith(s, self.i):
            self.i += len(s)
            return True
        return False

    def _done(self) -> bool:
        self._ws()
        return self.i >= len(self.t)

    def _fail(self, msg: str) -> None:
        snippet = self.t[self.i:].strip()
        raise ExpressionError(f"{msg}（剩余：{snippet!r}）")

    # --- 语法层 ----------------------------------------------------------
    def parse(self) -> None:
        if self._done():
            raise ExpressionError("空表达式")
        self._or()
        if not self._done():
            self._fail("表达式结束后还有多余内容")

    def _or(self) -> None:
        self._and()
        while self._eat("||"):
            self._and()

    def _and(self) -> None:
        self._eq()
        while self._eat("&&"):
            self._eq()

    def _eq(self) -> None:
        self._rel()
        while self._eat("==") or self._eat("!="):
            self._rel()

    def _rel(self) -> None:
        self._unary()
        while (self._eat(">=") or self._eat("<=")
               or self._eat(">") or self._eat("<")):
            self._unary()

    def _unary(self) -> None:
        if self._eat("!"):
            self._unary()
            return
        self._primary()

    def _primary(self) -> None:
        if self._eat("("):
            self._or()
            if not self._eat(")"):
                self._fail("括号没有闭合")
            return

        c = self._peek()
        if not c:
            self._fail("表达式意外结束")

        if c == "'":
            m = STR_RE.match(self.t, self.i)
            if not m:
                self._fail("字符串字面量没有闭合（只能用单引号，内部单引号写成两个）")
            self.i = m.end()
            self._tail()
            return

        if c.isdigit() or (c == "-" and self.i + 1 < len(self.t)
                           and (self.t[self.i + 1].isdigit()
                                or self.t[self.i + 1] == ".")):
            m = NUM_RE.match(self.t, self.i)
            if not m:
                self._fail("数字字面量无法解析")
            self.i = m.end()
            return

        m = IDENT_RE.match(self.t, self.i)
        if not m:
            self._fail("无法识别的表达式内容")
        word = m.group(0)
        self.i = m.end()

        # 函数调用
        if self._eat("("):
            low = word.lower()
            if low not in FUNCTIONS:
                self._fail(f"未知函数 {word}()")
            if not self._eat(")"):
                while True:
                    self._or()
                    if self._eat(","):
                        continue
                    if self._eat("]"):      # 括号用错成方括号，给个明确提示
                        self._fail("函数参数列表的 ')' 写成了 ']'")
                    if self._eat(")"):
                        break
                    self._fail("函数参数列表没有闭合")
            return

        # 字面量
        if word.lower() in LITERALS:
            return

        # 上下文引用：第一段必须是合法的上下文根
        if word.lower() not in CONTEXTS:
            self._fail(
                f"未知上下文 {word}（合法根：{', '.join(sorted(CONTEXTS))}）")
        self._tail()

    def _tail(self) -> None:
        while True:
            if self._eat("."):
                if self._eat("*"):
                    continue
                m = IDENT_RE.match(self.t, self.i)
                if not m:
                    self._fail("'.' 后面缺少属性名")
                self.i = m.end()
                continue
            if self._eat("["):
                if self._eat("]"):
                    self._fail("'[]' 里缺少索引")
                self._or()
                if not self._eat("]"):
                    self._fail("'[' 没有闭合")
                continue
            return


def check_expression(expr: str) -> str | None:
    """校验一个 GitHub 表达式的语法。返回 None 表示通过。"""
    try:
        _ExprParser(expr).parse()
        return None
    except ExpressionError as e:
        return str(e)


# =============================================================================
# 引用可解析性检查
# =============================================================================

def iter_strings(node, out: list[str]) -> None:
    """递归收集一个 YAML 节点里的所有字符串值。"""
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, dict):
        for k, v in node.items():
            if k == "env":          # env: 的【键名】是定义不是引用，跳过
                continue
            iter_strings(v, out)
    elif isinstance(node, list):
        for v in node:
            iter_strings(v, out)


def walk_all(node, ctx: str, out: list[tuple[str, str]]) -> None:
    """递归收集所有字符串【值】及其所在路径（用于报错定位）。

    只走值不走键，所以 env: 的键名（定义）天然不会被当成引用。
    """
    if isinstance(node, str):
        out.append((ctx, node))
    elif isinstance(node, dict):
        for k, v in node.items():
            walk_all(v, f"{ctx}.{k}", out)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            walk_all(v, f"{ctx}[{i}]", out)


def env_names(node) -> set[str]:
    """收集一层 env: 里定义的键名。"""
    if isinstance(node, dict) and isinstance(node.get("env"), dict):
        return set(node["env"].keys())
    return set()


def check_file(path: Path) -> list[str]:
    problems: list[str] = []
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        return [f"{path.name}: YAML 解析失败 —— {e}"]
    if not isinstance(doc, dict):
        return []

    # --- 4) 全文档表达式语法校验（含 run: 脚本正文与其中的注释）---
    texts: list[tuple[str, str]] = []
    walk_all(doc, "<workflow>", texts)
    for ctx, text in texts:
        for m in EXPR_RE.finditer(text):
            err = check_expression(m.group(1))
            if err:
                snippet = m.group(0) if len(m.group(0)) <= 80 \
                    else m.group(0)[:77] + "..."
                problems.append(
                    f"{path.name} {ctx}: 表达式语法错误 —— {err}\n"
                    f"        位置：{snippet}")
        # 漏写收尾的 }} 时上面的正则匹配不到，这里再兜一次
        for m in re.finditer(r"\$\{\{(?!.*?\}\})", text, re.DOTALL):
            problems.append(
                f"{path.name} {ctx}: ${{{{ 没有找到配对的 }}}}"
                f"（原文：{text[m.start():m.start() + 40]!r}）")

    # --- 5) if: 的裸表达式 ---
    def check_bare_if(node, ctx: str) -> None:
        if isinstance(node, dict):
            val = node.get("if")
            if isinstance(val, str) and "${{" not in val:
                err = check_expression(val)
                if err:
                    problems.append(
                        f"{path.name} {ctx}.if: 裸表达式语法错误 —— {err}")
            for k, v in node.items():
                check_bare_if(v, f"{ctx}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                check_bare_if(v, f"{ctx}[{i}]")

    check_bare_if(doc, "<workflow>")

    wf_env = env_names(doc)
    jobs = doc.get("jobs") or {}
    if not isinstance(jobs, dict):
        return problems

    for jname, job in jobs.items():
        if not isinstance(job, dict):
            continue
        job_env = env_names(job)
        steps = job.get("steps") or []
        if not isinstance(steps, list):
            continue

        # 该 job 里所有的 step id，以及它们写进 GITHUB_OUTPUT 的键
        step_ids = {s.get("id") for s in steps
                    if isinstance(s, dict) and s.get("id")}
        step_outputs: dict[str, set[str]] = {}
        # uses: 型 step 的 outputs 由 action 自己定义，静态枚举不出来 ——
        # 只校验 id 是否存在，不校验键名，避免一堆误报把真问题淹掉
        action_steps = set()
        for s in steps:
            if not isinstance(s, dict) or not s.get("id"):
                continue
            if s.get("uses"):
                action_steps.add(s["id"])
            run = s.get("run") or ""
            step_outputs.setdefault(s["id"], set()).update(OUT_KEY.findall(run))

        for s in steps:
            if not isinstance(s, dict):
                continue
            blob_parts: list[str] = []
            for key in ("run", "if", "name", "uses"):
                if isinstance(s.get(key), str):
                    blob_parts.append(s[key])
            if isinstance(s.get("with"), dict):
                blob_parts += [v for v in s["with"].values()
                               if isinstance(v, str)]
            blob = "\n".join(blob_parts)

            for m in ENV_REF.finditer(blob):
                name = m.group(1)
                if name in wf_env or name in job_env or \
                        name in env_names(s) or name in BUILTIN_ENV:
                    continue
                problems.append(
                    f"{path.name} / {jname} / {s.get('name', '?')}: "
                    f"${{{{ env.{name} }}}} 没有任何一层 env: 定义")

            for m in STEP_OUT_REF.finditer(blob):
                sid, key = m.group(1), m.group(2)
                if sid not in step_ids:
                    problems.append(
                        f"{path.name} / {jname} / {s.get('name', '?')}: "
                        f"steps.{sid}.outputs 在本 job 里不存在（没有 id: {sid}）")
                elif sid in action_steps:
                    # 第三方 action，outputs 无法静态校验，放行
                    continue
                elif key not in step_outputs.get(sid, set()):
                    problems.append(
                        f"{path.name} / {jname} / {s.get('name', '?')}: "
                        f"step {sid} 从不输出 outputs.{key}")

    return problems


def main(argv: list[str]) -> int:
    files = [Path(a) for a in argv[1:]] or sorted(DEFAULT_DIR.glob("*.yml"))
    if not files:
        # 找不到文件【不等于】没问题。默认路径为空只可能是两种原因：
        # 仓库被挪了位置/改了布局，或脚本自己被移到了别的深度。
        # 两种情况都该让人知道，而不是安静地返回 0。
        print(f"没有找到工作流文件（查找目录：{DEFAULT_DIR}）")
        print("如果这是意外的，说明脚本推断仓库根失败了 —— 请显式传入文件路径。")
        return 2

    all_problems: list[str] = []
    secrets: set[str] = set()
    for f in files:
        print(f"检查 {f}")
        text = f.read_text(encoding="utf-8")
        secrets |= set(SECRET_REF.findall(text))
        probs = check_file(f)
        all_problems += probs
        print(f"    {len(probs)} 个问题")

    print()
    if secrets:
        print("用到的仓库 secrets（需在 Settings → Secrets and variables → Actions 配置）：")
        for s in sorted(secrets):
            print(f"    - {s}")
        print()

    if all_problems:
        print(f"发现 {len(all_problems)} 个问题：")
        for p in all_problems:
            print(f"  [x] {p}")
        return 1

    print("✓ 表达式语法全部合法，env / steps.outputs 引用均可解析")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
