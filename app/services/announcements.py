"""定向赛务公告与确认回执：规则解析、收件人冻结、版本化与回执。

收件规则（见 schemas.AnnouncementRule）：

- 三类圈定条件取并集：直接指定的 participant_ids、当前排名闭区间
  （口径与 GET /standings 一致：积分 → Buchholz → 编号）、某轮某版
  工位方案中占用指定工位号的对局双方；
- 并集去重后整体应用排除名单 exclude_participant_ids。

发布/修订在调用方事务（writer_lock 持锁）内完成：

1. 解析并校验规则：参赛者 id 不存在、排名区间超出当前人数、方案
   版本不属于本赛事/轮次或不存在、工位号在该版方案中无对局，
   均抛 409（规则无效），且在任何写入之前抛出——不落库；
2. 校验 expected_event_seq 等于赛事当前事件序号（乐观快照）：
   不一致抛 409（快照已变化），不落库；
3. 冻结去重后的收件人（含编号/姓名快照），写入公告/版本行与
   announcement.published / announcement.revised 事件。

公告不可改写：修订只新增版本并重新冻结收件人，旧版回执按
(version_id, participant_id) 隔离，新版本的收件人须重新确认。

注意：本模块所有函数只 flush 不提交，提交与发布由路由层的
commit_and_publish 在同一事务中统一完成。
"""
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..events.service import (
    ANNOUNCEMENT_ACKNOWLEDGED,
    ANNOUNCEMENT_PUBLISHED,
    ANNOUNCEMENT_REVISED,
    append_event,
    current_event_seq,
)
from ..models import (
    Announcement,
    AnnouncementAction,
    AnnouncementRecipient,
    AnnouncementVersion,
    Event,
    Match,
    Participant,
    Round,
    StationPlan,
    StationPlanAssignment,
)
from ..schemas import (
    AcknowledgementListResponse,
    AcknowledgementRecipientResponse,
    AcknowledgementResponse,
    AnnouncementResponse,
    AnnouncementVersionResponse,
)
from ..services.scoring import compute_records


# ---------- 规则解析与收件人冻结 ----------

def _rule_snapshot(rule) -> dict:
    """把收件规则序列化为可冻结的 JSON 快照。"""
    return {
        "participant_ids": list(rule.participant_ids),
        "rank_from": rule.rank_from,
        "rank_to": rule.rank_to,
        "station_plan": (
            None
            if rule.station_plan is None
            else {
                "round_no": rule.station_plan.round_no,
                "version": rule.station_plan.version,
                "station_nos": list(rule.station_plan.station_nos),
            }
        ),
        "exclude_participant_ids": list(rule.exclude_participant_ids),
    }


