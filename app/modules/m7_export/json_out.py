"""JSON 导出：Decimal 与大整数安全序列化。

规则：

- Decimal → 字符串（保两位，不带科学计数法）；
- 绝对值超过 2^53 的整数 → 字符串（JS 侧不会掉精度，TRAP-09）；
- 时刻 → ISO8601 UTC 字符串；
- 键顺序固定，同一输入重复导出字节一致。
"""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

JS_SAFE_INT = 2 ** 53


def _convert(value):
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    if isinstance(value, dt.datetime):
        return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, dict):
        return {str(k): _convert(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_convert(v) for v in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and abs(value) > JS_SAFE_INT:
        return str(value)
    return value


def dumps(payload: dict) -> str:
    return json.dumps(_convert(payload), ensure_ascii=False, sort_keys=False,
                      indent=2, separators=(",", ": "))


def snapshot_document(*, batch: dict, summary: dict, rows: list[dict],
                      pending: list[dict], reconcile: list[dict]) -> dict:
    return {
        "batch_id": batch.get("batch_id"),
        "run_id": batch.get("run_id"),
        "as_of_utc": summary.get("as_of"),
        "watermark_utc": batch.get("watermark_utc"),
        "counters": {
            "keys_total": summary.get("keys_total"),
            "snapshot_keys": summary.get("snapshot_keys"),
            "active_keys": summary.get("active_keys"),
            "tombstone_keys": summary.get("tombstone_keys"),
            "pending_only_keys": summary.get("pending_only"),
        },
        "amounts": {
            "snapshot": str(summary.get("amount_snapshot") or "0.00"),
            "pending": str(summary.get("amount_pending") or "0.00"),
            "applied": str(summary.get("amount_applied") or "0.00"),
        },
        "reconcile": reconcile,
        "rows": [_row(row) for row in rows],
        "pending": [_row(row) for row in pending],
    }


def txn_document(*, batch: dict, summary: dict, results: list[dict],
                 reconcile: list[dict]) -> dict:
    """对外事务结果：complete 进 published；未完整/冲突保留在 held 里可回读。"""
    published, held = [], []
    for row in results:
        item = {
            "txn_id": row.get("txn_id"),
            "status": row.get("status"),
            "part_total": row.get("part_total"),
            "part_count": row.get("part_count"),
            "missing_parts": row.get("missing", []),
            "conflict_parts": row.get("conflict_parts", []),
            "late_parts": row.get("late_parts", []),
            "tables": row.get("tables", []),
            "event_count": row.get("event_count"),
            "max_event_time_utc": row.get("max_event_time"),
            "gate_utc": row.get("gate"),
            "published_amount": f"{Decimal(str(row.get('published_amount') or 0)):.2f}",
            "affected_amount": f"{Decimal(str(row.get('affected_amount') or 0)):.2f}",
            "reason_code": row.get("reason_code"),
            "evidence_row_refs": row.get("row_refs", []),
        }
        (published if row.get("status") == "complete" else held).append(item)
    return {
        "batch_id": batch.get("batch_id"),
        "run_id": batch.get("run_id"),
        "watermark_utc": batch.get("watermark_utc"),
        "rule": "only complete transactions are published; held rows are readable",
        "summary": {
            "txn_total": summary.get("txn_total"),
            "txn_complete": summary.get("txn_complete"),
            "txn_incomplete": summary.get("txn_incomplete"),
            "txn_conflict": summary.get("txn_conflict"),
            "published_amount": f"{Decimal(str(summary.get('txn_published_amount') or 0)):.2f}",
            "affected_amount": f"{Decimal(str(summary.get('txn_affected_amount') or 0)):.2f}",
        },
        "reconcile": reconcile,
        "published": published,
        "held": held,
    }


def _row(row: dict) -> dict:
    out = {
        "table": row.get("table") or row.get("table_name"),
        "pk": str(row.get("pk")),
        "version": row.get("version"),
        "op": row.get("op"),
        "kind": row.get("kind") or ("墓碑" if row.get("op") == "delete" else "活跃"),
        "amount": None if row.get("amount") is None else f"{Decimal(str(row['amount'])):.2f}",
        "event_time_utc": row.get("event_time") or row.get("event_time_utc"),
        "row_ref": row.get("row_ref"),
        "late": bool(row.get("late")),
        "pending": bool(row.get("pending")),
    }
    return out
