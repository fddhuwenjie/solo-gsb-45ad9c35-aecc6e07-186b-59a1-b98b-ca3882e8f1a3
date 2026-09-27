"""定向赛务公告与确认回执模块测试。

覆盖：
- 发布：收件规则并集（参赛者 / 当前排名区间 / 某版工位方案工位号）
  + 排除名单、去重冻结与编号姓名快照；规则无效 409 不落库；
  expected_event_seq 乐观快照（变化即 409 不落库）；request_id 幂等
  重放原样返回、参数冲突 409；
- 修订：公告不可改写，POST revisions 生成新版本并重新冻结收件人；
  旧版回执不沿用；新版本不影响旧版收件人与回执；
- 回执：仅该版冻结收件人可确认（非收件人 403、参赛者不存在 404、
  公告/版本不存在 404）；同一 participant_id 重复提交返回原结果、
  不重复写事件；查询返回已确认/未确认名单与计数；
- 事件：announcement.published / .revised / .acknowledged 与业务
  变更同事务提交，序号连续；失败请求不占号、不留痕。
"""


def create_tournament(client, name="公告赛"):
    r = client.post("/tournaments", json={"name": name})
    assert r.status_code == 201
    return r.json()["id"]


def add_players(client, tid, names):
    r = client.post(
        f"/tournaments/{tid}/participants/bulk", json={"names": names}
    )
    assert r.status_code == 201
    return r.json()


def next_round(client, tid):
    r = client.post(f"/tournaments/{tid}/rounds/next")
    assert r.status_code == 201, r.text
    return r.json()


def add_stations(client, tid, stations):
    r = client.post(
        f"/tournaments/{tid}/stations/bulk", json={"stations": stations}
    )
    assert r.status_code == 201, r.text


def publish_plan(client, tid, round_no, expected_version=0, pins=None):
    r = client.post(
        f"/tournaments/{tid}/rounds/{round_no}/station-plans",
        json={"expected_version": expected_version, "pins": pins or []},
    )
    assert r.status_code == 201, r.text
    return r.json()


def put_result(client, match, sw, sb):
    r = client.put(
        f"/matches/{match['id']}/result",
        json={"score_white": sw, "score_black": sb},
    )
    assert r.status_code == 200, r.text


def last_seq(client, tid):
    return client.get(
        f"/tournaments/{tid}/events?after=0&limit=200"
    ).json()["last_seq"]


def publish_announcement(client, tid, body, rule, request_id, expected):
    return client.post(
        f"/tournaments/{tid}/announcements",
        json={
            "request_id": request_id,
            "expected_event_seq": expected,
            "body": body,
            "rule": rule,
        },
    )


def revise(client, announcement_id, body, rule, request_id, expected):
    return client.post(
        f"/announcements/{announcement_id}/revisions",
        json={
            "request_id": request_id,
            "expected_event_seq": expected,
            "body": body,
            "rule": rule,
        },
    )


def ack(client, announcement_id, version, participant_id):
    return client.post(
        f"/announcements/{announcement_id}/revisions/{version}/acknowledgements",
        json={"participant_id": participant_id},
    )


def ack_list(client, announcement_id, version):
    return client.get(
        f"/announcements/{announcement_id}/revisions/{version}/acknowledgements"
    )


def setup_tournament(client, n_players=5, with_round=True, with_plan=True):
    tid = create_tournament(client)
    players = add_players(
        client, tid, [f"P{i}" for i in range(1, n_players + 1)]
    )
    round_obj = None
    plan = None
    if with_round:
        round_obj = next_round(client, tid)
        if with_plan:
            add_stations(
                client,
                tid,
                [{"station_no": i} for i in range(1, n_players)],
            )
            plan = publish_plan(client, tid, 1)
    return tid, players, round_obj, plan


# ---------- 发布：规则并集、排除、冻结 ----------

