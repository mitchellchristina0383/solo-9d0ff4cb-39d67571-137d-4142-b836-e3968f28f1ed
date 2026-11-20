"""现行转发路径合规核查：算法、并发替换语义与 API 自测。

运行（仓库根目录）：
    PYTHONPATH=/tmp/pylibs python -m pytest -q
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import reporting
from app.main import app
from app.models import BusinessSpec, ConfigLink, ReportRequest, TopologyConfig
from app.reporting import (
    ReportStore,
    build_remaining_adjacency,
    build_snapshot,
    check_business,
    constrained_shortest_path,
    load_topology_config,
    shortest_path,
)

DEMO_CONFIG = load_topology_config()


def _biz(**kw) -> BusinessSpec:
    return BusinessSpec(**kw)


def _adj(edges: list[tuple[str, str, int]], disabled: list[tuple[str, str]] = ()):
    links = [ConfigLink(a=a, b=b, cost=c) for a, b, c in edges]
    return build_remaining_adjacency(
        links, [frozenset(pair) for pair in disabled]
    )


# ---------------------------------------------------------------- 算法：最短路与合规
def test_shortest_path_deterministic_and_free_of_same_cost_trap():
    """同代价分支很多时也必须给出真正到终点的一条最短路径。"""
    adj = _adj(
        [
            ("S", "A", 1),
            ("S", "B", 1),
            ("A", "C", 1),
            ("B", "D", 1),
            ("C", "T", 1),
            ("D", "T", 1),
        ]
    )
    assert shortest_path(adj, "S", "T") == (3, ["S", "A", "C", "T"])
    assert shortest_path(adj, "T", "S") == (3, ["T", "C", "A", "S"])


def test_constrained_shortest_path_against_bellman_ford():
    """随机小图上，状态空间 Dijkstra 必须与对展开状态做 Bellman-Ford 松弛的最短距离一致。"""
    import random

    rng = random.Random(20261120)
    nodes = [f"n{i}" for i in range(6)]

    def bf_distance(adj, s, t, must_pass, blocked):
        """在 (设备, 已按序匹配数) 展开图上做纯松弛，不依赖堆，作为独立对照。"""
        states = [(n, k) for n in adj for k in range(len(must_pass) + 1)]
        dist = {st: float("inf") for st in states}
        dist[(s, 0)] = 0
        for _ in range(len(states)):
            changed = False
            for (u, k) in states:
                if dist[(u, k)] == float("inf"):
                    continue
                for v, w in adj[u].items():
                    if v in blocked:
                        continue
                    k2 = k + 1 if k < len(must_pass) and v == must_pass[k] else k
                    if dist[(u, k)] + w < dist[(v, k2)]:
                        dist[(v, k2)] = dist[(u, k)] + w
                        changed = True
            if not changed:
                break
        return dist[(t, len(must_pass))]

    for _ in range(80):
        edges: list[tuple[str, str, int]] = []
        idx = list(range(1, len(nodes)))
        rng.shuffle(idx)
        connected = {0}
        pairs: set[frozenset[int]] = set()
        for v in idx:
            u = rng.choice(tuple(connected))
            pairs.add(frozenset((u, v)))
            edges.append((nodes[u], nodes[v], rng.randint(1, 5)))
            connected.add(v)
        for _ in range(rng.randint(0, 4)):
            u, v = rng.sample(range(len(nodes)), 2)
            if frozenset((u, v)) not in pairs:
                pairs.add(frozenset((u, v)))
                edges.append((nodes[u], nodes[v], rng.randint(1, 5)))

        adj = _adj(edges)
        source, target = "n0", "n5"
        must_pass = rng.sample(["n1", "n2", "n3", "n4"], rng.randint(0, 2))
        avoid = rng.sample(["n1", "n2", "n3", "n4"], rng.randint(0, 1))
        must_pass = [n for n in must_pass if n not in avoid]
        blocked = frozenset(avoid)

        expected = bf_distance(adj, source, target, must_pass, blocked)
        got = constrained_shortest_path(
            adj, source, target, must_pass=must_pass, blocked=blocked
        )
        if expected == float("inf"):
            assert got is None, (edges, must_pass, avoid, got)
            continue
        assert got is not None, (edges, must_pass, avoid, expected)
        cost, path = got
        assert cost == expected, (edges, must_pass, avoid, got, expected)
        assert path[0] == source and path[-1] == target
        assert not set(path) & blocked
        pos = 0
        for node in path:
            if pos < len(must_pass) and node == must_pass[pos]:
                pos += 1
        assert pos == len(must_pass), (path, must_pass)
        # 返回路径逐段必须是真实边，且其代价与返回的 cost 一致
        assert sum(adj[u][v] for u, v in zip(path, path[1:])) == cost


def test_must_pass_out_of_order_is_strict():
    """无约束最短路先经过 B 再经过 A，与必经顺序 A -> B 相反，应判违规。"""
    adj = _adj(
        [
            ("S", "B", 1),
            ("B", "A", 1),
            ("A", "T", 1),
            ("S", "A", 5),
        ]
    )
    # 无约束最短路 S-B-A-T 代价 3，没有先经过 A，故违规
    assert shortest_path(adj, "S", "T") == (3, ["S", "B", "A", "T"])
    check = check_business(adj, _biz(id="x", source="S", target="T", must_pass=["A", "B"]))
    assert check.status == "violating"
    assert check.cost == 3
    assert check.missed_must_pass == ["B"]

    # 合规走法代价 5（如 S-B-A-B-A-T），比无约束最短代价 3 长
    constrained = constrained_shortest_path(adj, "S", "T", must_pass=["A", "B"])
    assert constrained is not None
    cost, path = constrained
    assert cost == 5
    assert path[0] == "S" and path[-1] == "T"
    assert sum(adj[u][v] for u, v in zip(path, path[1:])) == cost


def test_must_pass_ok_when_detour_is_shortest():
    """合规走法恰好也是唯一最短路时，应判 ok 并给出该路径。"""
    adj = _adj(
        [
            ("S", "A", 1),
            ("A", "B", 1),
            ("B", "T", 1),
            ("S", "B", 5),
        ]
    )
    check = check_business(adj, _biz(id="z", source="S", target="T", must_pass=["A", "B"]))
    assert check.status == "ok"
    assert check.cost == 3
    assert check.path == ["S", "A", "B", "T"]


def test_unreachable_after_disable():
    adj = _adj([("S", "A", 1), ("A", "T", 1), ("S", "T", 9)], [("A", "T")])
    assert shortest_path(adj, "S", "T") == (9, ["S", "T"])
    adj2 = _adj([("S", "A", 1), ("A", "T", 1)], [("A", "T")])
    assert shortest_path(adj2, "S", "T") is None


def test_compliant_shortest_path_among_equal_cost_alternatives():
    """两条等价最短路，其中一条踩避开设备——只要另一条合规就应判 ok。"""
    adj = _adj(
        [
            ("S", "BAD", 1),
            ("BAD", "T", 1),
            ("S", "GOOD", 1),
            ("GOOD", "T", 1),
        ]
    )
    check = check_business(adj, _biz(id="b1", source="S", target="T", must_avoid=["BAD"]))
    assert check.status == "ok"
    assert check.cost == 2
    assert check.path == ["S", "GOOD", "T"]
    assert "BAD" not in check.path


def test_all_shortest_paths_violate_is_violating_with_sample_path():
    """唯一最短路经过避开设备，而合规绕路更长：violating，附最短路径。"""
    adj = _adj(
        [
            ("S", "BAD", 1),
            ("BAD", "T", 1),
            ("S", "X", 5),
            ("X", "T", 5),
        ]
    )
    check = check_business(adj, _biz(id="b2", source="S", target="T", must_avoid=["BAD"]))
    assert check.status == "violating"
    assert check.cost == 2
    assert check.path == ["S", "BAD", "T"]
    assert check.avoided_devices == ["BAD"]
    assert check.missed_must_pass == []
    assert "不合规" in check.detail


def test_must_pass_in_order_ok_and_violating():
    adj = _adj(
        [
            ("A", "B", 1),
            ("B", "C", 1),
            ("C", "D", 1),
            ("A", "D", 10),
        ]
    )
    ok = check_business(adj, _biz(id="b3", source="A", target="D", must_pass=["B", "C"]))
    assert ok.status == "ok"
    assert ok.cost == 3
    assert ok.path == ["A", "B", "C", "D"]

    # 必经顺序与拓扑相反：唯一最短路 A-C-T 无法按 B -> C 顺序经过
    adj2 = _adj(
        [
            ("A", "B", 1),
            ("A", "C", 1),
            ("B", "D", 1),
            ("C", "D", 1),
            ("B", "C", 5),
        ]
    )
    bad = check_business(adj2, _biz(id="b4", source="A", target="D", must_pass=["C", "B"]))
    assert bad.status == "violating"
    assert bad.cost == 2
    assert bad.missed_must_pass == ["C", "B"]
    assert bad.path == ["A", "B", "D"]


def test_must_pass_waypoint_unreachable_after_disable_is_violating_not_unreachable():
    adj = _adj(
        [
            ("S", "T", 1),
            ("S", "W", 1),
            ("W", "T", 1),
        ],
        [("W", "T")],
    )
    check = check_business(adj, _biz(id="b5", source="S", target="T", must_pass=["W"]))
    # 起终点仍可达，所以不是 unreachable；但任何最短路都到不了 W
    assert check.status == "violating"
    assert check.cost == 1
    assert check.missed_must_pass == ["W"]


def test_unreachable_ignores_constraints():
    adj = _adj([("S", "A", 1)], [])
    check = check_business(adj, _biz(id="b6", source="S", target="T"))
    assert check.status == "unreachable"
    assert check.cost is None
    assert check.path == []


# ---------------------------------------------------------------- 启动配置校验
def _topology(links, businesses):
    return TopologyConfig(links=links, businesses=businesses)


def test_topology_config_validation():
    link = {"a": "A", "b": "B", "cost": 1}
    biz = {"id": "x", "source": "A", "target": "B"}
    with pytest.raises(ValueError):
        _topology([link, link], [biz])  # 重复链路
    with pytest.raises(ValueError):
        _topology([link, {"a": "B", "b": "A", "cost": 2}], [biz])  # 反向重复链路
    with pytest.raises(ValueError):
        _topology([link], [biz, biz])  # 重复业务
    with pytest.raises(ValueError):
        _topology([link], [{**biz, "target": "Z"}])  # 引用不存在设备
    with pytest.raises(ValueError):
        _topology([link], [{**biz, "target": "A"}])  # 起终点相同
    with pytest.raises(ValueError):
        _topology([link], [{**biz, "must_pass": ["B"], "must_avoid": ["B"]}])
    with pytest.raises(ValueError):
        _topology([link], [{**biz, "target": "B", "must_avoid": ["B"]}])  # 终点被避开
    with pytest.raises(ValueError):
        _topology([link], [{**biz, "must_pass": ["A"]}])  # 起点被列为必经
    with pytest.raises(ValueError):
        _topology([link], [{**biz, "must_avoid": ["B", "B"]}])  # 避开列表重复
    with pytest.raises(ValueError):
        _topology([{"a": "A", "b": "A", "cost": 1}], [biz])  # 自环链路


def test_demo_example_files_are_valid():
    """examples/ 下的示例上报必须都能通过校验并算出预期结果。"""
    examples = Path(__file__).resolve().parent.parent / "examples"
    expected = {
        2: {"ok": 2, "unreachable": 0, "violating": 2},
        3: {"ok": 4, "unreachable": 0, "violating": 0},
        4: {"ok": 3, "unreachable": 1, "violating": 0},
        5: {"ok": 4, "unreachable": 0, "violating": 0},
        6: {"ok": 3, "unreachable": 1, "violating": 0},
    }
    for path in sorted(examples.glob("report_rev*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        req = ReportRequest.model_validate(payload)
        snapshot = build_snapshot(DEMO_CONFIG, req.revision, req.disabled_links)
        assert snapshot.summary == expected[req.revision], path.name


def test_demo_config_loads_from_expected_path():
    assert DEMO_CONFIG.nodes == {"HQ", "CORE1", "CORE2", "PE1", "PE2", "ACCESS", "EDGE"}
    assert len(DEMO_CONFIG.links) == 9
    assert [b.id for b in DEMO_CONFIG.businesses] == [
        "biz-crm",
        "biz-iot",
        "biz-video",
        "biz-edge",
    ]


# ---------------------------------------------------------------- 报告快照
def test_demo_snapshot_ok_violating_unreachable():
    base = build_snapshot(DEMO_CONFIG, 0, [])
    assert base.summary == {"ok": 4, "unreachable": 0, "violating": 0}

    degraded = build_snapshot(DEMO_CONFIG, 2, [["PE1", "ACCESS"]])
    by_id = {b.id: b for b in degraded.businesses}
    assert degraded.summary == {"ok": 2, "unreachable": 0, "violating": 2}
    assert by_id["biz-crm"].status == "violating"
    assert by_id["biz-crm"].cost == 8
    assert "PE2" in by_id["biz-crm"].path
    assert by_id["biz-crm"].avoided_devices == ["PE2"]

    cut = build_snapshot(DEMO_CONFIG, 4, [["EDGE", "CORE1"]])
    by_id = {b.id: b for b in cut.businesses}
    assert by_id["biz-edge"].status == "unreachable"
    assert by_id["biz-edge"].cost is None
    assert cut.summary == {"ok": 3, "unreachable": 1, "violating": 0}
    assert cut.topology_links == len(DEMO_CONFIG.links) - 1


def test_snapshot_deterministic():
    a = build_snapshot(DEMO_CONFIG, 7, [["PE1", "ACCESS"]])
    b = build_snapshot(DEMO_CONFIG, 7, [["PE1", "ACCESS"]])
    assert a.model_dump(exclude={"generated_at"}) == b.model_dump(exclude={"generated_at"})


# ---------------------------------------------------------------- 存储替换语义
def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_store_keeps_old_report_until_new_one_finishes(monkeypatch):
    store = ReportStore(DEMO_CONFIG)
    old, _ = store.current()
    assert old.revision == 0

    monkeypatch.setenv("REPORT_COMPUTE_DELAYS", json.dumps({"1": 0.4}))
    accepted, _ = store.submit(1, [["PE1", "ACCESS"]])
    assert accepted
    # 重算中：现行报告仍是旧的一份，同时能看到 computing 状态
    mid, status = store.current()
    assert mid.revision == 0
    assert mid.summary == old.summary
    assert status.computing is True
    assert status.computing_revisions == [1]

    assert store.wait_idle()
    new, status = store.current()
    assert new.revision == 1
    assert new.summary["violating"] == 2
    assert status.computing is False
    assert status.computing_revisions == []


def test_out_of_order_finish_never_replaces_newer(monkeypatch):
    store = ReportStore(DEMO_CONFIG)
    # rev 2 慢、rev 3 快：3 先成为现行，2 算完必须被拒收
    monkeypatch.setenv("REPORT_COMPUTE_DELAYS", json.dumps({"2": 0.5, "3": 0.05}))
    assert store.submit(2, [["PE1", "ACCESS"]])[0]
    assert store.submit(3, [])[0]
    assert _wait_until(lambda: store.current()[0].revision == 3)
    assert store.wait_idle()
    current, status = store.current()
    assert current.revision == 3
    assert current.disabled_links == []
    assert status.last_rejected == 2


def test_stale_and_duplicate_submissions_do_not_start_work(monkeypatch):
    store = ReportStore(DEMO_CONFIG)
    monkeypatch.setenv("REPORT_COMPUTE_DELAYS", json.dumps({"1": 0.3}))
    assert store.submit(1, [])[0]
    accepted, message = store.submit(1, [])
    assert accepted is False and "已在重算" in message
    assert store.wait_idle()
    accepted, message = store.submit(1, [])
    assert accepted is False and "不高于" in message
    assert store.current()[0].revision == 1


def test_failed_recompute_keeps_current_report(monkeypatch):
    store = ReportStore(DEMO_CONFIG)
    before, _ = store.current()

    real_build = reporting.build_snapshot

    def boom(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(reporting, "build_snapshot", boom)
    assert store.submit(5, [])[0]
    assert store.wait_idle()
    after, status = store.current()
    assert after.revision == before.revision
    assert after.summary == before.summary
    assert status.last_failed == 5
    assert status.computing is False

    monkeypatch.setattr(reporting, "build_snapshot", real_build)
    assert store.submit(6, [])[0]
    assert store.wait_idle()
    assert store.current()[0].revision == 6


def test_many_concurrent_revisions_final_is_max(monkeypatch):
    store = ReportStore(DEMO_CONFIG)
    # 让所有版次都停在计算中，确保它们确实并发在途，而不是提交时就已算完
    monkeypatch.setenv("REPORT_COMPUTE_DELAY_SECONDS", "0.2")
    revisions = [10, 12, 11, 15, 13, 14]
    for rev in revisions:
        assert store.submit(rev, [])[0]
    assert sorted(store.current()[1].computing_revisions) == sorted(revisions)
    assert store.wait_idle(timeout=10)
    assert store.current()[0].revision == max(revisions)
    assert store.current()[0].revision == 15


def test_unknown_disabled_link_detected():
    store = ReportStore(DEMO_CONFIG)
    assert store.unknown_disabled_links([["PE1", "ACCESS"]]) == []
    assert store.unknown_disabled_links([["PE1", "NOPE"]]) == [["PE1", "NOPE"]]


# ---------------------------------------------------------------- HTTP API
@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _post_report(client, revision, disabled):
    return client.post(
        "/reports", json={"revision": revision, "disabled_links": disabled}
    )


def _get_report(client):
    return client.get("/reports/current")


def _wait_api(client, revision, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = _get_report(client).json()
        if body["active_revision"] == revision and not body["status"]["computing"]:
            return body
        time.sleep(0.02)
    raise AssertionError(f"等待版次 {revision} 超时：{_get_report(client).json()}")


def test_api_baseline_and_docs(client):
    body = _get_report(client).json()
    assert body["active_revision"] == 0
    assert body["report"]["revision"] == 0
    assert body["report"]["summary"] == {"ok": 4, "unreachable": 0, "violating": 0}
    assert body["report"]["businesses"][0]["path"]
    assert body["status"]["computing"] is False
    assert client.get("/docs").status_code == 200
    assert client.get("/health").json() == {"status": "ok"}


def test_api_disable_then_restore(client, monkeypatch):
    monkeypatch.setenv("REPORT_COMPUTE_DELAY_SECONDS", "0")

    resp = _post_report(client, 2, [["PE1", "ACCESS"]])
    assert resp.status_code == 202, resp.text
    assert resp.json()["accepted"] is True
    body = _wait_api(client, 2)
    assert body["report"]["disabled_links"] == [["ACCESS", "PE1"]]
    by_id = {b["id"]: b for b in body["report"]["businesses"]}
    assert by_id["biz-crm"]["status"] == "violating"
    assert by_id["biz-crm"]["cost"] == 8
    assert by_id["biz-crm"]["path"] == ["HQ", "CORE1", "CORE2", "PE2", "ACCESS"]

    # 恢复：同一链路不再停用，业务回到全合规
    resp = _post_report(client, 3, [])
    assert resp.status_code == 202
    body = _wait_api(client, 3)
    assert body["report"]["disabled_links"] == []
    assert body["report"]["summary"] == {"ok": 4, "unreachable": 0, "violating": 0}
    by_id = {b["id"]: b for b in body["report"]["businesses"]}
    assert by_id["biz-crm"]["path"] == ["HQ", "CORE1", "PE1", "ACCESS"]

    # 断开 EDGE 的唯一链路：不可达而非违规
    _post_report(client, 4, [["EDGE", "CORE1"]])
    body = _wait_api(client, 4)
    by_id = {b["id"]: b for b in body["report"]["businesses"]}
    assert by_id["biz-edge"]["status"] == "unreachable"
    assert by_id["biz-edge"]["cost"] is None
    assert body["report"]["summary"] == {"ok": 3, "unreachable": 1, "violating": 0}


def test_api_query_during_recompute(client, monkeypatch):
    monkeypatch.setenv("REPORT_COMPUTE_DELAYS", json.dumps({"7": 0.6}))
    assert _post_report(client, 7, [["PE1", "ACCESS"]]).status_code == 202

    # 计算尚未完成：仍返回上一份（版次 0），并标记 computing
    body = _get_report(client).json()
    assert body["active_revision"] == 0
    assert body["report"]["summary"] == {"ok": 4, "unreachable": 0, "violating": 0}
    assert body["status"]["computing"] is True
    assert body["status"]["computing_revisions"] == [7]

    body = _wait_api(client, 7)
    assert body["report"]["summary"]["violating"] == 2


def test_api_out_of_order_finish(client, monkeypatch):
    monkeypatch.setenv("REPORT_COMPUTE_DELAYS", json.dumps({"20": 0.6, "21": 0.05}))
    assert _post_report(client, 20, [["EDGE", "CORE1"]]).status_code == 202
    assert _post_report(client, 21, []).status_code == 202

    body = _wait_api(client, 21)
    assert body["report"]["disabled_links"] == []
    # 20 随后算完，但版次低于现行 21，必须被拒收且不改变报告
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        body = _get_report(client).json()
        if not body["status"]["computing"]:
            break
        time.sleep(0.02)
    assert body["active_revision"] == 21
    assert body["report"]["disabled_links"] == []
    assert body["status"]["last_rejected"] == 20

    # 再次上报旧版次：直接告知不会触发重算
    resp = _post_report(client, 20, [])
    assert resp.status_code == 202
    assert resp.json()["accepted"] is False
    assert "不高于" in resp.json()["message"]


def test_api_link_down_then_restored_and_two_out_of_order(client, monkeypatch):
    """链路停用 -> 恢复 -> 再停用，且两版乱序算完：只认最高版次。"""
    monkeypatch.setenv(
        "REPORT_COMPUTE_DELAYS",
        json.dumps({"31": 0.15, "32": 0.6, "33": 0.05}),
    )

    # 31：停用 PE1-ACCESS，复查项违规
    assert _post_report(client, 31, [["PE1", "ACCESS"]]).status_code == 202
    body = _wait_api(client, 31)
    assert body["active_revision"] == 31
    assert {b["id"]: b["status"] for b in body["report"]["businesses"]}["biz-crm"] == "violating"

    # 32 慢、33 快：33（恢复，全合规）先完成并生效；32 稍后算完必须被拒收
    assert _post_report(client, 32, [["PE1", "ACCESS"]]).status_code == 202
    assert _post_report(client, 33, []).status_code == 202
    body = _wait_api(client, 33)
    assert body["report"]["disabled_links"] == []
    assert body["report"]["summary"] == {"ok": 4, "unreachable": 0, "violating": 0}

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        body = _get_report(client).json()
        if not body["status"]["computing"]:
            break
        time.sleep(0.02)
    assert body["active_revision"] == 33
    assert body["report"]["disabled_links"] == []
    assert body["status"]["last_rejected"] == 32

    # 再停用一次（版次 34）确实能替换：证明 32 被拒收不是锁死
    assert _post_report(client, 34, [["EDGE", "CORE1"]]).status_code == 202
    body = _wait_api(client, 34)
    assert body["active_revision"] == 34
    assert {b["id"]: b["status"] for b in body["report"]["businesses"]}["biz-edge"] == "unreachable"

    # 链路恢复（版次 35）：不可达业务恢复为合规
    assert _post_report(client, 35, []).status_code == 202
    body = _wait_api(client, 35)
    assert body["report"]["summary"] == {"ok": 4, "unreachable": 0, "violating": 0}


def test_api_report_validation(client):
    bad = [
        {"revision": 0, "disabled_links": []},
        {"revision": 1, "disabled_links": [["PE1"]]},
        {"revision": 1, "disabled_links": [["PE1", "PE1"]]},
        {"revision": 1, "disabled_links": [["PE1", "ACCESS"], ["ACCESS", "PE1"]]},
        {"revision": 1, "disabled_links": [["", "ACCESS"]]},
    ]
    for payload in bad:
        resp = client.post("/reports", json=payload)
        assert resp.status_code == 422, payload

    resp = _post_report(client, 1, [["PE1", "NOPE"]])
    assert resp.status_code == 422
    assert "不在启动配置的拓扑中" in resp.text
