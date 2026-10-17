#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rdctl.py —— RustDesk 自建服务器跨平台管理工具

零第三方依赖，只用标准库。Windows / Linux / macOS 通用。

命令:
    python rdctl.py build      用本仓库源码编译服务端镜像（换过源码后要跑）
    python rdctl.py start      启动 hbbs + hbbr（首次自动等待并打印服务器公钥）
    python rdctl.py stop       停止并保留数据
    python rdctl.py restart    重启
    python rdctl.py status     查看容器状态与已注册设备概况
    python rdctl.py ports     打印当前端口规划（对照防火墙/安全组时用）
    python rdctl.py key       打印服务器公钥（客户端定制要用）
    python rdctl.py logs      查看实时日志（Ctrl+C 退出）
    python rdctl.py backup    备份密钥与数据库到 ./backup/<时间戳>/
    python rdctl.py upgrade   拉远端镜像并重启（仅 RD_IMAGE 指向远端时可用）
    python rdctl.py doctor    体检：查出"配置看着对、实际没生效"的问题
    python rdctl.py uninstall 停止并删除容器（保留 ./data）

upgrade 参数:
    --tag=<tag>      升级到指定版本
    --check          只报告有无更新，不重启
    --no-backup      跳过升级前自动备份（不推荐）

doctor 退出码 0 = 无错误（可能仍有提醒）；1 = 有需要处理的错误。
"""
import os
import sys
import subprocess
import time
import shutil
import re
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE.parent / "data"
BACKUP_DIR = HERE.parent / "backup"
KEY_PUB = DATA_DIR / "id_ed25519.pub"
KEY_PRIV = DATA_DIR / "id_ed25519"
DB_PATHS = ["db_v2.sqlite3", "db.sqlite3"]

GREEN = RED = YELLOW = BLUE = RESET = ""
if sys.platform != "win32":
    GREEN, RED, YELLOW, BLUE, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[34m", "\033[0m"
elif os.environ.get("WT_SESSION") or os.environ.get("TERM"):
    # Windows Terminal / 支持 ANSI 的终端启用彩色输出
    os.system("")
    GREEN, RED, YELLOW, BLUE, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[34m", "\033[0m"


def info(msg):
    print(f"{GREEN}[OK]{RESET} {msg}")


def warn(msg):
    print(f"{YELLOW}[!]{RESET} {msg}")


def err(msg):
    print(f"{RED}[x]{RESET} {msg}", file=sys.stderr)


def step(msg):
    print(f"{BLUE}[..]{RESET} {msg}")


def run(cmd, capture=True, check=False, text=True):
    """执行命令，返回 (returncode, stdout, stderr)"""
    try:
        p = subprocess.run(cmd, capture_output=capture, text=text, cwd=str(HERE), shell=False)
        if check and p.returncode != 0:
            raise RuntimeError(f"命令失败: {' '.join(cmd)}\n{p.stderr}")
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except FileNotFoundError:
        err("未找到命令: %s —— 请确认 Docker 已安装并加入 PATH" % cmd[0])
        sys.exit(1)


# ---------------------------------------------------------------------------
# 环境预检
# ---------------------------------------------------------------------------
def pick_compose_cmd():
    """优先使用 'docker compose'(v2 插件)，回退到 'docker-compose'(v1)"""
    rc, out, _ = run(["docker", "compose", "version"])
    if rc == 0:
        return ["docker", "compose"]
    rc, out, _ = run(["docker-compose", "--version"])
    if rc == 0:
        warn("检测到旧版 docker-compose v1，建议升级到 Docker Compose v2")
        return ["docker-compose"]
    err("未检测到 Docker Compose。请安装 Docker Desktop 或 Docker Engine + Compose 插件。")
    sys.exit(1)


COMPOSE = pick_compose_cmd()


def check_docker_running():
    rc, _, _ = run(["docker", "info"])
    if rc != 0:
        err("Docker 守护进程未运行。")
        if sys.platform == "win32":
            hint = "请先启动 Docker Desktop（开始菜单搜索 Docker Desktop 并打开，等待托盘图标变为 Running）"
        elif sys.platform == "darwin":
            hint = "请先启动 Docker Desktop 应用"
        else:
            hint = "请执行: sudo systemctl start docker"
        print(f"    {hint}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# 核心动作
# ---------------------------------------------------------------------------
def ensure_env_file():
    """没有 .env 时依据模板生成，避免 compose 变量缺失"""
    env = HERE / ".env"
    example = HERE / ".env.example"
    if env.exists():
        return
    if example.exists():
        shutil.copy(example, env)
        warn("已由 .env.example 生成 .env —— 建议修改其中的 RELAY_HOST")
    else:
        warn("未找到 .env，使用默认配置启动")


def wait_for_key(timeout=45):
    """首次启动后 hbbs 生成 ed25519 密钥对，轮询等待"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if KEY_PUB.exists() and KEY_PUB.stat().st_size > 0:
            return True
        time.sleep(1)
    return False


def read_key():
    if not KEY_PUB.exists():
        return None
    return KEY_PUB.read_text(encoding="utf-8").strip()


