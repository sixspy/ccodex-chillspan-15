"""封批快照与温谱导出同源摘要（唯一定义，两处不得各算一套）。

甲路径：导出前把温谱摘要与封批快照校验和逐项比对，不一致则正式温谱册
零行拒收。本模块是摘要口径的唯一来源，封批、温谱导出与五页互证都调用它。

同源校验纪要（2026-11-23 定稿，当日不再改口）：
  - 择甲路径；乙路径影子摘要仅作未并入的暂存，不得下发；第三路（各算一套）拒绝。
  - 封批页、温谱页、批次页、对账页、血缘页五页账实须同源。
  - 偏离纪要时记“同源偏航”并拒写。
"""
from __future__ import annotations

import hashlib
import json

POLICY = "甲"
FORMAL_STATUS = "已导出"
SHADOW_STATUS = "影子待对齐"
DEVIATION = "同源偏航"


def checksum_for(batch_code: str, generation: int, points: list) -> str:
    """同源摘要：批次码 + 代数 + 温度点序列的规范 JSON 的 SHA-256。"""
    payload = json.dumps(
        {
            "batch_code": batch_code,
            "generation": int(generation),
            "points": points,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def points_of(batch) -> list:
    return json.loads(batch["temp_points"] or "[]")


def latest_snapshot(conn, batch_id: int):
    return conn.execute(
        "SELECT * FROM seal_snapshots WHERE batch_id=? ORDER BY id DESC LIMIT 1",
        (batch_id,),
    ).fetchone()


def _snapshot_checksum(snapshot) -> str | None:
    if not snapshot:
        return None
    try:
        return snapshot["checksum"] or None
    except (IndexError, KeyError):
        return None


def latest_shadow_rows(conn, batch_id: int) -> int:
    return conn.execute(
        """SELECT COUNT(*) c FROM spectrum_exports
           WHERE batch_id=? AND status!=?""",
        (batch_id, FORMAL_STATUS),
    ).fetchone()["c"]


def verify_batch(conn, batch, snapshot=None) -> dict:
    """对单批做同源互证，返回封批/温谱/当前点序列三方摘要与判定。

    同源只看摘要是否一致；是否已导出属于互证另一条规则（见 /api/proof）。
    """
    points = points_of(batch)
    canonical = checksum_for(batch["batch_code"], batch["generation"], points)
    snap = snapshot if snapshot is not None else latest_snapshot(conn, batch["id"])
    seal_checksum = _snapshot_checksum(snap)

    formal = conn.execute(
        """SELECT * FROM spectrum_exports
           WHERE batch_id=? AND status=? ORDER BY id DESC""",
        (batch["id"], FORMAL_STATUS),
    ).fetchall()

    reasons: list[str] = []
    if snap is None and not formal and batch["status"] != "已封批":
        # 未封批且无导出：尚无同源要求，账实一致。
        pass
    elif snap is None:
        reasons.append("无封批快照")
    else:
        if int(snap["batch_generation"]) != int(batch["generation"]):
            reasons.append("封批代与批次代不一致")
        if seal_checksum != canonical:
            reasons.append("封批快照摘要与当前点序列不一致")

    for exp in formal:
        if exp["checksum"] != canonical:
            reasons.append("温谱导出摘要与当前点序列不一致")
        if seal_checksum != exp["checksum"]:
            reasons.append("温谱导出摘要与封批快照不一致")

    return {
        "batch_code": batch["batch_code"],
        "generation": int(batch["generation"]),
        "seal_generation": int(snap["batch_generation"]) if snap else None,
        "seal_checksum": seal_checksum,
        "export_checksum": formal[0]["checksum"] if formal else None,
        "canonical_checksum": canonical,
        "formal_export_rows": len(formal),
        "shadow_rows": latest_shadow_rows(conn, batch["id"]),
        "same_source": not reasons,
        "reasons": reasons,
    }
