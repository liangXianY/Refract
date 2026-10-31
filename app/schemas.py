"""数据契约。ContentBrief 是全项目的中心抽象：

三种输入（需求 / 文案 / 链接）都先归一化成它，下游所有能力只认它。
schema 一旦定死就别随便改，否则每条链路都要跟着返工。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Platform = Literal["wechat", "xiaohongshu", "video"]
RepairAction = Literal["pass", "retry", "refine", "replan"]


class Section(BaseModel):
    """大纲的一节。"""

    heading: str = Field(min_length=1, max_length=60)
    points: list[str] = Field(default_factory=list, max_length=8)


class Fact(BaseModel):
    """事实点。source 为空表示没找到出处，前端会标黄——这是防幻觉的第一道闸。"""

    claim: str
    source: str | None = None

    @property
    def verified(self) -> bool:
        return bool(self.source)


class ContentBrief(BaseModel):
    """统一内容表征。"""

    topic: str = Field(min_length=1, max_length=80)
    angle: str = Field(default="", max_length=120)
    audience: str = Field(default="普通读者", max_length=60)
    platform: Platform = "wechat"
    tone: str = Field(default="专业但不生硬", max_length=60)
    word_limit: int = Field(default=1200, ge=200, le=6000)
    hook: str = Field(default="", max_length=200)
    outline: list[Section] = Field(min_length=1, max_length=10)
    facts: list[Fact] = Field(default_factory=list)
    cta: str = Field(default="", max_length=200)

    @field_validator("outline")
    @classmethod
    def _need_sections(cls, v: list[Section]) -> list[Section]:
        if not v:
            raise ValueError("outline 不能为空")
        return v

    @property
    def unverified_ratio(self) -> float:
        if not self.facts:
            return 0.0
        return sum(1 for f in self.facts if not f.verified) / len(self.facts)


class ScoreItem(BaseModel):
    dim: str
    score: int = Field(ge=0, le=100)
    comment: str = ""


class Reflection(BaseModel):
    """反思结果。repair_action 三档决定下一步走哪条路，不是随便重试。"""

    scores: dict[str, int] = Field(default_factory=dict)
    total: float = 0.0
    issues: list[str] = Field(default_factory=list)
    repair_action: RepairAction = "pass"
    round: int = 1

    def recompute_total(self) -> float:
        if not self.scores:
            self.total = 0.0
        else:
            self.total = round(sum(self.scores.values()) / len(self.scores), 1)
        return self.total


class ComposeResult(BaseModel):
    """一次执行（Executor）的产物。"""

    title: str
    body: str
    word_count: int = 0
    elapsed_ms: int = 0
    tokens: int = 0
    model: str = ""
    mocked: bool = False


class TaskView(BaseModel):
    """给前端看的任务快照。"""

    id: str
    status: Literal["queued", "running", "done", "failed"]
    stage: str = ""
    error: str | None = None
