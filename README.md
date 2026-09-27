# 瑞士轮赛务 API（Swiss Tournament API）

基于 **Python + FastAPI + SQLite** 的编程挑战赛瑞士轮编排服务。

- 创建赛事、批量录入参赛者；
- 一键生成下一轮：按当前积分分组，在**禁止重复交手**的前提下
  **全局最小化跨分组积分差之和**；多个最优方案按参赛者编号取
  字典序固定排序；
- 人数为奇数时，**轮空授予尚未轮空且当前排名最低的选手**（轮空计 1 分）；
- 录入每轮赛果（胜 1 / 和 0.5 / 负 0），一轮全部录完自动封盘；
- 排名查询：积分降序，同分按 **Buchholz 分**（对手积分之和）降序，
  再按编号升序；
- **轮次工位分配**：为赛事维护工位（编号 / 可用状态 / 能力标签），
  为参赛者登记必需标签；轮次生成后一键发布**版本化**工位方案——
  依次**最大化已分配对局数**、**最小化选手相对上一轮的换位次数**，
  再按 `(match_id, station_no)` 字典序固定裁决；容量不足时仍保存
  最大可行方案，并逐场说明未分配对局缺少的标签或冲突的已占工位；
- **持久化轮次计时**：轮次生成后可启动计时（`start` 设定总时长），
  支持全局 `pause` / `resume` 与对指定非轮空对局的 `extend` 加时；
  剩余时间由服务端 UTC 锚点与累计暂停时长在读取时推算，**不运行
  逐秒写库任务，容器重启后读数连续**；命令经幂等键（`request_id`）
  与乐观版本（`expected_version`）控制，晚于该场截止时刻的赛果
  拒绝录入；
- **可断线续传的赛事事件流（SSE）**：创建赛事、录入参赛者、生成轮次、
  提交赛果、发布工位方案时在同一数据库事务中写入事件；每个赛事内序号由
  `event_counters` 计数器行在事务内原子分配（`INSERT ... ON CONFLICT
  DO UPDATE ... RETURNING`），并发提交同样唯一且连续；现场大屏/解说端
  可订阅实时推送，断线后凭 `Last-Event-ID` 精确续传，并提供按序号
  分页查询的历史事件接口。
- **定向赛务公告与确认回执**：按规则并集圈定收件人（指定参赛者、
  当前排名区间、某版工位方案中占用指定工位号的对局双方），再应用
  排除名单；发布时在同一写事务内校验赛事事件序号快照未变化，并把
  去重后的收件人冻结为快照。公告版本化、不可改写，修订只生成新版本
  并重新冻结收件人，各版收件人独立提交回执（重复提交幂等返回原结果，
  旧版回执不沿用到新版本），可查询每版已确认/未确认名单。发布、修订
  与确认均写入赛事事件流。

配对在 Edmonds 带花树（Blossom）最小权完美匹配算法上实现
（`networkx.min_weight_matching`，O(n³)），同代价方案用可加的
整数次级权重精确保证字典序唯一，不存在浮点误差。

## 一条命令启动

```bash
docker compose up -d --build
```

启动后：

| 用途 | 地址 |
| --- | --- |
| API 服务（健康检查） | http://localhost:8000/health |
| 交互式接口文档 Swagger UI | http://localhost:8000/docs |
| ReDoc 文档 | http://localhost:8000/redoc |

停止与清理：

```bash
docker compose down          # 停止（保留数据卷）
docker compose down -v       # 停止并删除赛事数据
```

## 配置

所有配置通过环境变量提供（见 `docker-compose.yml`）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite:////data/swiss.db` | SQLAlchemy 数据库地址；容器内 `/data` 已挂载为命名卷 `swiss-data`，数据持久化 |
| `SSE_HEARTBEAT_SECONDS` | `15` | SSE 心跳间隔（秒）：无事件时定期发送 `: keep-alive` 注释保活 |
| `EVENTS_PAGE_LIMIT_CEILING` | `200` | 事件分页接口每页最大条数 |

端口映射在 `docker-compose.yml` 的 `ports` 中调整（默认宿主机 `8000`）。

### 本地（无 Docker）运行

```bash
pip install -r requirements.txt
export DATABASE_URL="sqlite:///./swiss.db"   # 可选
uvicorn app.main:app --reload
```

## 接口示例

### 1. 创建赛事

```bash
curl -X POST http://localhost:8000/tournaments \
  -H "Content-Type: application/json" \
  -d '{"name": "2026 秋季编程挑战赛"}'
# {"id":1,"name":"2026 秋季编程挑战赛"}
```