def read_env():
    """读 .env 为 dict。缺文件返回空表 —— 此时 compose 会用自己的默认值。"""
    values = {}
    env_file = HERE / ".env"
    if not env_file.exists():
        return values
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip()
    except OSError:
        pass
    return values


def compose_image_name():
    """读取 .env 里的 RD_IMAGE，未设置则用 compose 里的默认值。"""
    img = read_env().get("RD_IMAGE")
    return img if img else "wedesk-server:latest"


def is_remote_image():
    """RD_IMAGE 是否指向远端镜像。

    判据就是【镜像名里有没有 '/'】：
      本地镜像是 docker-compose.yml 的默认值 `wedesk-server:latest`，不带 /；
      远端镜像一定带 / —— Docker Hub 简写 `owner/repo:tag` 也是（
      `liaozhiming/wedesk-server:latest`），带仓库地址的更是
      （`ghcr.io/foo/bar:1`、`127.0.0.1:5000/ws:1`）。

    注意别写成"第一段含 . 或 :"：那会把 Docker Hub 简写误判成本地镜像，
    而它恰恰是最常用的远端写法。

    区分它很重要：
      - 本地：compose build 真的会用本仓库源码编译，改源码会生效；
      - 远端：compose 直接用（或拉取）那个镜像，本地 Dockerfile 不参与，
              改源码【不会】生效 —— 这是最容易白改半天的地方。
    """
    img = read_env().get("RD_IMAGE")
    if not img:
        return False
    return "/" in img


# --- 端口规划 --------------------------------------------------------------
# 端口是从【主端口】推导出来的，不是一张随意的清单：
#   hbbs 主端口 P → UDP P / TCP P / TCP P-1(NAT 探测) / TCP P+2(WebSocket)
#   hbbr 主端口 R → TCP R      / TCP R+2(WebSocket)
# 所以 hbbs 一动就是三个端口跟着动。下面的默认值必须与 docker-compose.yml
# 里的 ${...:-5187} 保持一致，任何一处改了，另一处也要跟着改。
DEFAULT_PORTS = {"HBBS_PORT": 5187, "HBBS_NAT_PORT": 5186, "HBBR_PORT": 5188}


def resolve_ports():
    env = read_env()
    ports = {}
    for k, default in DEFAULT_PORTS.items():
        raw = env.get(k, "")
        ports[k] = int(raw) if raw.lstrip("-").isdigit() else default
    return ports


def check_ports(ports, fatal=False):
    """校验端口推导关系。

    这里检查的是"半通不通"最容易出错的两个点：
      1. NAT 端口必须 = 主端口-1，否则 compose 映射的是一个没有进程监听的端口，
         表面上三个口都开着，实际客户端注册永远超时；
      2. 派生出来的 WebSocket 端口（P+2 / R+2）不能被别的服务占用 —— hbbs/hbbr
         无条件监听它们，起不来会直接崩，报的是含糊的 "Address already in use"。
    """
    p = ports["HBBS_PORT"]
    nat = ports["HBBS_NAT_PORT"]
    r = ports["HBBR_PORT"]
    problems = []

    if nat != p - 1:
        problems.append(
            f"HBBS_NAT_PORT({nat}) ≠ HBBS_PORT({p}) - 1。\n"
            f"        hbbs 会在 {p - 1} 上监听 NAT 探测端口，把 {nat} 映射出去等于指向空气。"
        )

    derived = [(p, "hbbs 主"), (nat, "hbbs NAT"), (p + 2, "hbbs WebSocket"),
               (r, "hbbr 主"), (r + 2, "hbbr WebSocket")]
    seen = {}
    for port, what in derived:
        if port in seen:
            problems.append(f"{what}端口 {port} 与 {seen[port]}端口撞车")
        seen[port] = what
        if not (0 < port < 65536):
            problems.append(f"{what}端口 {port} 超出合法范围")

    if problems:
        for m in problems:
            (err if fatal else warn)(m)
        return False
    return True


def print_ports(ports):
    p, nat, r = ports["HBBS_PORT"], ports["HBBS_NAT_PORT"], ports["HBBR_PORT"]
    print()
    info("端口规划（P-1 / P / P+2 与 R / R+2 由 hbbs、hbbr 自行派生）：")
    print(f"""
      {nat}  TCP    hbbs NAT 类型探测          = P-1
      {p}  TCP    hbbs TCP 打洞与连接        = P
      {p}  UDP    hbbs ID 注册 + 心跳        = P
      {r}  TCP    hbbr 中继数据              = R
      {p + 2}  TCP    hbbs WebSocket  容器内监听，未 publish
      {r + 2}  TCP    hbbr WebSocket  容器内监听，未 publish
""")
    print("    防火墙只需放行：5186/tcp、5187/tcp、5187/udp、5188/tcp。")
    print("    WebSocket 两个端口为什么不能对外开，见 docs/10-ports.md。")
    print()


# 旧 .env 里用过的键名（端口改成派生表之后已废弃，保留检测是为了提示迁移）
LEGACY_KEYS = ["HBBS_TCP", "HBBS_UDP", "HBBR_TCP", "RELAY_PORT", "HBBS_TCP_HP", "HBBS_NAT"]


