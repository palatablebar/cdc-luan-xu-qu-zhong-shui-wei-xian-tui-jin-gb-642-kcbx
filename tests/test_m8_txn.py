"""M8 事务装配：完整才发布、缺片挂起、同片冲突整笔不发布、分组水位不拖全局、
受影响金额可回读、失败保留旧版、同补数重跑不新增不重复计入。
"""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import pytest

from app import config
from app.modules.m5_snapshot.apply import Applied
from app.modules.m6_batches import service as batch_service
from app.modules.m8_txn import assembly as asm
from app.modules.m8_txn import service as txn_service
from app.store import Store

WINDOW = dt.timedelta(minutes=10)


def _ev(*, seq, txn_id=None, part_no=None, part_total=None, table="orders",
        pk="1", version=1, op="upsert", amount="100.00",
        when="2026-11-15T10:00:00Z", status="已支付"):
    return Applied(
        table=table, pk=pk, version=version, op=op,
        payload={"status": status, **({"amount": amount} if amount is not None else {})},
        amount=None if amount is None else Decimal(amount), qty=None,
        event_time=dt.datetime.fromisoformat(when.replace("Z", "+00:00")),
        row_ref=f"f:{seq}", late=False, seq=seq,
        txn_id=txn_id or "", part_no=part_no, part_total=part_total)


# ---------- 纯规则：装配判定 ----------

def test_complete_transaction_published_only_when_all_parts_present():
    events = [
        _ev(seq=1, txn_id="T1", part_no=1, part_total=2, pk="1", amount="100.00"),
        _ev(seq=2, txn_id="T1", part_no=2, part_total=2, table="payments",
            pk="1", amount="100.00", when="2026-11-15T10:00:05Z"),
    ]
    result = asm.assemble(events, WINDOW)
    assert len(result.results) == 1
    out = result.results[0]
    assert out.status == "complete"
    assert out.missing_parts == []
    # 订单片 + 支付片各算各的钱
    assert out.published_amount == Decimal("200.00")
    assert result.published_amount == Decimal("200.00")


def test_missing_shard_holds_whole_transaction():
    events = [
        _ev(seq=1, txn_id="T2", part_no=1, part_total=3, pk="2", amount="300.00"),
        _ev(seq=2, txn_id="T2", part_no=2, part_total=3, table="payments",
            pk="2", amount="300.00"),
    ]
    result = asm.assemble(events, WINDOW)
    out = result.results[0]
    assert out.status == "incomplete"
    assert out.missing_parts == [3]
    assert out.published_amount == Decimal("0.00")
    # 受影响金额仍可回读（按分片去重）
    assert out.affected_amount == Decimal("600.00")
    assert result.published_amount == Decimal("0.00")


def test_same_part_two_payloads_conflicts_and_blocks_publication():
    events = [
        _ev(seq=1, txn_id="T3", part_no=1, part_total=2, pk="3", amount="500.00"),
        _ev(seq=2, txn_id="T3", part_no=2, part_total=2, table="payments",
            pk="3", amount="500.00", status="已支付"),
        _ev(seq=3, txn_id="T3", part_no=2, part_total=2, table="payments",
            pk="3", amount="500.00", status="支付失败",
            when="2026-11-15T10:00:09Z"),
    ]
    result = asm.assemble(events, WINDOW)
    out = result.results[0]
    assert out.status == "conflict"
    assert out.conflict_parts == [2]
    assert out.reason_code == "T_PART_CONFLICT"
    assert out.published_amount == Decimal("0.00")


def test_duplicate_part_delivery_counted_once():
    events = [
        _ev(seq=1, txn_id="T4", part_no=1, part_total=2, pk="4", amount="60.00"),
        _ev(seq=2, txn_id="T4", part_no=2, part_total=2, table="payments",
            pk="4", amount="60.00", when="2026-11-15T10:00:05Z"),
        # 同签名重投：与 part 2 内容、时间完全一致
        _ev(seq=3, txn_id="T4", part_no=2, part_total=2, table="payments",
            pk="4", amount="60.00", when="2026-11-15T10:00:05Z"),
    ]
    result = asm.assemble(events, WINDOW)
    out = result.results[0]
    assert out.status == "complete"
    assert out.published_amount == Decimal("120.00")  # 不是 180
    assert out.event_count == 3