### 2. 录入参赛者（编号按顺序自动从 1 分配）

```bash
curl -X POST http://localhost:8000/tournaments/1/participants/bulk \
  -H "Content-Type: application/json" \
  -d '{"names": ["Alice", "Bob", "Cara", "Dan", "Eve"]}'
```

也可单个录入：`POST /tournaments/{id}/participants`，body `{"name": "Alice"}`。

> 首轮生成后赛事名单即锁定，不能再增删参赛者。

### 3. 生成下一轮

```bash
curl -X POST http://localhost:8000/tournaments/1/rounds/next
```

```json
{
  "id": 1, "round_no": 1, "status": "open",
  "matches": [
    {"id": 1, "player_white": {"id": 1, "number": 1, "name": "Alice"},
               "player_black": {"id": 2, "number": 2, "name": "Bob"},
     "score_white": null, "score_black": null, "is_bye": false},
    {"id": 2, "player_white": {"id": 3, "number": 3, "name": "Cara"},
               "player_black": {"id": 4, "number": 4, "name": "Dan"},
     "score_white": null, "score_black": null, "is_bye": false},
    {"id": 3, "player_white": {"id": 5, "number": 5, "name": "Eve"},
               "player_black": null,
     "score_white": 1.0, "score_black": null, "is_bye": true}
  ]
}
```

- 首轮所有人同分为 0，按编号顺序相邻配对（字典序最小方案）；
- 奇数人的轮空场次 `is_bye=true`，轮空方自动得 1 分，无需录入；
- 上一轮未全部录完时调用会返回 `409`。

### 4. 录入赛果

每场得分只能取 `0`、`0.5`、`1`，双方之和必须为 `1`：

```bash
curl -X PUT http://localhost:8000/matches/1/result \
  -H "Content-Type: application/json" \
  -d '{"score_white": 1.0, "score_black": 0.0}'

# Cara 与 Dan 和棋（该轮最后一场录入后轮次自动 closed）
curl -X PUT http://localhost:8000/matches/2/result \
  -H "Content-Type: application/json" \
  -d '{"score_white": 0.5, "score_black": 0.5}'
```

### 5. 查询排名（含 Buchholz 分）

```bash
curl http://localhost:8000/tournaments/1/standings
```

```json
[
  {"rank": 1, "participant_id": 1, "number": 1, "name": "Alice",
   "points": 1.0, "buchholz": 0.0, "played": 1, "byes": 0},
  {"rank": 2, "participant_id": 5, "number": 5, "name": "Eve",
   "points": 1.0, "buchholz": 0.0, "played": 0, "byes": 1},
  {"rank": 3, "participant_id": 3, "number": 3, "name": "Cara",
   "points": 0.5, "buchholz": 0.5, "played": 1, "byes": 0},
  {"rank": 4, "participant_id": 4, "number": 4, "name": "Dan",
   "points": 0.5, "buchholz": 0.5, "played": 1, "byes": 0},
  {"rank": 5, "participant_id": 2, "number": 2, "name": "Bob",
   "points": 0.0, "buchholz": 1.0, "played": 1, "byes": 0}
]
```

排名规则：`points` 降序 → `buchholz`（所有实际对阵对手的累计积分之和，
轮空不计对手）降序 → `number` 升序。上例中 Alice、Eve 同为 1 分
且 Buchholz 均为 0，编号小的 Alice 居首；Bob 虽 0 分，但因输给
Alice，Buchholz 为 1.0。

### 6. 订阅赛事事件流（SSE，供现场大屏 / 解说端）

`GET /tournaments/{id}/events/stream` 以 `text/event-stream` 持续推送
该赛事的事件。服务默认监听 **8000 端口**，订阅命令：

```bash
# 从头订阅（先补发全部历史事件，再实时推送）
curl -N http://localhost:8000/tournaments/1/events/stream

# 断线续传：从序号 5 之后继续（等效于浏览器 EventSource 自动
# 携带的 Last-Event-ID: 5 请求头）
curl -N -H "Last-Event-ID: 5" \
  http://localhost:8000/tournaments/1/events/stream

# 也可以用查询参数指定起点
curl -N "http://localhost:8000/tournaments/1/events/stream?after=5"
```

每条消息的格式：

```
id: 6
event: round.generated
data: {"tournament_id": 1, "seq": 6, "type": "round.generated",
       "occurred_at": "2026-09-25T08:30:00.123456Z",
       "payload": {"round_id": 2, "round_no": 2, "status": "open",
                   "match_ids": [4, 5], "bye_match_id": 6}}
```

