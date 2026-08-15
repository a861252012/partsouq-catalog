from __future__ import annotations

import re
from dataclasses import dataclass

UNAMBIGUOUS_RANGES = (
    re.compile(r"^(\d{4}-\d{2})(?:-\d{2})?\s*(?:～|~|—|–|\s+-\s+)\s*(\d{4}-\d{2})(?:-\d{2})?$"),
    re.compile(r"^(\d{4}-\d{2})(?:-\d{2})?\s*(?:～|~|—|–|\s+-\s*)$"),
)
PARTSOUQ_MONTH_RANGE = re.compile(r"^(\d{2})\.(\d{4})\s*-\s*(?:(\d{2})\.(\d{4}))?$")
PARTSOUQ_MONTH_END = re.compile(r"^-\s*(\d{2})\.(\d{4})$")
PARTSOUQ_YEAR_RANGE = re.compile(r"^(\d{4})\s*-\s*(\d{4})$")
PARTSOUQ_YEAR_START = re.compile(r"^(\d{4})\s*-\s*$")
PARTSOUQ_YEAR_END = re.compile(r"^-\s*(\d{4})$")
MIN_VEHICLE_YEAR = 1886
MAX_VEHICLE_YEAR = 2100


@dataclass(frozen=True, slots=True)
class DateRange:
    start: str | None
    end: str | None
    precision: str
    confidence: float
    parser: str


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def normalize_part_number(value: str) -> str:
    return re.sub(r"[\s-]+", "", value).upper()


def _valid_year_month(value: str) -> bool:
    year, month = value.split("-")
    return MIN_VEHICLE_YEAR <= int(year) <= MAX_VEHICLE_YEAR and 1 <= int(month) <= 12


def parse_unambiguous_range(raw: str | None) -> DateRange:
    if not raw:
        return DateRange(None, None, "unknown", 0.0, "generic")
    value = clean_text(raw) or ""
    match = UNAMBIGUOUS_RANGES[0].match(value)
    if match:
        start, end = match.groups()
        if _valid_year_month(start) and _valid_year_month(end) and start <= end:
            return DateRange(start, end, "month", 1.0, "generic")
        return DateRange(None, None, "unknown", 0.0, "generic")
    match = UNAMBIGUOUS_RANGES[1].match(value)
    if match:
        start = match.group(1)
        if _valid_year_month(start):
            return DateRange(start, None, "month", 1.0, "generic")
        return DateRange(None, None, "unknown", 0.0, "generic")
    if re.fullmatch(r"\d{4}-\d{2}", value) and _valid_year_month(value):
        return DateRange(value, value, "month", 1.0, "generic")
    match = PARTSOUQ_MONTH_RANGE.match(value)
    if match:
        start_month, start_year, end_month, end_year = match.groups()
        start = f"{start_year}-{start_month}"
        end = f"{end_year}-{end_month}" if end_month and end_year else None
        if not _valid_year_month(start) or (end is not None and not _valid_year_month(end)):
            return DateRange(None, None, "unknown", 0.0, "generic")
        if end is not None and start > end:
            return DateRange(None, None, "unknown", 0.0, "generic")
        return DateRange(start, end, "month", 1.0, "partsouq")
    match = PARTSOUQ_MONTH_END.match(value)
    if match:
        end_month, end_year = match.groups()
        end = f"{end_year}-{end_month}"
        if not _valid_year_month(end):
            return DateRange(None, None, "unknown", 0.0, "generic")
        return DateRange(None, end, "month", 1.0, "partsouq")
    match = PARTSOUQ_YEAR_RANGE.match(value)
    if match:
        start_year, end_year = (int(part) for part in match.groups())
        if not (MIN_VEHICLE_YEAR <= start_year <= end_year <= MAX_VEHICLE_YEAR):
            return DateRange(None, None, "unknown", 0.0, "generic")
        return DateRange(f"{match.group(1)}-01", f"{match.group(2)}-12", "year", 1.0, "partsouq")
    match = PARTSOUQ_YEAR_START.match(value)
    if match:
        if not MIN_VEHICLE_YEAR <= int(match.group(1)) <= MAX_VEHICLE_YEAR:
            return DateRange(None, None, "unknown", 0.0, "generic")
        return DateRange(f"{match.group(1)}-01", None, "year", 1.0, "partsouq")
    match = PARTSOUQ_YEAR_END.match(value)
    if match:
        if not MIN_VEHICLE_YEAR <= int(match.group(1)) <= MAX_VEHICLE_YEAR:
            return DateRange(None, None, "unknown", 0.0, "generic")
        return DateRange(None, f"{match.group(1)}-12", "year", 1.0, "partsouq")
    if re.fullmatch(r"\d{4}", value) and MIN_VEHICLE_YEAR <= int(value) <= MAX_VEHICLE_YEAR:
        return DateRange(f"{value}-01", f"{value}-12", "year", 1.0, "partsouq")
    return DateRange(None, None, "unknown", 0.0, "generic")


def is_assembly_name(name: str | None) -> tuple[bool, str | None]:
    if name and re.search(r"\b(?:ASSY|ASSEMBLY|SUB-ASSY)\b", name, re.IGNORECASE):
        return True, "name_keyword"
    return False, None
