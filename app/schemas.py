from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)


def _normalize_tags(tags: list[str]) -> list[str]:
    """标签规范化：去空白、去重、排序；空标签视为非法。"""
    cleaned = sorted({t.strip() for t in tags})
    if any(not t for t in cleaned):
        raise ValueError("标签去空白后不能为空")
    if len(cleaned) > 32:
        raise ValueError("标签数量不能超过 32 个")
    if any(len(t) > 64 for t in cleaned):
        raise ValueError("单个标签长度不能超过 64 个字符")
    return cleaned


# 能力/必需标签列表：自动去空白、去重、排序
Tags = Annotated[list[str], AfterValidator(_normalize_tags)]


# ---------- Tournament ----------

class TournamentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255, description="赛事名称")


class TournamentResponse(BaseModel):
    id: int
    name: str

    model_config = {"from_attributes": True}


# ---------- Participant ----------

class ParticipantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255, description="参赛者姓名")
    required_tags: Tags = Field(
        default_factory=list, description="参赛者必需标签（工位须覆盖）"
    )


class ParticipantTagsUpdate(BaseModel):
    required_tags: Tags = Field(description="参赛者必需标签（整体替换）")


class ParticipantsBulkCreate(BaseModel):
    names: list[str] = Field(
        min_length=1,
        description="参赛者姓名列表，编号按此顺序从 1 开始分配",
    )


class ParticipantResponse(BaseModel):
    id: int
    number: int
    name: str
    required_tags: list[str] = []

    model_config = {"from_attributes": True}


# ---------- Match / Round ----------

class MatchResponse(BaseModel):
    id: int
    player_white: ParticipantResponse | None = None
    player_black: ParticipantResponse | None = None
    # 0 / 0.5 / 1；轮空方 1，另一方为 None
    score_white: float | None = None
    score_black: float | None = None
    is_bye: bool = False


class RoundResponse(BaseModel):
    id: int
    round_no: int
    status: str
    matches: list[MatchResponse] = []


class ResultUpdate(BaseModel):
    # 0 / 1 / 2 个半分，即允许 0、0.5、1
    score_white: float = Field(ge=0, le=1, description="小白方得分：0 / 0.5 / 1")
    score_black: float = Field(ge=0, le=1, description="小黑方得分：0 / 0.5 / 1")


# ---------- Standing ----------

class StandingResponse(BaseModel):
    rank: int
    participant_id: int
    number: int
    name: str
    points: float
    buchholz: float
    played: int
    byes: int


# ---------- Event ----------

class EventResponse(BaseModel):
    tournament_id: int
    # 赛事内单调递增序号（每赛事独立，从 1 开始）
    seq: int
    type: str
    # 相关资源标识（如 tournament_id / participant_id / round_id 等）
    payload: dict
    # UTC 发生时间，ISO 8601（Z 结尾）
    occurred_at: datetime

    model_config = {"from_attributes": True}

    @field_serializer("occurred_at")
    def serialize_occurred_at(self, v: datetime) -> str:
        # 存储为 naive UTC，序列化为带 Z 后缀的 ISO 8601
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class EventPageResponse(BaseModel):
    items: list[EventResponse]
    # 本页最后一条事件序号；本页为空时等于请求的 after
    last_seq: int
    # 之后是否还有更多事件
    has_more: bool


# ---------- Station ----------

class StationCreate(BaseModel):
    station_no: int = Field(ge=1, description="工位编号（赛事内唯一）")
    available: bool = Field(default=True, description="是否可用")
    tags: Tags = Field(default_factory=list, description="工位能力标签")


class StationsBulkCreate(BaseModel):
    stations: list[StationCreate] = Field(
        min_length=1, description="工位列表，全部成功才落库"
    )


class StationUpdate(BaseModel):
    available: bool = Field(description="是否可用")
    tags: Tags = Field(description="工位能力标签（整体替换）")


class StationResponse(BaseModel):
    id: int
    station_no: int
    available: bool
    tags: list[str]

    model_config = {"from_attributes": True}


# ---------- StationPlan ----------

class StationPin(BaseModel):
    """固定工位：指定某场非轮空对局必须使用的工位。"""

    match_id: int = Field(description="本轮非轮空对局 id")
    station_no: int = Field(description="工位编号")


class StationPlanCreate(BaseModel):
    expected_version: int = Field(
        ge=0,
        description="期望的当前最新版本号（尚无方案时为 0）；不一致返回 409",
    )
    pins: list[StationPin] = Field(
        default_factory=list, description="固定工位（必须全部满足）"
    )


