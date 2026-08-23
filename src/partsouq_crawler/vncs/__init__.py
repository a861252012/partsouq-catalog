"""台灣 MOENV VNCS（車型測試資料查詢）官方公開資料同步模組。

只抓汽油車與柴油車（排除機車），取得真實 17 碼 VIN 以餵 NHTSA
DecodeVinValues；非 17 碼的引擎號碼保留但不參與唯一約束。
"""

from __future__ import annotations

from .browser import KIND_OPTION_VALUES, VncsBrowserError, VncsBrowserHarvester
from .client import VncsClient, VncsClientError
from .config import VncsConfig
from .models import ParsedRecord, VncsRunHandle
from .parser import (
    GRID_RESULT_HEADERS,
    RESULT_HEADERS,
    VncsParserError,
    assert_form_contract,
    is_vin_code,
    parse_grid_records,
    parse_hidden_fields,
    parse_vehicle_name,
    parse_vehicles,
)
from .repository import VncsMySQLRepository
from .service import MALFORMED_RATIO_LIMIT, VncsSyncService

__all__ = [
    "GRID_RESULT_HEADERS",
    "KIND_OPTION_VALUES",
    "MALFORMED_RATIO_LIMIT",
    "RESULT_HEADERS",
    "ParsedRecord",
    "VncsBrowserError",
    "VncsBrowserHarvester",
    "VncsClient",
    "VncsClientError",
    "VncsConfig",
    "VncsMySQLRepository",
    "VncsParserError",
    "VncsRunHandle",
    "VncsSyncService",
    "assert_form_contract",
    "is_vin_code",
    "parse_grid_records",
    "parse_hidden_fields",
    "parse_vehicle_name",
    "parse_vehicles",
]
