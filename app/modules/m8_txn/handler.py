"""M8 入口：事务装配结果、未完整/冲突回读、分片证据与导出。"""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Request

from ... import errors
from ...web import Ctx, get_ctx, render
from . import service

router = APIRouter(tags=["M8 事务装配"])


def _batch(ctx: Ctx):
    batch = ctx.current_batch()
    if batch is None:
        raise errors.NotFound("还没有批次，先采信一次", code="E_NO_BATCH")
    return batch


@router.get("/api/txn")
def api_txn(ctx: Ctx = Depends(get_ctx)) -> dict:
    batch = ctx.current_batch()
    if batch is None:
        return {"batch_id": None, "results": [], "summary": service.blank_summary()}
    window = ctx.settings.late_window
    return {
        "batch_id": batch["batch_id"],
        "summary": service.summarize(ctx.store, batch["batch_id"]),
        "results": [_plain(row) for row in
                    service.page_rows(ctx.store, batch["batch_id"])],
        "reconcile": [_plain(row) for row in
                      service.reconcile(ctx.store, batch["batch_id"], window)],
        "gates": service.gate_rows(),
    }


@router.post("/api/txn/rebuild")
def api_txn_rebuild(ctx: Ctx = Depends(get_ctx)) -> dict:
    batch = _batch(ctx)
    result = service.rebuild(ctx.store, batch["batch_id"], ctx.settings.late_window)
    return {
        "batch_id": batch["batch_id"],
        "counters": result.counters(),
        "published_amount": f"{result.published_amount:.2f}",
        "affected_amount": f"{result.affected_amount:.2f}",
        "rows": len(result.results),
    }


@router.get("/api/txn/{txn_id}")
def api_txn_detail(txn_id: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    batch = _batch(ctx)
    rows = service.detail_rows(ctx.store, batch["batch_id"], txn_id)
    if not rows:
        raise errors.NotFound(f"事务 {txn_id} 不存在", code="E_NOT_FOUND")
    header = next((r for r in service.page_rows(ctx.store, batch["batch_id"])
                   if r["txn_id"] == txn_id), None)
    return {"batch_id": batch["batch_id"], "txn_id": txn_id,
            "header": _plain(header) if header else None,
            "parts": rows}


@router.get("/txn")
def page_txn(request: Request, ctx: Ctx = Depends(get_ctx)):
    batch = ctx.current_batch()
    if batch is None:
        return render(request, "txn.html", batch=None, page="txn",
                      results=[], summary=service.blank_summary(),
                      reconcile=[], gate_rows=service.gate_rows(),
                      totals={}, inputs=[], page_title="事务装配")
    window = ctx.settings.late_window
    return render(request, "txn.html", batch=batch, page="txn",
                  results=service.page_rows(ctx.store, batch["batch_id"]),
                  summary=service.summarize(ctx.store, batch["batch_id"]),
                  reconcile=service.reconcile(ctx.store, batch["batch_id"], window),
                  gate_rows=service.gate_rows(),
                  totals=ctx.store.load_totals(batch["batch_id"]),
                  inputs=ctx.store.load_inputs(batch["batch_id"]),
                  page_title="事务装配")


def _plain(row: dict | None) -> dict | None:
    if row is None:
        return None
    return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in row.items()}