def test_publish_union_rules_and_exclude(client):
    tid, players, round_obj, plan = setup_tournament(client)
    # 首轮（含轮空，P5 轮空得 1 分），尚未录赛果：
    # 排名为 P5(1分)、P1..P4(0分，按编号)；排名 4..5 => P3、P4
    seq = last_seq(client, tid)

    # 方案 v1：match1(P1vP2)->工位1，match2(P3vP4)->工位2
    # 工位 1 的对局双方 = P1、P2
    rule = {
        "participant_ids": [players[0]["id"]],      # P1
        "rank_from": 4,
        "rank_to": 5,                                # P3、P4
        "station_plan": {
            "round_no": 1,
            "version": 1,
            "station_nos": [1],                      # P1、P2
        },
        "exclude_participant_ids": [players[0]["id"]],  # 排除 P1
    }
    r = publish_announcement(client, tid, "集合", rule, "a1", seq)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["latest_version"] == 1
    v1 = body["versions"][0]
    assert v1["version"] == 1 and v1["body"] == "集合"
    # 并集 {P1,P3,P4,P2} 排除 P1 => {P2,P3,P4}
    assert v1["recipient_count"] == 3

    lst = ack_list(client, body["id"], 1).json()
    assert lst["recipient_count"] == 3
    assert lst["acknowledged_count"] == 0
    assert sorted(p["number"] for p in lst["unacknowledged"]) == [2, 3, 4]
    assert all(p["name"] == f"P{p['number']}" for p in lst["unacknowledged"])
    assert lst["acknowledged"] == []

    # 列表与详情接口
    listing = client.get(f"/tournaments/{tid}/announcements").json()
    assert [a["id"] for a in listing] == [body["id"]]
    detail = client.get(f"/announcements/{body['id']}").json()
    assert detail["versions"][0]["rule"]["station_plan"]["station_nos"] == [1]


def test_rule_snapshot_frozen(client):
    """收件人在发布时冻结：后续参赛者编号/排名变化不影响已发公告。"""
    tid, players, round_obj, plan = setup_tournament(client)
    seq = last_seq(client, tid)
    r = publish_announcement(
        client,
        tid,
        "冻结测试",
        {"rank_from": 1, "rank_to": 2},
        "freeze",
        seq,
    )
    assert r.status_code == 201
    aid = r.json()["id"]
    before = sorted(
        p["number"]
        for p in ack_list(client, aid, 1).json()["unacknowledged"]
    )
    assert before == [1, 5]  # 当前排名前二：轮空的 P5 与 P1

    # 录入赛果改变排名（P2 取胜升至前列）
    put_result(client, round_obj["matches"][0], 0.0, 1.0)  # P2 胜 P1
    put_result(client, round_obj["matches"][1], 0.5, 0.5)
    after = sorted(
        p["number"]
        for p in ack_list(client, aid, 1).json()["unacknowledged"]
    )
    # 冻结的仍是发布时的 P1、P5
    assert after == [1, 5]


def test_invalid_rules_are_rejected_and_not_persisted(client):
    tid, players, round_obj, plan = setup_tournament(client)
    seq = last_seq(client, tid)

    bad_rules = [
        # 方案版本不属于本赛事（轮次存在但版本不存在）
        ({"station_plan": {"round_no": 1, "version": 9, "station_nos": [1]}}, 409),
        # 工位号在该版方案中没有分配对局
        ({"station_plan": {"round_no": 1, "version": 1, "station_nos": [7]}}, 409),
        # 轮次不存在
        ({"station_plan": {"round_no": 99, "version": 1, "station_nos": [1]}}, 409),
        # 参赛者不存在 / 不属于本赛事
        ({"participant_ids": [99999]}, 409),
        # 排除名单中的参赛者不存在
        (
            {
                "participant_ids": [players[0]["id"]],
                "exclude_participant_ids": [99999],
            },
            409,
        ),
        # 排名区间超出当前人数
        ({"rank_from": 1, "rank_to": 99}, 409),
        # 并集后被全部排除
        (
            {
                "participant_ids": [players[0]["id"]],
                "exclude_participant_ids": [players[0]["id"]],
            },
            409,
        ),
    ]
    for i, (rule, expected_status) in enumerate(bad_rules):
        r = publish_announcement(
            client, tid, "x", rule, f"bad-{i}", seq
        )
        assert r.status_code == expected_status, (rule, r.status_code, r.text)

    # 请求体层面的规则非法 -> 422（不进入写事务）
    malformed = [
        {},  # 规则为空：没有任何圈定条件
        {"rank_from": 3, "rank_to": 1},  # 区间倒置
        {"rank_from": 2},  # 只给一个边界
        {"station_plan": {"round_no": 1, "version": 1, "station_nos": []}},
    ]
    for rule in malformed:
        r = publish_announcement(client, tid, "x", rule, "malformed", seq)
        assert r.status_code == 422, (rule, r.status_code)

    # 无任何公告落库；事件序号也未被占用
    assert client.get(f"/tournaments/{tid}/announcements").json() == []
    assert last_seq(client, tid) == seq