- `id` 即赛事内单调递增序号（每个赛事独立、从 1 开始）；浏览器
  `EventSource` 断线重连时会自动带上 `Last-Event-ID`，服务端先补发
  该序号之后的全部持久化事件，再无缝切换为实时推送——重放与实时
  切换期间不漏发、不乱序、不重复；
- `data` 为 JSON 信封：事件类型（`type`）、UTC 发生时间
  （`occurred_at`，ISO 8601）与相关资源标识（`payload`，如
  `participant_id` / `round_id` / `match_id`）；
- 无事件时定期发送 `: keep-alive` 心跳注释（间隔见
  `SSE_HEARTBEAT_SECONDS`），防止代理断开空闲连接；
- 事件类型：`tournament.created`、`participant.registered`、
  `round.generated`、`result.submitted`、`round.closed`、
  `station_plan.published`、`round_clock.started`、
  `round_clock.paused`、`round_clock.resumed`、
  `round_clock.extended`、`announcement.published`、
  `announcement.revised`、`announcement.acknowledged`；
- 事件与业务变更在**同一数据库事务**中写入：事务回滚则事件一并
  回滚，不会出现"有事件无变更"或"有变更无事件"；
- **并发安全**：事件序号不再"查询最大 seq 后插入"，而是对
  `event_counters` 计数器行做事务内原子 UPSERT——同一赛事同时提交
  参赛者、轮次或赛果变更时，数据库层裁决互斥，提交的序号唯一且
  连续（无重号、无空号），回滚不占号；进程内写请求另经一把写锁
  串行化，保证业务校验一致、"提交顺序 == 发布顺序"，且提交失败
  绝不广播；
- SSE 端点对实时事件做序号去重与缺口检测：内存中乱序/超前的事件
  会被挂起并与数据库回源结果合并，缓冲溢出则整体回源补发，
  客户端续接不漏、不重、不乱。

> 实时推送基于进程内事件总线，写请求也在进程内串行，请以单进程
> 方式运行（默认 `docker compose up` 即单容器单 worker）。序号的
> 唯一性由数据库原子计数器与唯一约束保证；多进程部署需改用跨进程
> 的事件总线（如 Redis/PostgreSQL LISTEN）。

### 7. 分页查询历史事件

```bash
# 按序号升序，每页 2 条
curl "http://localhost:8000/tournaments/1/events?after=0&limit=2"
```

```json
{
  "items": [
    {"tournament_id": 1, "seq": 1, "type": "tournament.created",
     "payload": {}, "occurred_at": "2026-09-25T08:00:00.000000Z"},
    {"tournament_id": 1, "seq": 2, "type": "participant.registered",
     "payload": {"participant_id": 1, "number": 1, "name": "Alice"},
     "occurred_at": "2026-09-25T08:01:00.000000Z"}
  ],
  "last_seq": 2,
  "has_more": true
}
```

用上一页返回的 `last_seq` 作为下一次的 `after` 即可翻页；
`has_more=false` 表示已读完全部。

### 8. 维护工位与参赛者必需标签

工位有**编号**（赛事内唯一）、**可用状态**与**能力标签**；参赛者可
登记**必需标签**。分配工位时，工位必须可用且其能力标签覆盖对局
双方选手必需标签的并集。

```bash
# 批量新增工位（也可单个 POST /tournaments/1/stations）
curl -X POST http://localhost:8000/tournaments/1/stations/bulk \
  -H "Content-Type: application/json" \
  -d '{"stations": [
        {"station_no": 1, "tags": ["直播"]},
        {"station_no": 2},
        {"station_no": 3, "available": false}
      ]}'

# 更新工位可用状态与能力标签（整体替换）
curl -X PUT http://localhost:8000/tournaments/1/stations/2 \
  -H "Content-Type: application/json" \
  -d '{"available": true, "tags": ["直播", "安静区"]}'

# 登记参赛者的必需标签（创建时也可带 required_tags 字段）
curl -X PUT http://localhost:8000/tournaments/1/participants/1/tags \
  -H "Content-Type: application/json" \
  -d '{"required_tags": ["直播"]}'

curl http://localhost:8000/tournaments/1/stations
```

标签自动去空白、去重、排序；空标签返回 `422`。工位编号重复返回
`409`。首轮生成后仍可调整工位与标签（已发布的方案是快照，不受
后续变更影响）。

### 9. 发布与查询轮次工位方案

轮次生成后，为该轮发布工位分配方案。请求携带：

- `expected_version`：期望的当前最新版本号（尚无方案时为 `0`），
  用于乐观并发控制——不一致返回 `409`，新方案版本为它加 1；
