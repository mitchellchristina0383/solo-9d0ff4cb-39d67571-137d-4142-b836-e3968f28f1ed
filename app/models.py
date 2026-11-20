"""Pydantic 请求 / 响应模型与输入校验。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_BATCH_SIZE = 50


class Link(BaseModel):
    """双向链路：a/b 两个端点，新旧两版正整数代价。"""

    a: str = Field(..., description="路由器 A 标识")
    b: str = Field(..., description="路由器 B 标识")
    old_cost: int = Field(..., ge=1, description="旧版代价（正整数）")
    new_cost: int = Field(..., ge=1, description="新版代价（正整数）")


class PrecheckRequest(BaseModel):
    target: str = Field(..., description="目标路由器标识")
    links: list[Link] = Field(..., min_length=1, description="双向链路列表")
    batches: list[list[str]] = Field(..., min_length=1, description="分批计划，按切换顺序排列")

    @model_validator(mode="after")
    def _validate(self) -> "PrecheckRequest":
        # 路由器标识不能为空
        if not self.target or not self.target.strip():
            raise ValueError("target 不能为空")
        for lk in self.links:
            if not lk.a or not lk.a.strip() or not lk.b or not lk.b.strip():
                raise ValueError("链路端点标识不能为空")
            if lk.a == lk.b:
                raise ValueError(f"不允许自环链路：{lk.a}")

        nodes: set[str] = set()
        undirected: set[frozenset[str]] = set()
        directed: set[tuple[str, str]] = set()
        for lk in self.links:
            key = frozenset((lk.a, lk.b))
            if key in undirected:
                raise ValueError(f"链路 {lk.a} <-> {lk.b} 重复定义")
            undirected.add(key)
            if (lk.a, lk.b) in directed:
                raise ValueError(f"链路 {lk.a} -> {lk.b} 重复定义")
            directed.add((lk.a, lk.b))
            directed.add((lk.b, lk.a))
            nodes.add(lk.a)
            nodes.add(lk.b)

        if self.target not in nodes:
            raise ValueError(f"目标路由器 {self.target} 不在拓扑中")

        # 分批计划校验
        if any(len(batch) == 0 for batch in self.batches):
            raise ValueError("不允许空批次")
        for i, batch in enumerate(self.batches, start=1):
            if len(batch) > MAX_BATCH_SIZE:
                raise ValueError(f"第 {i} 批包含 {len(batch)} 台设备，超过单批 {MAX_BATCH_SIZE} 台上限")
            if len(set(batch)) != len(batch):
                raise ValueError(f"第 {i} 批内部存在重复设备")
            for router in batch:
                if not router or not router.strip():
                    raise ValueError(f"第 {i} 批存在空设备标识")
                if router not in nodes:
                    raise ValueError(f"第 {i} 批中的设备 {router} 不在拓扑中")
        planned = [r for batch in self.batches for r in batch]
        if len(set(planned)) != len(planned):
            raise ValueError("分批计划中存在跨批次重复的设备")
        if set(planned) != nodes:
            missing = sorted(nodes - set(planned))
            raise ValueError(f"分批计划必须覆盖所有路由器，缺少：{missing}")
        return self


class Hop(BaseModel):
    router: str
    next_hop: str
    via: str = Field(..., description="该出边来自 old / new / both 哪一版下一跳")


class Danger(BaseModel):
    batch_index: int = Field(..., description="首个危险批次（从 1 开始）")
    switched_devices: list[str] = Field(..., description="该批已切换到新版下一跳的设备")
    cycle_routers: list[str] = Field(..., description="闭合转发路径上的路由器（首尾相同）")
    forwarding_cycle: list[Hop] = Field(..., description="逐跳转发路径及所用代价版本")


class PrecheckResponse(BaseModel):
    status: str = Field(..., description="pass 或 danger")
    target: str
    batches_total: int
    batches_checked: int = Field(..., description="实际检查到第几批（危险时即危险批次）")
    old_next_hops: dict[str, list[str]]
    new_next_hops: dict[str, list[str]]
    danger: Danger | None = None

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "status": "pass",
                    "target": "T",
                    "batches_total": 2,
                    "batches_checked": 2,
                    "old_next_hops": {"A": ["T"], "B": ["T"], "T": []},
                    "new_next_hops": {"A": ["B"], "B": ["T"], "T": []},
                    "danger": None,
                }
            ]
        }
    }


# ======================================================================
# 现行转发路径合规核查
# ======================================================================


class BusinessSpec(BaseModel):
    """一项业务：起终点、必须依序经过的设备、必须避开的设备。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    id: str = Field(..., min_length=1, description="业务唯一标识")
    source: str = Field(..., min_length=1, description="起点设备")
    target: str = Field(..., min_length=1, description="终点设备")
    must_pass: list[str] = Field(
        default_factory=list,
        description="必须按出现顺序依次经过的设备；提前路过不算数，之后须再次经过",
    )
    must_avoid: list[str] = Field(default_factory=list, description="必须避开的设备")

    @model_validator(mode="after")
    def _validate(self) -> "BusinessSpec":
        if self.source == self.target:
            raise ValueError(f"业务 {self.id} 的起点与终点不能相同")
        for name in self.must_pass:
            if not name:
                raise ValueError(f"业务 {self.id} 的必经设备标识不能为空")
        for name in self.must_avoid:
            if not name:
                raise ValueError(f"业务 {self.id} 的避开设备标识不能为空")
        if len(set(self.must_pass)) != len(self.must_pass):
            raise ValueError(f"业务 {self.id} 的必经设备列表存在重复")
        if len(set(self.must_avoid)) != len(self.must_avoid):
            raise ValueError(f"业务 {self.id} 的避开设备列表存在重复")
        overlap = sorted(set(self.must_pass) & set(self.must_avoid))
        if overlap:
            raise ValueError(f"业务 {self.id} 的同一设备不能既必经又避开：{overlap}")
        if self.source in self.must_avoid or self.target in self.must_avoid:
            raise ValueError(f"业务 {self.id} 的起点/终点不能在避开列表中")
        if self.source in self.must_pass:
            raise ValueError(f"业务 {self.id} 的起点不能同时出现在必经列表中")
        if self.target in self.must_pass:
            raise ValueError(f"业务 {self.id} 的终点不能同时出现在必经列表中")
        return self