def test_station_plan_rule_uses_assigned_match_players(client):
    tid, players, round_obj, plan = setup_tournament(client)
    seq = last_seq(client, tid)
    r = publish_announcement(
        client,
        tid,
        "工位 2",
        {"station_plan": {"round_no": 1, "version": 1, "station_nos": [2]}},
        "sp2",
        seq,
    )
    assert r.status_code == 201
    aid = r.json()["id"]
    nums = sorted(
        p["number"]
        for p in ack_list(client, aid, 1).json()["unacknowledged"]
    )
    # 工位 2 = match2(P3 v P4) 双方
    assert nums == [3, 4]


def test_station_plan_foreign_tournament_rejected(client):
    # 另一赛事的方案版本不能用于本赛事规则
    tid1, players1, r1, plan1 = setup_tournament(client)
    tid2 = create_tournament(client, "另一赛事")
    players2 = add_players(client, tid2, ["X", "Y", "Z"])
    # tid2 没有任何轮次：规则引用 tid1 的轮号 1（在 tid2 中不存在）
    seq2 = last_seq(client, tid2)
    r = publish_announcement(
        client,
        tid2,
        "外来方案",
        {"station_plan": {"round_no": 1, "version": 1, "station_nos": [1]}},
        "foreign",
        seq2,
    )
    assert r.status_code == 409
    assert client.get(f"/tournaments/{tid2}/announcements").json() == []


# ---------- 乐观快照 ----------

def test_expected_event_seq_snapshot_conflict(client):
    tid, players, round_obj, plan = setup_tournament(client)
    seq = last_seq(client, tid)

    # 序号落后：拒绝且不落库
    r = publish_announcement(
        client,
        tid,
        "过期快照",
        {"participant_ids": [players[0]["id"]]},
        "stale",
        seq - 1,
    )
    assert r.status_code == 409
    assert "快照已变化" in r.json()["detail"]
    assert client.get(f"/tournaments/{tid}/announcements").json() == []

    # 序号超前同样拒绝
    r = publish_announcement(
        client,
        tid,
        "超前快照",
        {"participant_ids": [players[0]["id"]]},
        "future",
        seq + 5,
    )
    assert r.status_code == 409
    assert last_seq(client, tid) == seq

    # 正确序号发布成功后，再用旧序号发布即冲突
    r = publish_announcement(
        client,
        tid,
        "正确",
        {"participant_ids": [players[0]["id"]]},
        "ok",
        seq,
    )
    assert r.status_code == 201
    r = publish_announcement(
        client,
        tid,
        "再次过期",
        {"participant_ids": [players[0]["id"]]},
        "ok2",
        seq,
    )
    assert r.status_code == 409
    # 冲突请求不占号：新公告序号恰好为 seq+1
    assert last_seq(client, tid) == seq + 1


def test_publish_writes_event_in_same_transaction(client):
    tid, players, _, _ = setup_tournament(client)
    seq = last_seq(client, tid)
    r = publish_announcement(
        client,
        tid,
        "事件",
        {"participant_ids": [players[0]["id"], players[1]["id"]]},
        "ev",
        seq,
    )
    aid = r.json()["id"]
    page = client.get(
        f"/tournaments/{tid}/events?after={seq}&limit=10"
    ).json()
    assert len(page["items"]) == 1
    event = page["items"][0]
    assert event["seq"] == seq + 1
    assert event["type"] == "announcement.published"
    assert event["payload"] == {
        "announcement_id": aid,
        "version": 1,
        "recipient_count": 2,
        "recipient_participant_ids": [players[0]["id"], players[1]["id"]],
    }


# ---------- request_id 幂等 ----------