- `pins`：固定工位列表（可为空），指定某场非轮空对局必须使用的
  工位。固定工位**重复**（同一对局/同一工位出现两次）或**不兼容**
  （工位不存在、不可用、不覆盖双方必需标签，或对局为轮空/不存在）
  时返回 `409` 且不落任何数据。

```bash
curl -X POST http://localhost:8000/tournaments/1/rounds/1/station-plans \
  -H "Content-Type: application/json" \
  -d '{"expected_version": 0, "pins": [{"match_id": 2, "station_no": 2}]}'
```

```json
{
  "id": 1, "round_id": 1, "round_no": 1, "version": 1,
  "assignments": [
    {"match_id": 1, "station_no": 1},
    {"match_id": 2, "station_no": 2}
  ],
  "unassigned": [
    {"match_id": 3, "required_tags": [], "missing_tags": [],
     "conflicting_stations": [1, 2],
     "reason": "可覆盖全部必需标签的工位已被占用：1、2"}
  ],
  "created_at": "2026-09-26T08:35:00.123456Z"
}
```

上例中第 1 场对局有选手登记了 `直播` 必需标签，被分到覆盖该标签
且编号最小的 1 号工位；固定工位 `pins` 指定的第 2 场 → 2 号工位
原样保留；第 3 场因可用工位不足未分配，`unassigned` 给出原因。

求解规则（优先级从高到低，详见「工位分配规则细则」）：

1. **最大化已分配对局数**：每场非轮空对局至多占一个可用工位，
   每个工位至多承接一场对局（轮空对局不占工位）；
2. **最小化选手相对上一轮的换位次数**：以上一轮最新方案为基线，
   两轮都被分配且工位编号不同的选手计一次换位；
3. **按 `(match_id, station_no)` 字典序固定裁决**：小编号场优先
   占用小编号的可行工位，结果唯一确定。

**容量不足时仍保存最大可行方案**：`unassigned` 逐场说明原因——
`missing_tags` 为所有可用工位都不提供的必需标签；
`conflicting_stations` 为能覆盖全部必需标签、但本方案中已被
占用的工位编号。

发布成功会在同一事务中写入 `station_plan.published` 事件（载荷含
`round_id`、`round_no`、`plan_id`、`version`、`assignments` 与
`unassigned_match_ids`），SSE 订阅者与事件分页接口立即可见。

方案按版本查询（版本从 1 递增，发布后不可变）：

```bash
# 该轮全部版本
curl http://localhost:8000/tournaments/1/rounds/1/station-plans

# 指定版本
curl http://localhost:8000/tournaments/1/rounds/1/station-plans/1
```

### 10. 轮次计时（持久化时钟）

轮次生成后可为该轮启动计时。所有计时命令走同一个端点，请求体携带：

- `request_id`：幂等键。**重复提交同一 request_id 与相同参数会原样
  返回首次结果**（版本不推进、加时不叠加、事件不重复）；同一
  request_id 携带不同参数返回 `409`；
- `expected_version`：期望的当前时钟版本（未启动时为 `0`），用于
  乐观并发控制——不一致返回 `409`；每个成功的命令使版本 +1；
- `action`：`start` / `pause` / `resume` / `extend` 之一。

```bash
# start：设定轮次总时长（秒）并启动计时（每轮仅一次）
curl -X POST http://localhost:8000/tournaments/1/rounds/1/clock/actions \
  -H "Content-Type: application/json" \
  -d '{"request_id": "r1-start", "expected_version": 0,
       "action": "start", "total_seconds": 3600}'

# pause / resume：全局暂停 / 恢复（暂停冻结所有对局，恢复后延续）
curl -X POST http://localhost:8000/tournaments/1/rounds/1/clock/actions \
  -H "Content-Type: application/json" \
  -d '{"request_id": "r2-pause", "expected_version": 1, "action": "pause"}'
curl -X POST http://localhost:8000/tournaments/1/rounds/1/clock/actions \
  -H "Content-Type: application/json" \
  -d '{"request_id": "r3-resume", "expected_version": 2, "action": "resume"}'

# extend：只给指定非轮空对局加时（可叠加；运行或暂停中均可）
curl -X POST http://localhost:8000/tournaments/1/rounds/1/clock/actions \
  -H "Content-Type: application/json" \
  -d '{"request_id": "r4-extend", "expected_version": 3,
       "action": "extend", "match_id": 2, "seconds": 300}'

# 查询时钟：版本、状态与各场截止时刻 / 剩余秒数
curl http://localhost:8000/tournaments/1/rounds/1/clock
```

