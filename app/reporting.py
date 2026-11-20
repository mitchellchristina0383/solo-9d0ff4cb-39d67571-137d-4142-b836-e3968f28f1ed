"""现行转发路径合规核查：算法与现行报告存储。

核查语义
--------
- 拓扑在启动时固定，链路均为双向、同代价；每次上报只提供“当时全部停用链路”。
- “剩余链路”= 配置链路 - 停用链路。停用链路从图中移除，其余代价不变。
- 业务的最短路径在剩余链路上计算，*不*预先剔除“须避开”的设备：
  避开设备只用于判定合规性。这样才能区分
    * unreachable：剩余链路上起点根本到不了终点；
    * violating：可达，但所有等代价最短路径都违反“依序经过 / 避开”约束。
- “依序经过”要求路径按出现顺序依次访问 must_pass 中的每一项：提前路过后
  面的必经设备不算数，之后仍须再次按序经过；“避开”要求路径不出现
  must_avoid 中任何设备。
- 等价最短路径全部考虑：合规路径的代价必须恰好等于无约束最短代价。约束下
  的最短路由状态空间 Dijkstra 求得（状态为“当前设备 + 已按序匹配的必经
  数量”），代价比无约束最短路长即说明不存在合规的最短路径。此时返回一条
  无约束的最短路径作为“违规的最短路径”样例。
"""

from __future__ import annotations

import heapq
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import (
    BusinessCheck,
    BusinessSpec,
    ConfigLink,
    ReportSnapshot,
    ReportStatus,
    TopologyConfig,
)

logger = logging.getLogger(__name__)

INF = float("inf")
DEFAULT_CONFIG_RELATIVE = Path("examples") / "report_demo.json"

Adjacency = dict[str, dict[str, int]]


# ----------------------------------------------------------------------
# 图与最短路
# ----------------------------------------------------------------------
def build_remaining_adjacency(
    links: Iterable[ConfigLink], disabled: Iterable[frozenset[str]]
) -> Adjacency:
    """由配置链路扣除停用链路，构造无向邻接表。"""
    disabled_set = set(disabled)
    adj: Adjacency = {}
    for lk in links:
        if frozenset((lk.a, lk.b)) in disabled_set:
            continue
        adj.setdefault(lk.a, {})[lk.b] = lk.cost
        adj.setdefault(lk.b, {})[lk.a] = lk.cost
    return adj


def dijkstra_distances(
    adj: Adjacency, source: str, blocked: frozenset[str] = frozenset()
) -> dict[str, float]:
    """非负代价 Dijkstra；blocked 中的设备不可经过（起点/终点由调用方保证不在其中）。"""
    dist: dict[str, float] = {n: INF for n in adj}
    if source not in adj:
        return dist
    dist[source] = 0
    pq: list[tuple[int, str]] = [(0, source)]
    while pq:
        d, u = heapq.heappop(pq)
        if d != dist[u]:
            continue
        for v, w in adj[u].items():
            if v in blocked:
                continue
            nd = d + w
            if nd < dist[v]:
                dist[v] = nd
                heapq.heappush(pq, (nd, v))
    return dist


def shortest_path(
    adj: Adjacency,
    source: str,
    target: str,
    blocked: frozenset[str] = frozenset(),
) -> tuple[int, list[str]] | None:
    """确定性的最短路径；不可达返回 None。

    从终点做一次 Dijkstra 得到各点到终点的最短距离，再从起点沿
    ``dist_target[cur] == w + dist_target[v]`` 走出去。这样每一步距离都
    严格递减，不会误入“离起点更近、却不在通往终点的最短路”的分支。
    所有等代价候选邻居中取标识最小者，保证结果确定。
    """
    if source == target:
        return 0, [source]
    if source not in adj or target not in adj:
        return None
    to_target = dijkstra_distances(adj, target, blocked)
    if to_target[source] == INF:
        return None

    path = [source]
    cur = source
    while cur != target:
        choices = [
            v
            for v, w in adj[cur].items()
            if v not in blocked and to_target[v] < INF and w + to_target[v] == to_target[cur]
        ]
        if not choices:
            # 理论上不可达：到终点距离严格递减且最终为 0，必然存在下一跳
            raise RuntimeError(f"最短路径重建失败：{source} -> {target} 在 {cur} 处中断")
        cur = min(choices)
        path.append(cur)
    return int(to_target[source]), path