def test_part_total_mismatch_is_conflict():
    events = [
        _ev(seq=1, txn_id="T5", part_no=1, part_total=2, pk="5"),
        _ev(seq=2, txn_id="T5", part_no=2, part_total=3, table="payments", pk="5"),
    ]
    out = asm.assemble(events, WINDOW).results[0]
    assert out.status == "conflict"
    assert out.reason_code == "T_PART_CONFLICT"


def test_group_gate_independent_of_global_watermark():
    """慢分片只挂自己那笔事务：T6 内部迟到，T7 不受影响照常发布。"""
    events = [
        # T6：part2 比组内最大时间晚 15 分钟（超过 10 分钟窗口），但仍齐全
        _ev(seq=1, txn_id="T6", part_no=1, part_total=2, pk="6", amount="80.00",
            when="2026-11-15T10:20:00Z"),
        _ev(seq=2, txn_id="T6", part_no=2, part_total=2, table="payments",
            pk="6", amount="80.00", when="2026-11-15T10:35:00Z"),
        # T7：时间更晚，独立成组，门限自己算
        _ev(seq=3, txn_id="T7", part_no=1, part_total=1, pk="7", amount="40.00",
            when="2026-11-15T11:00:00Z"),
    ]
    result = asm.assemble(events, WINDOW)
    by_id = {r.txn_id: r for r in result.results}
    assert by_id["T6"].status == "complete"
    assert by_id["T6"].late_parts == [1]
    assert by_id["T7"].status == "complete"
    assert by_id["T7"].late_parts == []
    # T6 自己的门限 = 10:35 − 10min = 10:25
    assert by_id["T6"].gate_utc == "2026-11-15T10:25:00Z"


def test_incomplete_transaction_does_not_block_others():
    events = [
        _ev(seq=1, txn_id="HOLD", part_no=1, part_total=2, pk="8",
            when="2026-11-15T09:00:00Z"),
        _ev(seq=2, txn_id="OK", part_no=1, part_total=1, pk="9", amount="10.00",
            when="2026-11-15T12:00:00Z"),
    ]
    result = asm.assemble(events, WINDOW)
    statuses = {r.txn_id: r.status for r in result.results}
    assert statuses == {"HOLD": "incomplete", "OK": "complete"}


def test_amounts_are_decimal_and_quantized():
    # 量化在 M2 归一层完成；M8 只做 Decimal 精确累加，禁止 float
    events = [
        _ev(seq=1, txn_id="T8", part_no=1, part_total=1, pk="10",
            amount="12.35"),
    ]
    out = asm.assemble(events, WINDOW).results[0]
    assert isinstance(out.published_amount, Decimal)
    assert out.published_amount == Decimal("12.35")
    total = out.published_amount + Decimal("0.01")
    assert isinstance(total, Decimal)


def test_judgement_independent_of_arrival_order():
    """乱序到达：分片 2 先到、1 后到，结论仍由集合决定。"""
    events = [
        _ev(seq=1, txn_id="T9", part_no=2, part_total=2, table="payments", pk="11",
            when="2026-11-15T10:00:05Z"),
        _ev(seq=2, txn_id="T9", part_no=1, part_total=2, pk="11"),
    ]
    out = asm.assemble(events, WINDOW).results[0]
    assert out.status == "complete"
    assert out.missing_parts == []


# ---------- 落库与重算 ----------

@pytest.fixture()
def txn_run(settings, store):
    return batch_service.run(settings=settings, store=store, source="txn")


def test_pipeline_publishes_only_complete_transactions(settings, store, txn_run):
    batch_id = txn_run["batch"]["batch_id"]
    rows = store.load_txn_results(batch_id)
    by_id = {r["txn_id"]: r for r in rows}
    assert by_id["T-8001"]["status"] == "complete"
    assert by_id["T-8004"]["status"] == "complete"
    assert by_id["T-8005"]["status"] == "complete"
    assert by_id["T-8002"]["status"] == "incomplete"
    assert by_id["T-8002"]["missing"] == [3]
    assert by_id["T-8003"]["status"] == "conflict"
    assert by_id["T-8003"]["conflict_parts"] == [2]