```json
{
  "tournament_id": 1, "round_id": 1, "round_no": 1,
  "version": 4, "status": "running",
  "total_seconds": 3600,
  "started_at": "2026-09-26T09:00:00.000000Z",
  "paused_at": null, "ended_at": null,
  "accumulated_pause_seconds": 95.2,
  "server_time": "2026-09-26T09:20:10.000000Z",
  "matches": [
    {"match_id": 1, "extra_seconds": 0,
     "deadline_at": "2026-09-26T10:01:35.200000Z",
     "remaining_seconds": 2465},
    {"match_id": 2, "extra_seconds": 300,
     "deadline_at": "2026-09-26T10:06:35.200000Z",
     "remaining_seconds": 2765}
  ]
}
```

计时规则：

1. **持久化推算**：剩余时间由 `started_at`（服务端 UTC 锚点）、
   `accumulated_pause_seconds`（累计暂停时长）与 `paused_at` /
   `ended_at` 在读取时推算，不运行逐秒写库任务；进程/容器重启后
   读数自然连续。`server_time` 为服务端当前 UTC，供客户端校准。
2. **状态机**：`idle`（未启动）→ `running` ⇄ `paused` → `ended`
   （轮次封盘时自动冻结）。`start` 每轮仅一次；`pause` 仅在运行中、
   `resume` 仅在暂停中可用；非法状态返回 `409` 且不落库。
3. **暂停与恢复**：`pause` 冻结所有对局的剩余时间（暂停中
   `deadline_at` 为 `null`，恢复后才重新确定）；`resume` 把本段
   暂停计入累计暂停后从冻结处延续。暂停中仍可 `extend`。
4. **加时**：`extend` 只作用于指定的**非轮空**对局（轮空对局 `409`，
   对局不存在或不属于该轮 `404`），`seconds` 累计叠加到该场
   `extra_seconds`；已提交赛果的对局不能再加时。
5. **赛果截止**：计时已启动（`running` / `paused`）的轮次中，晚于
   该场截止时刻提交的赛果返回 `409`；对该场 `extend` 加时后可重新
   获得提交窗口。未启动计时的轮次不做截止校验（与旧行为一致）。
6. **封盘**：轮次封盘（全部赛果录完）时时钟自动冻结为 `ended`，
   剩余时间停在封盘时刻；已结束轮次拒绝一切计时命令（`409`），
   已提交的赛果不受任何后续命令影响。
7. **事务与事件**：成功的命令与对应事件（`round_clock.started` /
   `.paused` / `.resumed` / `.extended`，载荷含 `round_id`、
   `round_no`、`version` 及命令参数）在**同一数据库事务**中提交，
   SSE 订阅者与事件分页接口立即可见；被拒绝的命令不产生事件、
   不落任何数据。

### 11. 定向赛务公告与确认回执

赛事进行中可发布定向公告（如通知某排名区间或某工位的选手）。

**发布** `POST /tournaments/{id}/announcements`，请求体：

- `request_id`：幂等键。重复提交同一 request_id 与相同参数原样返回
  首次结果（不重复落库、不占事件序号）；同一 request_id 携带不同
  参数返回 `409`；
- `expected_event_seq`：期望的赛事**当前事件序号**（即客户端所见
  快照；可先 `GET /tournaments/{id}/events` 取 `last_seq`）。发布在
  同一写事务内确认序号未变化——已被其他写请求推进则返回 `409`
  （快照已变化）且不落库；
- `body`：公告正文；
- `rule`：定向收件规则（见下）。

收件规则 `rule` 的三类圈定条件取**并集**（至少指定一类），再应用
排除名单，最后去重冻结：

| 字段 | 含义 |
| --- | --- |
| `participant_ids` | 直接指定的参赛者 id 列表 |
| `rank_from` / `rank_to` | 当前排名闭区间（口径同 `GET /standings`，含进行中轮次） |
| `station_plan` | `{round_no, version, station_nos}`：某轮**某版**工位方案中占用指定工位号的对局双方 |
| `exclude_participant_ids` | 排除名单（并集后整体排除） |

规则无效返回 `409` 且不落库：参赛者/排除名单 id 不属于本赛事、
排名区间超出当前人数、工位方案版本不属于本赛事（轮次或版本不存在）、
工位号在该版方案中没有分配对局、并集排除后没有收件人。

