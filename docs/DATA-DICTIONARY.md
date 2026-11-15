# 定义 cdcwater

本文件是字段、时间、金额、窗口与去重键的唯一书面定义。页面只显示列名与数值，定义看这里。

## 1 事件信封

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `table` | string | 是 | 表名，必须在 8 张登记表内：orders / order_items / customers / payments / shipments / products / inventory / refunds |
| `pk` | string | 是 | 主键。**全程字符串**：前导零 `00123` 与超过 2^53 的整数都按原样保存 |
| `version` | int | 是 | 版本号，正整数；决定同键内的应用顺序 |
| `op` | enum | 是 | `upsert` / `delete`；`delete` 写墓碑 |
| `event_time` | string | 是 | 源库事务时间。`Z`、`+08:00` 与无偏移三种写法都接受，入库统一 UTC |
| `schema` | enum | 否 | `v1` / `v2`，缺省按 `v2`；只影响列名映射 |
| `txn_id` | string | 否 | 业务事务号。非空即进入 M8 事务装配；三字段全缺按普通事件处理 |
| `part_no` | int | 条件 | 事务分片号，从 1 起；带 `txn_id` 时必填 |
| `part_total` | int | 条件 | 该事务的分片总数；带 `txn_id` 时必填，且需 `part_no ≤ part_total` |
| `payload` | object | 是 | 业务字段，见第 2 节 |

事务信封约束：`txn_id` 非空而 `part_no` / `part_total` 缺失、非正整数或越界
（`part_no > part_total`），判 `E_BAD_TXN` 隔离，不允许拼成「伪完整」事务；
同一事务内 `part_total` 声明不一致按 `T_PART_CONFLICT` 处理，整笔不发布。

## 2 payload 字段

| 规范列名 | 类型 | 必填 | v1 旧列名 | 说明 |
|---|---|---|---|---|
| `ref` | string | 否 | `ref_no` | 业务引用 |
| `status` | string | 是（upsert） | `st` | 状态；`delete` 可为空 |
| `amount` | decimal | 否 | `amt` | 金额，单位元，刻度 0.01 |
| `qty` | int | 否 | `qt` | 数量 |

别名规则：v1、v2 都接受旧列名（读兼容）。同一个规范列名被新旧两种写法同时赋值且取值不同，
判 `E_ALIAS_AMBIGUOUS` 并隔离；取值相同按一条处理。契约外的列判 `E_UNKNOWN_FIELD`。

## 3 数值与金额

- 金额一律 `Decimal`，量化到 `0.01`，舍入 `ROUND_HALF_UP`；禁止 `float` 累加。
- 输入允许千分位与全角数字：`1,200.50` / `１２００．５０` 都归一为 `1200.50`。
- 空值哨兵（`-`、`--`、`N/A`、`null`、`未知`、`无`、空串…）→ `null`，并记 `W_SENTINEL_NULL`；
  哨兵与 `0.00` 是两种语义：`0.00` 计入合计，哨兵不计入。
- 对外 JSON 里金额是两位小数字符串；CSV 里也是两位。

## 4 时间

- 入库：UTC，ISO8601 带 `Z`（例 `2026-11-15T08:52:00Z`）。
- 显示：`Asia/Shanghai (+08:00)`，页面用 `YYYY-MM-DD HH:MM:SS`。
- 无偏移的时间按 `+08:00` 解释，并记 `W_TIME_NO_OFFSET`；非法时间（`25:10`、斜杠日期）判
  `E_BAD_TIME` 隔离。

## 5 水位线与窗口（关键定义）

- **水位线** = 已应用事件的最大 `event_time` − 10 分钟。只由已应用事件推进：重复、冲突、
  隔离都不推进。
- **迟到**：`event_time < 水位线` 判迟到（`W_LATE_ARRIVAL`），**但仍然应用**；等于水位线不算迟到。
- **快照窗口**：`event_time ≤ as_of`，`as_of` = 本次水位线。含等号一侧。
- **待定窗口**：`(as_of, +inf)`，半开。两窗口互不重叠、合起来覆盖全部已应用事件。
- 同一键既有快照内事件、又有待定事件时，快照取 `≤ as_of` 的最大版本，并记 `drift`。

## 6 去重键与冲突

- **去重键** = `(table, pk, version, op, event_time_utc, 规范化 payload)` 的 sha256。
  用规范化 payload（金额两位、字段排序）而不是原始行哈希，所以 `120.5` 与 `120.50`
  视为同一事件；用 UTC 时刻而不是原始字符串，所以两种时区写法视为同一事件。
- **版本冲突** `C_VERSION_CONFLICT`：同键同版本出现两种以上签名 → 整组不应用。
- **墓碑复活** `C_TOMBSTONE_RESURRECT`：某键存在版本 `d` 的 `delete`，之后到达的 `version < d`
  的 `upsert` 不应用；删除之前到达的低版本照常应用（会被删除覆盖，结果一致）。
- **优先级**：`quarantine > conflict > duplicate > apply`，判定由集合运算得出，与遍历顺序无关。