# ----------------------------------------------------------------------
# 业务核查
# ----------------------------------------------------------------------
def constrained_shortest_path(
    adj: Adjacency,
    source: str,
    target: str,
    must_pass: Iterable[str] = (),
    blocked: frozenset[str] = frozenset(),
) -> tuple[int, list[str]] | None:
    """在“避开 blocked、按序经过 must_pass”约束下的最短路。

    状态为 ``(设备, 已按序匹配的必经数量)``：只有走到 must_pass[k] 才推进到
    k+1；提前路过后面的必经设备不算数，之后仍须按序再次经过。这样求得的是
    严格满足“依序经过”的最小代价，且仍是多项式复杂度
    ``O(V·|must_pass| · log(V·|must_pass|))``。

    不可达返回 None；等代价时按状态 (设备, 序号) 的最小者确定性地取一条。
    """
    waypoints = list(must_pass)
    need = len(waypoints)
    if source == target and need == 0:
        return 0, [source]
    if source not in adj or target not in adj:
        return None

    start = (source, 0)
    dist: dict[tuple[str, int], int] = {start: 0}
    pq: list[tuple[int, str, int]] = [(0, source, 0)]
    while pq:
        d, u, k = heapq.heappop(pq)
        if d != dist.get((u, k)):
            continue
        for v, w in adj[u].items():
            if v in blocked:
                continue
            k2 = k + 1 if k < need and v == waypoints[k] else k
            state = (v, k2)
            nd = d + w
            if nd < dist.get(state, INF):
                dist[state] = nd
                heapq.heappush(pq, (nd, v, k2))

    goal = (target, need)
    if goal not in dist:
        return None

    # 从目标状态反向回溯：每步选取字典序最小的可行前驱，结果确定。
    # 代价为正整数且沿路严格递减，因此回溯步数不超过总代价，状态不会重复。
    rev_nodes: list[str] = []
    state = goal
    guard = dist[goal] + 2
    while state != start:
        guard -= 1
        if guard < 0:
            raise RuntimeError(f"约束最短路回溯失败：{source} -> {target}")
        u, k = state
        rev_nodes.append(u)
        best_prev: tuple[str, int] | None = None
        for v, w in adj[u].items():
            if v in blocked:
                continue
            for kp in ({k, k - 1} if k > 0 else {0}):
                if kp < 0 or kp > need:
                    continue
                expected = kp + 1 if kp < need and u == waypoints[kp] else kp
                if expected != k:
                    continue
                if dist.get((v, kp), INF) + w == dist[state]:
                    cand = (v, kp)
                    if best_prev is None or cand < best_prev:
                        best_prev = cand
        if best_prev is None:
            raise RuntimeError(f"约束最短路回溯失败：{source} -> {target} 在 {u} 处中断")
        state = best_prev

    rev_nodes.append(source)
    path = list(reversed(rev_nodes))
    return dist[goal], path


def _missing_must_pass(path: list[str], must_pass: list[str]) -> list[str]:
    """按顺序在路径中匹配必经设备，返回未能按序匹配的剩余项。"""
    idx = 0
    for node in path:
        if idx < len(must_pass) and node == must_pass[idx]:
            idx += 1
    return must_pass[idx:]