```bash
# 并集：P1 + 当前排名第 4~5 名 + 第 1 轮 v1 方案中占用 1 号工位的
# 对局双方，再排除 P1
curl -X POST http://localhost:8000/tournaments/1/announcements \
  -H "Content-Type: application/json" \
  -d '{"request_id": "ann-1", "expected_event_seq": 8,
       "body": "请相关选手于 10:00 到检录处集合",
       "rule": {
         "participant_ids": [1],
         "rank_from": 4, "rank_to": 5,
         "station_plan": {"round_no": 1, "version": 1, "station_nos": [1]},
         "exclude_participant_ids": [1]
       }}'
```

```json
{
  "id": 1, "tournament_id": 1,
  "created_at": "2026-09-27T08:00:00.000000Z",
  "latest_version": 1,
  "versions": [
    {"id": 1, "version": 1,
     "body": "请相关选手于 10:00 到检录处集合",
     "rule": {"participant_ids": [1], "rank_from": 4, "rank_to": 5,
              "station_plan": {"round_no": 1, "version": 1,
                               "station_nos": [1]},
              "exclude_participant_ids": [1]},
     "recipient_count": 3, "acknowledged_count": 0,
     "created_at": "2026-09-27T08:00:00.000000Z"}
  ]
}
```

收件人在发布事务内**冻结**（保存参赛者 id / 编号 / 姓名快照）：
之后排名变化、工位调整或选手改名都不影响已发布版本的收件人。

**修订（新版本）**：公告正文不可改写，需要更正时调用
`POST /announcements/{id}/revisions`，请求体同发布（`request_id`、
`expected_event_seq`、新正文、新规则）。修订生成新版本（版本号 +1）
并按新规则**重新冻结收件人**；历史版本、旧版收件人及其回执永久保留。
旧版回执**不沿用**——新版本的收件人即使确认过旧版，也必须重新确认。

```bash
curl -X POST http://localhost:8000/announcements/1/revisions \
  -H "Content-Type: application/json" \
  -d '{"request_id": "ann-1-v2", "expected_event_seq": 10,
       "body": "集合时间更正为 10:30",
       "rule": {"rank_from": 1, "rank_to": 8}}'
```

**确认回执**：`POST /announcements/{id}/revisions/{version}/acknowledgements`，
请求体 `{"participant_id": 3}`。

- 只有**该版冻结收件人**可确认：非收件人返回 `403`（旧版收件人不
  能确认新版本，反之亦然），参赛者不属于本赛事 `404`，公告/版本
  不存在 `404`；
- 同一 `participant_id` 重复提交**原样返回首次结果**（相同
  `acked_at`），不重复写事件。

```bash
curl -X POST http://localhost:8000/announcements/1/revisions/1/acknowledgements \
  -H "Content-Type: application/json" \
  -d '{"participant_id": 3}'
# {"announcement_id":1,"version":1,"participant_id":3,
#  "acknowledged":true,"acked_at":"2026-09-27T08:05:00.000000Z"}
```

**查询回执名单**（同路径 `GET`）返回已确认与未确认名单及计数：

```bash
curl http://localhost:8000/announcements/1/revisions/1/acknowledgements
```

```json
{
  "announcement_id": 1, "version": 1,
  "recipient_count": 3, "acknowledged_count": 1,
  "acknowledged": [
    {"participant_id": 3, "number": 3, "name": "Cara",
     "acked_at": "2026-09-27T08:05:00.000000Z"}
  ],
  "unacknowledged": [
    {"participant_id": 2, "number": 2, "name": "Bob", "acked_at": null},
    {"participant_id": 4, "number": 4, "name": "Dan", "acked_at": null}
  ]
}
```

还可通过 `GET /tournaments/{id}/announcements`（公告列表）与
`GET /announcements/{id}`（详情，含全部版本及各版回执计数）查询。