def check_env_legacy():
    """提示 .env 里已经失效的写法。

    这些键在新版 compose 里不再被读取，留着不会报错、但会让人以为改了生效；
    更麻烦的是 RD_IMAGE 若仍指向官方镜像，跑起来的就不是我们自己编的二进制。
    """
    env = read_env()
    legacy = [k for k in LEGACY_KEYS if k in env]
    if legacy:
        warn(".env 里有已废弃的端口变量：" + "、".join(legacy))
        print("    现在只认 HBBS_PORT / HBBS_NAT_PORT / HBBR_PORT 三个，"
              "旧的会被引擎静默忽略。")
        print("    直接覆盖即可: copy .env.example .env（然后重填 RELAY_HOST）")
    img = env.get("RD_IMAGE", "")
    if img.startswith("rustdesk/rustdesk-server"):
        warn(f".env 里 RD_IMAGE={img} —— 这会拉【官方镜像】启动，")
        print("    既不是你自己编的二进制，也没有我们关掉的版本检查补丁。")
        print("    要用本地自编译镜像，把这一行删掉或改成 #RD_IMAGE=wedesk-server:latest。")

    # ------------------------------------------------------------------
    # data/.env 是静默陷阱，必须拦住
    # ------------------------------------------------------------------
    # hbbs 与 hbbr 启动时都会读【工作目录】下的 .env，并把里面的键当成
    # 服务端参数（src/common.rs 的 init_args()、src/hbbr.rs）。
    # 而容器的 WORKDIR 是 /root，正是挂载 ./data:/root 的地方。
    # 于是只要 data/.env 存在，它的键就被静默变成服务端参数：
    #   PORT=5188      → hbbr 改听 5189（PORT 会被 +1），撞上 hbbs 的 WS 端口
    #   RELAY_HOST=x   → 覆盖 command 里的 -r，客户端被指到别处
    # 这些全都不报错，只是"端口开着但连不上"，且启动日志看不出异常。
    # ------------------------------------------------------------------
    data_env = DATA_DIR / ".env"
    if data_env.exists():
        err(f"发现 {data_env} —— 它会被两个容器当成服务端配置文件读！")
        print("    hbbs / hbbr 启动时会读工作目录（/root = ./data）下的 .env，")
        print("    并把里面的键静默变成服务端参数，覆盖默认行为。")
        print("    最典型的事故：PORT=5188 会让 hbbr 改听 5189，撞上 hbbs 的 WebSocket 端口。")
        print("    处理：确认这个文件是你有意放的；否则请移出 data/ 目录。")


def cmd_ports(args):
    """打印当前端口规划，方便对照防火墙/安全组。"""
    check_env_legacy()
    ports = resolve_ports()
    print_ports(ports)
    check_ports(ports, fatal=False)


# --- 数据库后端 ------------------------------------------------------------
# ID/设备数据默认落在 SQLite；改成 PostgreSQL 后可以统一管理与出报表。
# 后端由 DB_TYPE 决定，但**配置写了不等于生效** —— 官方镜像不认这个变量，
# 会静默继续写 SQLite，不报错、日志也看不出来。所以下面既有"看配置"的
# db_type()，也有"看数据实际落在哪"的 probe_*()，doctor 会把两者交叉比对。
PG_TYPES = ("postgres", "postgresql", "pg")


def db_type():
    """配置的后端：sqlite（默认）或 postgres"""
    return read_env().get("DB_TYPE", os.environ.get("DB_TYPE", "sqlite")).strip().lower()


def is_pg():
    return db_type() in PG_TYPES


def pg_conf(key, default):
    return read_env().get(key, os.environ.get(key, default))


def probe_sqlite_rows():
    """本地 SQLite 里 peer 表的行数；不可用返回 None"""
    dbfile = next((DATA_DIR / n for n in DB_PATHS if (DATA_DIR / n).exists()), None)
    if not dbfile:
        return None
    import sqlite3

    try:
        con = sqlite3.connect(f"file:{dbfile}?mode=ro", uri=True)
        n = con.execute("SELECT COUNT(*) FROM peer").fetchone()[0]
        con.close()
        return n
    except Exception:
        return None


def probe_pg_rows():
    """PostgreSQL 里 peer 表的行数；不可用返回 None。
    借容器内的 psql 查询，宿主机无需安装 PostgreSQL 客户端。"""
    user = pg_conf("PG_USER", "rustdesk")
    db = pg_conf("PG_DB", "rustdesk")
    rc, out, _ = run(
        COMPOSE + ["exec", "-T", "postgres", "psql", "-U", user, "-d", db, "-tAc",
                   "select count(*) from peer;"]
    )
    if rc != 0:
        return None
    try:
        return int(out.strip().splitlines()[-1].strip())
    except (ValueError, IndexError):
        return None


def image_exists(tag):
    rc, _, _ = run(["docker", "image", "inspect", tag])
    return rc == 0