def check_business(adj: Adjacency, biz: BusinessSpec) -> BusinessCheck:
    """核查单项业务，返回结构化结论。"""
    unrestricted = shortest_path(adj, biz.source, biz.target)
    if unrestricted is None:
        return BusinessCheck(
            id=biz.id,
            source=biz.source,
            target=biz.target,
            status="unreachable",
            path=[],
            cost=None,
            avoided_devices=[],
            missed_must_pass=[],
            detail="剩余链路下起点无法到达终点",
        )

    best_cost, best_path = unrestricted

    # 合规最短路：避开设备不可经过，必经设备按序匹配。
    compliant = constrained_shortest_path(
        adj,
        biz.source,
        biz.target,
        must_pass=biz.must_pass,
        blocked=frozenset(biz.must_avoid),
    )

    if compliant is not None and compliant[0] == best_cost:
        compliant_cost, compliant_path = compliant
        return BusinessCheck(
            id=biz.id,
            source=biz.source,
            target=biz.target,
            status="ok",
            path=compliant_path,
            cost=compliant_cost,
            avoided_devices=[],
            missed_must_pass=[],
            detail=f"存在合规最短路径，代价 {compliant_cost}",
        )

    avoided = sorted({n for n in best_path if n in biz.must_avoid})
    missed = _missing_must_pass(best_path, biz.must_pass)
    reasons: list[str] = []
    if avoided:
        reasons.append(f"经过须避开的设备 {avoided}")
    if missed:
        reasons.append(f"未按序经过必经设备 {missed}")
    if not reasons:
        if compliant is None:
            reasons.append("在避开约束下不存在任何可达的合规路径")
        else:
            reasons.append(
                f"最短的合规路径代价 {compliant[0]}，高于无约束最短代价 {best_cost}"
            )
    detail = (
        f"可达，但全部最短路径（代价 {best_cost}）均不合规："
        + "；".join(reasons)
    )
    return BusinessCheck(
        id=biz.id,
        source=biz.source,
        target=biz.target,
        status="violating",
        path=best_path,
        cost=best_cost,
        avoided_devices=avoided,
        missed_must_pass=missed,
        detail=detail,
    )


def build_snapshot(
    topology: TopologyConfig,
    revision: int,
    disabled_links: list[list[str]],
    generated_at: datetime | None = None,
) -> ReportSnapshot:
    """在剩余链路上核查全部业务，生成一整份报告。"""
    disabled = [frozenset(pair) for pair in disabled_links]
    adj = build_remaining_adjacency(topology.links, disabled)
    checks = [check_business(adj, biz) for biz in topology.businesses]

    summary = {"ok": 0, "unreachable": 0, "violating": 0}
    for check in checks:
        summary[check.status] += 1

    remaining_links = sum(len(neighbors) for neighbors in adj.values()) // 2

    return ReportSnapshot(
        revision=revision,
        generated_at=generated_at or datetime.now(timezone.utc),
        disabled_links=sorted([sorted(pair) for pair in disabled_links]),
        topology_nodes=len(topology.nodes),
        topology_links=remaining_links,
        summary=summary,
        businesses=checks,
    )


# ----------------------------------------------------------------------
# 配置加载
# ----------------------------------------------------------------------
def default_config_path() -> Path:
    env = os.environ.get("TOPOLOGY_CONFIG_PATH")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / DEFAULT_CONFIG_RELATIVE


def load_topology_config(path: Path | str | None = None) -> TopologyConfig:
    config_path = Path(path) if path is not None else default_config_path()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"无法读取拓扑配置 {config_path}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"拓扑配置 {config_path} 不是合法 JSON：{exc}") from exc
    return TopologyConfig.model_validate(raw)


def canonicalize_disabled(disabled_links: Iterable[Iterable[str]]) -> list[list[str]]:
    """把停用链路规范为“端点排序、列表排序”的形式，便于去重与稳定展示。"""
    return sorted([sorted(pair) for pair in disabled_links])


