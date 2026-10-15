#!/usr/bin/env python3
# =============================================================================
# 检查 GitHub Actions 工作流里的引用是否都能解析
# =============================================================================
# 【为什么需要它】
#   工作流里有一类错误不会让 YAML 解析失败，也不会让 action 报错，
#   只会让某个变量悄悄变成空字符串 —— 然后你会看到诸如
#       cp: cannot stat 'target//release/hbbs'
#   这种"路径中间空了一截"的怪现象，以及一段把人往错误方向引的报错文案。
#
#   本仓库就真实踩过一次：删掉 workflow 层的 env.SERVER_TARGET 之后，
#   打包步骤里 ${{ env.SERVER_TARGET }} 静默变成空串，
#   而编译脚本自己还有一份同名默认值 —— 两个真源，各走各的。
#
# 【检查什么】
#   1. ${{ env.FOO }}         —— FOO 在 workflow / job / step 任何一层都没定义
#   2. ${{ steps.X.outputs.Y }} —— 该 job 里没有 id: X 的 step，或 X 从不输出 Y
#   3. ${{ secrets.X }} 统计   —— 只列出用到哪些 secret，提醒去仓库配置。
#      secrets 是否真的配了，静态检查看不到，只能靠运行时兜底
#      （本仓库的做法：用到前先判空，缺了就硬失败并指名道姓）
#
# 【不检查什么】
#   - env: 里定义的 shell 局部变量（如 run 里自己 VAR=xxx 再 $VAR）——
#     脚本识别不了，属于已知噪声，靠人工
#
# 用法：
#   python build/check-workflow.py                     # 检查 .github/workflows/*.yml
#   python build/check-workflow.py <file.yml> [...]    # 检查指定文件
#
# 退出码：0 = 没问题；1 = 发现无法解析的引用
# =============================================================================

from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("需要 pyyaml：pip install pyyaml")

ROOT = Path(__file__).resolve().parent.parent
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

    wf_env = env_names(doc)
    jobs = doc.get("jobs") or {}
    if not isinstance(jobs, dict):
        return []

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
            texts: list[str] = []
            for key in ("run", "if", "name", "uses"):
                if isinstance(s.get(key), str):
                    texts.append(s[key])
            if isinstance(s.get("with"), dict):
                texts += [v for v in s["with"].values() if isinstance(v, str)]
            blob = "\n".join(texts)

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
        print("没有找到工作流文件")
        return 0

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
        print(f"发现 {len(all_problems)} 个无法解析的引用：")
        for p in all_problems:
            print(f"  [x] {p}")
        return 1

    print("✓ 所有 env / steps.outputs 引用均可解析")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