# 探针脚本：逐个判定三个二进制的状态，输出一行结论后退出。
# 刻意用固定的 /usr/bin 路径而不是 command -v —— 后者对"存在但没 +x"的文件
# 同样返回空，会把「缺可执行位」误报成「文件不存在」，而这正是最容易踩的一种。
#
# 三种结论对应三个完全不同的成因与修法，所以分开报：
#   MISSING:<b>  文件不在镜像里        → COPY 源路径写错，或产物目录被挂成
#                                        BuildKit cache mount（内容不进镜像层）
#   EMPTY:<b>    是 0 字节文件          → 打包/传输环节损坏
#   NOEXEC:<b>   存在但没有可执行位      → artifact 上传/下载丢失了 POSIX mode
#   BROKEN:<b>   有 +x 但 execve 返回 ENOENT → 不是静态二进制，动态链接器缺失
#                                        （典型：glibc 产物被 COPY 进了 alpine）
# 解析顺序刻意与 Docker 自身一致：先用 PATH 查找（Docker 就是这么找 command
# 第一段的），找到后再执行【解析出的绝对路径】而不是裸名字 ——
# 否则"检查的"和"执行的"可能不是同一个文件，诊断结论会跑偏。
_PROBE_BINS = r'''
for b in hbbs hbbr rustdesk-utils; do
    p="$(command -v "$b" 2>/dev/null || true)"
    if [ -z "$p" ]; then
        # PATH 上找不到。command -v 对"存在但没 +x"的文件同样会跳过，
        # 所以补一次直查，把「文件不在」与「在但缺执行位」分开。
        if [ -e "/usr/bin/$b" ]; then echo "NOEXEC:$b"; else echo "MISSING:$b"; fi
        exit 0
    fi
    if [ ! -s "$p" ]; then echo "EMPTY:$b"; exit 0; fi
    if [ ! -x "$p" ]; then echo "NOEXEC:$b"; exit 0; fi
    "$p" --help >/dev/null 2>&1 && rc=0 || rc=$?
    # 127 = command not found：execve 返回 ENOENT，典型是 ELF 解释器
    #       （如 /lib64/ld-linux-x86-64.so.2）在 alpine 里不存在 —— glibc
    #       产物被 COPY 进 alpine 就是这个现象。
    # 126 = 找到了但执行不了：不是可执行的二进制格式，或 shebang 指向的
    #       解释器不存在（比如产物被截断 / 传成了脚本）。
    # 两个都要判，只判 127 会漏掉后者。
    if [ "$rc" -eq 127 ] || [ "$rc" -eq 126 ]; then echo "BROKEN:$b:$rc"; exit 0; fi
done
echo "OK"
'''

# 每种结论对应的解释与处理建议
_PROBE_HELP = {
    "MISSING": (
        "镜像里找不到这个文件。最可能是 COPY 的源路径写错了 —— 尤其注意："
        "Dockerfile 里若把 cargo 的 target 目录挂成了 BuildKit cache mount "
        "（--mount=type=cache），它的内容不会进入镜像层，从挂载点 COPY 必然落空。",
        "docker compose build --no-cache",
    ),
    "EMPTY": (
        "文件是 0 字节，打包或传输环节损坏。",
        "重新构建：docker compose build --no-cache",
    ),
    "NOEXEC": (
        "文件存在但没有可执行位。典型成因：二进制经过 artifact 上传/下载 "
        "（zip 打包）后丢失了 POSIX mode。Docker 解析容器 command 第一段走的是 "
        "PATH 查找，而 PATH 查找要求可执行位，所以报的是 not found 而不是 "
        "permission denied —— 这句话极具误导性。",
        "在 Dockerfile 里 COPY 之后补 chmod 0755，然后 docker compose build --no-cache",
    ),
    # BROKEN 不在这里 —— 它的主句要按退出码（126 / 127）分别措辞，
    # 写在 image_has_binaries 里的组装段。
}


def image_has_binaries(tag):
    """检查镜像里是否真有可执行的 hbbs / hbbr / rustdesk-utils。

    返回 (ok, detail)：ok=False 时 detail 是可直接打印的原因与处理建议。

    为什么要在启动前专门查一次：
      容器起不来的报错来自 runc 层
          error during container init: exec: "hbbr": executable file not found in $PATH
      它回指的是容器 init 进程，行列上完全指不到"镜像里少了个文件"这回事，
      而且三种成因共用这一句话，排查方向南辕北辙。
    """
    rc, out, _ = run([
        "docker", "run", "--rm", "--entrypoint", "sh", tag, "-c", _PROBE_BINS
    ])

    if rc != 0:
        # 连 sh 都跑不起来，说明基础镜像或 entrypoint 被改过
        return False, (
            "无法在镜像里执行 sh —— 基础镜像可能不是 alpine，"
            "或者镜像被设了自定义 ENTRYPOINT。\n"
            "    用下面这条命令看看镜像里到底有什么：\n"
            f"        docker run --rm --entrypoint sh {tag} -c 'ls -l /usr/bin'"
        )

    verdict = out.strip().splitlines()[-1] if out.strip() else ""

    if verdict == "OK":
        return True, ""

    parts = verdict.split(":")
    kind = parts[0]
    name = parts[1] if len(parts) > 1 else ""
    extra = parts[2] if len(parts) > 2 else ""

    # 有可执行位但执行失败：126 与 127 指向完全不同的问题，分开说。
    if kind == "BROKEN":
        if extra == "127":
            why = (
                "execve 返回 ENOENT（退出码 127）：它不是静态二进制，依赖的"
                "动态链接器在本镜像里不存在 —— 通常是把 glibc 编译的产物 "
                "COPY 进了 alpine。"
            )
            how = "确认 build-musl.sh 的 TARGET 是 *-unknown-linux-musl，或改用带 glibc 的基础镜像"
        else:
            why = (
                "退出码 126 = 找到了但执行不了：文件不是本平台的可执行格式，"
                "或 shebang 指向的解释器不存在（产物被截断 / 架构不对 / 传错文件）。"
            )
            how = "用 file <产物> 看它的真实类型与目标架构"
        return False, f"{name}：{why}\n    处理：{how}"

    why, how = _PROBE_HELP.get(
        kind,
        ("镜像自检返回了未预期的结果：" + verdict, "检查 check-bins.sh 的实现"),
    )
    return False, f"{name}：{why}\n    处理：{how}"