def _resolve_recipients(
    db: Session, tournament_id: int, rule
) -> list[Participant]:
    """按规则计算去重后的收件人；规则无效抛 409（尚未任何写入）。"""
    participants = (
        db.query(Participant)
        .filter(Participant.tournament_id == tournament_id)
        .order_by(Participant.number)
        .all()
    )
    by_id = {p.id: p for p in participants}

    problems: list[str] = []
    selected: set[int] = set()

    # 1) 直接指定的参赛者
    for pid in rule.participant_ids:
        obj = by_id.get(pid)
        if obj is None:
            problems.append(f"参赛者 {pid} 不属于本赛事")
        else:
            selected.add(pid)

    # 排除名单中的 id 也必须属于本赛事
    excluded: set[int] = set()
    for pid in rule.exclude_participant_ids:
        obj = by_id.get(pid)
        if obj is None:
            problems.append(f"排除名单中的参赛者 {pid} 不属于本赛事")
        else:
            excluded.add(pid)

    # 2) 当前排名闭区间（实时排名口径，与 GET /standings 一致）
    if rule.rank_from is not None:
        records = compute_records(db, tournament_id, include_open=True)
        if rule.rank_to > len(records):
            problems.append(
                f"排名区间 [{rule.rank_from}, {rule.rank_to}] 超出当前"
                f"参赛人数 {len(records)}"
            )
        else:
            for rec in records[rule.rank_from - 1 : rule.rank_to]:
                selected.add(rec.participant_id)

    # 3) 某轮某版工位方案中占用指定工位号的对局双方
    if rule.station_plan is not None:
        sp = rule.station_plan
        round_obj = (
            db.query(Round)
            .filter(
                Round.tournament_id == tournament_id,
                Round.round_no == sp.round_no,
            )
            .first()
        )
        if round_obj is None:
            problems.append(f"第 {sp.round_no} 轮不存在")
        else:
            plan = (
                db.query(StationPlan)
                .filter(
                    StationPlan.round_id == round_obj.id,
                    StationPlan.version == sp.version,
                )
                .first()
            )
            if plan is None:
                problems.append(
                    f"第 {sp.round_no} 轮不存在版本 {sp.version} 的工位方案"
                )
            else:
                assignments = (
                    db.query(StationPlanAssignment)
                    .filter(
                        StationPlanAssignment.plan_id == plan.id,
                        StationPlanAssignment.station_no.in_(sp.station_nos),
                    )
                    .all()
                )
                found_nos = {a.station_no for a in assignments}
                for no in sp.station_nos:
                    if no not in found_nos:
                        problems.append(
                            f"工位 {no} 在该版方案中没有分配对局"
                        )
                matches = (
                    db.query(Match)
                    .filter(
                        Match.id.in_([a.match_id for a in assignments])
                    )
                    .all()
                )
                for m in matches:
                    if m.player_white_id is not None:
                        selected.add(m.player_white_id)
                    if m.player_black_id is not None:
                        selected.add(m.player_black_id)

    if problems:
        raise HTTPException(status_code=409, detail="；".join(problems))

    # 并集后应用排除名单
    final_ids = selected - excluded
    if not final_ids:
        raise HTTPException(
            status_code=409, detail="收件规则无效：去重并排除后没有收件人"
        )
    return [p for p in participants if p.id in final_ids]


def _check_snapshot(
    db: Session, tournament_id: int, expected_event_seq: int
) -> None:
    """乐观快照校验：赛事事件序号必须未变化，否则 409（不落库）。"""
    current = current_event_seq(db, tournament_id)
    if expected_event_seq != current:
        raise HTTPException(
            status_code=409,
            detail=(
                f"赛事快照已变化：当前事件序号 {current}，"
                f"请求期望 {expected_event_seq}"
            ),
        )


def _freeze_recipients(
    db: Session, version_id: int, recipients: list[Participant]
) -> None:
    """把去重后的收件人（含编号/姓名快照）写入该版收件人行。"""
    for p in recipients:
        db.add(
            AnnouncementRecipient(
                version_id=version_id,
                participant_id=p.id,
                number=p.number,
                name=p.name,
                acked_at=None,
            )
        )
    db.flush()


# ---------- 幂等 ----------

def _params_fingerprint(req, announcement_id: int | None = None) -> dict:
    """发布/修订请求参数指纹：重放判定时忽略 request_id 本身。"""
    fp = {
        "expected_event_seq": req.expected_event_seq,
        "body": req.body,
        "rule": _rule_snapshot(req.rule),
    }
    if announcement_id is not None:
        fp["announcement_id"] = announcement_id
    return fp


def _find_replay(
    db: Session,
    tournament_id: int,
    action: str,
    request_id: str,
    fingerprint: dict,
) -> dict | None:
    """同一 request_id 的已成功请求：参数一致返回首次响应，否则 409。"""
    record = (
        db.query(AnnouncementAction)
        .filter(
            AnnouncementAction.tournament_id == tournament_id,
            AnnouncementAction.request_id == request_id,
        )
        .first()
    )
    if record is None:
        return None
    if record.action == action and record.params == fingerprint:
        return record.response
    raise HTTPException(
        status_code=409,
        detail="request_id 已被用于其他公告请求，请更换 request_id",
    )


