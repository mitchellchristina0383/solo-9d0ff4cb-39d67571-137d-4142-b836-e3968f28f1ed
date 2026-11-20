# 链路代价分批调整离线预检 & 现行转发路径核查

一个 FastAPI 服务，提供两件事：

1. **分批改动预检**（原有能力）：机房分批调整链路代价前，逐批判断错峰切换
   过程中是否可能出现转发环路。
2. **现行转发路径核查报告**（本次新增）：拓扑与业务清单在启动时配置；每次
   上报带监控侧递增版次和当时全部停用链路，服务在**剩余链路**上核查每项业务
   是否还有合规的最短路径，并维护一份可被查询的现行整份报告。

---

## 一、分批改动离线预检

提交连通路由拓扑、每条双向链路的新旧两版正整数代价、目标路由器和覆盖全部
路由器的分批计划，服务按两版代价分别计算到目标的最短路径下一跳（等价路径的
所有下一跳都可能被选中），逐批判断错峰切换过程中是否可能出现转发环路。

### 判环模型

对每一批 `k`，切换期间各路由器的可用出边为：

| 路由器所属 | 切换期间可用出边 |
| --- | --- |
| 早于第 `k` 批 | 仅新版下一跳（前一批已全部生效） |
| 晚于第 `k` 批 | 仅旧版下一跳 |
| 第 `k` 批（本批） | **旧版 ∪ 新版**下一跳（设备可按任意顺序切换） |

在该并集转发图中：

- **存在有向环** ⇒ 存在一种错峰切换顺序与下一跳选择使流量兜圈，本批危险；
  环上仅新版可达的出边对应的本批设备，即为构造该环所需的“已切换设备”。
- **不存在有向环** ⇒ 本批切换的任意中间状态（任意已切换子集、任意等价
  下一跳选择）都不可能成环。

该等价关系由随机小拓扑暴力枚举（枚举全部切换子集 × 全部下一跳组合）
回归测试守护，见 `tests/test_api.py::test_union_lemma_against_bruteforce`。

下一跳由 Dijkstra 最短距离推导：无向边 `(u, v, w)` 上，`v` 是 `u` 的下一跳
当且仅当 `dist(u) == w + dist(v)`，等号成立的邻居全部纳入等价下一跳集合。

---

## 二、现行转发路径核查报告

### 启动配置

拓扑、链路代价和业务清单在进程启动时加载，默认读
`examples/report_demo.json`，可用环境变量 `TOPOLOGY_CONFIG_PATH` 指向其他
文件。配置非法（重复链路、业务引用不存在的设备、起终点相同等）会让容器直接
启动失败并输出原因，不会带着半套配置对外服务。

```json
{
  "links": [
    {"a": "HQ", "b": "CORE1", "cost": 2},
    {"a": "CORE1", "b": "PE1", "cost": 2}
  ],
  "businesses": [
    {
      "id": "biz-crm",
      "source": "HQ",
      "target": "ACCESS",
      "must_pass": ["CORE1"],
      "must_avoid": ["PE2"]
    }
  ]
}
```

- 链路为**双向、同代价**。
- `must_pass` 是**按出现顺序**必须依次经过的设备；提前路过后面的设备不算数，
  之后仍须再次按序经过。
- `must_avoid` 是路径上不得出现的设备。
- 起点/终点不能出现在 `must_avoid`，也不能出现在 `must_pass`；`must_pass`
  与 `must_avoid` 不得重叠。

`GET /reports/topology` 可在 `/docs` 里直接查看当前生效的配置。

### 上报与查询接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/reports` | 上报版次 + 当时**全部**停用链路，返回 202 并触发后台重算 |
| `GET` | `/reports/current` | 查询现行整份报告、对应版次与重算状态 |
| `GET` | `/reports/topology` | 查看启动配置的拓扑与业务清单 |
| `POST` | `/precheck` | 原有的分批切换环路预检 |
| `GET` | `/health`、`/docs` | 健康检查、交互式文档 |

上报体（`disabled_links` 是**当前全部**停用链路，不是增量）：

