"""定向赛务公告与确认回执接口。

- POST /tournaments/{id}/announcements：发布公告（版本 1）。请求携带
  幂等键 request_id、乐观快照 expected_event_seq、正文 body 与定向
  收件规则 rule；规则无效或快照已变化返回 409 且不落库。
- GET  /tournaments/{id}/announcements：赛事公告列表（含全部版本）。
- POST /announcements/{id}/revisions：公告不可改写，本接口生成新版本
  并重新冻结收件人；旧版回执不沿用。
- GET  /announcements/{id}：公告详情（含全部版本与各版回执统计）。
- POST /announcements/{id}/revisions/{version}/acknowledgements：
  该版收件人确认回执；非收件人 403，同一 participant_id 重复提交
  返回原结果。
- GET  同一路径：返回该版已确认 / 未确认名单。

所有写请求经进程级写锁串行；公告/版本/收件人/回执与
announcement.* 事件在同一数据库事务中提交。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..events.service import commit_and_publish
from ..locking import writer_lock
from ..models import Announcement
from ..schemas import (
    AcknowledgementListResponse,
    AcknowledgementRequest,
    AcknowledgementResponse,
    AnnouncementCreate,
    AnnouncementResponse,
    AnnouncementRevisionCreate,
)
from ..services.announcements import (
    acknowledge,
    list_acknowledgements,
    publish_announcement,
    revise_announcement,
    serialize_announcement,
)
from .tournaments import get_tournament_or_404

router = APIRouter()


def _get_announcement_or_404(
    db: Session, announcement_id: int
) -> Announcement:
    obj = db.get(Announcement, announcement_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="公告不存在")
    return obj


# ---------- 发布 / 查询公告 ----------

@router.post(
    "/tournaments/{tournament_id}/announcements",
    response_model=AnnouncementResponse,
    status_code=201,
)
def publish(
    tournament_id: int,
    payload: AnnouncementCreate,
    db: Session = Depends(get_db),
):
    get_tournament_or_404(db, tournament_id)
    # 写请求进程级串行：幂等判定、规则解析、快照校验、收件人冻结、
    # 事件序号与「提交→发布」全程持锁
    with writer_lock():
        response, event = publish_announcement(db, tournament_id, payload)
        # 幂等重放（event 为 None）没有任何写入，直接返回首次结果
        if event is not None:
            commit_and_publish(db, [event])
        return response


@router.get(
    "/tournaments/{tournament_id}/announcements",
    response_model=list[AnnouncementResponse],
)
def list_tournament_announcements(
    tournament_id: int, db: Session = Depends(get_db)
):
    get_tournament_or_404(db, tournament_id)
    rows = (
        db.query(Announcement)
        .filter(Announcement.tournament_id == tournament_id)
        .order_by(Announcement.id)
        .all()
    )
    return [serialize_announcement(a) for a in rows]


@router.get(
    "/announcements/{announcement_id}",
    response_model=AnnouncementResponse,
)
def get_announcement(announcement_id: int, db: Session = Depends(get_db)):
    return serialize_announcement(_get_announcement_or_404(db, announcement_id))


# ---------- 修订（新版本） ----------

@router.post(
    "/announcements/{announcement_id}/revisions",
    response_model=AnnouncementResponse,
    status_code=201,
)
def revise(
    announcement_id: int,
    payload: AnnouncementRevisionCreate,
    db: Session = Depends(get_db),
):
    with writer_lock():
        # 锁内读取公告（可能经过等待才取得锁），按最新状态处理
        announcement = _get_announcement_or_404(db, announcement_id)
        response, event = revise_announcement(
            db, announcement.tournament_id, announcement, payload
        )
        if event is not None:
            commit_and_publish(db, [event])
        return response


# ---------- 回执 ----------

@router.post(
    "/announcements/{announcement_id}/revisions/{version}/acknowledgements",
    response_model=AcknowledgementResponse,
    status_code=201,
)
def submit_acknowledgement(
    announcement_id: int,
    version: int,
    payload: AcknowledgementRequest,
    db: Session = Depends(get_db),
):
    with writer_lock():
        announcement = _get_announcement_or_404(db, announcement_id)
        response, event = acknowledge(
            db,
            announcement.tournament_id,
            announcement,
            version,
            payload.participant_id,
        )
        if event is not None:
            commit_and_publish(db, [event])
        return response


@router.get(
    "/announcements/{announcement_id}/revisions/{version}/acknowledgements",
    response_model=AcknowledgementListResponse,
)
def get_acknowledgements(
    announcement_id: int,
    version: int,
    db: Session = Depends(get_db),
):
    announcement = _get_announcement_or_404(db, announcement_id)
    return list_acknowledgements(announcement, version)
