"""检索指标的离线测试：指标本身必须先可信，才能去评系统。

python tests/test_retrieval_metrics.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from evals.retrieval import ndcg_at_k, precision_at_1, recall_at_k


def test_recall_and_precision_basics():
    assert recall_at_k(["a", "b", "c"], ["b"], 3) == 1.0
    assert recall_at_k(["a", "b", "c"], ["d"], 3) == 0.0
    assert recall_at_k(["a"], ["b", "c"], 3) == 0.0
    assert precision_at_1(["a", "b"], ["a"]) == 1.0
    assert precision_at_1(["x", "a"], ["a"]) == 0.0
    assert precision_at_1([], ["a"]) == 0.0


def test_ndcg_is_normalized_and_order_sensitive():
    relevant = ["a"]
    perfect = ndcg_at_k(["a", "x", "y"], relevant, 3)
    second = ndcg_at_k(["x", "a", "y"], relevant, 3)
    miss = ndcg_at_k(["x", "y", "z"], relevant, 3)
    assert perfect == 1.0, "第一名命中理想位置时 nDCG 必须是 1"
    assert 0 < second < 1.0, f"排到第二名的折价应在 0-1 之间，实际 {second}"
    assert miss == 0.0
    # 多相关文档：三个都相关时，任意排列的 nDCG 都应该是 1——
    # 我第一版断言写成「换序就要 <1」，那是错的：增益全为 1 时排列不改变 DCG。
    many = ["a", "b", "c"]
    assert ndcg_at_k(["a", "b", "c"], many, 3) == 1.0
    assert ndcg_at_k(["b", "a", "c"], many, 3) == 1.0
    # 真正的退化是「相关文档被挤到后面 / 混进无关文档」
    assert ndcg_at_k(["x", "a", "b"], many, 3) < 1.0
    assert ndcg_at_k(["x", "y", "a"], many, 3) < ndcg_at_k(["x", "a", "y"], many, 3)
    # 关键回归：曾经因为 IDCG 排成升序，nDCG 算出 1.85 这种大于 1 的荒谬值
    assert all(ndcg_at_k(r, relevant, 3) <= 1.0 for r in (["a", "b", "c"], ["b", "a", "c"], ["a", "a", "a"]))


def test_rrf_fuse_prefers_consensus_and_respects_k():
    from travel.rag import rrf_fuse

    fused = rrf_fuse([["a", "b", "c"], ["a", "d", "e"]], k=2)
    assert list(fused)[:1] == ["a"], "两路都排第一的必须最靠前"
    assert len(fused) == 2
    assert list(fused)[1] in {"b", "d"}
    empty = rrf_fuse([[], []], k=3)
    assert empty == {}


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