```json
{
  "revision": 2,
  "disabled_links": [["PE1", "ACCESS"]]
}
```

约束（违反返回 422）：

- `revision` 为正整数，由监控侧单调递增。
- 每个停用链路必须是两个不同的端点，且该链路必须存在于启动配置中；重复停用
  （含端点顺序颠倒）会被拒绝。
- 服务会把停用链路的端点排序后保存，所以 `["PE1","ACCESS"]` 与
  `["ACCESS","PE1"]` 视为同一条。

查询返回：

```json
{
  "active_revision": 2,
  "report": {
    "revision": 2,
    "generated_at": "2026-11-20T12:00:00Z",
    "disabled_links": [["ACCESS", "PE1"]],
    "topology_nodes": 7,
    "topology_links": 8,
    "summary": {"ok": 2, "unreachable": 0, "violating": 2},
    "businesses": [
      {
        "id": "biz-crm",
        "source": "HQ",
        "target": "ACCESS",
        "status": "violating",
        "path": ["HQ", "CORE1", "CORE2", "PE2", "ACCESS"],
        "cost": 8,
        "avoided_devices": ["PE2"],
        "missed_must_pass": [],
        "detail": "可达，但全部最短路径（代价 8）均不合规：经过须避开的设备 ['PE2']"
      }
    ]
  },
  "status": {
    "revision": 2,
    "generated_at": "2026-11-20T12:00:00Z",
    "summary": {"ok": 2, "unreachable": 0, "violating": 2},
    "computing": false,
    "computing_revisions": [],
    "last_rejected": null,
    "last_failed": null
  }
}
```

### 三种业务结论

| status | 含义 | `path` / `cost` |
| --- | --- | --- |
| `ok` | 存在合规的最短路径（代价等于无约束最短路） | 一条合规最短路径 |
| `unreachable` | 剩余链路上起点根本到不了终点 | 空 / `null` |
| `violating` | 可达，但**全部**最短路都违反必经/避开约束 | 一条违规的最短路径 + 踩中/漏掉的项 |

关键点：`unreachable` 只看剩余链路是否连通，**不**把“须避开”的设备预先删掉；
否则“只能经过被禁止设备才到得了”的业务会被误报为不可达。等价最短路是否全部
违规，用“约束下的最短路代价是否等于无约束最短路代价”判定——代价相等就说明
存在一条同样短的合规路径。

### 重算与替换语义

- 每次上报在后台线程里算，`POST /reports` 立即返回 202。
- 重算期间 `GET /reports/current` 继续返回**上一份完整报告**，`status.computing`
  为 `true`，`computing_revisions` 列出在途版次。
- 只有**算成功**且 `revision > 现行版次` 的结果才会原子替换现行报告；算完但
  版次不更高（两版乱序完成）的结果被丢弃，记入 `status.last_rejected`；计算
  失败只记入 `status.last_failed`，现行报告不变。
- 上报时若版次不高于现行报告，`accepted=false`，不会启动重算；同一版次重复
  上报（仍在算）也不会重复受理。
- 服务启动时会先按“无停用链路”的拓扑生成一份**版次 0** 的基线报告，因此
  任何时候都能查到一份完整报告。

### 演示（Docker Compose + /docs）

```bash
docker compose up --build
```

打开 <http://localhost:8000/docs>，按下面顺序操作（Compose 默认给 v2 配了 4 秒、
v4 配了 3 秒的重算延迟，专门用来观察“重算期间还能查旧报告”和“乱序算完”）：

1. `GET /reports/current`：看到版次 0 的基线报告，4 项业务全 `ok`。
2. `POST /reports` 提交 `examples/report_rev2_pe1_access_down.json`（版次 2，停用
   `PE1-ACCESS`），接口立刻返回 202。
3. 马上 `GET /reports/current`：仍返回**版次 0** 的整份报告，
   `status.computing=true`、`computing_revisions=[2]`。
