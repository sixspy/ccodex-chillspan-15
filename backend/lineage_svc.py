"""冷链血缘边登记。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from models import LineageIn
from same_source import DEVIATION, verify_batch

router = APIRouter(tags=["lineage"])


def _same_source_by_batch(conn, batch_id: int) -> dict:
    batch = conn.execute(
        "SELECT * FROM temp_batches WHERE id=?", (batch_id,)
    ).fetchone()
    if not batch:
        return {"same_source": False, "reasons": ["批次不存在"]}
    return verify_batch(conn, batch)


@router.get("/api/lineage")
def list_edges(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT e.*, a.batch_code from_code, b.batch_code to_code
               FROM lineage_edges e
               JOIN temp_batches a ON a.id=e.from_batch_id
               LEFT JOIN temp_batches b ON b.id=e.to_batch_id
               ORDER BY e.id DESC"""
        ).fetchall()
        cache: dict[int, dict] = {}
        items = []
        for r in rows:
            d = dict(r)
            for side in ("from", "to"):
                bid = d[f"{side}_batch_id"]
                if bid is None:
                    continue
                if bid not in cache:
                    cache[bid] = _same_source_by_batch(conn, bid)
                d[f"{side}_same_source"] = cache[bid]["same_source"]
            items.append(d)
        return {"items": items}


@router.post("/api/lineage")
def create_edge(body: LineageIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        src = conn.execute(
            "SELECT * FROM temp_batches WHERE id=?", (body.from_batch_id,)
        ).fetchone()
        if not src:
            raise HTTPException(404, "来源批次不存在")
        if body.to_batch_id:
            dst = conn.execute(
                "SELECT * FROM temp_batches WHERE id=?", (body.to_batch_id,)
            ).fetchone()
            if not dst:
                raise HTTPException(404, "目标批次不存在")
        # 偏离纪要：同源偏航批次不得登记血缘边，记偏航并拒写。
        for batch in (src,):
            check = verify_batch(conn, batch)
            if not check["same_source"]:
                write_audit(
                    conn,
                    user,
                    "lineage_rejected",
                    batch["batch_code"],
                    {"reason": DEVIATION, "detail": check["reasons"]},
                )
                conn.commit()
                raise HTTPException(409, f"{DEVIATION}：来源批次摘要不同源，拒写血缘边")
        cur = conn.execute(
            """INSERT INTO lineage_edges(from_batch_id,to_batch_id,edge_type,detail,created_at)
               VALUES (?,?,?,?,?)""",
            (
                body.from_batch_id,
                body.to_batch_id,
                body.edge_type,
                body.detail,
                now_iso(),
            ),
        )
        write_audit(
            conn,
            user,
            "lineage_add",
            str(body.from_batch_id),
            {"edge_id": cur.lastrowid, "type": body.edge_type},
        )
        conn.commit()
        return {"id": cur.lastrowid, "edge_type": body.edge_type}


@router.get("/api/lineage/batch/{batch_id}")
def batch_lineage(batch_id: int, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        batch = conn.execute(
            "SELECT * FROM temp_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise HTTPException(404, "批次不存在")
        out_edges = conn.execute(
            "SELECT * FROM lineage_edges WHERE from_batch_id=?", (batch_id,)
        ).fetchall()
        in_edges = conn.execute(
            "SELECT * FROM lineage_edges WHERE to_batch_id=?", (batch_id,)
        ).fetchall()
        return {
            "outbound": [dict(e) for e in out_edges],
            "inbound": [dict(e) for e in in_edges],
            "same_source": verify_batch(conn, batch),
        }