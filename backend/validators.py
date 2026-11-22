"""温度范围、探头在线、封批拦截等校验（须被业务模块调用）。"""
from __future__ import annotations

from fastapi import HTTPException


def validate_temp_point(value: float, target_min: float, target_max: float) -> None:
    if value < -80 or value > 40:
        raise HTTPException(422, "温度点超出传感器量程")
    if value < target_min - 15 or value > target_max + 15:
        raise HTTPException(422, "温度点偏离仓间目标带过远")


def validate_probe_online(row) -> None:
    if row["online_status"] != "在线":
        raise HTTPException(409, "探头离线，禁止写批次")


def calibration_expired(row) -> bool:
    """探头校准是否已过期（服务端唯一判定源，五页同源）。"""
    from datetime import date

    due = row["calibration_due"]
    return bool(due) and due < date.today().isoformat()


def require_calibration_intake(row) -> None:
    """甲路径入批门闸：校准过期探头禁止入批，正式表零增（2026-11-22 封死）。

    见 docs/expired-intake-gate.md；乙路径（影子待检批次）永不启用。
    """
    if calibration_expired(row):
        raise HTTPException(409, "探头校准已过期，禁止入批（甲路径：正式表零增）")


def require_calibration_fresh(row) -> None:
    from datetime import date

    if row["calibration_due"] < date.today().isoformat():
        raise HTTPException(409, "探头校准已到期，禁止封批")


def validate_batch_sealable(batch, open_excursions: int) -> None:
    if batch["status"] not in ("已入库", "超温处理中"):
        raise HTTPException(409, "批次状态不允许封批")
    if open_excursions > 0:
        raise HTTPException(409, "存在开放超温游标，须先闭环")
    if not batch["temp_points"]:
        raise HTTPException(409, "无温度点序列，禁止封批")


def summarize_points(points: list) -> tuple[float, float]:
    vals = [float(p.get("c", p.get("temp", 0))) for p in points if p]
    if not vals:
        return 0.0, 0.0
    return min(vals), max(vals)


def detect_excursion(points: list, threshold: float) -> float | None:
    peak = None
    for p in points:
        v = float(p.get("c", p.get("temp", 0)))
        if v > threshold:
            peak = v if peak is None else max(peak, v)
    return peak