"""LLM-as-judge：让第二个模型按固定维度给行程打分。

规则检查（travel/eval.py）只能抓结构性问题，抓不出「这行程根本走不完」这种语义问题；
但 judge 本身也会错，所以这里坚持三件事：
1. 维度、分值区间、输出结构全部写死在 schema 里，不给自由发挥空间；
2. 只做**相对比较**和趋势监控，不把它当唯一结论；
3. 汇总逻辑是纯函数，可以离线单测。
"""

from __future__ import annotations

from collections import Counter

from pydantic import BaseModel, Field

DIMENSIONS = ("需求覆盖", "可执行性", "事实可靠性", "预算合理性", "节奏合理性")
JUDGE_SYSTEM = """
你是严格的行程评审。只按给定维度打分，不要夸奖，不要补充建议。
每一项给 1-5 分（5 最好），并用一句话说明扣分原因；没有明显问题也要写清依据来自材料哪一处。
判断只能依据提供的用户需求和行程 JSON，不得引入外部知识去断言门票价格等具体数字。
"""


class DimensionScore(BaseModel):
    name: str = Field(description=f"维度名，必须是 {DIMENSIONS} 之一")
    score: int = Field(ge=1, le=5, description="1-5 分")
    reason: str = Field(description="一句话依据")


class JudgeVerdict(BaseModel):
    dimensions: list[DimensionScore] = Field(description="五个维度各自的评分")
    blocking_issues: list[str] = Field(
        default_factory=list, description="会让行程无法执行的硬问题，没有就留空"
    )
    overall: int = Field(ge=1, le=5, description="综合分")


def average_score(verdict: JudgeVerdict | None) -> float | None:
    if not verdict or not verdict.dimensions:
        return None
    return round(sum(item.score for item in verdict.dimensions) / len(verdict.dimensions), 2)


def aggregate_verdicts(verdicts: list[JudgeVerdict | None]) -> dict:
    """汇总多份评分：维度均分、最低维度、硬问题分布、判分失败数。"""
    per_dim: dict[str, list[int]] = {}
    blockers: Counter[str] = Counter()
    scored = 0
    for verdict in verdicts:
        if verdict is None:
            continue
        scored += 1
        for item in verdict.dimensions:
            per_dim.setdefault(item.name, []).append(item.score)
        for issue in verdict.blocking_issues:
            blockers[issue.strip()] += 1

    dims = {name: round(sum(vals) / len(vals), 2) for name, vals in per_dim.items()}
    weakest = min(dims, key=dims.get) if dims else None
    return {
        "scored": scored,
        "failed": len(verdicts) - scored,
        "dimension_avg": dims,
        "weakest_dimension": weakest,
        "blocking_issues": dict(blockers.most_common()),
        "overall_avg": round(
            sum(average_score(v) for v in verdicts if average_score(v) is not None)
            / max(1, sum(1 for v in verdicts if average_score(v) is not None)),
            2,
        )
        if scored
        else None,
    }


def build_judge(model_name: str = "deepseek-chat"):
    from langchain.chat_models import init_chat_model

    model = init_chat_model(model_name).with_structured_output(JudgeVerdict)

    def judge(request: str, plan_json: str) -> JudgeVerdict | None:
        from langchain_core.messages import HumanMessage, SystemMessage

        try:
            return model.invoke(
                [
                    SystemMessage(content=JUDGE_SYSTEM),
                    HumanMessage(content=f"用户需求：\n{request}\n\n行程 JSON：\n{plan_json}"),
                ]
            )
        except Exception:
            return None

    return judge


def _name_similarity(a: str, b: str) -> float:
    """去括号后的中文 bigram Jaccard，用来判断「是不是同一个对象」。"""
    import re

    def norm(text: str) -> str:
        return re.sub(r"[（(].*?[)）]", "", text or "")

    x, y = norm(a), norm(b)
    gx = {x[i : i + 2] for i in range(len(x) - 1)}
    gy = {y[i : i + 2] for i in range(len(y) - 1)}
    if not gx or not gy:
        return 0.0
    return len(gx & gy) / len(gx | gy)


# 这些限定词说明行程写的是「外围/路过/免费视角」，本来就不该等于景区门票价
FREE_VIEW_HINTS = ("外围", "机位", "漫步", "巷弄", "外观", "远眺", "广场", "街")
MIN_NAME_SIMILARITY = 0.55


def fact_check_vs_kb(plan, index, tolerance: float = 1.0) -> list[str]:
    """用本地 POI 知识库做参照，挑出**同一个对象**上对不上的门票价。

    第一版只按「名字里含关键词」匹配，结果把「锦里 / 武侯祠与锦里」
    「三塔倒影公园（外围机位） / 崇圣寺三塔文化旅游区」当成同一个对象，
    报出 5 条不一致、其中 4 条是误报。核对器说谎比没有核对器更糟，
    所以现在要求：名称相似度达标 + 不是外围视角，才参与判定。

    仍然只是「与知识库一致率」，不是绝对真值。
    """
    mismatches: list[str] = []
    for spot in plan.flattened_spots():
        if any(hint in spot.name for hint in FREE_VIEW_HINTS):
            continue
        hits = index.search(spot.name, top_k=1, min_score=0.4)
        if not hits:
            continue
        ref = hits[0]["row"]
        if _name_similarity(spot.name, ref["name"]) < MIN_NAME_SIMILARITY:
            continue
        if abs(float(spot.ticket_yuan) - float(ref["ticket_yuan"])) > tolerance:
            mismatches.append(f"{spot.name}：行程写 {spot.ticket_yuan} 元，知识库写 {ref['ticket_yuan']} 元")
    return mismatches
