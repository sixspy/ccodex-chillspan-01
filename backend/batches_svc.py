"""温控批次入库：温度点序列 JSON。"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from locks import (
    acquire_lock_committed,
    release,
    release_lock_committed,
    require_lock,
)
from models import BatchIn
from validators import (
    calibration_expired,
    require_calibration_intake,
    summarize_points,
    validate_probe_online,
    validate_temp_point,
)

router = APIRouter(tags=["batches"])


def _next_batch_code(conn, chamber_id: int) -> str:
    ch = conn.execute(
        "SELECT chamber_code FROM cold_chambers WHERE id=?", (chamber_id,)
    ).fetchone()
    prefix = ch["chamber_code"] if ch else f"CH-{chamber_id}"
    n = conn.execute(
        "SELECT COUNT(*) c FROM temp_batches WHERE chamber_id=?",
        (chamber_id,),
    ).fetchone()["c"]
    return f"TB-{prefix}-{n + 1:03d}"


@router.get("/api/batches")
def list_batches(authorization: AuthHeader = None, status: str | None = None):
    require_user(authorization)
    with connect() as conn:
        base = """SELECT b.*, c.chamber_code, p.probe_code,
                     p.calibration_due AS probe_calibration_due,
                     (p.calibration_due < date('now')) AS probe_calibration_expired
              FROM temp_batches b
              JOIN cold_chambers c ON c.id=b.chamber_id
              JOIN probes p ON p.id=b.probe_id"""
        if status:
            rows = conn.execute(
                base + " WHERE b.status=? ORDER BY b.id DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute(base + " ORDER BY b.id DESC").fetchall()
        items = []
        for r in rows:
            d = dict(r)
            d["temp_points"] = json.loads(d["temp_points"] or "[]")
            d["probe_calibration_expired"] = bool(d["probe_calibration_expired"])
            items.append(d)
        return {"items": items}


@router.get("/api/batches/{batch_id}")
def get_batch(batch_id: int, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        row = conn.execute(
            """SELECT b.*, c.name chamber_name, c.target_min, c.target_max, p.probe_code,
                      p.calibration_due AS probe_calibration_due,
                      (p.calibration_due < date('now')) AS probe_calibration_expired
               FROM temp_batches b
               JOIN cold_chambers c ON c.id=b.chamber_id
               JOIN probes p ON p.id=b.probe_id WHERE b.id=?""",
            (batch_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "批次不存在")
        data = dict(row)
        data["temp_points"] = json.loads(data["temp_points"] or "[]")
        data["probe_calibration_expired"] = bool(data["probe_calibration_expired"])
        exc = conn.execute(
            "SELECT * FROM excursion_cursors WHERE batch_id=? ORDER BY id DESC",
            (batch_id,),
        ).fetchall()
        seals = conn.execute(
            "SELECT * FROM seal_snapshots WHERE batch_id=? ORDER BY id DESC",
            (batch_id,),
        ).fetchall()
        return {
            "batch": data,
            "excursions": [dict(e) for e in exc],
            "seals": [dict(s) for s in seals],
        }


@router.post("/api/batches")
def create_batch(body: BatchIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    # 争用验收段：同探头建批先占探头锁，后到只留争用痕
    probe_lock = f"probe:{body.probe_id}"
    if not acquire_lock_committed(probe_lock, user):
        raise HTTPException(409, "争用：同探头建批冲突，后到只留争用痕")
    try:
        # 命运取舍段：占锁之后才做业务校验与门闸
        with connect() as conn:
            ch = conn.execute(
                "SELECT * FROM cold_chambers WHERE id=?", (body.chamber_id,)
            ).fetchone()
            if not ch:
                raise HTTPException(404, "仓间不存在")
            probe = conn.execute(
                "SELECT * FROM probes WHERE id=?", (body.probe_id,)
            ).fetchone()
            if not probe or probe["chamber_id"] != body.chamber_id:
                raise HTTPException(422, "探头与仓间不匹配")
            validate_probe_online(probe)
            if calibration_expired(probe):
                # 偏离已封甲路径取舍：记过期入批偏航并拒写，正式表零增
                write_audit(
                    conn,
                    user,
                    "过期入批偏航",
                    probe["probe_code"],
                    {
                        "probe_id": body.probe_id,
                        "chamber_id": body.chamber_id,
                        "cargo_ref": body.cargo_ref,
                        "calibration_due": probe["calibration_due"],
                        "decision": "甲路径：正式表零增并拒写",
                    },
                )
                conn.commit()
                require_calibration_intake(probe)
            chamber_lock = f"chamber:{body.chamber_id}"
            require_lock(conn, chamber_lock, user)
            try:
                active = conn.execute(
                    """SELECT COUNT(*) c FROM temp_batches
                       WHERE chamber_id=? AND status NOT IN ('已封批','作废')""",
                    (body.chamber_id,),
                ).fetchone()["c"]
                if active >= int(ch["quota_batches"]):
                    raise HTTPException(409, "仓间在批配额已满")
                points = body.temp_points or []
                for pt in points:
                    validate_temp_point(
                        float(pt.get("c", pt.get("temp", 0))),
                        float(ch["target_min"]),
                        float(ch["target_max"]),
                    )
                tmin, tmax = summarize_points(points)
                code = body.batch_code or _next_batch_code(conn, body.chamber_id)
                if conn.execute(
                    "SELECT id FROM temp_batches WHERE batch_code=?", (code,)
                ).fetchone():
                    raise HTTPException(409, "批次编码已存在")
                status = "已入库" if points else "草稿"
                now = now_iso()
                cur = conn.execute(
                    """INSERT INTO temp_batches(batch_code,chamber_id,probe_id,cargo_ref,
                       temp_points,generation,status,min_observed,max_observed,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        code,
                        body.chamber_id,
                        body.probe_id,
                        body.cargo_ref,
                        json.dumps(points, ensure_ascii=False),
                        1,
                        status,
                        tmin if points else None,
                        tmax if points else None,
                        now,
                    ),
                )
                write_audit(
                    conn,
                    user,
                    "batch_inbound",
                    code,
                    {"batch_id": cur.lastrowid, "point_count": len(points)},
                )
                conn.commit()
                return {
                    "id": cur.lastrowid,
                    "batch_code": code,
                    "status": status,
                    "point_count": len(points),
                }
            finally:
                release(conn, chamber_lock, user)
    finally:
        release_lock_committed(probe_lock, user)