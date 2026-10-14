#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rdctl.py —— RustDesk 自建服务器跨平台管理工具

零第三方依赖，只用标准库。Windows / Linux / macOS 通用。

命令:
    python rdctl.py start     启动 hbbs + hbbr（首次自动等待并打印服务器公钥）
    python rdctl.py stop      停止并保留数据
    python rdctl.py restart   重启
    python rdctl.py status    查看容器状态与已注册设备概况
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


def cmd_start(args):
    check_docker_running()
    ensure_env_file()
    DATA_DIR.mkdir(exist_ok=True)

    step("拉取/更新镜像并启动容器 ...")
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
    print("可用命令: start / stop / restart / status / key / logs / backup / uninstall")


def main():
    if len(sys.argv) < 2:
        usage()
        sys.exit(0)
    c = sys.argv[1].lower()
    extra = sys.argv[2:]
    table = {
        "start": cmd_start,
        "up": cmd_start,
        "stop": cmd_stop,
        "down": cmd_stop,
        "restart": cmd_restart,
        "status": cmd_status,
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