class ReportRequest(BaseModel):
    """每次上报：监控侧单调递增版次 + 当次全部停用链路。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    revision: int = Field(..., ge=1, description="监控侧递增版次（正整数）")
    disabled_links: list[list[str]] = Field(
        default_factory=list,
        description='本次全部停用链路，每项为 ["设备A", "设备B"]（无向，顺序不限）',
    )

    @model_validator(mode="after")
    def _validate(self) -> "ReportRequest":
        seen: set[frozenset[str]] = set()
        for i, link in enumerate(self.disabled_links, start=1):
            if len(link) != 2:
                raise ValueError(f"第 {i} 项停用链路必须恰好包含两个端点")
            a, b = link
            if not a or not b:
                raise ValueError(f"第 {i} 项停用链路端点不能为空")
            if a == b:
                raise ValueError(f"第 {i} 项停用链路不能是自环：{a}")
            key = frozenset((a, b))
            if key in seen:
                raise ValueError(f"停用链路 {a} <-> {b} 重复")
            seen.add(key)
        return self


class BusinessCheck(BaseModel):
    """单项业务的核查结论。"""

    id: str
    source: str
    target: str
    status: Literal["ok", "unreachable", "violating"] = Field(
        ...,
        description="ok=存在合规最短路径；unreachable=剩余拓扑上不可达；"
        "violating=可达但全部最短路径都违规",
    )
    path: list[str] = Field(
        default_factory=list,
        description="status=ok 时为一条合规最短路径；status=violating 时为一条违规最短路径；"
        "unreachable 时为空",
    )
    cost: int | None = Field(None, description="路径总代价；unreachable 时为 null")
    avoided_devices: list[str] = Field(
        default_factory=list, description="违规最短路径上踩中的避开设备"
    )
    missed_must_pass: list[str] = Field(
        default_factory=list, description="违规最短路径上未按序经过的必经设备"
    )
    detail: str = Field(..., description="人类可读的结论说明")


class ReportSnapshot(BaseModel):
    """已生效的整份报告（查询返回的就是这个结构）。"""

    revision: int
    generated_at: datetime
    disabled_links: list[list[str]] = Field(..., description="生成该报告时使用的全部停用链路")
    topology_nodes: int
    topology_links: int
    summary: dict[str, int] = Field(
        ..., description="各状态计数：ok / unreachable / violating"
    )
    businesses: list[BusinessCheck]


class ReportStatus(BaseModel):
    """当前报告与重算状态。"""

    revision: int
    generated_at: datetime
    summary: dict[str, int]
    computing: bool = Field(..., description="是否正在重算")
    computing_revisions: list[int] = Field(
        default_factory=list, description="正在重算的版次（可能有多版乱序完成）"
    )
    last_rejected: int | None = Field(
        None, description="最近一次算完但被拒收的版次：成功但版次不高于现行报告"
    )
    last_failed: int | None = Field(None, description="最近一次计算失败的版次")


class ReportRequestAccepted(BaseModel):
    revision: int
    accepted: bool
    active_revision: int
    message: str


class ReportResponse(BaseModel):
    """查询结果：整份报告 + 对应版次；无现行报告时 report 为 null。"""

    active_revision: int | None
    report: ReportSnapshot | None
    status: ReportStatus | None


# ======================================================================
# 启动配置
# ======================================================================


class ConfigLink(BaseModel):
    """启动时配置的双向链路，代价固定。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    a: str = Field(..., min_length=1)
    b: str = Field(..., min_length=1)
    cost: int = Field(..., ge=1)

    @model_validator(mode="after")
    def _validate(self) -> "ConfigLink":
        if self.a == self.b:
            raise ValueError(f"不允许自环链路：{self.a}")
        return self


class TopologyConfig(BaseModel):
    """启动时加载的一套拓扑 + 业务清单。"""

    links: list[ConfigLink] = Field(..., min_length=1)
    businesses: list[BusinessSpec] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _validate(self) -> "TopologyConfig":
        seen_links: set[frozenset[str]] = set()
        nodes: set[str] = set()
        for lk in self.links:
            key = frozenset((lk.a, lk.b))
            if key in seen_links:
                raise ValueError(f"链路 {lk.a} <-> {lk.b} 重复定义")
            seen_links.add(key)
            nodes.add(lk.a)
            nodes.add(lk.b)

        seen_ids: set[str] = set()
        for biz in self.businesses:
            if biz.id in seen_ids:
                raise ValueError(f"业务标识 {biz.id} 重复")
            seen_ids.add(biz.id)
            referenced = {biz.source, biz.target, *biz.must_pass, *biz.must_avoid}
            missing = sorted(referenced - nodes)
            if missing:
                raise ValueError(f"业务 {biz.id} 引用了拓扑中不存在的设备：{missing}")
        return self

    @property
    def nodes(self) -> set[str]:
        return {lk.a for lk in self.links} | {lk.b for lk in self.links}


