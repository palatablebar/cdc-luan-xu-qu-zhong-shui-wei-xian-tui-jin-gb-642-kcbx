"""事务装配纯规则：把同一业务事务的多分片事件折叠成可发布的整体。

设计要点（对应需求）：

- 只采信「来源依据完整」的事务：part_total 声明的每个分片都到齐、且没有同片冲突，
  状态才是 complete；缺片 → incomplete，同片两种内容 → conflict（整笔不发布）。
- 分组水位线：每个事务的门限 = 组内最大 event_time − 迟到窗口。一个迟到分片只挂住
  它所属的那笔事务，其他事务照常发布，全局水位不会因为单个慢分片一直停住。
- 完全重复的分片（同 part_no 同规范化签名）按首条保留，补数重跑不会重复计入。
- 金额一律 Decimal 累加；受影响金额（挂起/冲突事务里已经到达的分片金额）单独汇总，
  与已发布金额严格分开，页面与导出可回读。

本文件无 IO、无 store，输入是 Applied 列表，输出是确定性的装配结果，便于独立复算。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from ..m2_schema.coerce import iso_utc
from ..m5_snapshot.apply import Applied


@dataclass
class TxnPart:
    """事务里的一个分片：同一 part_no 只允许一个权威内容。"""

    part_no: int
    event: Applied
    duplicates: list[Applied] = field(default_factory=list)


@dataclass
class TxnGroup:
    txn_id: str
    part_total: int
    parts: dict[int, TxnPart] = field(default_factory=dict)
    # part_no -> 内容不同的事件列表（含首条），非空即整事务冲突
    conflicts: dict[int, list[Applied]] = field(default_factory=dict)
    # part_total 声明自相矛盾，或分片号越界，整事务按冲突处理
    total_mismatch: bool = False

    @property
    def events(self) -> list[Applied]:
        ordered: list[Applied] = []
        for part in self.parts.values():
            ordered.append(part.event)
            ordered.extend(part.duplicates)
        for items in self.conflicts.values():
            ordered.extend(items)
        return sorted(ordered, key=lambda e: e.seq)

    @property
    def max_event_time(self) -> dt.datetime | None:
        times = [e.event_time for e in self.events if e.event_time is not None]
        return max(times, default=None)

    @property
    def missing_parts(self) -> list[int]:
        return [n for n in range(1, self.part_total + 1) if n not in self.parts
                and n not in self.conflicts]

    @property
    def arrived_amount(self) -> Decimal:
        """已经到达的分片金额（不管完整与否），用于受影响金额回读。

        按 part_no 去重：同一分片的重投/冲突副本不重复计；delete 分片不计金额。
        """
        total = Decimal("0.00")
        for part_no in sorted(set([p.part_no for p in self.parts.values()]
                                  + list(self.conflicts))):
            if part_no in self.parts:
                event = self.parts[part_no].event
            else:
                event = sorted(self.conflicts[part_no], key=lambda e: e.seq)[0]
            if event.op != "delete" and event.amount is not None:
                total += event.amount
        return total

    def compute_published_amount(self) -> Decimal:
        """发布金额：完整事务按权威分片求和，每个 part_no 只算一次。"""
        total = Decimal("0.00")
        for part in sorted(self.parts.values(), key=lambda p: p.part_no):
            event = part.event
            if event.op != "delete" and event.amount is not None:
                total += event.amount
        return total


@dataclass
class TxnResult:
    """一笔事务的装配结论，落库与页面共用同一形状。"""

    txn_id: str
    status: str                      # complete / incomplete / conflict
    part_total: int
    part_count: int
    missing_parts: list[int]
    conflict_parts: list[int]
    row_refs: list[str]
    tables: list[str]
    event_count: int
    max_event_time: str | None
    gate_utc: str | None             # 该事务自己的分组水位门限
    late_parts: list[int]
    published_amount: Decimal
    affected_amount: Decimal
    reason_code: str
    detail: str

    def as_store_row(self, batch_id: str) -> dict:
        return {
            "batch_id": batch_id,
            "txn_id": self.txn_id,
            "status": self.status,
            "part_total": self.part_total,
            "part_count": self.part_count,
            "missing_json": _json(self.missing_parts),
            "conflict_parts_json": _json(self.conflict_parts),
            "late_parts_json": _json(self.late_parts),
            "row_refs_json": _json(self.row_refs),
            "tables_json": _json(self.tables),
            "event_count": self.event_count,
            "max_event_time_utc": self.max_event_time,
            "gate_utc": self.gate_utc,
            "published_amount": str(self.published_amount),
            "affected_amount": str(self.affected_amount),
            "reason_code": self.reason_code,
            "detail": self.detail,
        }


@dataclass
class Assembly:
    results: list[TxnResult] = field(default_factory=list)
    # 被事务接管的事件 seq → 所属 txn_id，供主流程把它们从事务外口径里摘出去
    membership: dict[int, str] = field(default_factory=dict)

    @property
    def complete(self) -> list[TxnResult]:
        return [r for r in self.results if r.status == "complete"]

    @property
    def incomplete(self) -> list[TxnResult]:
        return [r for r in self.results if r.status == "incomplete"]

    @property
    def conflicts(self) -> list[TxnResult]:
        return [r for r in self.results if r.status == "conflict"]

    @property
    def published_amount(self) -> Decimal:
        return sum((r.published_amount for r in self.complete), Decimal("0.00"))

    @property
    def affected_amount(self) -> Decimal:
        """未完整 + 冲突事务已经到达分片的金额，对外不发布但要能回读。"""
        return sum((r.affected_amount for r in self.incomplete + self.conflicts),
                   Decimal("0.00"))

    def counters(self) -> dict:
        return {
            "txn_total": len(self.results),
            "txn_complete": len(self.complete),
            "txn_incomplete": len(self.incomplete),
            "txn_conflict": len(self.conflicts),
            "txn_published_events": sum(r.event_count for r in self.complete),
            "txn_held_events": sum(r.event_count
                                   for r in self.incomplete + self.conflicts),
        }


def _signature(event: Applied) -> str:
    """分片内容签名：规范化后的整条事件，金额两位、字段排序，保证重投可判同。"""
    import hashlib
    import json

    body = json.dumps({
        "table": event.table, "pk": event.pk, "version": event.version,
        "op": event.op, "event_time": iso_utc(event.event_time),
        "amount": None if event.amount is None else str(event.amount),
        "qty": event.qty, "payload": event.payload,
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _json(value) -> str:
    import json

    return json.dumps(list(value), ensure_ascii=False)


def assemble(events: list[Applied], window: dt.timedelta) -> Assembly:
    """把带 txn_id 的已应用事件装配成事务结论。

    冲突判定与遍历顺序无关：先按 part_no 分桶，再用签名集合判断是否同片异内容。
    """
    groups = _group(events)
    assembly = Assembly()
    results: list[TxnResult] = []
    for txn_id in sorted(groups):
        group = groups[txn_id]
        for event in group.events:
            assembly.membership[event.seq] = txn_id
        results.append(_judge(group, window))
    assembly.results = results
    return assembly


def _group(events: list[Applied]) -> dict[str, TxnGroup]:
    groups: dict[str, TxnGroup] = {}
    for event in sorted(events, key=lambda e: e.seq):
        if not getattr(event, "txn_id", ""):
            continue
        group = groups.get(event.txn_id)
        if group is None:
            group = TxnGroup(txn_id=event.txn_id,
                             part_total=event.part_total or 1)
            groups[event.txn_id] = group
        # 同一事务内 part_total 不一致 = 来源依据自相矛盾，整事务判冲突
        if event.part_total and event.part_total != group.part_total:
            group.total_mismatch = True
        part_no = event.part_no or 1
        current = group.parts.get(part_no)
        if current is None and part_no not in group.conflicts:
            group.parts[part_no] = TxnPart(part_no=part_no, event=event)
            continue
        if part_no in group.conflicts:
            # 冲突事务整体不发布：异内容进桶，同签名重投也留在事件列表里
            # （arrived_amount 按签名去重，不会重复计金额）
            group.conflicts[part_no].append(event)
            continue
        assert current is not None
        if _signature(event) == _signature(current.event):
            current.duplicates.append(event)
        else:
            # 同片异内容：把已有权威分片移入冲突桶，整事务注定不发布
            group.conflicts[part_no] = [current.event, event]
            group.parts.pop(part_no, None)
    return groups


def _judge(group: TxnGroup, window: dt.timedelta) -> TxnResult:
    max_time = group.max_event_time
    gate = None if max_time is None else max_time - window
    late_parts: list[int] = []
    if gate is not None:
        for part in group.parts.values():
            if part.event.event_time is not None and part.event.event_time < gate:
                late_parts.append(part.part_no)
        for part_no, items in group.conflicts.items():
            if any(e.event_time is not None and e.event_time < gate for e in items):
                late_parts.append(part_no)
    late_parts = sorted(set(late_parts))

    row_refs = sorted({e.row_ref for e in group.events})
    tables = sorted({e.table for e in group.events})
    conflict_parts = sorted(group.conflicts)
    missing = group.missing_parts

    if group.total_mismatch or conflict_parts:
        status, code = "conflict", "T_PART_CONFLICT"
        reasons_txt = []
        if group.total_mismatch:
            reasons_txt.append("part_total 声明不一致")
        if conflict_parts:
            reasons_txt.append(f"分片 {','.join(map(str, conflict_parts))} 同片异内容")
        detail = "；".join(reasons_txt) + "，整笔事务不发布"
        published = Decimal("0.00")
    elif missing:
        status, code = "incomplete", "T_INCOMPLETE"
        detail = (f"缺分片 {','.join(map(str, missing))}，"
                  f"应到 {group.part_total} 片实到 {len(group.parts)} 片")
        published = Decimal("0.00")
    else:
        status, code = "complete", "T_PUBLISHED"
        detail = f"{group.part_total} 个分片齐全，整体发布"
        published = group.compute_published_amount()

    return TxnResult(
        txn_id=group.txn_id, status=status, part_total=group.part_total,
        part_count=len(group.parts), missing_parts=missing,
        conflict_parts=conflict_parts, row_refs=row_refs, tables=tables,
        event_count=len(group.events),
        max_event_time=iso_utc(max_time), gate_utc=iso_utc(gate),
        late_parts=late_parts, published_amount=published,
        affected_amount=group.arrived_amount, reason_code=code, detail=detail)
