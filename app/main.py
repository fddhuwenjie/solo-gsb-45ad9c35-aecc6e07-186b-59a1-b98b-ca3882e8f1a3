import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .database import init_db
from .events.bus import event_bus
from .routers import clock, events, rounds, stations, tournaments


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 实时事件总线绑定主事件循环：同步端点线程由此向 SSE 订阅者投递
    event_bus.bind_loop(asyncio.get_running_loop())
    init_db()
    yield
    event_bus.bind_loop(None)


app = FastAPI(
    title="瑞士轮赛务 API",
    description=(
        "编程挑战赛瑞士轮编排服务：创建赛事、录入参赛者与每轮赛果，"
        "按积分分组生成下一轮（禁止重复交手、全局最小化跨组积分差、"
        "奇数人轮空给未轮空且排名最低者），并提供含 Buchholz 分的排名。"
        "支持维护赛事工位（编号/可用状态/能力标签）与参赛者必需标签，"
        "为每一轮发布版本化工位分配方案（最多分配 → 最少换位 → "
        "固定裁决，容量不足时保存最大可行方案并说明原因）。"
        "支持持久化轮次计时：start/pause/resume/extend 命令经幂等键与"
        "乐观版本控制落库，剩余时间由服务端 UTC 锚点与累计暂停时长推算，"
        "重启后读数连续；晚于该场截止时刻的赛果拒绝录入。"
        "另提供每赛事独立序号、可断线续传的 SSE 赛事事件流。"
    ),
    version="1.3.0",
    lifespan=lifespan,
)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(tournaments.router)
app.include_router(rounds.router)
app.include_router(stations.router)
app.include_router(clock.router)
app.include_router(events.router)
