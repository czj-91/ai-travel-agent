"""检索质量指标：Recall@k / Precision@1 / nDCG@k，对比单路与混合召回。

没有这些数字，「RAG 做得好不好」就只能靠感觉。标注集是 evals/retrieval_labels.jsonl
（20 条查询，人工标注相关 POI id），规模小，只能看方向、不能当基准。

    python evals/retrieval.py
"""

from __future__ import annotations

import json
import math
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from travel.rag import PoiIndex

LABELS = os.path.join(os.path.dirname(__file__), "retrieval_labels.jsonl")


def load_labels() -> list[dict]:
    with open(LABELS, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def recall_at_k(retrieved: list[str], relevant: list[str], k: int) -> float:
    if not relevant:
        return 0.0
    return len(set(retrieved[:k]) & set(relevant)) / len(relevant)


def precision_at_1(retrieved: list[str], relevant: list[str]) -> float:
    if not retrieved:
        return 0.0
    return 1.0 if retrieved[0] in relevant else 0.0


def _dedupe(items: list[str]) -> list[str]:
    """指标必须在去重后计算：同一文档重复出现会让 nDCG 超过 1（实测 2.13），
    那是指标缺陷而不是检索效果。"""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def ndcg_at_k(retrieved: list[str], relevant: list[str], k: int) -> float:
    gains = [1.0 if doc in relevant else 0.0 for doc in _dedupe(retrieved)[:k]]
    dcg = sum(gain / math.log2(rank + 2) for rank, gain in enumerate(gains))
    # 必须降序：升序会把理想增益排到末尾，IDCG 变小、nDCG 虚高（实测能到 1.85，而 nDCG 上限是 1）
    ideal = sorted([1.0] * len(relevant) + [0.0] * max(0, k - len(relevant)), reverse=True)[:k]
    idcg = sum(gain / math.log2(rank + 2) for rank, gain in enumerate(ideal) if gain)
    return dcg / idcg if idcg else 0.0


def evaluate(retriever, labels: list[dict], k: int = 3) -> dict:
    hits = {"recall": [], "p1": [], "ndcg": []}
    for case in labels:
        got = [hit["row"]["id"] for hit in retriever(case["query"], case.get("city", ""), k)]
        relevant = case["relevant"]
        hits["recall"].append(recall_at_k(got, relevant, k))
        hits["p1"].append(precision_at_1(got, relevant))
        hits["ndcg"].append(ndcg_at_k(got, relevant, k))
    return {
        "k": k,
        "queries": len(labels),
        "recall": round(sum(hits["recall"]) / len(hits["recall"]), 3),
        "precision_at_1": round(sum(hits["p1"]) / len(hits["p1"]), 3),
        "ndcg": round(sum(hits["ndcg"]) / len(hits["ndcg"]), 3),
    }


def main() -> int:
    labels = load_labels()
    print(f"标注查询 = {len(labels)} 条")
    rows = []
    for name, retriever in (
        ("keyword", lambda q, c, k: PoiIndex(use_embeddings=False).search(q, city=c, top_k=k)),
        ("embedding", lambda q, c, k: PoiIndex().search(q, city=c, top_k=k)),
        ("hybrid(RRF)", lambda q, c, k: PoiIndex().search_hybrid(q, city=c, top_k=k)),
    ):
        try:
            metric = evaluate(retriever, labels)
        except Exception as exc:
            print(f"  {name} 跳过：{type(exc).__name__}: {str(exc)[:80]}")
            continue
        rows.append((name, metric))
        print(
            f"  {name:<12} recall@3={metric['recall']:.3f} p@1={metric['precision_at_1']:.3f} nDCG@3={metric['ndcg']:.3f}"
        )

    if len(rows) > 1:
        best = max(rows, key=lambda item: item[1]["ndcg"])
        print(f"\n按 nDCG@3，最优策略 = {best[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