def build_image(image, nocache=False):
    """用本仓库源码构建镜像，并在构建后做一次可执行性自检。

    为什么要自检：docker build 退出码为 0 完全不代表镜像能用 ——
    产物可能压根没进镜像层（cache mount），也可能丢了可执行位（artifact 传输）。
    这两种情况都要拖到 `docker compose up` 才炸，且报错指向容器 init，极难定位。
    """
    step(f"从源码构建镜像 {image} ...")
    info("首次构建较慢：容器内要编译 musl 静态二进制（约 15-25 分钟）")
    cmd = COMPOSE + ["build"] + (["--no-cache"] if nocache else [])
    rc, _, errout = run(cmd)
    if rc != 0:
        err(f"镜像构建失败:\n{errout}")
        sys.exit(1)
    info(f"镜像 {image} 构建完成")

    ok, detail = image_has_binaries(image)
    if not ok:
        err(f"构建完成，但镜像 {image} 自检未通过：")
        for line in detail.splitlines():
            print("    " + line)
        sys.exit(1)
    info("镜像自检通过（hbbs / hbbr / rustdesk-utils 均可执行）")


def cmd_build(args):
    """用本仓库源码构建服务端镜像。

    docker-compose.yml 里写了 build 段（源码在 ../src/），
    所以镜像是本地编译出来的，与 rustdesk/rustdesk-server 官方镜像无关。
    首次构建会在容器内编译 musl 静态二进制，约 15-25 分钟。
    """
    check_docker_running()
    ensure_env_file()
    check_env_legacy()
    check_ports(resolve_ports(), fatal=True)
    image = compose_image_name()

    # 若 .env 的 RD_IMAGE 指向了远端镜像，本地源码构建根本不会被执行 ——
    # 那样跑的就不是自己编的二进制，也不会带我们关掉的版本检查补丁。
    # 这里提前说清楚，避免"改了源码发现没生效"。
    if is_remote_image():
        warn(f".env 里 RD_IMAGE={image} 指向远端镜像 —— 下面的构建只作用于该 tag 的本地副本，")
        warn("真正生效的仍然是你指定的远端镜像。若要完全本地化，请注释掉 RD_IMAGE。")

    # args 就是 sys.argv[2:]（字符串列表），没有 argparse 的命名空间，
    # 所以只能在这里认标志位 —— 别用 getattr(args, "no_cache")，列表上取不到，
    # 会静默永远为 False。
    nocache = "--no-cache" in args or "--nocache" in args
    build_image(image, nocache=nocache)


def cmd_start(args):
    check_docker_running()
    ensure_env_file()
    DATA_DIR.mkdir(exist_ok=True)

    # 端口关系错了宁可不启动：三个口都开着但连不上的状态，比直接报错难查得多
    check_env_legacy()
    ports = resolve_ports()
    check_ports(ports, fatal=True)
    print_ports(ports)

    # 镜像来自本仓库源码，本地还没有的话要先构建。
    # 不直接 `compose up --build` 是为了避免每次重启都触发一次重新编译。
    image = compose_image_name()
    if not image_exists(image):
        # 不直接 `compose up --build` 是为了避免每次重启都触发一次重新编译
        step(f"本地没有镜像 {image}，先用本仓库源码构建（首次约 15-25 分钟）...")
        build_image(image)
    else:
        ok, detail = image_has_binaries(image)
        if not ok:
            # 镜像在，但里面没有可用的 hbbs / hbbr —— 这个镜像是坏的。
            # 上面已经把三种成因逐条判定好了，这里先转述再决定怎么办。
            err(f"镜像 {image} 里没有可执行的 hbbs / hbbr，这个镜像是坏的。")
            print("    直接 up 必定报：")
            print('        exec: "hbbr": executable file not found in $PATH')
            print()
            for line in detail.splitlines():
                print("    " + line)
            print()
            if is_remote_image():
                # 远端镜像：本地源码构建帮不上忙，只能重新拉
                print("    RD_IMAGE 指向远端镜像，本地构建不会被执行。请：")
                print("        docker compose pull")
                print("        docker compose up -d")
                print()
                print("    或注释掉 .env 里的 RD_IMAGE，改用本地自编译镜像")
                print("    （那样跑的才是本仓库源码，也才带我们的版本检查补丁）。")
                sys.exit(1)
            # 本地镜像坏了 —— 直接重建。
            # 这是最容易反复踩的坑：docker compose up（不带 --build）发现有
            # 同 tag 镜像就静默复用，改了 Dockerfile 也不会重新构建。
            warn("本地镜像缺少可执行的服务端二进制，即将用本仓库源码重建。")
            info("重建约 15-25 分钟，且会跳过缓存以确保产物真的重新编译。")
            build_image(image, nocache=True)

    step("启动容器 ...")
    rc, out, errout = run(COMPOSE + ["up", "-d"])
    if rc != 0:
        err(f"启动失败:\n{errout}")
        sys.exit(1)
    info("容器已启动")

    step("等待服务器生成密钥 ...")
    if wait_for_key():
        key = read_key()
        print()
        info("服务器就绪。以下是你的服务器公钥，客户端定制时需要写入：")
        print()
        print(f"    {YELLOW}{key}{RESET}")
        print()
        print("    下一步：把这串公钥与你的服务器地址填入 ../../build/client/custom.toml")
        print("            然后运行 ../../build/client/customize.py --source ../../client 生成定制客户端源码")
    else:
        warn("尚未检测到密钥文件，可稍后用 'python rdctl.py key' 查看")
        print(f"    排查: {COMPOSE[0]} logs rd-hbbs")

    show_status()