def test_published_amount_excludes_held_and_conflicted(settings, store, txn_run):
    totals = txn_run["totals"]
    # 完整三笔：220.00 (8001) + 160.00 (8004) + 120.00 (8005)
    assert totals["txn_published_amount"] == "500.00"
    # 挂起 600 + 冲突 1000，受影响金额单独披露
    assert totals["txn_affected_amount"] == "1600.00"
    assert Decimal(totals["txn_published_amount"]) >= 0


def test_held_transaction_evidence_is_readable(settings, store, txn_run):
    batch_id = txn_run["batch"]["batch_id"]
    parts = txn_service.detail_rows(store, batch_id, "T-8002")
    assert {p["part_no"] for p in parts} == {1, 2}
    refs = {p["row_ref"] for p in parts}
    assert any("events_txn.jsonl" in r for r in refs)
    page = next(r for r in txn_service.page_rows(store, batch_id)
                if r["txn_id"] == "T-8002")
    assert page["missing"] == [3]
    assert page["affected_amount"] == Decimal("600.00")


def test_rebuild_is_idempotent_and_keeps_totals(settings, store, txn_run):
    batch_id = txn_run["batch"]["batch_id"]
    before = store.count_rows(batch_id)
    first = txn_service.summarize(store, batch_id)
    txn_service.rebuild(store, batch_id, settings.late_window)
    txn_service.rebuild(store, batch_id, settings.late_window)
    after = store.count_rows(batch_id)
    second = txn_service.summarize(store, batch_id)
    assert before == after
    assert first == second


def test_same_backfill_rerun_adds_no_rows(settings, store, txn_run):
    batch_id = txn_run["batch"]["batch_id"]
    before = store.count_rows(batch_id)
    rerun = batch_service.run(settings=settings, store=store, source="txn")
    assert rerun["idempotent_replay"] is True
    assert store.count_rows(batch_id) == before
    assert len(store.list_batches()) == 1


def test_failed_run_keeps_previous_txn_results(settings, store, txn_run,
                                                monkeypatch):
    """落库阶段抛错时整事务回滚，旧版 txn_results 一行不丢。"""
    import sqlite3

    batch_id = txn_run["batch"]["batch_id"]
    before = store.load_txn_results(batch_id)

    class Boom(Exception):
        pass

    real_connect = sqlite3.connect

    class _ConnProxy:
        """只在写 txn_results 的 INSERT 上炸，其余语句透传。"""

        def __init__(self, real):
            object.__setattr__(self, "_real", real)

        def execute(self, sql, *args, **kwargs):
            if "INSERT INTO txn_results" in sql:
                raise Boom("simulated write failure")
            return self._real.execute(sql, *args, **kwargs)

        def __setattr__(self, name, value):
            setattr(self._real, name, value)

        def __getattr__(self, name):
            return getattr(self._real, name)

    def fake_connect(*args, **kwargs):
        return _ConnProxy(real_connect(*args, **kwargs))

    monkeypatch.setattr(sqlite3, "connect", fake_connect)
    with pytest.raises(Boom):
        txn_service.rebuild(store, batch_id, settings.late_window)
    monkeypatch.undo()
    after = store.load_txn_results(batch_id)
    assert [(r["txn_id"], r["status"]) for r in after] == \
        [(r["txn_id"], r["status"]) for r in before]


def test_csv_transaction_shards_assemble_end_to_end(tmp_path):
    """CSV 导出带 txn 列时走同一条装配路径，完整事务正常发布。"""
    samples = tmp_path / "samples"
    samples.mkdir()
    (samples / "txn.csv").write_text(
        "table,pk,version,op,event_time,schema,ref_no,amt,st,qt,"
        "txn_id,part_no,part_total\n"
        "orders,9100,1,upsert,2026-11-15T10:00:00Z,v2,,100.00,已支付,,"
        "T-CSV,1,2\n"
        "payments,9100,1,upsert,2026-11-15T10:00:05Z,v2,,100.00,已支付,,"
        "T-CSV,2,2\n",
        encoding="utf-8")
    local_settings = config.Settings(db_path=str(tmp_path / "csv.sqlite"),
                                     sample_dir=str(samples))
    local_store = Store(local_settings.db_path)
    run = batch_service.run(settings=local_settings, store=local_store,
                            source="txn.csv")
    result = local_store.load_txn_results(run["batch"]["batch_id"])
    assert len(result) == 1
    assert result[0]["status"] == "complete"
    assert result[0]["published_amount"] == Decimal("200.00")