# ---------- 发布 / 修订 ----------

def publish_announcement(
    db: Session, tournament_id: int, req
) -> tuple[AnnouncementResponse, Event | None]:
    """发布新公告（flush，不提交），返回响应与 published 事件。

    幂等重放时事件为 None，不产生任何写入。
    """
    fingerprint = _params_fingerprint(req)
    # 幂等判定先于快照校验：重试已成功请求必须返回原结果，而不是因
    # 事件序号已推进报 409（与计时命令的幂等语义一致）
    replay = _find_replay(
        db, tournament_id, "publish", req.request_id, fingerprint
    )
    if replay is not None:
        return AnnouncementResponse(**replay), None

    # 规则校验 → 快照校验：任一失败在任何写入之前抛出，不落库
    recipients = _resolve_recipients(db, tournament_id, req.rule)
    _check_snapshot(db, tournament_id, req.expected_event_seq)

    announcement = Announcement(tournament_id=tournament_id)
    db.add(announcement)
    db.flush()  # 取得 announcement.id
    version = AnnouncementVersion(
        announcement=announcement,
        version=1,
        body=req.body,
        rule=_rule_snapshot(req.rule),
    )
    db.add(version)
    db.flush()  # 取得 version.id，供收件人行与事件使用
    _freeze_recipients(db, version.id, recipients)

    event = append_event(
        db,
        tournament_id,
        ANNOUNCEMENT_PUBLISHED,
        {
            "announcement_id": announcement.id,
            "version": 1,
            "recipient_count": len(recipients),
            "recipient_participant_ids": [p.id for p in recipients],
        },
    )

    response = serialize_announcement(announcement)
    db.add(
        AnnouncementAction(
            tournament_id=tournament_id,
            request_id=req.request_id,
            action="publish",
            params=fingerprint,
            response=response.model_dump(mode="json"),
        )
    )
    return response, event


def revise_announcement(
    db: Session, tournament_id: int, announcement: Announcement, req
) -> tuple[AnnouncementResponse, Event | None]:
    """为已存在公告生成新版本（flush，不提交），旧版回执保持不变。"""
    fingerprint = _params_fingerprint(req, announcement_id=announcement.id)
    replay = _find_replay(
        db, tournament_id, "revise", req.request_id, fingerprint
    )
    if replay is not None:
        return AnnouncementResponse(**replay), None

    recipients = _resolve_recipients(db, tournament_id, req.rule)
    _check_snapshot(db, tournament_id, req.expected_event_seq)

    new_version_no = max(v.version for v in announcement.versions) + 1
    version = AnnouncementVersion(
        announcement_id=announcement.id,
        version=new_version_no,
        body=req.body,
        rule=_rule_snapshot(req.rule),
    )
    # 经关系集合挂接：随后 serialize_announcement 能直接读到新版本
    announcement.versions.append(version)
    db.flush()
    _freeze_recipients(db, version.id, recipients)

    event = append_event(
        db,
        tournament_id,
        ANNOUNCEMENT_REVISED,
        {
            "announcement_id": announcement.id,
            "version": new_version_no,
            "recipient_count": len(recipients),
            "recipient_participant_ids": [p.id for p in recipients],
        },
    )

    response = serialize_announcement(announcement)
    db.add(
        AnnouncementAction(
            tournament_id=tournament_id,
            request_id=req.request_id,
            action="revise",
            params=fingerprint,
            response=response.model_dump(mode="json"),
        )
    )
    return response, event


# ---------- 回执 ----------

def _get_version_or_404(
    db: Session, announcement: Announcement, version_no: int
) -> AnnouncementVersion:
    for v in announcement.versions:
        if v.version == version_no:
            return v
    raise HTTPException(status_code=404, detail="该公告版本不存在")


