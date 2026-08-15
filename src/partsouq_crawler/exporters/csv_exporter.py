from __future__ import annotations

import csv
from collections.abc import Iterable, Mapping
from pathlib import Path


def spreadsheet_safe(value: object) -> object:
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return f"'{value}"
    return value


def write_csv(path: Path, rows: Iterable[Mapping[str, object]]) -> int:
    materialized = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not materialized:
        path.write_text("", encoding="utf-8")
        return 0
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(materialized[0].keys()))
        writer.writeheader()
        for row in materialized:
            writer.writerow({key: spreadsheet_safe(value) for key, value in row.items()})
    return len(materialized)