def test_bad_txn_envelope_is_quarantined(settings, tmp_path):
    """part_no > part_total 等坏事务信封直接隔离，不拼出伪完整事务。"""
    samples = tmp_path / "samples"
    samples.mkdir(exist_ok=True)
    line = json.dumps({
        "table": "orders", "pk": "1", "version": 1, "op": "upsert",
        "event_time": "2026-11-15T10:00:00Z", "schema": "v2",
        "txn_id": "BAD", "part_no": 3, "part_total": 2,
        "payload": {"status": "已支付", "amount": "1.00"},
    })
    (samples / "events_bad.jsonl").write_text(
        "# cdc-export v2 rows=1 charset=utf-8\n" + line + "\n", encoding="utf-8")
    local_settings = config.Settings(db_path=str(tmp_path / "t.sqlite"),
                                     sample_dir=str(samples))
    local_store = Store(local_settings.db_path)
    run = batch_service.run(settings=local_settings, store=local_store,
                            source="events_bad.jsonl")
    assert run["counters"]["quarantine"] == 1
    assert store_load_reason(local_store, run) == "E_BAD_TXN"


def store_load_reason(store, run):
    rows = store.load_events(run["batch"]["batch_id"], "quarantine")
    return rows[0]["reason_code"]


# ---------- API 与导出 ----------

def test_txn_http(settings):
    from fastapi.testclient import TestClient

    from app.main import create_app
    # seed 只跑默认 all（不含事务文件），这里显式采信 txn 来源
    with TestClient(create_app(settings, seed=True)) as client:
        client.post("/api/events/import", json={"source": "txn"})
        payload = client.get("/api/txn").json()
        assert payload["summary"]["txn_total"] == 5
        assert payload["summary"]["txn_complete"] == 3
        statuses = {r["txn_id"]: r["status"] for r in payload["results"]}
        assert statuses["T-8002"] == "incomplete"
        # 明细回读
        detail = client.get("/api/txn/T-8002").json()
        assert len(detail["parts"]) == 2
        assert client.get("/api/txn/NOPE").status_code == 404
        # 导出
        csv_resp = client.get("/api/export/txn.csv")
        assert csv_resp.status_code == 200
        assert "T-8001" in csv_resp.content.decode("utf-8")
        doc = client.get("/api/export/txn.json").json()
        published_ids = {r["txn_id"] for r in doc["published"]}
        held_ids = {r["txn_id"] for r in doc["held"]}
        assert "T-8001" in published_ids
        assert "T-8002" in held_ids and "T-8003" in held_ids
        assert doc["summary"]["published_amount"] == "500.00"
        # 页面
        page = client.get("/txn")
        assert page.status_code == 200
        assert "事务装配" in page.text


def test_reconcile_balances(settings, store, txn_run):
    rows = txn_service.reconcile(store, txn_run["batch"]["batch_id"],
                                 settings.late_window)
    assert all(r["ok"] for r in rows)
    names = [r["name"] for r in rows]
    assert names == ["事务账", "事务状态账", "事务金额账"]


def test_manifest_carries_transaction_section(settings, store, txn_run):
    from app.modules.m7_export import manifest_out
    doc = manifest_out.build(store, txn_run["batch"]["batch_id"])
    section = doc["transactions"]
    assert section["published_count"] == 3
    assert section["held_count"] == 2
    published_ids = {r["txn_id"] for r in section["published"]}
    held_ids = {r["txn_id"] for r in section["held"]}
    assert {"T-8001", "T-8004", "T-8005"} == published_ids
    assert {"T-8002", "T-8003"} == held_ids
    # 计数里也带事务口径
    assert doc["counters"]["txn_published_amount"] == "500.00"


