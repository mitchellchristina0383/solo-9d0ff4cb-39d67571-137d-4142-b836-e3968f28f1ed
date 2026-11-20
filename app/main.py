"""FastAPI 入口：链路代价分批调整离线预检 + 现行转发路径合规核查。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse

from .core import collect_nodes, is_connected, run_precheck
from .models import (
    PrecheckRequest,
    PrecheckResponse,
    ReportRequest,
    ReportRequestAccepted,
    ReportResponse,
)
from .reporting import ReportStore, load_topology_config

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        topology = load_topology_config()
    except Exception as exc:  # noqa: BLE001 - 启动配置错误直接让容器失败并留下日志
        logger.error("启动配置加载失败：%s", exc)
        raise
    app.state.report_store = ReportStore(topology)
    logger.info(
        "已加载拓扑：%s 台设备、%s 条链路、%s 项业务",
        len(topology.nodes),
        len(topology.links),
        len(topology.businesses),
    )
    yield
    store: ReportStore = app.state.report_store
    store.wait_idle(timeout=5.0)


app = FastAPI(
    title="链路代价分批调整离线预检 & 现行转发路径核查",
    version="1.1.0",
    description=(
        "**分批改动预检**：提交连通路由拓扑、每条双向链路的新旧正整数代价、"
        "目标路由器与覆盖所有路由器的分批计划，逐批判断错峰切换过程中是否可能"
        "出现转发环路。等价最短路径的所有下一跳均视为可能被选中。\n\n"
        "**现行核查报告**：拓扑与业务清单在启动时由 `examples/report_demo.json` "
        "配置（可用 `TOPOLOGY_CONFIG_PATH` 覆盖）。每次上报带监控侧递增版次与"
        "当时全部停用链路，服务在剩余链路上核查每项业务是否存在合规最短路径。"
        "重算在后台进行，期间查询仍返回上一份完整报告；只有成功算完且版次高于"
        "现行报告的结果才会替换它。\n\n"
        "**演示重算窗口**：设置环境变量 `REPORT_COMPUTE_DELAYS`（如 "
        '`{"2": 3}`）可让指定版次的重算等待若干秒，便于在 /docs 观察'
        "“重算期间仍返回上一份报告”。"
    ),
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get(
    "/reports/topology",
    summary="查看启动配置的拓扑与业务清单",
)
def report_topology(request: Request) -> dict:
    topology = _store(request).topology
    return {
        "nodes": sorted(topology.nodes),
        "links": [lk.model_dump() for lk in topology.links],
        "businesses": [biz.model_dump() for biz in topology.businesses],
    }


@app.post("/precheck", response_model=PrecheckResponse, summary="提交分批计划并执行环路预检")
def precheck(req: PrecheckRequest) -> PrecheckResponse:
    nodes = collect_nodes(req.links)
    if not is_connected(nodes, req.links, req.target):
        # 连通性在模型层之后二次确认（模型已保证端点一致，这里防御孤立分量）
        raise HTTPException(status_code=422, detail="拓扑必须连通，且目标路由器可达所有设备")

    result = run_precheck(nodes, req.links, req.target, req.batches)
    danger = result["dangerous"]
    return PrecheckResponse(
        status="danger" if danger else "pass",
        target=req.target,
        batches_total=len(req.batches),
        batches_checked=result["batches_checked"],
        old_next_hops=result["old_nh"],
        new_next_hops=result["new_nh"],
        danger=danger,
    )


def _store(request: Request) -> ReportStore:
    return request.app.state.report_store


@app.post(
    "/reports",
    response_model=ReportRequestAccepted,
    status_code=202,
    summary="上报版次与全部停用链路，触发后台重算",
)
def submit_report(req: ReportRequest, request: Request) -> ReportRequestAccepted:
    store = _store(request)
    unknown = store.unknown_disabled_links(req.disabled_links)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"停用链路不在启动配置的拓扑中：{sorted(sorted(pair) for pair in unknown)}",
        )

    accepted, message = store.submit(req.revision, req.disabled_links)
    active, _ = store.current()
    active_revision = active.revision if active else 0
    return ReportRequestAccepted(
        revision=req.revision,
        accepted=accepted,
        active_revision=active_revision,
        message=message,
    )


@app.get(
    "/reports/current",
    response_model=ReportResponse,
    summary="查询现行整份报告与对应版次",
)
def current_report(request: Request) -> ReportResponse:
    active, status = _store(request).current()
    return ReportResponse(
        active_revision=active.revision if active else None,
        report=active,
        status=status,
    )
