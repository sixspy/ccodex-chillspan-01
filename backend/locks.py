"""仓间锁与争用登记（公共并发面）。"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit

router = APIRouter(tags=["locks"])


class LockIn(BaseModel):
    lock_key: str
    action: str = "acquire"


def _log_contention(conn, lock_key: str, actor: str, holder: str) -> None:
    conn.execute(
        """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
           VALUES (?,?,?,?,?)""",
        (lock_key, actor, "争用", f"持有者={holder}", now_iso()),
    )


def try_acquire(conn, lock_key: str, holder: str) -> bool:
    now = now_iso()
    row = conn.execute(
        "SELECT holder FROM chamber_locks WHERE lock_key=?", (lock_key,)
    ).fetchone()
    if row is None:
        try:
            conn.execute(
                "INSERT INTO chamber_locks(lock_key,holder,acquired_at) VALUES (?,?,?)",
                (lock_key, holder, now),
            )
        except sqlite3.IntegrityError:
            # 并发同键抢插：后到按争用处理，只留痕
            winner = conn.execute(
                "SELECT holder FROM chamber_locks WHERE lock_key=?", (lock_key,)
            ).fetchone()
            _log_contention(conn, lock_key, holder, winner["holder"] if winner else "?")
            return False
        return True
    if row["holder"] == holder:
        return True
    _log_contention(conn, lock_key, holder, row["holder"])
    return False


def release(conn, lock_key: str, holder: str) -> None:
    conn.execute(
        "DELETE FROM chamber_locks WHERE lock_key=? AND holder=?",
        (lock_key, holder),
    )


def require_lock(conn, lock_key: str, holder: str) -> None:
    if not try_acquire(conn, lock_key, holder):
        raise HTTPException(409, "争用：后到只记争用，不写业务")


def acquire_lock_committed(lock_key: str, holder: str) -> bool:
    """独立连接占锁并立即提交：锁行对其它会话立即可见，先到先得。

    争用验收段专用（如探头锁 probe:{id}）；后到只留争用痕，不写业务。
    """
    with connect() as conn:
        ok = try_acquire(conn, lock_key, holder)
        conn.commit()
        return ok


def release_lock_committed(lock_key: str, holder: str) -> None:
    """独立连接放锁并立即提交，与 acquire_lock_committed 配对。"""
    with connect() as conn:
        release(conn, lock_key, holder)
        conn.commit()


@router.get("/api/locks")
def list_locks(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM chamber_locks ORDER BY acquired_at DESC"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/contention")
def list_contention(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM contention_log ORDER BY id DESC LIMIT 200"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/locks/acquire")
def acquire_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        ok = try_acquire(conn, body.lock_key, user)
        write_audit(conn, user, "lock_acquire", body.lock_key, {"ok": ok})
        conn.commit()
        if not ok:
            raise HTTPException(409, "争用：未能获得锁")
        return {"ok": True, "lock_key": body.lock_key, "holder": user}


@router.post("/api/locks/release")
def release_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        release(conn, body.lock_key, user)
        write_audit(conn, user, "lock_release", body.lock_key, {})
        conn.commit()
        return {"ok": True}