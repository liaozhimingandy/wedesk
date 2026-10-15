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
    python rdctl.py uninstall 停止并删除容器（保留 ./data）
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
DATA_DIR = HERE / "data"
BACKUP_DIR = HERE / "backup"
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


def cmd_ports(args):
    """打印当前端口规划，方便对照防火墙/安全组。"""
    check_env_legacy()
    ports = resolve_ports()
    print_ports(ports)
    check_ports(ports, fatal=False)


def image_exists(tag):
    rc, _, _ = run(["docker", "image", "inspect", tag])
    return rc == 0


def cmd_build(args):
    """用本仓库源码构建服务端镜像。

    docker-compose.yml 里写了 build 段（源码在 rustdesk-server/），
    所以镜像是本地编译出来的，与 rustdesk/rustdesk-server 官方镜像无关。
    首次构建会在容器内编译 musl 静态二进制，约 15-25 分钟。
    """
    check_docker_running()
    ensure_env_file()
    check_env_legacy()
    check_ports(resolve_ports(), fatal=True)
    image = compose_image_name()
    step(f"从源码构建镜像 {image} ...")
    info("首次构建较慢：容器内要编译 musl 静态二进制（约 15-25 分钟）")
    rc, out, errout = run(COMPOSE + ["build"])
    if rc != 0:
        err(f"镜像构建失败:\n{errout}")
        sys.exit(1)
    info(f"镜像 {image} 构建完成")


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
        step(f"本地没有镜像 {image}，先用本仓库源码构建（首次约 15-25 分钟）...")
        rc, out, errout = run(COMPOSE + ["build"])
        if rc != 0:
            err(f"镜像构建失败:\n{errout}")
            print("    也可以先手动构建看完整日志: build\\build-server-image.bat")
            sys.exit(1)
        info("镜像构建完成")

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
        print("    下一步：把这串公钥与你的服务器地址填入 ../build/custom.toml")
        print("            然后运行 ../build/customize.py 生成定制客户端源码")
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
    svc = ["hbbs"] if "hbbs" in args else (["hbrr"] if "hbrr" in args else ["hbbs", "hbbr"])
    svc = ["rd-hbbs"] if "hbbs" in args else (["rd-hbbr"] if "hbbr" in args else [])
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
    if n == 0:
        warn("没有可备份的文件（服务可能尚未运行过）")
        return
    info(f"已备份 {n} 个文件 -> {dest}")
    print("    密钥泄露风险提示：私钥 id_ed25519 一旦泄露，攻击者可伪造你的服务器")


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
    print("可用命令: build / start / stop / restart / status / ports / key / logs / backup / uninstall")


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
        "uninstall": cmd_uninstall,
    }
    fn = table.get(c)
    if not fn:
        usage()
        sys.exit(1)
    fn(extra)


if __name__ == "__main__":
    main()
