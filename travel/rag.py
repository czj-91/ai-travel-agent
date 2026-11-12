"""本地 POI 知识库（RAG）：向量召回 + 关键词兜底 + 时效性提示。

设计要点
1. 混合召回：优先用百炼 text-embedding-v4 做语义召回；embedding 不可用（离线/无 key/
   接口异常）时自动降级为中文 bigram 关键词打分，保证功能不中断、且可离线单测。
2. 向量缓存：按文本 sha1 缓存到 resources/poi_vectors.json，重复构建不再调用接口。
3. 时效性：每条记录带 verified_at，超过 TTL 就显式提示「可能已过期，建议 web_search
   复核」——知识库不能当成静态真理。
4. 输出裁剪：只回给模型决策需要的几个字段，控制进入上下文的 token。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from langchain.tools import tool

from .security import wrap_untrusted

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_PATH = BASE_DIR / "data" / "pois.jsonl"
CACHE_PATH = BASE_DIR / "resources" / "poi_vectors.json"
EMBED_MODEL = "text-embedding-v4"
EMBED_BATCH = 10
EMBED_DIM = 512
DEFAULT_TTL_DAYS = 180
# 阈值来自本语料的实测分布：真实命中 top1 落在 0.60-0.70，跑题查询（火星基地/上海迪士尼）
# 落在 0.41-0.48，取 0.55 干净分开。换语料必须重新标定，不能照搬。
MIN_SCORE_EMBEDDING = 0.55
# 降级路径是词面（bigram）匹配，精度天然低于语义召回，只用于离线或无 embedding key
# 的场景；它无法区分「火星基地」和「大熊猫繁育研究基地」这类词面重合，
# 因此只保证 top1 相关性，不保证能拦住跑题查询。
MIN_SCORE_KEYWORD = 0.25

# 单独使用本模块时也能读到 .env，不依赖调用方先 import config
load_dotenv(BASE_DIR / ".env")


def _clip(text: str, limit: int) -> str:
    return (text or "").strip()[:limit]


def record_text(row: dict) -> str:
    """拼出用于 embedding / 关键词匹配的检索文本。"""
    parts = [
        row["name"],
        row["city"],
        row["category"],
        row["highlights"],
        row["booking"],
        row["closed_weekday"],
    ]
    return " | ".join(p for p in parts if p)


def _bigrams(text: str) -> set[str]:
    cleaned = re.sub(r"\s+", "", text or "")
    return {cleaned[i : i + 2] for i in range(len(cleaned) - 1)}


def rrf_fuse(ranked_lists: list[list[str]], k: int, weight: float = 60.0) -> dict[str, float]:
    """Reciprocal Rank Fusion：只按名次融合，不需要归一化两路分数。

    向量余弦和 bigram 重合度量纲完全不同，硬加权求和要先调一堆系数；
    RRF 只用名次，天然免调参，是混合检索的常规做法。
    """
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for position, doc_id in enumerate(ranked, 1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (weight + position)
    return dict(sorted(scores.items(), key=lambda item: item[1], reverse=True)[:k])


def keyword_score(query: str, row: dict) -> float:
    """中文 bigram 重合度 + 名称/城市命中加权。纯函数，可离线测。"""
    q, d = _bigrams(query), _bigrams(record_text(row))
    base = len(q & d) / max(1, len(q))
    name = row["name"]
    if name and (name in query or any(seg in query for seg in re.split(r"[（(、/]", name) if len(seg) >= 3)):
        base += 1.0
    if row["city"] and row["city"] in query:
        base += 0.5
    return round(base, 4)


def _sqrt(x: float) -> float:
    return x**0.5


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na, nb = _sqrt(sum(x * x for x in a)), _sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def days_since(iso_date: str, today: str | None = None) -> int:
    try:
        d = date.fromisoformat(iso_date)
    except ValueError:
        return 10_000
    now = date.fromisoformat(today) if today else date.today()
    return (now - d).days


class PoiIndex:
    def __init__(
        self, ttl_days: int = DEFAULT_TTL_DAYS, use_embeddings: bool | None = None, mode: str = "auto"
    ):
        self.rows = [
            json.loads(line) for line in DATA_PATH.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        self.ttl_days = ttl_days
        self.mode = mode
        self.vectors: dict[str, list[float]] = {}
        self._query_cache: dict[str, list[float]] = {}
        self.embed_backend = "keyword"
        want = use_embeddings if use_embeddings is not None else bool(os.getenv("DASHSCOPE_API_KEY"))
        if want:
            try:
                self._load_vectors()
                self.embed_backend = "embedding"
            except Exception as exc:
                print(f"[rag] embedding 不可用，降级为关键词召回: {type(exc).__name__}: {str(exc)[:120]}")
                self.embed_backend = "keyword"

    @staticmethod
    def _key(text: str) -> str:
        return hashlib.sha1(text.encode("utf-8")).hexdigest()

    def _load_vectors(self) -> None:
        cache: dict[str, list[float]] = {}
        if CACHE_PATH.exists():
            cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        live = {self._key(record_text(row)) for row in self.rows}
        # 语料增删后旧向量只会让缓存文件越长越大，写回前先丢掉不再引用的条目
        cache = {key: value for key, value in cache.items() if key in live}
        missing = [r for r in self.rows if self._key(record_text(r)) not in cache]
        if missing:
            vectors = self._embed([record_text(r) for r in missing])
            for row, vec in zip(missing, vectors, strict=False):
                cache[self._key(record_text(row))] = vec
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        self.vectors = cache

    @staticmethod
    def _embed(texts: list[str], batch: int = EMBED_BATCH) -> list[list[float]]:
        """百炼 embedding 接口有 batch 上限（超过会 400 InvalidParameter），必须分批。"""
        from openai import OpenAI

        client = OpenAI(api_key=os.environ["DASHSCOPE_API_KEY"], base_url=os.environ["DASHSCOPE_BASE_URL"])
        vectors: list[list[float]] = []
        for start in range(0, len(texts), batch):
            resp = client.embeddings.create(
                model=EMBED_MODEL, input=texts[start : start + batch], dimensions=EMBED_DIM
            )
            vectors.extend(item.embedding for item in sorted(resp.data, key=lambda d: d.index))
        return vectors

    def _query_vector(self, query: str) -> list[float] | None:
        if self.embed_backend != "embedding":
            return None
        cached = self._query_cache.get(query)
        if cached is not None:
            return cached
        try:
            vector = self._embed([query])[0]
        except Exception:
            return None
        # 一轮规划里模型常用几乎相同的检索词问两次，命中缓存就少打一次 embedding 接口
        self._query_cache[query] = vector
        return vector

    def search(
        self, query: str, city: str = "", top_k: int = 3, min_score: float | None = None
    ) -> list[dict]:
        """单路召回（embedding 或 keyword），行为与历史一致。"""
        """召回 POI。低于阈值的条目必须丢弃：把无关条目喂给模型，
        既污染上下文，又会让模型以为已经查到、从而不再调用 web_search。
        """
        qv = self._query_vector(query)
        floor = (
            min_score
            if min_score is not None
            else (MIN_SCORE_EMBEDDING if qv is not None else MIN_SCORE_KEYWORD)
        )
        hits: list[dict] = []
        for row in self.rows:
            if city and row["city"] != city:
                continue
            if qv is not None:
                score = cosine(qv, self.vectors[self._key(record_text(row))])
            else:
                score = keyword_score(query, row)
            if score < floor:
                continue
            age = days_since(row.get("verified_at", ""))
            hits.append({"row": row, "score": round(score, 4), "stale": age > self.ttl_days, "age_days": age})
        hits.sort(key=lambda h: h["score"], reverse=True)
        return hits[:top_k]

    def search_hybrid(
        self, query: str, city: str = "", top_k: int = 3, min_score: float | None = None
    ) -> list[dict]:
        """混合召回：向量与关键词各出一张榜单，再用 RRF 融合名次。

        与 `search` 的区别不是「分数相加」，而是**只用名次**——两路分数量纲不可比，
        硬加权会逼出一堆系数；RRF 免调参，也避免关键词命中一个词就压过语义结果。
        """
        floor = min_score if min_score is not None else MIN_SCORE_EMBEDDING
        vector_hits = self.search(query, city=city, top_k=top_k * 3, min_score=floor)
        keyword_hits = self.search(query, city=city, top_k=top_k * 3, min_score=MIN_SCORE_KEYWORD)
        if not vector_hits and not keyword_hits:
            return []

        fused = rrf_fuse(
            [[h["row"]["id"] for h in vector_hits], [h["row"]["id"] for h in keyword_hits]],
            top_k,
        )
        by_id = {h["row"]["id"]: h for h in vector_hits + keyword_hits}
        merged = []
        for rank, doc_id in enumerate(fused, 1):
            hit = dict(by_id[doc_id])
            hit["score"] = round(fused[doc_id], 5)
            hit["fused_rank"] = rank
            hit["in_vector"] = doc_id in {h["row"]["id"] for h in vector_hits}
            hit["in_keyword"] = doc_id in {h["row"]["id"] for h in keyword_hits}
            merged.append(hit)
        return merged

    def format_hits(self, hits: list[dict]) -> str:
        """工具输出直接进入模型上下文，必须紧凑：字段截断、一条一行、去掉冗余标签。"""
        lines: list[str] = []
        for h in hits:
            r = h["row"]
            closed = f"；周{r['closed_weekday'][-1]}闭馆" if r.get("closed_weekday") else ""
            booking = _clip(r.get("booking", ""), 46)
            highlights = _clip(r.get("highlights", ""), 34)
            ticket = r.get("ticket_yuan")
            ticket_text = f"{ticket:.0f}元" if isinstance(ticket, (int, float)) else "门票待核实"
            lines.append(
                f"- {r.get('name', '未知景点')}｜{ticket_text}｜{r.get('open_hours', '开放时间待核实')}{closed}"
                f"｜{booking}｜{highlights}｜{r.get('source', '')}"
            )
            if h["stale"]:
                lines.append(
                    f"  ⚠ 核实于 {r.get('verified_at', '未知')}（{h['age_days']} 天前），可能过期，需 web_search 复核"
                )
        return "\n".join(lines) if lines else "本地 POI 库没有匹配结果。"


def build_poi_tool(index: PoiIndex | None = None, top_k: int = 2):
    idx = index or PoiIndex()

    @tool
    def poi_lookup(query: str, city: str = "") -> str:
        """查本地权威 POI 知识库：门票价格、开放时间、闭馆日、预约要求。

        规划行程时应优先用它，比联网搜索更准更省 token。可按景点名或主题查询。

        Args:
            query: 景点名或主题词，例如 "兵马俑 预约" 或 "博物馆 周一闭馆"
            city: 可选，限定城市，例如 "西安"
        """
        return wrap_untrusted(idx.format_hits(idx.search(query, city=city, top_k=top_k)), "local-poi-kb")

    return poi_lookup