# ----------------------------------------------------------------------
# 现行报告存储与后台重算
# ----------------------------------------------------------------------
class ReportStore:
    """维护“现行报告”，后台重算，按版次单调替换。

    并发不变量（由同一把锁保护）：
    - 查询只读 ``_active``，重算期间继续返回上一份完整报告；
    - 一次重算只有成功且 ``revision > 现行版次`` 时才原子替换；
    - 计算失败或版次不够新的结果被丢弃，绝不覆盖现行报告。
    """

    def __init__(self, topology: TopologyConfig):
        self.topology = topology
        self._lock = threading.RLock()
        self._active: ReportSnapshot | None = None
        self._computing: dict[int, datetime] = {}
        self._last_rejected: int | None = None
        self._last_failed: int | None = None
        self._threads: list[threading.Thread] = []

        # 基线报告：启动配置在当前（未停用）拓扑上的结论，版次 0。
        self._active = build_snapshot(topology, 0, [])

    # -- 可替换的延迟钩子：测试与演示用，返回该版次计算前的等待秒数 ----------
    def delay_for_revision(self, revision: int) -> float:
        """按版次读取人为延迟，便于在 /docs 演示“重算期间仍可查旧报告”。

        支持两种环境变量（均为可选）：
        - REPORT_COMPUTE_DELAYS：JSON 对象，如 {"2": 1.5, "3": 0.2}，按版次精确指定；
        - REPORT_COMPUTE_DELAY_SECONDS：所有版次统一的延迟秒数。
        """
        mapping = os.environ.get("REPORT_COMPUTE_DELAYS")
        if mapping:
            try:
                parsed = json.loads(mapping)
                if str(revision) in parsed:
                    return max(0.0, float(parsed[str(revision)]))
            except (ValueError, TypeError):
                logger.warning("REPORT_COMPUTE_DELAYS 不是合法 JSON 对象，已忽略")
        raw = os.environ.get("REPORT_COMPUTE_DELAY_SECONDS", "0")
        try:
            return max(0.0, float(raw))
        except ValueError:
            return 0.0

    def unknown_disabled_links(self, disabled_links: Iterable[list[str]]) -> list[list[str]]:
        known = {frozenset((lk.a, lk.b)) for lk in self.topology.links}
        return [pair for pair in disabled_links if frozenset(pair) not in known]

    def submit(self, revision: int, disabled_links: list[list[str]]) -> tuple[bool, str]:
        """受理一次上报。返回 (是否进入重算, 说明)。"""
        disabled = canonicalize_disabled(disabled_links)
        with self._lock:
            active_rev = self._active.revision if self._active else None
            if active_rev is not None and revision <= active_rev:
                return (
                    False,
                    f"版次 {revision} 不高于现行报告版次 {active_rev}，不会触发重算",
                )
            if revision in self._computing:
                return False, f"版次 {revision} 已在重算中，未重复受理"
            self._computing[revision] = datetime.now(timezone.utc)
            thread = threading.Thread(
                target=self._compute,
                args=(revision, disabled),
                name=f"report-recompute-{revision}",
                daemon=True,
            )
            self._threads.append(thread)
            thread.start()
        return True, f"版次 {revision} 已受理，重算完成后按版次替换现行报告"

    def _compute(self, revision: int, disabled_links: list[list[str]]) -> None:
        snapshot: ReportSnapshot | None = None
        try:
            delay = self.delay_for_revision(revision)
            if delay:
                time.sleep(delay)
            snapshot = build_snapshot(self.topology, revision, disabled_links)
        except Exception:  # noqa: BLE001 - 重算失败必须只记录，不影响现行报告
            logger.exception("版次 %s 重算失败", revision)
            with self._lock:
                self._computing.pop(revision, None)
                self._last_failed = revision
            return

        with self._lock:
            self._computing.pop(revision, None)
            if self._active is None or revision > self._active.revision:
                self._active = snapshot
                logger.info("版次 %s 已成为现行报告", revision)
            else:
                self._last_rejected = revision
                logger.info(
                    "版次 %s 算完但未替换现行版次 %s", revision, self._active.revision
                )

    def current(self) -> tuple[ReportSnapshot | None, ReportStatus | None]:
        with self._lock:
            active = self._active
            if active is None:
                return None, None
            status = ReportStatus(
                revision=active.revision,
                generated_at=active.generated_at,
                summary=dict(active.summary),
                computing=bool(self._computing),
                computing_revisions=sorted(self._computing),
                last_rejected=self._last_rejected,
                last_failed=self._last_failed,
            )
            return active, status

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """等待所有在途重算结束（自测用）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                threads = list(self._threads)
                if not self._computing:
                    break
            for thread in threads:
                thread.join(timeout=0.05)
            time.sleep(0.01)
        with self._lock:
            return not self._computing
