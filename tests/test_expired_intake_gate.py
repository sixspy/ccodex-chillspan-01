"""失败测甲：校准已过期探头建批，正式表必须零增并拒写（甲路径门闸）。

种子探头 PR-HB-B1（CK-HB-02，在线，calibration_due=2026-11-20 已过期）。
"""
import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db


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


def _expired_seed_probe(c: TestClient, h: dict) -> dict:
    probes = c.get("/api/probes", headers=h).json()["items"]
    probe = next(p for p in probes if p["probe_code"] == "PR-HB-B1")
    assert probe["online_status"] == "在线"
    assert probe["calibration_due"] < "2026-11-22"  # 种子即校准过期
    return probe


def test_expired_probe_intake_rejected_zero_growth():
    c = TestClient(app)
    h = _auth(c)
    probe = _expired_seed_probe(c, h)
    with connect() as conn:
        before = conn.execute("SELECT COUNT(*) c FROM temp_batches").fetchone()["c"]
    r = c.post(
        "/api/batches",
        headers=h,
        json={
            "chamber_id": probe["chamber_id"],
            "probe_id": probe["id"],
            "cargo_ref": "过期探头假绿货载",
            "temp_points": [{"t": "08:00", "c": -21.0}],
            "batch_code": "TB-EXPIRED-GATE-01",
        },
    )
    # 甲路径：拒写
    assert r.status_code == 409, f"过期探头入批须 409，实得 {r.status_code}: {r.text}"
    # 正式表零增
    with connect() as conn:
        after = conn.execute("SELECT COUNT(*) c FROM temp_batches").fetchone()["c"]
        leaked = conn.execute(
            "SELECT COUNT(*) c FROM temp_batches WHERE batch_code=?",
            ("TB-EXPIRED-GATE-01",),
        ).fetchone()["c"]
    assert after == before, "过期探头入批后正式表必须零增"
    assert leaked == 0, "过期探头批次不得落入正式表"
    # 偏离已封取舍须记过期入批偏航
    audit = c.get("/api/audit?limit=200", headers=h).json()["items"]
    assert any(
        a["action"] == "过期入批偏航" and a["target"] == "PR-HB-B1" for a in audit
    ), "过期入批被拒须记审计：过期入批偏航"


def test_fresh_probe_intake_still_ok():
    """门闸不误伤：校准则内探头照常入批。"""
    c = TestClient(app)
    h = _auth(c)
    probes = c.get("/api/probes", headers=h).json()["items"]
    probe = next(p for p in probes if p["probe_code"] == "PR-HN-C1")
    r = c.post(
        "/api/batches",
        headers=h,
        json={
            "chamber_id": probe["chamber_id"],
            "probe_id": probe["id"],
            "cargo_ref": "正常货载",
            "temp_points": [{"t": "08:00", "c": 3.0}],
            "batch_code": "TB-FRESH-GATE-01",
        },
    )
    assert r.status_code == 200, f"校准有效探头入批不应被拦: {r.text}"