def test_backfill_completes_held_transaction(tmp_path):
    """补数到达后同一来源重跑：挂起事务转完整，发布金额更新，不产生重复批次。"""
    samples = tmp_path / "samples"
    samples.mkdir()
    header = "# cdc-export v2 rows={n} charset=utf-8\n"

    def line(part_no, part_total, when, amount, status="已支付"):
        return json.dumps({
            "table": "payments" if part_no == 2 else "orders",
            "pk": "900", "version": 1, "op": "upsert",
            "event_time": when, "schema": "v2", "txn_id": "TX-BACKFILL",
            "part_no": part_no, "part_total": part_total,
            "payload": {"status": status, "amount": amount},
        }, ensure_ascii=False)

    # 第一轮：只有前两片，缺第 3 片
    (samples / "events_bf.jsonl").write_text(
        header.format(n=2)
        + line(1, 3, "2026-11-15T10:00:00Z", "70.00") + "\n"
        + line(2, 3, "2026-11-15T10:00:05Z", "70.00") + "\n",
        encoding="utf-8")
    local_settings = config.Settings(db_path=str(tmp_path / "bf.sqlite"),
                                     sample_dir=str(samples))
    local_store = Store(local_settings.db_path)
    first = batch_service.run(settings=local_settings, store=local_store,
                              source="events_bf.jsonl")
    bid = first["batch"]["batch_id"]
    held = local_store.load_txn_results(bid)[0]
    assert held["status"] == "incomplete"
    assert held["missing"] == [3]
    assert first["totals"]["txn_published_amount"] == "0.00"

    # 第二轮：补数文件补齐第 3 片（新文件 → 新 input_digest → 新批次）
    (samples / "events_bf_fill.jsonl").write_text(
        header.format(n=1)
        + json.dumps({
            "table": "refunds", "pk": "900", "version": 1, "op": "upsert",
            "event_time": "2026-11-15T10:00:10Z", "schema": "v2",
            "txn_id": "TX-BACKFILL", "part_no": 3, "part_total": 3,
            "payload": {"status": "退款完成", "amount": "10.00"}},
            ensure_ascii=False) + "\n",
        encoding="utf-8")
    second = batch_service.run(
        settings=local_settings, store=local_store, source="events_bf_fill.jsonl")
    # 注意：跨文件补数在本批次里只看到第 3 片，会先挂起；这里验证的是
    # 同输入重跑幂等 + 全量补数（把三片放进同一文件）转完整的主路径
    assert second["idempotent_replay"] is False

    # 全量补数：同一文件名内容换成完整三片（模拟回源重导），同输入再跑
    (samples / "events_bf.jsonl").write_text(
        header.format(n=3)
        + line(1, 3, "2026-11-15T10:00:00Z", "70.00") + "\n"
        + line(2, 3, "2026-11-15T10:00:05Z", "70.00") + "\n"
        + json.dumps({
            "table": "refunds", "pk": "900", "version": 1, "op": "upsert",
            "event_time": "2026-11-15T10:00:10Z", "schema": "v2",
            "txn_id": "TX-BACKFILL", "part_no": 3, "part_total": 3,
            "payload": {"status": "退款完成", "amount": "10.00"}},
            ensure_ascii=False) + "\n",
        encoding="utf-8")
    final = batch_service.run(settings=local_settings, store=local_store,
                              source="events_bf.jsonl")
    fbid = final["batch"]["batch_id"]
    done = local_store.load_txn_results(fbid)[0]
    assert done["status"] == "complete"
    assert done["missing"] == []
    # 三片金额 70 + 70 + 10，各算一次
    assert final["totals"]["txn_published_amount"] == "150.00"


def test_global_watermark_not_held_by_slow_shard(settings, store, txn_run):
    """T-8002 缺片挂起，但全局水位仍由最大已应用事件推进，不会停住。"""
    wm = txn_run["watermark"]
    # 最大事件 10:40:05 − 10 分钟 = 10:30:05，与缺片事务无关
    assert wm["final"] == "2026-11-15T10:30:05Z"
    assert store.watermark_high() == "2026-11-15T10:30:05Z"
    # 挂起事务有自己的分组门限可读
    held = next(r for r in store.load_txn_results(txn_run["batch"]["batch_id"])
                if r["txn_id"] == "T-8002")
    assert held["gate_utc"] == "2026-11-15T09:55:05Z"
