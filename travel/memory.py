"""会话库维护：评测与调试产生的 thread 会把 checkpointer 越堆越大，必须能清理。

实测本机 `resources/checkpoint.db` 长到 30.8 MB、146 个 thread，其中 **135 个是 eval-* 评测会话**——
也就是说绝大部分体积是跑分垃圾数据，不是用户会话。

为什么不用时间判断新旧：`checkpoint_id` 前 48 位实测是 3.42e13（换算成年份是 3082 年），
**不是** Unix 毫秒时间戳，所以按时间清理会算错。这里改用 SQLite `rowid` 作为插入顺序的代理，
可靠且可单测。
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "resources" / "checkpoint.db"
TABLES = ("checkpoints", "writes")


def thread_stats(conn: sqlite3.Connection) -> list[dict]:
    """每个 thread 的行数与最近插入位置（rowid 越大越新）。"""
    rows = conn.execute(
        "select thread_id, count(*), max(rowid) from checkpoints group by thread_id"
    ).fetchall()
    stats = [{"thread_id": tid, "checkpoints": n, "recency": rec} for tid, n, rec in rows]
    return sorted(stats, key=lambda item: item["recency"], reverse=True)


def plan_prune(
    conn: sqlite3.Connection,
    prefixes: tuple[str, ...] = ("eval-",),
    keep_recent: int = 2,
) -> list[str]:
    """返回待删除的 thread_id：只动指定前缀，且每个前缀保留最近 keep_recent 个。"""
    doomed: list[str] = []
    for prefix in prefixes:
        matched = [item for item in thread_stats(conn) if item["thread_id"].startswith(prefix)]
        doomed.extend(item["thread_id"] for item in matched[keep_recent:])
    return doomed


def apply_prune(conn: sqlite3.Connection, thread_ids: list[str], vacuum: bool = True) -> dict:
    if not thread_ids:
        return {"threads": 0, "rows_deleted": 0, "vacuumed": False}
    placeholders = ",".join("?" * len(thread_ids))
    deleted = 0
    for table in TABLES:
        deleted += conn.execute(
            f"delete from {table} where thread_id in ({placeholders})", thread_ids
        ).rowcount
    conn.commit()
    if vacuum:
        conn.execute("vacuum")
    return {"threads": len(thread_ids), "rows_deleted": deleted, "vacuumed": vacuum}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清理 checkpointer 里的评测/调试会话")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--prefix", action="append", dest="prefixes", help="要清理的 thread 前缀，可多次")
    parser.add_argument("--keep-recent", type=int, default=2, help="每个前缀保留最近几个 thread")
    parser.add_argument("--apply", action="store_true", help="真正删除（默认只 dry-run）")
    parser.add_argument("--no-vacuum", action="store_true", help="删除后不回收空间（更快，但文件不缩小）")
    args = parser.parse_args(argv)

    db = Path(args.db)
    if not db.is_file():
        print(f"数据库不存在：{db}")
        return 1
    prefixes = tuple(args.prefixes) if args.prefixes else ("eval-",)

    conn = sqlite3.connect(db)
    try:
        before = conn.execute("select count(*) from checkpoints").fetchone()[0]
        doomed = plan_prune(conn, prefixes=prefixes, keep_recent=args.keep_recent)
        print(f"数据库 = {db}")
        print(f"前缀 = {prefixes} | 每前缀保留最近 {args.keep_recent} 个")
        print(f"当前 checkpoints 行数 = {before} | thread 总数 = {len(thread_stats(conn))}")
        print(f"命中待清理 = {len(doomed)} 个 thread")
        if not doomed:
            print("无需清理。")
            return 0
        if not args.apply:
            print("dry-run：加 --apply 才会真的删除。示例：")
            for tid in doomed[:5]:
                print(f"   {tid}")
            if len(doomed) > 5:
                print(f"   ...共 {len(doomed)} 个")
            return 0
        result = apply_prune(conn, doomed, vacuum=not args.no_vacuum)
        after = conn.execute("select count(*) from checkpoints").fetchone()[0]
        print(
            f"已删除 {result['threads']} 个 thread / {result['rows_deleted']} 行，剩余 {after} 行，"
            f"{'已 VACUUM 回收空间' if result['vacuumed'] else '未回收空间'}"
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