def cmd_stop(args):
    rc, _, errout = run(COMPOSE + ["down"])
    if rc != 0:
        err(f"停止失败: {errout}")
        sys.exit(1)
    info("已停止。数据保留在 ./data")


def cmd_restart(args):
    cmd_stop(args)
    cmd_start(args)


def cmd_key(args):
    key = read_key()
    if not key:
        err("未找到密钥文件 ./data/id_ed25519.pub —— 请先执行 start")
        sys.exit(1)
    print(key)


def show_status():
    rc, out, _ = run(COMPOSE + ["ps", "--format", "table {{.Name}}\t{{.Status}}\t{{.Ports}}"])
    if rc == 0 and out:
        print()
        print(out)
    # 显示设备在线概况
    dbfile = next((DATA_DIR / n for n in DB_PATHS if (DATA_DIR / n).exists()), None)
    if dbfile:
        import sqlite3

        try:
            con = sqlite3.connect(f"file:{dbfile}?mode=ro", uri=True)
            # 不同版本的库表名可能不同，容错处理
            tables = [
                r[0]
                for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            ]
            print()
            info(f"数据库: {dbfile.name}  表: {', '.join(tables) if tables else '(空)'}")
            for t in tables:
                try:
                    n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    print(f"    {t}: {n} 行")
                except Exception:
                    pass
            con.close()
        except Exception as e:
            warn(f"读取数据库失败（可忽略）: {e}")


def cmd_status(args):
    check_docker_running()
    show_status()


def cmd_logs(args):
    check_docker_running()
    # 只跟一个容器时传服务名（compose logs 用服务名而不是容器名）。
    # 注意别写成容器名 rd-hbbs —— compose logs 收服务名，写容器名会报
    # "no such service"，而且报错信息看不出是名字用错了。
    if "hbbs" in args:
        svc = ["hbbs"]
    elif "hbbr" in args:
        svc = ["hbbr"]
    else:
        svc = []
    try:
        subprocess.run(COMPOSE + ["logs", "-f", "--tail", "100"] + svc, cwd=str(HERE))
    except KeyboardInterrupt:
        print("\n已退出日志")


def cmd_backup(args):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = BACKUP_DIR / ts
    dest.mkdir(parents=True, exist_ok=True)
    n = 0
    for name in ["id_ed25519", "id_ed25519.pub"] + DB_PATHS:
        src = DATA_DIR / name
        if src.exists():
            shutil.copy2(src, dest / name)
            n += 1
    # PostgreSQL 后端：借容器内的 pg_dump 导出，宿主机无需装 PostgreSQL 客户端
    if is_pg():
        user = pg_conf("PG_USER", "rustdesk")
        db = pg_conf("PG_DB", "rustdesk")
        rc, out, errout = run(
            COMPOSE + ["exec", "-T", "postgres", "pg_dump", "-U", user, db]
        )
        if rc != 0:
            err(f"pg_dump 失败: {errout}")
            print(f"    确认已执行: {' '.join(COMPOSE)} --profile pg up -d")
        else:
            (dest / "postgres_dump.sql").write_text(out + "\n", encoding="utf-8")
            n += 1
    if n == 0:
        warn("没有可备份的文件（服务可能尚未运行过）")
        return
    info(f"已备份 {n} 个文件 -> {dest}")
    print("    密钥泄露风险提示：私钥 id_ed25519 一旦泄露，攻击者可伪造你的服务器")


# --- 升级 ------------------------------------------------------------------
def _opt(args, name):
    """取 --name=value 或 --name value"""
    prefix = "--" + name + "="
    for i, a in enumerate(args):
        if a.startswith(prefix):
            return a[len(prefix):]
        if a == "--" + name and i + 1 < len(args):
            return args[i + 1]
    return None


def _flag(args, name):
    return ("--" + name) in args


