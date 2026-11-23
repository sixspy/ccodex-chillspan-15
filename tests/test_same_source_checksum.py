"""封批快照与温谱导出同源校验（甲路径）失败测先行。

三条断言先落测试文件，须先见红：
  1. 摘要不一致仍导出 —— 封批快照摘要与温谱摘要逐项比对不一致时，正式温谱册零行拒收；
  2. 影子摘要未并入正式册却已下发 —— 未哈希对齐的影子导出不得下发；
  3. 双人并发导出且手改快照摘要 —— 抢封批锁，争用只留痕，不得混用两套摘要。
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user: str = "admin") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": "123456"}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _make_batch(c: TestClient, h: dict, code: str, points: list | None = None) -> dict:
    chambers = c.get("/api/chambers", headers=h).json()["items"]
    ch = next(x for x in chambers if x["chamber_code"] == "CK-HN-03")
    probes = c.get(f"/api/probes?chamber_id={ch['id']}", headers=h).json()["items"]
    points = points or [{"t": "10:00", "c": 3.0}, {"t": "11:00", "c": 4.0}]
    r = c.post(
        "/api/batches",
        headers=h,
        json={
            "chamber_id": ch["id"],
            "probe_id": probes[0]["id"],
            "cargo_ref": "同源校验夹具",
            "temp_points": points,
            "batch_code": code,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _seal(c: TestClient, h: dict, batch_id: int) -> dict:
    r = c.post("/api/seals", headers=h, json={"batch_id": batch_id})
    assert r.status_code == 200, r.text
    return r.json()


def _formal_rows(batch_id: int) -> list:
    with connect() as conn:
        return conn.execute(
            """SELECT * FROM spectrum_exports
               WHERE batch_id=? AND status='已导出'""",
            (batch_id,),
        ).fetchall()


def _all_export_rows(batch_id: int) -> list:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM spectrum_exports WHERE batch_id=?", (batch_id,)
        ).fetchall()


# ── 失败测甲：摘要分裂仍可导出 ───────────────────────────────────────
def test_a_split_checksum_must_reject_export():
    c = TestClient(app)
    h = _auth(c)
    batch = _make_batch(c, h, "TB-SAME-A-001")
    _seal(c, h, batch["id"])

    # 摘要分裂夹具：手改封批快照摘要，温谱摘要仍按温度点序列生成。
    with connect() as conn:
        snap = conn.execute(
            "SELECT * FROM seal_snapshots WHERE batch_id=? ORDER BY id DESC LIMIT 1",
            (batch["id"],),
        ).fetchone()
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(seal_snapshots)")}
        if "checksum" in cols:
            conn.execute(
                "UPDATE seal_snapshots SET checksum='deadbeefdeadbeef' WHERE id=?",
                (snap["id"],),
            )
            conn.commit()
        else:
            # 旧库尚无摘要列：退化为改写温谱点，模拟两边各一套摘要。
            conn.execute(
                "UPDATE temp_batches SET temp_points=? WHERE id=?",
                (json.dumps([{"t": "10:00", "c": 9.9}], ensure_ascii=False), batch["id"]),
            )
            conn.commit()

    r = c.post("/api/spectrum/export", headers=h, json={"batch_id": batch["id"]})
    # 甲路径：不一致则正式温谱册零行拒收。
    assert r.status_code == 409, r.text
    assert _formal_rows(batch["id"]) == []


# ── 失败测乙：影子摘要未并入正式册却已下发 ───────────────────────────
def test_b_shadow_checksum_cannot_be_served_before_promotion():
    c = TestClient(app)
    h = _auth(c)
    batch = _make_batch(c, h, "TB-SAME-B-001")
    _seal(c, h, batch["id"])

    # 造影子：温谱导出摘要写为“待哈希对齐”，且未并入正式册（零行）。
    with connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(spectrum_exports)")}
        shadow_status = "影子待对齐"
        values = {
            "batch_id": batch["id"],
            "export_ref": f"SP-SHADOW-{batch['batch_code']}",
            "point_count": 2,
            "checksum": "pending-shadow",
            "status": shadow_status,
        }
        if "created_at" in cols:
            values["created_at"] = "2026-11-23T00:00:00"
        keys = ",".join(values)
        marks = ",".join("?" for _ in values)
        conn.execute(
            f"INSERT INTO spectrum_exports({keys}) VALUES ({marks})",
            tuple(values.values()),
        )
        conn.commit()

    # 正式册零行：影子不得作为已导出下发。
    assert _formal_rows(batch["id"]) == []
    r = c.get(
        f"/api/spectrum/SP-SHADOW-{batch['batch_code']}", headers=h
    )
    assert r.status_code == 409, r.text


# ── 争用失败测：双人并发导出且手改快照摘要 ──────────────────────────
def test_c_concurrent_export_with_snapshot_tamper_only_logs_contention():
    c = TestClient(app)
    h_admin = _auth(c, "admin")
    h_ops = _auth(c, "ops")
    batch = _make_batch(c, h_admin, "TB-SAME-C-001")
    _seal(c, h_admin, batch["id"])
    lock_key = f"batch:{batch['id']}"

    # 手改封批快照摘要，制造摘要分裂。
    with connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(seal_snapshots)")}
        if "checksum" in cols:
            conn.execute(
                "UPDATE seal_snapshots SET checksum='tampered-diagest' WHERE batch_id=?",
                (batch["id"],),
            )
        conn.commit()

    # 甲：一人正持封批锁导出时，另一人同批导出须被争用拦截，只留痕。
    held = c.post("/api/locks/acquire", headers=h_admin, json={"lock_key": lock_key})
    assert held.status_code == 200, held.text
    ops_try = c.post(
        "/api/spectrum/export", headers=h_ops, json={"batch_id": batch["id"]}
    )
    assert ops_try.status_code == 409, ops_try.text
    assert _formal_rows(batch["id"]) == []
    cont = c.get("/api/contention", headers=h_admin).json()["items"]
    assert any(x["lock_key"] == lock_key and x["action"] == "争用" for x in cont)
    c.post("/api/locks/release", headers=h_admin, json={"lock_key": lock_key})

    # 乙：双人并发导出手改快照的批次，两边都须拒收，不得各写一套摘要。
    def export(headers):
        return TestClient(app).post(
            "/api/spectrum/export", headers=headers, json={"batch_id": batch["id"]}
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(export, h_admin)
        f2 = pool.submit(export, h_ops)
        r1, r2 = f1.result(), f2.result()

    assert r1.status_code == 409, r1.text
    assert r2.status_code == 409, r2.text
    assert _formal_rows(batch["id"]) == []
    assert _all_export_rows(batch["id"]) == []


# ── 贯通链路：五页账实同源 ──────────────────────────────────────────
def test_d_five_pages_report_one_checksum_after_full_flow():
    c = TestClient(app)
    h = _auth(c)
    points = [{"t": "10:00", "c": 3.0}, {"t": "11:00", "c": 3.5}]
    batch = _make_batch(c, h, "TB-SAME-D-001", points)
    seal = _seal(c, h, batch["id"])
    code = batch["batch_code"]

    exp = c.post("/api/spectrum/export", headers=h, json={"batch_id": batch["id"]})
    assert exp.status_code == 200, exp.text
    checksum = exp.json()["checksum"]
    assert seal["checksum"] == checksum  # 封批升代与导出同一摘要

    # 封批页
    seals = c.get("/api/seals", headers=h).json()["items"]
    seal_row = next(s for s in seals if s["batch_code"] == code)
    assert seal_row["checksum"] == checksum
    assert seal_row["same_source"] is True

    # 温谱页
    exports = c.get("/api/spectrum", headers=h).json()["items"]
    exp_row = next(x for x in exports if x["export_ref"] == exp.json()["export_ref"])
    assert exp_row["checksum"] == checksum
    assert exp_row["seal_checksum"] == checksum
    assert exp_row["same_source"] is True

    # 批次页
    batches = c.get("/api/batches", headers=h).json()["items"]
    b_row = next(b for b in batches if b["batch_code"] == code)
    assert b_row["seal_checksum"] == checksum
    assert b_row["export_checksum"] == checksum
    assert b_row["same_source"] is True

    # 对账页（批次互证 + 同源总览）
    proof = c.get(f"/api/proof/batch/{code}", headers=h).json()
    assert proof["spectrum_matches_seal"] is True
    assert proof["same_source"]["seal_checksum"] == checksum
    assert proof["same_source"]["export_checksum"] == checksum
    overview = c.get("/api/same-source", headers=h).json()
    ov_row = next(i for i in overview["items"] if i["batch_code"] == code)
    assert ov_row["same_source"] is True
    assert ov_row["seal_checksum"] == ov_row["export_checksum"] == checksum

    # 血缘页：登记边后两端仍判同源
    edge = c.post(
        "/api/lineage",
        headers=h,
        json={
            "from_batch_id": batch["id"],
            "to_batch_id": None,
            "edge_type": "分装衍生",
            "detail": "同源贯通",
        },
    )
    assert edge.status_code == 200, edge.text
    edges = c.get("/api/lineage", headers=h).json()["items"]
    e_row = next(e for e in edges if e["id"] == edge.json()["id"])
    assert e_row["from_same_source"] is True
