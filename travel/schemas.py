"""结构化输出契约：旅行计划的 schema。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class Spot(BaseModel):
    """一个游玩点。"""

    name: str = Field(description="景点/餐厅/街区名称")
    why: str = Field(description="为什么安排它，一句话")
    minutes: int = Field(ge=15, le=600, description="建议停留时长（分钟）")
    ticket_yuan: float = Field(ge=0, description="门票参考价，免费填 0")


class DayPlan(BaseModel):
    """某一天的安排。"""

    day: int = Field(ge=1, le=30, description="第几天，从 1 开始")
    theme: str = Field(description="当天主题，例如 西湖人文 / 城市美食")
    base_area: str = Field(description="当天主要活动区域，用于控制通勤距离")
    spots: list[Spot] = Field(min_length=2, description="当天游玩点，按游览顺序")
    meals: list[str] = Field(default_factory=list, description="当天推荐吃喝，写清店名或品类")
    transport: str = Field(description="当天通勤方式，例如 地铁+步行 / 打车约 20 元")
    day_cost_yuan: float = Field(ge=0, description="当天人均预估花费（不含住宿与往返大交通）")


class TripPlan(BaseModel):
    """给用户的完整行程方案。"""

    destination: str = Field(description="目的地")
    days: int = Field(ge=1, le=30, description="总天数")
    traveler_profile: str = Field(description="对用户需求的复述：人数、偏好、忌口、预算等")
    best_season_note: str = Field(default="", description="出行时间与天气/人流的提示")
    plan: list[DayPlan] = Field(description="按天排列的行程")
    budget_total_yuan: float = Field(ge=0, description="人均总预估花费（不含往返大交通）")
    risks: list[str] = Field(default_factory=list, description="需要提前注意的事，如预约、闭馆日、体力")
    references: list[str] = Field(description="参考来源链接；没有外部来源时留空列表")
    summary: str = Field(description="两三句话的总体建议")

    def flattened_spots(self) -> list[Spot]:
        return [spot for day in self.plan for spot in day.spots]