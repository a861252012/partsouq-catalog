from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path


def write_jsonl(path: Path, rows: Iterable[Mapping[str, object]]) -> int:
    materialized = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in materialized:
            handle.write(json.dumps(dict(row), ensure_ascii=False, default=str) + "\n")
    return len(materialized)
