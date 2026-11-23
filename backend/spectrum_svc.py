"""温谱导出（甲路径：导出前与封批快照逐项比对，不一致零行拒收）。"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from locks import release_and_commit, require_lock
from models import SpectrumIn
from same_source import (
    FORMAL_STATUS,
    checksum_for,
    latest_snapshot,
    points_of,
)

router = APIRouter(tags=["spectrum"])


@router.get("/api/spectrum")
def list_exports(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT s.*, b.batch_code, b.generation, b.temp_points
               FROM spectrum_exports s
               JOIN temp_batches b ON b.id=s.batch_id ORDER BY s.id DESC"""
        ).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            points = json.loads(d.pop("temp_points", None) or "[]")
            snapshot = latest_snapshot(conn, d["batch_id"])
            d["seal_checksum"] = snapshot["checksum"] if snapshot else None
            d["computed_checksum"] = checksum_for(
                d["batch_code"], d["generation"], points
            )
            d["same_source"] = (
                d["status"] == FORMAL_STATUS
                and d["seal_checksum"] == d["checksum"]
                and d["checksum"] == d["computed_checksum"]
            )
            items.append(d)
        return {"items": items}


@router.post("/api/spectrum/export")
def export_spectrum(body: SpectrumIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    lock_key = f"batch:{body.batch_id}"
    with connect() as conn:
        require_lock(conn, lock_key, user)
        try:
            batch = conn.execute(
                "SELECT * FROM temp_batches WHERE id=?", (body.batch_id,)
            ).fetchone()
            if not batch:
                raise HTTPException(404, "批次不存在")
            if batch["status"] != "已封批":
                raise HTTPException(409, "须先封批再导出温谱")

            points = points_of(batch)
            checksum = checksum_for(batch["batch_code"], batch["generation"], points)

            # 甲路径硬闸：温谱摘要须与封批快照校验和逐项一致。
            snapshot = latest_snapshot(conn, body.batch_id)
            if snapshot is None or not snapshot["checksum"]:
                raise HTTPException(409, "封批快照缺校验和，禁止导出温谱")
            if int(snapshot["batch_generation"]) != int(batch["generation"]):
                raise HTTPException(409, "封批快照代数与批次不一致，禁止导出温谱")
            if snapshot["checksum"] != checksum:
                write_audit(
                    conn,
                    user,
                    "spectrum_export_rejected",
                    f"batch:{body.batch_id}",
                    {
                        "reason": "同源偏航",
                        "seal_checksum": snapshot["checksum"],
                        "spectrum_checksum": checksum,
                    },
                )
                conn.commit()
                raise HTTPException(409, "摘要不一致：同源偏航，正式温谱册拒收")

            ref = f"SP-{batch['batch_code']}-G{batch['generation']}"
            dup = conn.execute(
                "SELECT id FROM spectrum_exports WHERE export_ref=?", (ref,)
            ).fetchone()
            if dup:
                raise HTTPException(409, "该代温谱已导出")
            cur = conn.execute(
                """INSERT INTO spectrum_exports(batch_id,export_ref,point_count,checksum,
                   status,created_at) VALUES (?,?,?,?,?,?)""",
                (
                    body.batch_id,
                    ref,
                    len(points),
                    checksum,
                    FORMAL_STATUS,
                    now_iso(),
                ),
            )
            write_audit(
                conn,
                user,
                "spectrum_export",
                ref,
                {"checksum": checksum, "seal_checksum": snapshot["checksum"]},
            )
            conn.commit()
            return {
                "id": cur.lastrowid,
                "export_ref": ref,
                "point_count": len(points),
                "checksum": checksum,
                "seal_checksum": snapshot["checksum"],
                "status": FORMAL_STATUS,
            }
        finally:
            release_and_commit(conn, lock_key, user)


@router.get("/api/spectrum/{export_ref}")
def get_export(export_ref: str, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        row = conn.execute(
            """SELECT s.*, b.temp_points, b.batch_code, b.generation
               FROM spectrum_exports s
               JOIN temp_batches b ON b.id=s.batch_id WHERE s.export_ref=?""",
            (export_ref,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "导出不存在")
        # 影子摘要未并入正式册前不得下发。
        if row["status"] != FORMAL_STATUS:
            raise HTTPException(409, "影子摘要未并入正式温谱册，禁止下发")

        points = json.loads(row["temp_points"] or "[]")
        checksum = checksum_for(row["batch_code"], row["generation"], points)
        snapshot = latest_snapshot(conn, row["batch_id"])
        seal_checksum = snapshot["checksum"] if snapshot else None
        if row["checksum"] != checksum or seal_checksum != checksum:
            raise HTTPException(409, "摘要不一致：同源偏航，拒绝下发")

        data = dict(row)
        data["temp_points"] = points
        data["seal_checksum"] = seal_checksum
        return data
