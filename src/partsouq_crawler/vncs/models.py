from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VncsRunHandle:
    """vncs_sync_runs 的極簡 run 代柄（無 lease/token；排程鎖由 scheduler 持有）。"""

    id: int


@dataclass(frozen=True, slots=True)
class ParsedRecord:
    """單一 VNCS 結果列，已套用車型名稱啟發式拆分。"""

    vehicle_kind: str
    make: str
    model_raw: str
    displacement_cc: int | None
    body_rule: str | None
    transmission: str | None
    doors: int | None
    style: str | None
    model_year: int
    model_group_code: str
    body_or_engine_code: str
    is_vin: bool
    period: str | None
    approval_date: str | None
    check_code: str | None
    source_url: str
    payload_json: str
