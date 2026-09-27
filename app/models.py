from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class Tournament(Base):
    __tablename__ = "tournaments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    participants: Mapped[list["Participant"]] = relationship(
        back_populates="tournament",
        cascade="all, delete-orphan",
        order_by="Participant.number",
    )
    rounds: Mapped[list["Round"]] = relationship(
        back_populates="tournament",
        cascade="all, delete-orphan",
        order_by="Round.round_no",
    )
    stations: Mapped[list["Station"]] = relationship(
        back_populates="tournament",
        cascade="all, delete-orphan",
        order_by="Station.station_no",
    )
    events: Mapped[list["Event"]] = relationship(
        back_populates="tournament",
        cascade="all, delete-orphan",
        order_by="Event.seq",
    )


class Participant(Base):
    __tablename__ = "participants"
    __table_args__ = (
        UniqueConstraint("tournament_id", "number", name="uq_participant_number"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tournament_id: Mapped[int] = mapped_column(
        ForeignKey("tournaments.id", ondelete="CASCADE"), nullable=False
    )
    # 参赛者编号：赛事内从 1 开始，按录入顺序自增
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 必需标签：分配工位时，工位能力标签必须覆盖这些标签
    required_tags: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    tournament: Mapped[Tournament] = relationship(back_populates="participants")


class Station(Base):
    """赛事工位：编号、可用状态与能力标签。

    工位可随赛事维护（新增、停用、调整标签）；已发布的工位方案
    保存工位编号快照，不受后续变更影响。
    """

    __tablename__ = "stations"
    __table_args__ = (
        UniqueConstraint("tournament_id", "station_no", name="uq_station_no"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tournament_id: Mapped[int] = mapped_column(
        ForeignKey("tournaments.id", ondelete="CASCADE"), nullable=False
    )
    # 工位编号：赛事内唯一
    station_no: Mapped[int] = mapped_column(Integer, nullable=False)
    # 是否可用：不可用工位不参与分配
    available: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    # 能力标签：该工位可满足的标签集合
    tags: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    tournament: Mapped[Tournament] = relationship(back_populates="stations")


class Round(Base):
    __tablename__ = "rounds"
    __table_args__ = (
        UniqueConstraint("tournament_id", "round_no", name="uq_round_no"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tournament_id: Mapped[int] = mapped_column(
        ForeignKey("tournaments.id", ondelete="CASCADE"), nullable=False
    )
    round_no: Mapped[int] = mapped_column(Integer, nullable=False)
    # open: 可继续录入赛果；closed: 全部赛果已录入
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    tournament: Mapped[Tournament] = relationship(back_populates="rounds")
    matches: Mapped[list["Match"]] = relationship(
        back_populates="round",
        cascade="all, delete-orphan",
        order_by="Match.id",
    )
    station_plans: Mapped[list["StationPlan"]] = relationship(
        back_populates="round",
        cascade="all, delete-orphan",
        order_by="StationPlan.version",
    )


class Match(Base):
    __tablename__ = "matches"
    __table_args__ = (
        CheckConstraint(
            "player_white_id IS NOT NULL OR player_black_id IS NOT NULL",
            name="ck_match_has_player",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    round_id: Mapped[int] = mapped_column(
        ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False
    )
    # 奇数人轮空：player_white_id 为轮空选手，player_black_id 为 NULL
    player_white_id: Mapped[int | None] = mapped_column(
        ForeignKey("participants.id", ondelete="CASCADE"), nullable=True
    )
    player_black_id: Mapped[int | None] = mapped_column(
        ForeignKey("participants.id", ondelete="CASCADE"), nullable=True
    )
    # 双方得分（各为 0 / 1 / 2 个半分，即 0 / 0.5 / 1）；
    # 轮空场次：轮空方 score_white=2、score_black=NULL
    score_white: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score_black: Mapped[int | None] = mapped_column(Integer, nullable=True)

    round: Mapped[Round] = relationship(back_populates="matches")
    white: Mapped[Participant | None] = relationship(
        foreign_keys=[player_white_id]
    )
    black: Mapped[Participant | None] = relationship(
        foreign_keys=[player_black_id]
    )


class StationPlan(Base):
    """一轮的工位分配方案：版本化、发布后不可变。

    每轮可发布多个版本（version 从 1 递增）；未分配对局的说明
    以求解时的工位/标签状态快照保存，后续工位变更不影响已发布
    方案。
    """

    __tablename__ = "station_plans"
    __table_args__ = (
        UniqueConstraint("round_id", "version", name="uq_station_plan_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    round_id: Mapped[int] = mapped_column(
        ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False
    )
    # 方案版本：轮次内从 1 开始递增
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 未分配对局说明快照：
    # [{match_id, required_tags, missing_tags, conflicting_stations, reason}]
    unassigned: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    round: Mapped[Round] = relationship(back_populates="station_plans")
    assignments: Mapped[list["StationPlanAssignment"]] = relationship(
        back_populates="plan",
        cascade="all, delete-orphan",
        order_by="StationPlanAssignment.match_id",
    )


class StationPlanAssignment(Base):
    """方案中的一条分配：一场非轮空对局占用一个工位。"""

    __tablename__ = "station_plan_assignments"
    __table_args__ = (
        UniqueConstraint("plan_id", "match_id", name="uq_plan_match"),
        UniqueConstraint("plan_id", "station_no", name="uq_plan_station"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(
        ForeignKey("station_plans.id", ondelete="CASCADE"), nullable=False
    )
    match_id: Mapped[int] = mapped_column(
        ForeignKey("matches.id", ondelete="CASCADE"), nullable=False
    )
    station_id: Mapped[int] = mapped_column(
        ForeignKey("stations.id", ondelete="CASCADE"), nullable=False
    )
    # 工位编号快照：工位后续变更不影响已发布方案
    station_no: Mapped[int] = mapped_column(Integer, nullable=False)

    plan: Mapped[StationPlan] = relationship(back_populates="assignments")


class RoundClock(Base):
    """一轮的计时状态：每轮至多一行，由 start 命令创建。

    剩余时间不在数据库中逐秒更新，而是在读取时由
    started_at（服务端 UTC 锚点）、accumulated_pause_seconds
    （累计暂停时长）与 paused_at/ended_at 推算；没有任何后台
    写库任务，进程/容器重启后读数自然连续。
    """

    __tablename__ = "round_clocks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    round_id: Mapped[int] = mapped_column(
        ForeignKey("rounds.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    # 乐观并发版本：每个成功应用的命令 +1；start 之前行不存在，视为 0
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # running / paused / ended（行由 start 创建，不存在 idle 行）
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # start 设定的轮次总时长（秒）
    total_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    # UTC 锚点：计时起点（naive UTC，与库内其他时间戳一致）
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # 累计暂停时长（秒）：resume/封盘时把本段暂停时长计入
    accumulated_pause_seconds: Mapped[float] = mapped_column(
        Float, default=0.0, nullable=False
    )
    # 暂停时刻（仅 paused 状态有值）；恢复时清空并计入累计暂停
    paused_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 轮次封盘时刻（仅 ended 状态有值）：冻结剩余时间
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 各非轮空对局的累计加时：{"<match_id>": seconds}
    match_extensions: Mapped[dict] = mapped_column(
        JSON, default=dict, nullable=False
    )

    round: Mapped[Round] = relationship()


class ClockAction(Base):
    """计时命令的幂等记录：同一 request_id 重放时原样返回首次结果。

    记录与计时状态变更、round_clock.* 事件在同一事务中提交；
    被拒绝的命令（非法状态/版本冲突 409）不落任何记录。
    """

    __tablename__ = "clock_actions"
    __table_args__ = (
        UniqueConstraint("clock_id", "request_id", name="uq_clock_action_request"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    clock_id: Mapped[int] = mapped_column(
        ForeignKey("round_clocks.id", ondelete="CASCADE"), nullable=False
    )
    # 客户端幂等键：同一时钟内唯一
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # start / pause / resume / extend
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    expected_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 命令参数指纹：start {"total_seconds": n}；
    # extend {"match_id": m, "seconds": s}；pause/resume {}
    params: Mapped[dict] = mapped_column(JSON, nullable=False)
    # 首次执行后的响应快照：重复 request_id 原样返回
    response: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    clock: Mapped[RoundClock] = relationship()


class EventCounter(Base):
    """赛事事件序号计数器：每个赛事一行。

    序号通过对该行的原子 UPSERT（INSERT ... ON CONFLICT DO UPDATE
    ... RETURNING）在当前写事务内分配，不再使用「查询最大 seq 后
    插入」的方式：并发事务在数据库层互斥排队，提交的序号因此唯一
    且连续；事务回滚时计数器的增量一并回滚，不留空号。
    """

    __tablename__ = "event_counters"

    tournament_id: Mapped[int] = mapped_column(
        ForeignKey("tournaments.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # 该赛事已分配的最大事件序号；行不存在表示尚未分配（下一条为 1）
    last_seq: Mapped[int] = mapped_column(Integer, nullable=False)


class Event(Base):
    """赛事事件：序号在每个赛事内独立且单调递增。

    事件与引发它的业务变更在同一数据库事务中写入，序号由
    EventCounter 在事务内原子分配；事务回滚则事件与序号增量
    一并回滚。
    """

    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("tournament_id", "seq", name="uq_event_tournament_seq"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tournament_id: Mapped[int] = mapped_column(
        ForeignKey("tournaments.id", ondelete="CASCADE"), nullable=False
    )
    # 赛事内单调递增序号，从 1 开始
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    # 事件类型，见 app/events/ 中 TOURNAMENT_CREATED 等常量
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    # 事件载荷：事件类型、发生时间之外的相关资源标识
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    tournament: Mapped[Tournament] = relationship(back_populates="events")