class StationAssignmentResponse(BaseModel):
    match_id: int
    station_no: int


class UnassignedMatchResponse(BaseModel):
    match_id: int
    # 双方选手必需标签的并集
    required_tags: list[str]
    # 所有可用工位都不提供的标签
    missing_tags: list[str]
    # 能覆盖全部必需标签、但本方案中已被占用的工位编号
    conflicting_stations: list[int]
    # 人类可读的原因说明
    reason: str


class StationPlanResponse(BaseModel):
    id: int
    round_id: int
    round_no: int
    version: int
    assignments: list[StationAssignmentResponse]
    unassigned: list[UnassignedMatchResponse]
    # UTC 发布时间，ISO 8601（Z 结尾）
    created_at: datetime

    @field_serializer("created_at")
    def serialize_created_at(self, v: datetime) -> str:
        # 存储为 naive UTC，序列化为带 Z 后缀的 ISO 8601
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------- Announcement ----------

class AnnouncementStationPlanRule(BaseModel):
    """按工位方案圈定收件人：指定某轮某版方案中占用指定工位号的
    对局双方。"""

    round_no: int = Field(ge=1, description="工位方案所属轮次号")
    version: int = Field(ge=1, description="工位方案版本号")
    station_nos: list[int] = Field(
        min_length=1, description="工位号列表（占用这些工位的对局双方）"
    )

    @field_validator("station_nos")
    @classmethod
    def _normalize_station_nos(cls, v: list[int]) -> list[int]:
        # 去重排序，保留集合语义（规则为并集）
        return sorted(set(v))


class AnnouncementRule(BaseModel):
    """定向收件规则：三类圈定条件取并集，再应用排除名单。

    三类条件（至少指定一类）：
    - participant_ids：参赛者 id 列表；
    - rank_from/rank_to：当前排名闭区间（按 /standings 口径）；
    - station_plan：某轮某版工位方案中占用指定工位号的对局双方。
    以上并集去重后，排除 exclude_participant_ids 中的参赛者。
    """

    participant_ids: list[int] = Field(
        default_factory=list, description="直接指定的参赛者 id（并集）"
    )
    rank_from: int | None = Field(
        default=None, ge=1, description="当前排名区间起点（含）"
    )
    rank_to: int | None = Field(
        default=None, ge=1, description="当前排名区间终点（含）"
    )
    station_plan: AnnouncementStationPlanRule | None = Field(
        default=None, description="按某版工位方案工位号圈定"
    )
    exclude_participant_ids: list[int] = Field(
        default_factory=list, description="排除名单（并集后整体排除）"
    )

    @field_validator("participant_ids", "exclude_participant_ids")
    @classmethod
    def _normalize_ids(cls, v: list[int]) -> list[int]:
        return sorted(set(v))

    @model_validator(mode="after")
    def _check_rule_nonempty_and_rank(self):
        if not (
            self.participant_ids
            or self.rank_from is not None
            or self.rank_to is not None
            or self.station_plan is not None
        ):
            raise ValueError("收件规则为空：至少指定一类圈定条件")
        # 排名区间两个边界要么都给，要么都不给；给则起点不大于终点
        if (self.rank_from is None) != (self.rank_to is None):
            raise ValueError("排名区间必须同时提供 rank_from 与 rank_to")
        if (
            self.rank_from is not None
            and self.rank_to is not None
            and self.rank_from > self.rank_to
        ):
            raise ValueError("排名区间起点不能大于终点")
        return self


class AnnouncementCreate(BaseModel):
    request_id: str = Field(
        min_length=1, max_length=128, description="幂等键：重复提交返回首次结果"
    )
    expected_event_seq: int = Field(
        ge=0,
        description="期望的赛事当前事件序号；不一致（快照已变化）返回 409",
    )
    body: str = Field(min_length=1, description="公告正文")
    rule: AnnouncementRule = Field(description="定向收件规则")


class AnnouncementRevisionCreate(BaseModel):
    request_id: str = Field(
        min_length=1, max_length=128, description="幂等键：重复提交返回首次结果"
    )
    expected_event_seq: int = Field(
        ge=0,
        description="期望的赛事当前事件序号；不一致（快照已变化）返回 409",
    )
    body: str = Field(min_length=1, description="新版本正文")
    rule: AnnouncementRule = Field(description="新版本的定向收件规则（重新冻结）")