def retag(image, tag):
    """替换镜像 tag。注意 'localhost:5000/ws' 这种 registry 带端口的情形：
    只有最后一个 ':' 之后不含 '/' 时，那一段才是 tag。"""
    head, _, last = image.rpartition(":")
    if not head or "/" in last:
        return f"{image}:{tag}"
    return f"{head}:{tag}"


def local_image_id(image):
    rc, out, _ = run(["docker", "images", "--no-trunc", "--format", "{{.ID}}", image])
    if rc != 0 or not out:
        return None
    return out.splitlines()[0].strip() or None


def cmd_upgrade(args):
    """拉取远端镜像并重启。

    只在镜像真的变了才重启 —— 否则"例行升级"会白白中断正在进行的远程连接。
    本地构建的镜像（默认 wedesk-server:latest）远端没有，pull 不了，
    那种情况应当用 build 重新编译。
    """
    check_docker_running()
    image = compose_image_name()

    if not is_remote_image():
        warn(f"RD_IMAGE 未设置或指向本地镜像（{image}）—— 远端没有它，无法 pull")
        print("    本仓库的镜像是用源码编出来的，改过源码请执行：")
        print("      python rdctl.py build            # 或 build --no-cache")
        print("    若确实要拉远端镜像，先在 .env 里设置 RD_IMAGE=owner/repo:tag")
        return 1

    tag = _opt(args, "tag")
    check_only = _flag(args, "check")
    no_backup = _flag(args, "no-backup")

    if tag:
        new_image = retag(image, tag)
        if new_image != image:
            step(f"指定版本: {image} -> {new_image}")
            image = new_image

    print(f"    镜像: {image}")
    before = local_image_id(image)
    print(f"    当前: {before[:19] + '…' if before else '(本地还没有)'}")

    step("拉取镜像 ...")
    rc, _, errout = run(["docker", "pull", image])
    if rc != 0:
        err(f"拉取失败:\n{errout}")
        if "ghcr.io" in image:
            print("    GHCR 镜像默认私有，需先登录：")
            print("      echo <TOKEN> | docker login ghcr.io -u <用户名> --password-stdin")
            print("    Token 需具备 read:packages 权限。")
        elif "denied" in errout or "unauthorized" in errout:
            print("    鉴权失败，请确认已登录该镜像仓库。")
        return 1

    after = local_image_id(image)
    if before and before == after:
        info("镜像已是最新，无需重启")
        return 0

    if before:
        info(f"发现新版本: {before[:19]}… -> {after[:19]}…")
    else:
        info(f"镜像已就绪: {after[:19]}…" if after else "镜像已拉取")

    if check_only:
        info("--check 模式：已拉取但不重启。去掉 --check 执行即可应用。")
        return 0

    if not no_backup:
        step("升级前自动备份 ...")
        cmd_backup([])
    else:
        warn("已跳过备份（--no-backup），升级失败时将无法回滚数据")

    step("用新镜像重建容器 ...")
    rc, _, errout = run(COMPOSE + ["up", "-d"])
    if rc != 0:
        err(f"重启失败:\n{errout}")
        return 1
    info("已重启")
    print("    回滚：把 .env 的 RD_IMAGE 改回上一个 tag，再执行一次 upgrade")
    show_status()
    return 0


# --- 体检 ------------------------------------------------------------------
def _check(ok, msg, fix=None):
    """打印一条检查结果。失败返回 1（计入错误），通过返回 0"""
    if ok:
        info(msg)
        return 0
    err(msg)
    if fix:
        print(f"    {fix}")
    return 1


