"""争用失败测：两路会话并发打同探头建批，须占探头锁，后到只留争用痕。

探头锁键为 probe:{id}；争用验收先于命运取舍。用校准有效的 PR-HN-C1，
排除校准门闸干扰，只验锁口径。
"""
import threading
import time

import pytest
from fastapi.testclient import TestClient

import batches_svc
import validators
from app import app
from db import init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user: str) -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": "123456"}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def test_concurrent_same_probe_intake_probe_lock(monkeypatch):
    # 拉宽占锁窗口，让两路请求必然在锁内相遇
    orig_summarize = validators.summarize_points

    def slow_summarize(points):
        time.sleep(0.6)
        return orig_summarize(points)

    monkeypatch.setattr(batches_svc, "summarize_points", slow_summarize)

    c = TestClient(app)
    h_admin = _auth(c, "admin")
    h_ops = _auth(c, "ops")
    probes = c.get("/api/probes", headers=h_admin).json()["items"]
    probe = next(p for p in probes if p["probe_code"] == "PR-HN-C1")

    results: dict[str, int] = {}

    def create(tag: str, headers: dict, code: str) -> None:
        client = TestClient(app)
        r = client.post(
            "/api/batches",
            headers=headers,
            json={
                "chamber_id": probe["chamber_id"],
                "probe_id": probe["id"],
                "cargo_ref": f"争用货载-{tag}",
                "temp_points": [{"t": "08:00", "c": 3.0}],
                "batch_code": code,
            },
        )
        results[tag] = r.status_code

    t1 = threading.Thread(target=create, args=("A", h_admin, "TB-CONTEND-A-01"))
    t2 = threading.Thread(target=create, args=("B", h_ops, "TB-CONTEND-B-01"))
    t1.start()
    time.sleep(0.1)  # 让 A 先占锁
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    # 先到先得：恰一成一拒
    assert sorted(results.values()) == [200, 409], (
        f"同探头并发建批须恰一成一 409，实得 {results}"
    )
    # 后到只留争用痕，且痕在探头锁键上
    cont = c.get("/api/contention", headers=h_admin).json()["items"]
    assert any(
        x["lock_key"] == f"probe:{probe['id']}" for x in cont
    ), "后到者须在 contention_log 留下 probe:{id} 争用痕"
    # 正式表只落一批
    batches = c.get("/api/batches", headers=h_admin).json()["items"]
    mine = [
        b for b in batches
        if b["batch_code"] in ("TB-CONTEND-A-01", "TB-CONTEND-B-01")
    ]
    assert len(mine) == 1, "争用负方不得写入正式批"