def acknowledge(
    db: Session,
    tournament_id: int,
    announcement: Announcement,
    version_no: int,
    participant_id: int,
) -> tuple[AcknowledgementResponse, Event | None]:
    """该版收件人提交回执（flush，不提交）。

    - 参赛者不属于本赛事：404；
    - 不是该版冻结收件人：403（旧版回执不得沿用到新版本）；
    - 同一 participant_id 重复提交：原样返回首次结果（event 为 None）。
    """
    version_obj = _get_version_or_404(db, announcement, version_no)

    recipient = (
        db.query(AnnouncementRecipient)
        .filter(
            AnnouncementRecipient.version_id == version_obj.id,
            AnnouncementRecipient.participant_id == participant_id,
        )
        .first()
    )
    if recipient is None:
        # 区分「参赛者不存在/非本赛事」与「不是该版收件人」
        participant = (
            db.query(Participant)
            .filter(
                Participant.id == participant_id,
                Participant.tournament_id == tournament_id,
            )
            .first()
        )
        if participant is None:
            raise HTTPException(status_code=404, detail="参赛者不存在")
        raise HTTPException(
            status_code=403, detail="该参赛者不是此版本公告的收件人"
        )

    # 重复提交：原样返回首次结果，不产生任何写入
    response = AcknowledgementResponse(
        announcement_id=announcement.id,
        version=version_no,
        participant_id=participant_id,
        acknowledged=recipient.acked_at is not None,
        acked_at=recipient.acked_at,
    )
    if recipient.acked_at is not None:
        return response, None

    now = datetime.utcnow()
    recipient.acked_at = now
    db.flush()

    event = append_event(
        db,
        tournament_id,
        ANNOUNCEMENT_ACKNOWLEDGED,
        {
            "announcement_id": announcement.id,
            "version": version_no,
            "participant_id": participant_id,
        },
        occurred_at=now,
    )
    return response.model_copy(
        update={"acknowledged": True, "acked_at": now}
    ), event


def list_acknowledgements(
    announcement: Announcement, version_no: int
) -> AcknowledgementListResponse:
    """返回该版已确认/未确认名单（按参赛者编号升序）。"""
    version_obj = None
    for v in announcement.versions:
        if v.version == version_no:
            version_obj = v
            break
    if version_obj is None:
        raise HTTPException(status_code=404, detail="该公告版本不存在")

    recipients = sorted(version_obj.recipients, key=lambda r: r.number)
    acked = [r for r in recipients if r.acked_at is not None]
    unacked = [r for r in recipients if r.acked_at is None]

    def item(r: AnnouncementRecipient) -> AcknowledgementRecipientResponse:
        return AcknowledgementRecipientResponse(
            participant_id=r.participant_id,
            number=r.number,
            name=r.name,
            acked_at=r.acked_at,
        )

    return AcknowledgementListResponse(
        announcement_id=announcement.id,
        version=version_no,
        recipient_count=len(recipients),
        acknowledged_count=len(acked),
        acknowledged=[item(r) for r in acked],
        unacknowledged=[item(r) for r in unacked],
    )


# ---------- 序列化 ----------

def serialize_version(
    version_obj: AnnouncementVersion,
) -> AnnouncementVersionResponse:
    recipients = version_obj.recipients
    return AnnouncementVersionResponse(
        id=version_obj.id,
        version=version_obj.version,
        body=version_obj.body,
        rule=dict(version_obj.rule),
        recipient_count=len(recipients),
        acknowledged_count=sum(1 for r in recipients if r.acked_at is not None),
        created_at=version_obj.created_at,
    )


def serialize_announcement(announcement: Announcement) -> AnnouncementResponse:
    return AnnouncementResponse(
        id=announcement.id,
        tournament_id=announcement.tournament_id,
        created_at=announcement.created_at,
        latest_version=max(v.version for v in announcement.versions),
        versions=[serialize_version(v) for v in announcement.versions],
    )