def cmd_doctor(args):
    """部署体检。

    主要抓"配置看着对、实际没生效"的静默失败。错误与提醒分开统计：
    否则"还没部署"这种正常状态也会被判成失败，全是噪音。
    """
    errors = 0
    warns = 0

    def w(msg, fix=None):
        nonlocal warns
        warns += 1
        warn(msg)
        if fix:
            print(f"    {fix}")

    print("=== 环境 ===")
    rc, _, _ = run(["docker", "info"])
    errors += _check(rc == 0,
                     "Docker 守护进程运行中" if rc == 0 else "Docker 守护进程未运行",
                     "启动 Docker Desktop，或 sudo systemctl start docker")
    if rc != 0:
        return 1

    rc, _, errout = run(COMPOSE + ["config", "-q"])
    errors += _check(rc == 0,
                     "compose 配置合法" if rc == 0 else f"compose 配置有误: {errout}",
                     "检查 .env 里的变量是否都填了")

    print()
    print("=== 配置 ===")
    env = read_env()
    if not (HERE / ".env").exists():
        w("没有 .env（将使用 compose 里的默认值）", "copy .env.example .env")
    else:
        info(".env 存在")
    relay = env.get("RELAY_HOST", "127.0.0.1")
    if relay in ("127.0.0.1", "localhost", ""):
        w(f"RELAY_HOST={relay or '(空)'} —— 只有本机能连中继，外部客户端连不上",
          ".env 里改成服务器的外网 IP 或域名")
    else:
        info(f"RELAY_HOST={relay}")
    # 复用已有的检查：废弃变量、data/.env 陷阱、RD_IMAGE 指向官方镜像
    check_env_legacy()

    print()
    print("=== 端口 ===")
    ports = resolve_ports()
    if check_ports(ports, fatal=False):
        info(f"端口规划自洽（hbbs {ports['HBBS_PORT']}/tcp+udp、"
             f"{ports['HBBS_NAT_PORT']}/tcp；hbbr {ports['HBBR_PORT']}/tcp）")
        print("    防火墙放行清单见: python rdctl.py ports")
    else:
        warns += 1

    print()
    print("=== 镜像 ===")
    image = compose_image_name()
    print(f"    镜像: {image}   ({'远端' if is_remote_image() else '本地构建'})")
    if not is_remote_image():
        print("    本地镜像由本仓库源码编译，改源码后要 build 才会生效")
    if image_exists(image):
        info("镜像已存在")
    else:
        w(f"镜像 {image} 还没构建",
          "python rdctl.py build   （远端镜像则先 docker pull / 检查 RD_IMAGE）")

    print()
    print("=== 数据库后端 ===")
    if is_pg():
        # 最关键的一条：官方镜像不认 DB_TYPE，会静默退回 SQLite。
        if image.startswith("rustdesk/rustdesk-server"):
            w("镜像是官方 rustdesk/rustdesk-server，它不认识 DB_TYPE，"
              "ID 数据仍会写进 SQLite 文件",
              "把 RD_IMAGE 指向本仓库编译的镜像")
        # 实证：数据到底落在哪，而不是只看写了什么配置
        pg_rows = probe_pg_rows()
        sq_rows = probe_sqlite_rows()
        if pg_rows is None and sq_rows is not None:
            w(f"PostgreSQL 查不到 peer 表，但本地 SQLite 有 {sq_rows} 行 —— 实际仍在用 SQLite",
              "确认已执行 " + " ".join(COMPOSE) + " --profile pg up -d，"
              "且 DB_URL 是 postgres:// 连接串而非文件路径")
        elif pg_rows is None:
            w("PostgreSQL 暂时查不到（容器可能没起，或还没建表）")
        else:
            info(f"PostgreSQL peer 表: {pg_rows} 行")
            if sq_rows:
                print(f"    注：本地 SQLite 还有 {sq_rows} 行历史数据，"
                      f"需合并请运行 migrate-sqlite-to-pg.py")
    else:
        info("DB_TYPE=sqlite（默认，与官方行为一致）")
        sq_rows = probe_sqlite_rows()
        if sq_rows is None:
            w("还没查到 SQLite 数据（服务可能尚未启动过）")
        else:
            info(f"SQLite peer 表: {sq_rows} 行")

    print()
    print("=== 运行时 ===")
    rc, out, _ = run(COMPOSE + ["ps", "--format", "{{.Service}}\t{{.Status}}"])
    if rc == 0 and out.strip():
        for line in out.strip().splitlines():
            svc, _, status = line.partition("\t")
            up = "Up" in status or "running" in status
            errors += _check(up, f"{svc}: {status}", f"启动: {' '.join(COMPOSE)} up -d")
    else:
        w("没有运行中的容器", f"启动: {' '.join(COMPOSE)} up -d")

    if KEY_PUB.exists():
        info("服务器密钥已生成")
    else:
        w("还没生成密钥（首次 start 后会自动生成）")

    print()
    if errors:
        err(f"发现 {errors} 个错误" + (f"，另有 {warns} 项提醒" if warns else ""))
        return 1
    if warns:
        warn(f"发现 {warns} 项提醒（不影响运行，按需处理）")
        return 0
    info("体检通过，未发现问题")
    return 0


def cmd_uninstall(args):
    warn("此操作仅删除容器，不会删除 ./data 中的数据与密钥")
    ans = input("确认删除容器? [y/N] ").strip().lower()
    if ans != "y":
        print("已取消")
        return
    run(COMPOSE + ["down"])
    info("容器已删除。数据仍在 ./data")


def usage():
    print(__doc__)
    print("可用命令: build / start / stop / restart / status / ports / key / logs / "
          "backup / upgrade / doctor / uninstall")
    print("  build [--no-cache]  从本仓库源码构建服务端镜像（跳过缓存用 --no-cache）")
    print()
    print("提示：改过源码或 Dockerfile 后必须重新 build ——")
    print("      docker compose up（不带 --build）发现有同 tag 镜像会静默复用，不会重建。")


def main():
    if len(sys.argv) < 2:
        usage()
        sys.exit(0)
    c = sys.argv[1].lower()
    extra = sys.argv[2:]
    table = {
        "build": cmd_build,
        "start": cmd_start,
        "up": cmd_start,
        "stop": cmd_stop,
        "down": cmd_stop,
        "restart": cmd_restart,
        "status": cmd_status,
        "ports": cmd_ports,
        "key": cmd_key,
        "logs": cmd_logs,
        "backup": cmd_backup,
        "upgrade": cmd_upgrade,
        "doctor": cmd_doctor,
        "check": cmd_doctor,
        "uninstall": cmd_uninstall,
    }
    fn = table.get(c)
    if not fn:
        usage()
        sys.exit(1)
    # doctor 用返回码表达"有没有问题"，便于脚本/CI 判断
    rc = fn(extra)
    sys.exit(rc if isinstance(rc, int) else 0)


if __name__ == "__main__":
    main()
