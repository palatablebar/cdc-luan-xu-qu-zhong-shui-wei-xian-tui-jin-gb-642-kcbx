"""事务装配编排：从已应用事件构建事务、落库、给页面/导出提供行与对账。

口径分离（关键）：

- 事件级快照（M5）仍保留全部已应用事件，是「事件口径」；
- 事务对外结果（M8）只含 complete 事务，是「业务事务口径」，金额合计只累加完整事务；
- incomplete / conflict 事务不进对外结果，但其分片证据、缺失号、冲突号与受影响金额
  全部可回读、可导出。
"""
from __future__ import annotations

from decimal import Decimal

from ... import config, reasons
from ..m5_snapshot.apply import Applied
from . import assembly as asm


def build(store, batch_id: str, window) -> asm.Assembly:
    """从落库事件重算事务装配，结果确定、可重复。

    纳入全部非隔离事件（含 M3 冲突/重复），与主流程采信口径保持一致。
    """
    events = [Applied.from_row(row)
              for row in store.load_events(batch_id)
              if row.get("txn_id") and row["row_class"] != "quarantine"]
    return asm.assemble(events, window)


def rebuild(store, batch_id: str, window) -> asm.Assembly:
    """可重复调用：同一输入重算事务结果，行数与合计不变。"""
    result = build(store, batch_id, window)
    store.replace_txn_results(batch_id,
                              [r.as_store_row(batch_id) for r in result.results],
                              totals=txn_totals(result))
    return result


def txn_totals(result: asm.Assembly) -> dict:
    return {
        "txn_total": len(result.results),
        "txn_complete": len(result.complete),
        "txn_incomplete": len(result.incomplete),
        "txn_conflict": len(result.conflicts),
        "txn_published_events": sum(r.event_count for r in result.complete),
        "txn_held_events": sum(r.event_count
                               for r in result.incomplete + result.conflicts),
        "txn_published_amount": result.published_amount,
        "txn_affected_amount": result.affected_amount,
    }


def page_rows(store, batch_id: str, status: str | None = None) -> list[dict]:
    rows = []
    for row in store.load_txn_results(batch_id, status):
        rows.append({
            "txn_id": row["txn_id"], "status": row["status"],
            "status_label": config.TXN_STATUS_LABELS.get(row["status"], row["status"]),
            "part_total": row["part_total"], "part_count": row["part_count"],
            "missing": row["missing"], "conflict_parts": row["conflict_parts"],
            "late_parts": row["late_parts"], "row_refs": row["row_refs"],
            "tables": row["tables"], "event_count": row["event_count"],
            "max_event_time": row["max_event_time_utc"], "gate": row["gate_utc"],
            "published_amount": row["published_amount"],
            "affected_amount": row["affected_amount"],
            "reason_code": row["reason_code"],
            "reason_label": reasons.label(row["reason_code"]),
            "detail": row["detail"],
        })
    return rows


def detail_rows(store, batch_id: str, txn_id: str) -> list[dict]:
    """回读某笔事务的全部分片事件证据（含重复/冲突分片）。"""
    out = []
    for row in store.load_events(batch_id):
        if row.get("txn_id") != txn_id:
            continue
        out.append({
            "row_ref": row["row_ref"], "seq": row["seq"], "table": row["table_name"],
            "pk": row["pk"], "version": row["version"], "op": row["op"],
            "part_no": row.get("part_no"), "part_total": row.get("part_total"),
            "event_time": row["event_time_utc"], "row_class": row["row_class"],
            "reason_code": row.get("reason_code"),
            "amount": None if row.get("amount") is None else str(row["amount"]),
            "late": bool(row.get("late")),
        })
    return sorted(out, key=lambda r: (r["part_no"] or 0, r["seq"]))


def reconcile(store, batch_id: str, window) -> list[dict]:
    """事务账：分片守恒 + 金额口径分离，任一条不平显式标「不平」。"""
    result = build(store, batch_id, window)
    txn_events = [row for row in store.load_events(batch_id)
                  if row.get("txn_id") and row["row_class"] != "quarantine"]
    parts_seen = sum(r.event_count for r in result.results)
    held = result.incomplete + result.conflicts
    rows = [
        {"name": "事务账", "kind": "count", "left": "事务事件数",
         "left_value": len(txn_events),
         "right": "已发布事务事件 + 挂起/冲突事务事件",
         "right_value": (sum(r.event_count for r in result.complete)
                         + sum(r.event_count for r in held)),
         "ok": len(txn_events) == (
             sum(r.event_count for r in result.complete)
             + sum(r.event_count for r in held))},
        {"name": "事务状态账", "kind": "count", "left": "事务总数",
         "left_value": len(result.results),
         "right": "完整 + 未完整 + 冲突",
         "right_value": (len(result.complete) + len(result.incomplete)
                         + len(result.conflicts)),
         "ok": len(result.results) == (len(result.complete)
                                       + len(result.incomplete)
                                       + len(result.conflicts))},
        {"name": "事务金额账", "kind": "money",
         "left": "对外发布金额", "left_value": result.published_amount,
         "right": "受影响金额(不发布)", "right_value": result.affected_amount,
         "ok": result.published_amount >= 0 and result.affected_amount >= 0},
    ]
    # 分片守恒：每个 complete 事务实到分片数必须等于声明
    for item in result.complete:
        if item.part_count != item.part_total:
            rows[1]["ok"] = False
    return rows


def blank_summary() -> dict:
    return {
        "txn_total": 0, "txn_complete": 0, "txn_incomplete": 0, "txn_conflict": 0,
        "txn_published_events": 0, "txn_held_events": 0,
        "txn_published_amount": Decimal("0.00"),
        "txn_affected_amount": Decimal("0.00"),
    }


def summarize(store, batch_id: str) -> dict:
    totals = store.load_totals(batch_id)
    def _int(key):
        try:
            return int(totals.get(key) or 0)
        except (TypeError, ValueError):
            return 0
    return {
        "txn_total": _int("txn_total"), "txn_complete": _int("txn_complete"),
        "txn_incomplete": _int("txn_incomplete"),
        "txn_conflict": _int("txn_conflict"),
        "txn_published_events": _int("txn_published_events"),
        "txn_held_events": _int("txn_held_events"),
        "txn_published_amount": Decimal(str(totals.get("txn_published_amount")
                                            or "0.00")),
        "txn_affected_amount": Decimal(str(totals.get("txn_affected_amount")
                                           or "0.00")),
    }


def gate_rows() -> list[dict]:
    return [
        {"side": "分组门限", "expr": "gate(txn) = max(event_time∈txn) − 迟到窗口",
         "note": "每个事务独立推进，慢分片只挂自己"},
        {"side": "全局水位", "expr": "watermark = max(全部已应用 event_time) − 迟到窗口",
         "note": "不被任何单个未完整事务拖住"},
    ]