def test_publish_request_id_idempotent(client):
    tid, players, _, _ = setup_tournament(client)
    seq = last_seq(client, tid)
    body = {
        "request_id": "idem",
        "expected_event_seq": seq,
        "body": "幂等",
        "rule": {"participant_ids": [players[0]["id"]]},
    }
    r1 = client.post(f"/tournaments/{tid}/announcements", json=body)
    assert r1.status_code == 201
    # 原样重放（含相同 expected_event_seq）：返回首次结果，不占新序号
    r2 = client.post(f"/tournaments/{tid}/announcements", json=body)
    assert r2.status_code == 201
    assert r2.json() == r1.json()
    assert last_seq(client, tid) == seq + 1

    # 同一 request_id 携带不同参数 -> 409
    r3 = client.post(
        f"/tournaments/{tid}/announcements",
        json={**body, "body": "不同正文"},
    )
    assert r3.status_code == 409
    assert "request_id" in r3.json()["detail"]

    # 同一 request_id 不能跨操作类型（publish/revise）
    aid = r1.json()["id"]
    cur = last_seq(client, tid)
    r4 = revise(
        client,
        aid,
        "新版",
        {"participant_ids": [players[0]["id"]]},
        "idem",
        cur,
    )
    assert r4.status_code == 409


# ---------- 修订 ----------