发布、修订与确认回执均在同一数据库事务中写入事件：
`announcement.published`、`announcement.revised`（载荷含
`announcement_id`、`version`、`recipient_count` 与
`recipient_participant_ids`）和 `announcement.acknowledged`（载荷含
`announcement_id`、`version`、`participant_id`），SSE 订阅者与事件
分页接口立即可见；被拒绝的请求不产生事件、不占序号、不落任何数据。

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/tournaments` | 创建赛事 |
| GET | `/tournaments` | 赛事列表 |
| GET | `/tournaments/{id}` | 赛事详情 |
| POST | `/tournaments/{id}/participants` | 录入单个参赛者（可带必需标签） |
| POST | `/tournaments/{id}/participants/bulk` | 批量录入参赛者 |
| GET | `/tournaments/{id}/participants` | 参赛者名单（按编号） |
| PUT | `/tournaments/{id}/participants/{number}/tags` | 登记/更新参赛者必需标签 |
| POST | `/tournaments/{id}/stations` | 新增工位 |
| POST | `/tournaments/{id}/stations/bulk` | 批量新增工位 |
| GET | `/tournaments/{id}/stations` | 工位列表（按编号） |
| PUT | `/tournaments/{id}/stations/{station_no}` | 更新工位可用状态与能力标签 |
| POST | `/tournaments/{id}/rounds/next` | 生成下一轮 |
| GET | `/tournaments/{id}/rounds` | 所有轮次与对阵 |
| GET | `/tournaments/{id}/rounds/{round_no}` | 指定轮次 |
| POST | `/tournaments/{id}/rounds/{round_no}/station-plans` | 发布该轮工位方案（新版本） |
| GET | `/tournaments/{id}/rounds/{round_no}/station-plans` | 该轮全部工位方案版本 |
| GET | `/tournaments/{id}/rounds/{round_no}/station-plans/{version}` | 指定版本的工位方案 |
| POST | `/tournaments/{id}/rounds/{round_no}/clock/actions` | 执行计时命令（start/pause/resume/extend，幂等 + 乐观版本） |
| GET | `/tournaments/{id}/rounds/{round_no}/clock` | 查询时钟版本、状态与各场截止时刻/剩余秒数 |
| POST | `/tournaments/{id}/announcements` | 发布定向公告（规则并集圈定收件人，幂等 + 事件序号快照） |
| GET | `/tournaments/{id}/announcements` | 赛事公告列表（含全部版本与回执统计） |
| GET | `/announcements/{id}` | 公告详情（含全部版本） |
| POST | `/announcements/{id}/revisions` | 公告修订：生成新版本并重新冻结收件人（旧版回执不沿用） |
| POST | `/announcements/{id}/revisions/{version}/acknowledgements` | 该版收件人提交确认回执（重复提交返回原结果） |
| GET | `/announcements/{id}/revisions/{version}/acknowledgements` | 查询该版已确认/未确认名单 |
| PUT | `/matches/{match_id}/result` | 录入/修改一场赛果 |
| GET | `/tournaments/{id}/standings` | 排名（含 Buchholz 分） |
| GET | `/tournaments/{id}/events/stream` | SSE 事件流（支持 `Last-Event-ID` 续传、心跳注释） |
| GET | `/tournaments/{id}/events` | 事件分页查询（`after` / `limit`） |
| GET | `/health` | 健康检查 |

## 配对规则细则

1. 每个积分分组成员优先组内配对；当组内无法消化（人数奇偶、
   重复交手禁忌）时产生跨组配对，算法在所有可行完美匹配中
   最小化跨组积分差之和（半分为最小单位）。
2. 任何两位选手至多交手一次；若因禁忌过多已无可行匹配，
   生成轮次返回 `409` 并说明原因。
3. 多个最优方案的固定排序：把配对表示为 `(小编号, 大编号)`
   并按小编号升序排列，取该配对元组的字典序最小者——等价于
   「编号最小的选手优先搭配编号尽可能小的可行搭档，依次类推」。
4. 奇数人轮空：按当前排名（积分 → Buchholz → 编号）取排名最低
   且尚未轮空者；全部都轮空过时再取排名最低者。

## 工位分配规则细则

1. 可行约束：每场非轮空对局至多占一个**可用**工位，每个工位
   至多承接一场对局；工位能力标签必须**覆盖双方选手必需标签的
   并集**。轮空对局不参与分配。请求中的固定工位（`pins`）视为
   硬约束，求解前校验，重复或不兼容直接 `409` 且不落库。
2. 优化目标按优先级依次满足：先**最大化已分配对局数**；再
   **最小化选手相对上一轮最新方案的换位次数**（仅统计两轮都被
   分配且工位编号不同的选手）；最后按 `(match_id, station_no)`
   字典序固定裁决——把分配表示为按 `match_id` 升序的
   `(match_id, station_no)` 序列，取字典序最小者。
3. 实现：在「对局 × 可用工位」二分图上求最大权匹配
   （`networkx.max_weight_matching`）。三级目标用大整数权重精确
   编码（与瑞士轮配对同一思路），权值唯一确定
   （分配数, 换位数, 逐场工位选择），最优解唯一，不存在浮点误差，
   多次发布同一版本基线结果一致。
4. 容量不足时不报错：保存当前约束下的最大可行方案，
   `unassigned` 逐场给出 `missing_tags`（所有可用工位都不提供的
   必需标签）与 `conflicting_stations`（能覆盖全部必需标签但被
   占用的工位），便于现场增补工位或调整标签后发布下一版。
5. 方案版本化：每轮版本从 1 递增、发布后不可变；`expected_version`
   提供乐观并发控制，发布成功写入 `station_plan.published` 事件。
   已发布方案保存工位编号与未分配说明的快照，后续工位/标签变更
   不影响历史版本。

## 赛务公告规则细则

1. 收件圈定为三类条件的**并集**：显式参赛者 id 列表、当前排名
   闭区间（排名口径与 `GET /standings` 一致，实时计入进行中轮次
   已录入赛果与轮空）、某轮某版工位方案中占用指定工位号的对局
   双方（轮空对局不占工位，不会被选中）。并集去重后整体应用排除
   名单。三类条件至少指定一类（否则 `422`），并集排除后为空
   （规则无效）返回 `409`。
2. 规则在落库前完整校验：参赛者与排除名单 id 必须属于本赛事；
   排名区间必须两端齐全、起点不大于终点且不超出当前人数；
   `station_plan` 的轮次与版本必须属于本赛事，且每个工位号在该版
   方案中都有分配对局。任一不满足返回 `409`，公告、收件人、
   幂等记录与事件均不落库。
3. 乐观快照：`expected_event_seq` 必须等于发布/修订事务内读取到的
   赛事当前事件序号；客户端基于过期快照提交（期间有任何其他写
   请求推进了序号）返回 `409`，客户端重新读取状态后重试。冲突
   不占序号、不留数据。
4. 收件人冻结：发布/修订在同一写事务内把去重后的收件人及当时的
   编号、姓名写入版本快照；之后参赛者/排名/工位方案的变化只影响
   后续新版本，不改变历史版本的收件人。回执只认其所属版本的冻结
   收件人集合——旧版收件人若不在新版本集合中，不能确认新版本
   （`403`），旧版已确认状态也不迁移。
5. 版本化与不可变：公告首次发布为版本 1；`POST /announcements/{id}/revisions`
   只追加新版本，历史版本正文、规则快照、收件人与回执永久保留、
   不可改写。
6. 幂等：发布与修订均以赛事内唯一的 `request_id` 作为幂等键，
   相同参数重放原样返回首次结果（幂等判定先于快照校验，因此重放
   不会因序号已推进而报 `409`）；同一 `request_id` 携带不同参数
   或用于不同操作返回 `409`。回执以 `(版本, participant_id)` 幂等：
   同一收件人重复提交返回首次的 `acked_at`，不重复写事件。
7. 事务与事件：公告/版本/收件人/幂等记录与
   `announcement.published` / `announcement.revised` /
   `announcement.acknowledged` 事件在同一数据库事务中提交，事件
   序号由赛事计数器原子分配，SSE 订阅者与事件分页接口立即可见；
   被拒绝的请求不产生事件、不占序号。

## 测试

```bash
pip install -r requirements-dev.txt
pytest
```

包含：核心算法对暴力枚举的随机对拍（450+ 组）、字典序最优性的
独立序贯校验，端到端 API 流程（含轮空轮换、重复对阵检测、
赛果校验、自动封盘），工位模块专项（工位维护、必需标签登记、
固定工位校验 409 不落库、最多分配/最少换位/字典序裁决、容量
不足的部分方案与原因说明、版本化查询与事件发布），轮次计时专项
（状态机非法转换 409 不落库、版本冲突、request_id 幂等重放与
冲突、暂停冻结/恢复延续、读取时推算无需写库、extend 目标校验、
截止时刻拒绝赛果与加时重开窗口、封盘冻结与命令拒绝、事件同事务
有序发布——时间断言经注入固定时钟完成，不依赖真实等待），赛务公告
专项（三类规则并集 + 排除名单的去重冻结与信息快照、规则无效/外赛事
方案/排名超界/工位无对局 409 不落库、expected_event_seq 快照冲突
409 不落库不占号、并发同快照仅一成一冲突、request_id 幂等重放与
冲突、修订生成新版本重新冻结且旧版回执不沿用、非收件人 403/不存在
404、重复回执幂等不重复写事件、已确认/未确认名单查询、
announcement.* 事件同事务连续），以及
事件流专项测试——事务回滚不留事件、
提交失败不广播且不留事件、原子计数器回滚不占号、并发报名/赛果/
开轮（多线程屏障同时放行）下编号与序号唯一连续、并发提交按序号
有序发布、断线续传、重放/实时切换竞态（不漏/不乱/不重）、实时
乱序缺口的挂起-回源合并、心跳、缓冲溢出回源、赛事隔离与分页查询
（SSE 用例通过线程内真实 uvicorn 服务器消费）。