## 7 空态三态

| 状态 | 判定 | 结论 |
|---|---|---|
| 0 字节文件 | 没有任何行 | `S_EMPTY_INPUT`，不算失败 |
| 只有表头 | CSV 有表头无数据行 | `S_HEADER_ONLY`，不算失败 |
| 只有空行 | 有行但全空白 | `E_ALL_BLANK`，文件级失败 |

## 8 计数与四账

| 计数 | 含义 |
|---|---|
| `raw_lines` | 数据行（不含注释行、空行、重复表头） |
| `applied` | 通过契约与去重冲突、进入折叠的事件 |
| `duplicates` | 同签名重复到达（保留首条） |
| `conflicts` | 版本冲突 + 墓碑复活 |
| `quarantine` | 契约不通过（字段、类型、必填、表名、时间、截断、列数） |
| `keys_total` | 有已应用事件的键 |
| `snapshot_keys` | 进快照的键（含墓碑） |
| `active_keys` / `tombstone_keys` | 快照内活跃 / 墓碑 |
| `pending_only_keys` | 只有 `as_of` 之后事件的键 |
| `amount_snapshot` / `amount_pending` / `amount_drift` / `amount_applied` | 四个金额位，见 DEPTH.md 的金额账 |

## 9 运行清单字段

`run_id`（`CDC-时间戳-随机`）、`batch_id`（`B-批次键`）、`batch_key`（输入摘要 + 规则版本 +
参数 的 sha256 前 10 位）、`input_digest`（每个文件 `name:sha256` 的摘要）、`inputs[]`
（`name/bytes/sha256/lines/encoding/kind/declared_rows/status/code`）、`params`、
`watermark_utc`、`max_event_time_utc`、`stages[]`、`counters`、`reconcile`。

同一 `input_digest` 重跑命中同一 `batch_key`，返回原批次、不新增行。

## 10 事务装配（M8，关键定义）

一笔业务事务（下单 + 支付 + 退款）在源库是一次提交，导出时被拆成多条分片事件，
可能丢片、重投、乱序。M8 把带同一 `txn_id` 的事件折回一个整体，再决定是否对外可见。

### 10.1 三种事务状态

| 状态 | 判定 | 对外结果 | 原因码 |
|---|---|---|---|
| `complete` 完整 | `1..part_total` 每个分片都到齐，且无同片异内容、无 `part_total` 矛盾 | **发布**，金额进对外合计 | `T_PUBLISHED` |
| `incomplete` 未完整 | 至少一个分片号缺失 | 不发布，整笔挂起可回读 | `T_INCOMPLETE` |
| `conflict` 冲突 | 同一 `part_no` 出现两种规范化签名，或 `part_total` 声明不一致 | 整笔不发布 | `T_PART_CONFLICT` |

同片同签名的重复到达是重投（`T_DUP_PART` 语义，M3 已标 `D_EXACT_DUP`），保留首条、
不重复计金额；判定全部由集合运算得出，与分片到达顺序无关。

### 10.2 分组水位线（不让一个慢分片拖停整库）

- 全局水位线仍是 `max(全部已应用 event_time) − 迟到窗口`，**不被任何未完整事务拖住**；
- 每个事务另有自己的分组门限 `gate(txn) = max(event_time ∈ 该事务) − 迟到窗口`；
- 分片 `event_time < gate(txn)` 只在该事务上记 `late_parts`（`T_LATE_SHARD`），
  分片齐全时事务仍可发布；缺片事务超过自身门限记 `T_AGED`，但保留全部证据可回读。

### 10.3 金额口径（两本账严格分离）

- **对外发布金额** `txn_published_amount`：只累加 `complete` 事务的权威分片，
  每个 `part_no` 只算一次，`delete` 分片不计；
- **受影响金额** `txn_affected_amount`：`incomplete` 与 `conflict` 事务已到达分片的
  金额，按分片去重，**不进对外合计**，只用于回读与对账；
- 金额全程 `Decimal`、刻度 `0.01`，禁止 float；事务 JSON 里是两位小数字符串。

### 10.4 落库、失败与重跑

- 事务结果写 `txn_results` 表，与事件/快照在同一个 SQLite 事务里「先删后插」，
  任一步失败整体回滚，**旧版结果保留**；
- 同一输入重跑命中 `batch_key` 走幂等重放，不新增事务行、不改合计；
- `POST /api/txn/rebuild` 按同一输入重算，行数与金额不变；
- 回读入口：`GET /api/txn`（全量）、`GET /api/txn/{txn_id}`（分片证据）、
  导出 `GET /api/export/txn.csv` 与 `/api/export/txn.json`（published/held 分组）。

### 10.5 事务对账

- 事务账：事务事件数 = 已发布事务事件 + 挂起/冲突事务事件；
- 状态账：事务总数 = 完整 + 未完整 + 冲突；
- 金额账：对外发布金额与受影响金额分别披露、互不混算；
- 三条账在 `/txn` 页面、`/api/txn` 与 manifest 的 `transactions` 段同时出现。