def test_revision_creates_new_version_and_refreezes(client):
    tid, players, round_obj, plan = setup_tournament(client)
    seq = last_seq(client, tid)
    r = publish_announcement(
        client,
        tid,
        "v1 正文",
        {"participant_ids": [players[0]["id"], players[1]["id"]]},
        "p1",
        seq,
    )
    aid = r.json()["id"]
    # 版本 1：P2 确认
    assert ack(client, aid, 1, players[1]["id"]).status_code == 201

    cur = last_seq(client, tid)
    # 修订：新版本只发给 P3；旧版正文/收件人不变
    r = revise(
        client,
        aid,
        "v2 正文",
        {"participant_ids": [players[2]["id"]]},
        "rv1",
        cur,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["latest_version"] == 2
    v1, v2 = body["versions"]
    assert v1["body"] == "v1 正文"
    assert v1["recipient_count"] == 2 and v1["acknowledged_count"] == 1
    assert v2["version"] == 2 and v2["body"] == "v2 正文"
    assert v2["recipient_count"] == 1 and v2["acknowledged_count"] == 0

    # 旧版回执不沿用：P2 在 v2 不是收件人 -> 403
    assert ack(client, aid, 2, players[1]["id"]).status_code == 403
    # P3 是 v2 收件人，可以确认
    r = ack(client, aid, 2, players[2]["id"])
    assert r.status_code == 201 and r.json()["acknowledged"] is True
    # P3 不是 v1 收件人
    assert ack(client, aid, 1, players[2]["id"]).status_code == 403

    # 旧版查询保持冻结状态（仅 P2 已确认）
    l1 = ack_list(client, aid, 1).json()
    assert sorted(p["number"] for p in l1["acknowledged"]) == [2]
    assert sorted(p["number"] for p in l1["unacknowledged"]) == [1]
    l2 = ack_list(client, aid, 2).json()
    assert sorted(p["number"] for p in l2["acknowledged"]) == [3]
    assert l2["unacknowledged"] == []


def test_revision_snapshot_and_invalid_rule_not_persisted(client):
    tid, players, _, _ = setup_tournament(client)
    seq = last_seq(client, tid)
    r = publish_announcement(
        client,
        tid,
        "v1",
        {"participant_ids": [players[0]["id"]]},
        "p1",
        seq,
    )
    aid = r.json()["id"]
    cur = last_seq(client, tid)

    # 快照已变化
    r = revise(
        client,
        aid,
        "v2",
        {"participant_ids": [players[0]["id"]]},
        "rv-bad-seq",
        cur - 1,
    )
    assert r.status_code == 409
    # 规则无效（引用不存在的方案）
    r = revise(
        client,
        aid,
        "v2",
        {"station_plan": {"round_no": 1, "version": 9, "station_nos": [1]}},
        "rv-bad-rule",
        cur,
    )
    assert r.status_code == 409
    # 均未产生新版本/新事件
    detail = client.get(f"/announcements/{aid}").json()
    assert detail["latest_version"] == 1
    assert last_seq(client, tid) == cur

    # 不存在的公告
    r = revise(
        client, 99999, "v2", {"participant_ids": [players[0]["id"]]}, "x", cur
    )
    assert r.status_code == 404


def test_revision_idempotent_replay(client):
    tid, players, _, _ = setup_tournament(client)
    seq = last_seq(client, tid)
    aid = publish_announcement(
        client,
        tid,
        "v1",
        {"participant_ids": [players[0]["id"]]},
        "p1",
        seq,
    ).json()["id"]
    cur = last_seq(client, tid)
    payload = {
        "request_id": "rv",
        "expected_event_seq": cur,
        "body": "v2",
        "rule": {"participant_ids": [players[1]["id"]]},
    }
    r1 = client.post(f"/announcements/{aid}/revisions", json=payload)
    r2 = client.post(f"/announcements/{aid}/revisions", json=payload)
    assert r1.status_code == r2.status_code == 201
    assert r1.json() == r2.json()
    # 只生成一个新版本、一条 revised 事件
    assert client.get(f"/announcements/{aid}").json()["latest_version"] == 2
    assert last_seq(client, tid) == cur + 1


# ---------- 回执 ----------

def test_acknowledgement_only_for_recipients_and_duplicate(client):
    tid, players, round_obj, plan = setup_tournament(client)
    seq = last_seq(client, tid)
    aid = publish_announcement(
        client,
        tid,
        "请确认",
        {"participant_ids": [players[0]["id"], players[1]["id"]]},
        "p1",
        seq,
    ).json()["id"]

    # 非收件人 -> 403
    r = ack(client, aid, 1, players[2]["id"])
    assert r.status_code == 403
    # 参赛者不存在 -> 404
    r = ack(client, aid, 1, 99999)
    assert r.status_code == 404
    # 版本不存在 -> 404
    r = ack(client, aid, 9, players[0]["id"])
    assert r.status_code == 404
    # 公告不存在 -> 404
    assert client.post(
        "/announcements/99999/revisions/1/acknowledgements",
        json={"participant_id": players[0]["id"]},
    ).status_code == 404

    # 首次确认
    r1 = ack(client, aid, 1, players[0]["id"])
    assert r1.status_code == 201
    first = r1.json()
    assert first["acknowledged"] is True
    assert first["acked_at"] is not None
    # 同一 participant_id 重复提交：返回原结果（相同 acked_at）
    r2 = ack(client, aid, 1, players[0]["id"])
    assert r2.status_code == 201
    assert r2.json() == first

    # 只写一条确认事件
    page = client.get(
        f"/tournaments/{tid}/events?after={seq}&limit=10"
    ).json()["items"]
    types = [e["type"] for e in page]
    assert types == ["announcement.published", "announcement.acknowledged"]
    assert page[-1]["payload"] == {
        "announcement_id": aid,
        "version": 1,
        "participant_id": players[0]["id"],
    }

    # 名单：已确认 P1，未确认 P2
    lst = ack_list(client, aid, 1).json()
    assert lst["acknowledged_count"] == 1
    assert [p["number"] for p in lst["acknowledged"]] == [1]
    assert [p["number"] for p in lst["unacknowledged"]] == [2]
    assert lst["acknowledged"][0]["acked_at"] is not None


def test_tournament_not_found(client):
    r = client.get("/tournaments/999/announcements")
    assert r.status_code == 404
    r = client.post(
        "/tournaments/999/announcements",
        json={
            "request_id": "x",
            "expected_event_seq": 0,
            "body": "x",
            "rule": {"participant_ids": [1]},
        },
    )
    assert r.status_code == 404


def test_announcement_works_without_rounds(client):
    """尚无轮次/方案时，可按参赛者 id 与排名区间发布。"""
    tid = create_tournament(client)
    players = add_players(client, tid, ["A", "B", "C"])
    seq = last_seq(client, tid)
    r = publish_announcement(
        client,
        tid,
        "开赛前通知",
        {"rank_from": 1, "rank_to": 3},
        "pre",
        seq,
    )
    assert r.status_code == 201, r.text
    assert r.json()["versions"][0]["recipient_count"] == 3
