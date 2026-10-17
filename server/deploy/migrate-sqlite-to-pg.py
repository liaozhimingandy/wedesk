#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
SQLite -> PostgreSQL 迁移工具（把 hbbs 里已有的 ID/设备数据搬进 Postgres）

为什么要这个脚本
----------------
hbbs 原本把 ID、公钥、设备信息都存在 db_v2.sqlite3（SQLite）。
切到 PostgreSQL 后，存量数据必须一并搬过去，否则客户端会用旧 ID 连不上 /
服务端会给同一台机器重新分配一个新 ID。

设计约束（与本项目一致）
------------------------
- 零第三方依赖：只用标准库 sqlite3 生成 SQL 文本。
  PostgreSQL 侧通过已有的 psql 执行（docker 里就有），不需要 psycopg2。
- 幂等安全：只用 INSERT ... ON CONFLICT DO NOTHING，重复执行不会产生脏数据。
- 二进制字段（blob）用 PostgreSQL 的 bytea 十六进制字面量 '\\x...' 表达，
  不用靠八进制转义，避免 0x00/非 ASCII 字节被口头/FTP 之类的环节破坏。

用法
----
  # 1) 先生成 SQL 文件，人工检查后再灌入
  python migrate-sqlite-to-pg.py db_v2.sqlite3 --out migration.sql

  # 2) 直接灌进 docker compose 里跑着的 postgres
  docker compose exec -T postgres psql -U rustdesk -d rustdesk < migration.sql

  # 3) 或让脚本一次做完：
  python migrate-sqlite-to-pg.py db_v2.sqlite3 \
      --psql "docker compose exec -T postgres psql -U rustdesk -d rustdesk"
"""

import argparse
import binascii
import sqlite3
import subprocess
import sys
from pathlib import Path

# 与 src/database.rs 里 PG_DDL_STATEMENTS / SQLite 建表语句保持一致
# 注意 "user" 在 PostgreSQL 里是保留字，必须加双引号
PG_COLUMNS = ["guid", "id", "uuid", "pk", "created_at", "user", "status", "note", "info"]


def sql_str(value: str) -> str:
    """转义字符串字面量：PostgreSQL 里单引号翻倍即可，不要用反斜杠转义。"""
    return "'" + value.replace("'", "''") + "'"


def sql_bytea(value: bytes | None) -> str:
    """二进制字段 -> bytea 十六进制字面量。NULL 原样保留。"""
    if value is None:
        return "NULL"
    return "'\\x" + binascii.hexlify(value).decode("ascii") + "'"


def sql_int(value) -> str:
    return "NULL" if value is None else str(int(value))


def quote_ident(name: str) -> str:
    """PostgreSQL 标识符加双引号。user / status 这类保留字或半保留字需要它。"""
    return '"' + name.replace('"', '""') + '"'


def build_copy_sql(source_path: Path) -> tuple[str, int]:
    """读取 SQLite 的 peer 表，生成整段 INSERT 语句与行数。"""
    if not source_path.exists():
        sys.exit(f"[x] 找不到 SQLite 库：{source_path}")

    try:
        con = sqlite3.connect(str(source_path))
        tables = {
            r[0]
            for r in con.execute("select name from sqlite_master where type='table'")
        }
    except sqlite3.DatabaseError as e:
        sys.exit(f"[x] {source_path} 打不开或不是 SQLite 库：{e}")

    if "peer" not in tables:
        sys.exit(f"[x] {source_path} 里没有 peer 表，看起来不是 hbbs 的库")
    con.row_factory = sqlite3.Row

    cols = ",".join(quote_ident(c) for c in PG_COLUMNS)
    rows = con.execute(f"select {','.join(PG_COLUMNS)} from peer").fetchall()
    total = len(rows)

    out = [
        "-- 由 migrate-sqlite-to-pg.py 生成，来源：" + str(source_path),
        "-- ON CONFLICT DO NOTHING 保证可重复执行；主键冲突的行会被跳过（通常不是问题，",
        "-- 说明那台设备已经在 PG 里了）",
        "begin;",
    ]
    for r in rows:
        # created_at 直接当字符串交给 PostgreSQL 的 timestamptz 解析，
        # 兼容 SQLite 的 'YYYY-MM-DD HH:MM:SS' 写法
        created = r["created_at"]
        created_sql = "NULL" if created is None else sql_str(str(created))
        note = r["note"]
        note_sql = "NULL" if note is None else sql_str(str(note))
        vals = ",".join(
            [
                sql_bytea(r["guid"]),
                sql_str(str(r["id"])),
                sql_bytea(r["uuid"]),
                sql_bytea(r["pk"]),
                created_sql,
                sql_bytea(r["user"]),
                sql_int(r["status"]),
                note_sql,
                sql_str(str(r["info"])),
            ]
        )
        out.append(
            f"insert into peer ({cols}) values ({vals}) on conflict (guid) do nothing;"
        )
    out.append("commit;")
    con.close()
    return "\n".join(out) + "\n", total


def main() -> None:
    ap = argparse.ArgumentParser(description="把 hbbs 的 SQLite 数据迁到 PostgreSQL")
    ap.add_argument("sqlite_db", help="源 SQLite 文件路径，如 ./data/db_v2.sqlite3")
    ap.add_argument("--out", help="输出 SQL 文件路径；不指定则打印到标准输出")
    ap.add_argument(
        "--psql",
        help="直接执行的 psql 命令，例如 "
        '"docker compose exec -T postgres psql -U rustdesk -d rustdesk"',
    )
    args = ap.parse_args()

    sql, total = build_copy_sql(Path(args.sqlite_db))
    print(f"[i] 源库有 {total} 条 peer 记录", file=sys.stderr)
    if total == 0:
        print("[!] 没有数据可迁移，跳过", file=sys.stderr)
        return

    if args.out:
        Path(args.out).write_text(sql, encoding="utf-8")
        print(f"[OK] 已生成 {args.out}（{total} 条），请复核后灌入 PostgreSQL", file=sys.stderr)
        return

    if args.psql:
        print(f"[i] 执行：{args.psql}", file=sys.stderr)
        p = subprocess.run(args.psql, shell=True, input=sql.encode("utf-8"))
        if p.returncode != 0:
            sys.exit(f"[x] psql 执行失败，退出码 {p.returncode}")
        print(f"[OK] 已迁移 {total} 条记录", file=sys.stderr)
        return

    sys.stdout.write(sql)


if __name__ == "__main__":
    main()
