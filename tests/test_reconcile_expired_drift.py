"""失败测乙：对账不得再把过期探头的正式批计入在批绿灯。

直接往库层注入一条过期探头（PR-HB-B1，calibration_due=2026-11-20）的在批
正式批，模拟门闸落地前的历史污染；对账必须单列污染并判失衡，不得假绿。
"""
import json

import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db, now_iso


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user="admin") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": "123456"}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _plant_expired_official_batch() -> None:
    with connect() as conn:
        probe = conn.execute(
            "SELECT * FROM probes WHERE probe_code='PR-HB-B1'"
        ).fetchone()
        assert probe["calibration_due"] < "2026-11-22"
        conn.execute(
            """INSERT INTO temp_batches(batch_code,chamber_id,probe_id,cargo_ref,
               temp_points,generation,status,min_observed,max_observed,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                "TB-POLLUTED-EXP-01",
                probe["chamber_id"],
                probe["id"],
                "历史污染货载",
                json.dumps([{"t": "08:00", "c": -21.0}]),
                1,
                "已入库",
                -21.0,
                -21.0,
                now_iso(),
            ),
        )
        conn.commit()


def _clean_active_count(chamber_id: int) -> int:
    """在批且探头校准未过期的正式批数（对账应呈现的口径）。"""
    with connect() as conn:
        return conn.execute(
            """SELECT COUNT(*) c FROM temp_batches b
               JOIN probes p ON p.id=b.probe_id
               WHERE b.chamber_id=? AND b.status NOT IN ('已封批','作废')
                 AND p.calibration_due >= date('now')""",
            (chamber_id,),
        ).fetchone()["c"]


def test_reconcile_flags_expired_official_batch():
    _plant_expired_official_batch()
    c = TestClient(app)
    h = _auth(c)
    recon = c.get("/api/reconcile/CK-HB-02", headers=h).json()
    # 对账仍把该批计入在批绿灯即假绿：须单列污染数
    assert (
        recon.get("expired_official_batches", 0) >= 1
    ), "对账须单列过期探头正式批（expired_official_batches>=1）"
    # 在批数不得计入该污染批
    chamber_id = recon["chamber_id"]
    assert (
        recon["active_batches"] == _clean_active_count(chamber_id)
    ), "过期探头正式批不得计入在批数"
    # 有污染即失衡，不得假绿
    assert recon["balanced"] is False, "存在过期正式批时仓间对账必须判失衡"


def test_reconcile_all_summary_not_false_green():
    _plant_expired_official_batch()
    c = TestClient(app)
    h = _auth(c)
    recon_all = c.get("/api/reconcile", headers=h).json()
    row = next(i for i in recon_all["items"] if i["chamber_code"] == "CK-HB-02")
    assert row.get("expired_official_batches", 0) >= 1
    assert row["balanced"] is False
    assert (
        recon_all["summary"]["unbalanced"] >= 1
    ), "全仓对账汇总须把污染仓间计入失衡，不得假绿"