4. 等约 4 秒再查：`active_revision=2`，`biz-crm`、`biz-iot` 变为 `violating`，
   各自附一条踩中 `PE2` 的最短路径；`biz-video`、`biz-edge` 仍 `ok`。
5. `POST /reports` 提交 `examples/report_rev3_restored.json`（版次 3，无停用链路）
   —— 模拟链路恢复，查询后 4 项业务全部回到 `ok`。
6. 乱序演示：先提交 `examples/report_rev4_edge_down.json`（版次 4，停用
   `EDGE-CORE1`，会慢 3 秒），紧接着提交 `examples/report_rev5_restored.json`
   （版次 5，无停用链路，立即算完）。版次 5 先成为现行；版次 4 算完后被拒收，
   `status.last_rejected=4`，`active_revision` 仍为 5，`EDGE` 业务保持 `ok`。
7. 如需看 `unreachable`：提交 `examples/report_rev6_edge_down.json`（版次 6，
   停用 `EDGE-CORE1`）并等待，`biz-edge` 会变成 `unreachable`、`cost=null`。

示例文件与演示拓扑：

| 文件 | 内容 |
| --- | --- |
| `examples/report_demo.json` | 启动配置：7 台设备、9 条链路、4 项业务 |
| `examples/report_rev2_pe1_access_down.json` | 版次 2，停用 `PE1-ACCESS` |
| `examples/report_rev3_restored.json` | 版次 3，链路恢复 |
| `examples/report_rev4_edge_down.json` | 版次 4，停用 `EDGE-CORE1`（慢算，用于乱序演示） |
| `examples/report_rev5_restored.json` | 版次 5，链路恢复 |
| `examples/report_rev6_edge_down.json` | 版次 6，停用 `EDGE-CORE1`（观察不可达） |

关闭演示延迟：把 `docker-compose.yml` 里的 `REPORT_COMPUTE_DELAYS` 改成 `{}`。

无 Docker 时本地运行：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## 三、目录结构

```
app/
  main.py       FastAPI 入口（/、/health、/precheck、/reports*、/docs）
  core.py       原有预检：Dijkstra、下一跳集合、并集图与三色 DFS 找环
  models.py     请求/响应模型与输入校验（预检 + 启动配置 + 报告）
  reporting.py  剩余链路最短路、约束最短路、报告快照与现行报告存储
tests/
  test_api.py       原有预检自测（含并集判环 vs 暴力枚举对照）
  test_reports.py   报告算法、配置校验、并发替换语义、HTTP 端到端
examples/           启动配置与可直接提交的上报示例
Dockerfile
docker-compose.yml
requirements.txt
```

## 四、自测

```bash
pip install -r requirements.txt pytest httpx
python -m pytest -q
```

`test_api.py`：同批错峰成环、等价路径并集成环、安全多批次、第二批才成环、
单批 50 台上限等输入校验、断链拒绝、单路由器拓扑、结果确定性，以及 40 组随机
拓扑的并集判环 vs 全状态暴力枚举对照。

`test_reports.py` 覆盖：

- 最短路与约束最短路（含“先经过后面的必经设备不算数”的严格顺序语义）；
- 约束最短路 vs 在展开状态上做 Bellman-Ford 松弛的随机对照（80 组）；
- 合规/违规/不可达的区分，等价最短路中只有一条合规的情形；
- 启动配置校验（重复链路/业务、未知设备、约束自相矛盾）；
- 现行报告存储：重算中仍可查旧报告、乱序完成只认高版次、重复与过期上报不
  触发重算、计算失败保留旧报告、多版并发后收敛到最高版次；
- HTTP 端到端：链路停用 → 恢复 → 再停用，两版乱序算完，非法上报 422。

## 五、复杂度

- 单项业务：一次无约束 Dijkstra `O((V+E) log V)`；约束最短路在
  `(设备, 已按序匹配数)` 状态上做 Dijkstra，约 `O((V·M + E·M) log(V·M))`，
  `M` 为必经设备数（通常很小）。整份报告为各业务之和乘以业务数。
- 预检部分同前：两次 Dijkstra + 每批一次 `O(V+E)` 并集找环。