class AnnouncementVersionResponse(BaseModel):
    id: int
    version: int
    body: str
    rule: dict
    # 冻结收件人数
    recipient_count: int
    # 已回执人数
    acknowledged_count: int
    created_at: datetime

    @field_serializer("created_at")
    def serialize_created_at(self, v: datetime) -> str:
        return _serialize_utc(v)


class AnnouncementResponse(BaseModel):
    id: int
    tournament_id: int
    created_at: datetime
    # 最新版本号
    latest_version: int
    versions: list[AnnouncementVersionResponse]

    @field_serializer("created_at")
    def serialize_created_at(self, v: datetime) -> str:
        return _serialize_utc(v)


class AcknowledgementRequest(BaseModel):
    participant_id: int = Field(description="提交回执的参赛者 id（须为该版收件人）")


class AcknowledgementRecipientResponse(BaseModel):
    participant_id: int
    number: int
    name: str
    # 回执提交时刻（UTC，Z 结尾）；未确认为 null
    acked_at: datetime | None = None

    @field_serializer("acked_at")
    def serialize_acked_at(self, v: datetime | None) -> str | None:
        return _serialize_utc(v)


class AcknowledgementResponse(BaseModel):
    announcement_id: int
    version: int
    participant_id: int
    # 该参与者当前是否已确认本版（首次确认与重复提交均为 true；
    # 重复提交原样返回首次的 acked_at，不重复写事件）
    acknowledged: bool
    acked_at: datetime | None = None

    @field_serializer("acked_at")
    def serialize_acked_at(self, v: datetime | None) -> str | None:
        return _serialize_utc(v)


class AcknowledgementListResponse(BaseModel):
    announcement_id: int
    version: int
    recipient_count: int
    acknowledged_count: int
    acknowledged: list[AcknowledgementRecipientResponse]
    unacknowledged: list[AcknowledgementRecipientResponse]


# ---------- Round Clock ----------

def _serialize_utc(v: datetime | None) -> str | None:
    # 存储为 naive UTC，序列化为带 Z 后缀的 ISO 8601
    if v is None:
        return None
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class ClockActionRequest(BaseModel):
    """轮次计时命令：一个请求执行一个命令。

    - start：设定轮次总时长并启动计时（每轮仅一次）；
    - pause / resume：全局暂停 / 恢复（暂停期间所有对局冻结）；
    - extend：给指定非轮空对局加时（match_id + seconds）。
    """

    request_id: str = Field(
        min_length=1, max_length=128, description="幂等键：重复提交返回首次结果"
    )
    expected_version: int = Field(
        ge=0, description="期望的当前时钟版本（未启动时为 0）；不一致返回 409"
    )
    action: Literal["start", "pause", "resume", "extend"]
    # start 参数：轮次总时长（秒）
    total_seconds: int | None = Field(default=None, ge=1)
    # extend 参数：目标对局与加时秒数
    match_id: int | None = None
    seconds: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _check_action_params(self):
        if self.action == "start" and self.total_seconds is None:
            raise ValueError("start 命令必须提供 total_seconds")
        if self.action == "extend" and (
            self.match_id is None or self.seconds is None
        ):
            raise ValueError("extend 命令必须提供 match_id 和 seconds")
        return self


class ClockMatchResponse(BaseModel):
    match_id: int
    # 该场累计加时（extend 叠加，秒）
    extra_seconds: int
    # 该场截止时刻（UTC，Z 结尾）；暂停中未知（恢复后才确定）为 null，
    # 计时未启动也为 null
    deadline_at: datetime | None = None
    # 该场剩余秒数（暂停/封盘时为冻结值，超时为负）；未启动为 null
    remaining_seconds: int | None = None

    @field_serializer("deadline_at")
    def serialize_deadline_at(self, v: datetime | None) -> str | None:
        return _serialize_utc(v)


class ClockResponse(BaseModel):
    tournament_id: int
    round_id: int
    round_no: int
    # 时钟版本：每个成功命令 +1；未启动为 0
    version: int
    # idle（未启动）/ running / paused / ended（轮次已封盘）
    status: str
    total_seconds: int | None = None
    started_at: datetime | None = None
    paused_at: datetime | None = None
    ended_at: datetime | None = None
    accumulated_pause_seconds: float = 0.0
    # 服务端当前 UTC 时间，供客户端校准倒计时
    server_time: datetime
    # 本轮各非轮空对局的计时（轮空对局不计时）
    matches: list[ClockMatchResponse] = []

    @field_serializer("started_at", "paused_at", "ended_at", "server_time")
    def serialize_utc_fields(self, v: datetime | None) -> str | None:
        return _serialize_utc(v)
