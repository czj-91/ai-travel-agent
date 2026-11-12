"""会话库清理的离线测试：用临时 sqlite，不碰真实数据库。

python tests/test_memory.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from travel.memory import apply_prune, plan_prune, thread_stats
from travel.memory import main as memory_main

DDL_CP = (
    "create table checkpoints (thread_id text, checkpoint_ns text, checkpoint_id text, "
    "parent_checkpoint_id text, type text, checkpoint blob, metadata blob)"
)
DDL_WR = (
    "create table writes (thread_id text, checkpoint_ns text, checkpoint_id text, task_id text, "
    "task_path text, idx integer, channel text, type text, value blob)"
)


def _seed(conn, threads):
    conn.execute(DDL_CP)
    conn.execute(DDL_WR)
    for tid in threads:
        for n in range(3):
            conn.execute(
                "insert into checkpoints (thread_id, checkpoint_id) values (?, ?)", (tid, f"{tid}-{n}")
            )
            conn.execute("insert into writes (thread_id, checkpoint_id) values (?, ?)", (tid, f"{tid}-{n}"))
    conn.commit()


def test_prune_keeps_recent_and_protects_other_prefixes():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as work:
        conn = sqlite3.connect(Path(work) / "cp.db")
        _seed(conn, ["eval-1", "eval-2", "eval-3", "cli-keep"])

        assert [t["thread_id"] for t in thread_stats(conn)] == ["cli-keep", "eval-3", "eval-2", "eval-1"]
        doomed = plan_prune(conn, prefixes=("eval-",), keep_recent=1)
        assert doomed == ["eval-2", "eval-1"], doomed

        result = apply_prune(conn, doomed, vacuum=False)
        assert result["threads"] == 2 and result["rows_deleted"] == 12
        left = {row[0] for row in conn.execute("select distinct thread_id from checkpoints")}
        assert left == {"cli-keep", "eval-3"}
        assert plan_prune(conn, prefixes=("eval-",), keep_recent=1) == []
        conn.close()


def test_prune_zero_orphans_is_noop():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as work:
        conn = sqlite3.connect(Path(work) / "cp.db")
        _seed(conn, ["cli-a"])
        assert plan_prune(conn, prefixes=("eval-",), keep_recent=0) == []
        assert apply_prune(conn, [], vacuum=False) == {"threads": 0, "rows_deleted": 0, "vacuumed": False}
        conn.close()


def test_dry_run_writes_nothing():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as work:
        db = Path(work) / "cp.db"
        conn = sqlite3.connect(db)
        _seed(conn, ["eval-a", "eval-b", "eval-c"])
        conn.close()

        assert memory_main(["--db", str(db), "--keep-recent", "1"]) == 0
        conn = sqlite3.connect(db)
        assert len(thread_stats(conn)) == 3, "dry-run 不该删数据"
        conn.close()

        assert memory_main(["--db", str(db), "--keep-recent", "1", "--apply", "--no-vacuum"]) == 0
        conn = sqlite3.connect(db)
        assert len(thread_stats(conn)) == 1, "保留最近 1 个，删掉另外 2 个"
        conn.close()


def test_missing_db_returns_error_code():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as work:
        assert memory_main(["--db", str(Path(work) / "nope.db")]) == 1


if __name__ == "__main__":
    tests = [(n, fn) for n, fn in sorted(globals().items()) if n.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as exc:
            failed += 1
            print(f"  FAIL  {name}: {type(exc).__name__}: {str(exc)[:160]}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    raise SystemExit(1 if failed else 0)
