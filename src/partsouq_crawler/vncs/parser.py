from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from typing import Any

RESULT_HEADERS = (
    "車輛種類",
    "車型名稱",
    "車型年份",
    "車型組代號",
    "車身碼或引擎碼",
    "期別",
    "核准日期",
    "查核碼",
)
# 「使用中機動車輛噪音查詢」格線（Infragistics wdgMain）的實際欄位順序，
# 2026-08-23 於真站驗證。瀏覽器路線以 DOM 列提取後交給 parse_grid_records。
GRID_RESULT_HEADERS = (
    "車輛種類",
    "車型名稱",
    "車型年份",
    "受測轉速(rpm)",
    "使用中原地噪音管制值",
    "車型組代號",
    "車身碼或引擎碼",
    "噪音測值原地dB(A)",
    "噪音測值加速dB(A)",
    "最大馬力轉速(rpm)",
    "核准日期",
    "查核碼",
    "期別",
    "原地檢測模式",
)
# 官方頁面同時提供汽油車/柴油車/機車；本模組依政策只抓前兩類。
VEHICLE_KINDS_QUERIED = ("汽油車", "柴油車")
ALLOWED_VEHICLE_KINDS = frozenset(VEHICLE_KINDS_QUERIED)
FORM_CONTROL_NAMES = ("dlFtrMOBTYPE", "dlFtrPERIOD", "dlFtrTESTTYPE")
REQUIRED_HIDDEN_FIELDS = ("__VIEWSTATE", "__EVENTVALIDATION")
MODEL_YEAR_MIN = 1900
MODEL_YEAR_MAX = 2100

_VIN_RE = re.compile(r"[A-HJ-NPR-Z0-9]{17}")
_CODE_RE = re.compile(r"[A-Za-z0-9-]+")
_WHITESPACE_RE = re.compile(r"\s+")
_FORM_CONTROL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(rf"name=[\"']{name}[\"']", re.IGNORECASE)) for name in FORM_CONTROL_NAMES
)

_MULTI_TOKEN_BRANDS = {
    "LAND ROVER",
    "ALFA ROMEO",
    "ASTON MARTIN",
    "MERCEDES BENZ",
}
_BRANDS = frozenset(
    {
        "AUDI",
        "BENTLEY",
        "BMW",
        "BUICK",
        "CADILLAC",
        "CHERY",
        "CHEVROLET",
        "CHRYSLER",
        "CITROEN",
        "CMC",
        "DACIA",
        "DAIHATSU",
        "DATSUN",
        "DS",
        "FERRARI",
        "FIAT",
        "FORD",
        "GENESIS",
        "GMC",
        "HONDA",
        "HYUNDAI",
        "INFINITI",
        "ISUZU",
        "JAGUAR",
        "JEEP",
        "KIA",
        "LAMBORGHINI",
        "LANCIA",
        "LEXUS",
        "LINCOLN",
        "LUXGEN",
        "MASERATI",
        "MAZDA",
        "MCLAREN",
        "MERCEDES-BENZ",
        "MG",
        "MINI",
        "MITSUBISHI",
        "NISSAN",
        "OPEL",
        "PEUGEOT",
        "PORSCHE",
        "RENAULT",
        "ROLLS-ROYCE",
        "SAAB",
        "SEAT",
        "SKODA",
        "SMART",
        "SSANGYONG",
        "SUBARU",
        "SUZUKI",
        "TESLA",
        "TOYOTA",
        "VOLKSWAGEN",
        "VOLVO",
    }
)
_BRAND_ALIASES = {
    "BENZ": "MERCEDES-BENZ",
    "MERCEDES": "MERCEDES-BENZ",
    "MERCEDES BENZ": "MERCEDES-BENZ",
}
# 順序有意義：自動手排 必須先於 自排/手排 比對。
_TRANSMISSION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile("自動手排"), "AMT"),
    (re.compile(r"\bCVT\b", re.IGNORECASE), "CVT"),
    (re.compile(r"\bDCT\b", re.IGNORECASE), "DCT"),
    (re.compile(r"\bAMT\b", re.IGNORECASE), "AMT"),
    (re.compile(r"(?<![A-Za-z])AT(?![A-Za-z])", re.IGNORECASE), "AT"),
    (re.compile(r"(?<![A-Za-z])MT(?![A-Za-z])", re.IGNORECASE), "MT"),
    (re.compile("自排"), "AT"),
    (re.compile("手排"), "MT"),
    (re.compile("無段變速|無段"), "CVT"),
)
_STYLE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"TURBO|渦輪", re.IGNORECASE), "Turbo"),
    (re.compile(r"HYBRID|油電", re.IGNORECASE), "Hybrid"),
    (re.compile(r"\bEV\b|純電", re.IGNORECASE), "EV"),
)
_BODY_RULE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"SUV|休旅", re.IGNORECASE), "SUV"),
    (re.compile(r"\bMPV\b", re.IGNORECASE), "MPV"),
    (re.compile(r"\bVAN\b|廂型|箱型", re.IGNORECASE), "VAN"),
    (re.compile(r"WAGON|旅行車", re.IGNORECASE), "WAGON"),
    (re.compile(r"COUPE|轎跑", re.IGNORECASE), "COUPE"),
    (re.compile(r"PICKUP|皮卡", re.IGNORECASE), "PICKUP"),
    (re.compile(r"TRUCK|貨車", re.IGNORECASE), "TRUCK"),
    (re.compile(r"\bBUS\b|客車|巴士", re.IGNORECASE), "BUS"),
)
_CC_WITH_UNIT_TOKEN_RE = re.compile(r"^([1-9]\d{3})(?:C\.?C\.?|㏄)$", re.IGNORECASE)
_CC_BARE_TOKEN_RE = re.compile(r"^([1-9]\d{2,3})$")
_CC_MIN, _CC_MAX = 600, 7000
_DOOR_TOKEN_RE = re.compile(r"^([2-7])(?:[-~])?D$", re.IGNORECASE)


class VncsParserError(RuntimeError):
    """VNCS HTML 與預期合約不符（缺 hidden fields／表格改版）。"""


def is_vin_code(code: str) -> bool:
    """17 碼且不含 I/O/Q 才視為 VIN；其餘（引擎號碼）一律回 False。"""
    return bool(_VIN_RE.fullmatch(code))


def parse_vehicle_name(raw: str) -> dict[str, Any]:
    """把「車型名稱」拆成七個結構欄位（啟發式、純函式）。

    - brand：前置品牌詞（支援 LAND ROVER 等雙詞與 BENZ→MERCEDES-BENZ 別名）；
      未知名稱仍取第一個詞，不丟資料。
    - displacement_cc：獨立 3-4 位數字（可帶 CC/㏄ 後綴），合理範圍 600-7000。
    - doors：「4D」「5-D」等單一詞符。
    - transmission：CVT/DCT/AMT/AT/MT/自排/手排/自動手排/無段。
    - style：Turbo/Hybrid/EV 標記（含 渦輪/油電/純電）。
    - body_rule：SUV/VAN/WAGON/COUPE/PICKUP/TRUCK/BUS 關鍵字（掃描全文）。
    - model_raw：扣除已辨識詞符後的剩餘字串（保留 ALTIS/S5/TDI 等原始資訊）。
    """
    text = _WHITESPACE_RE.sub(" ", raw).strip()
    tokens = text.split(" ") if text else []
    brand_token_count, brand = _match_brand(tokens)
    displacement_cc: int | None = None
    doors: int | None = None
    transmission: str | None = None
    styles: list[str] = []
    model_tokens: list[str] = []
    for token in tokens[brand_token_count:]:
        door_match = _DOOR_TOKEN_RE.fullmatch(token)
        if door_match is not None:
            doors = int(door_match.group(1))
            continue
        cc_with_unit = _CC_WITH_UNIT_TOKEN_RE.fullmatch(token)
        if cc_with_unit is not None:
            if displacement_cc is None:
                displacement_cc = int(cc_with_unit.group(1))
            continue
        cc_bare = _CC_BARE_TOKEN_RE.fullmatch(token)
        if cc_bare is not None and displacement_cc is None:
            candidate = int(cc_bare.group(1))
            if _CC_MIN <= candidate <= _CC_MAX:
                displacement_cc = candidate
                continue
        upper_token = token.upper()
        style_label = next(
            (label for pattern, label in _STYLE_PATTERNS if pattern.search(upper_token)), None
        )
        if style_label is not None:
            styles.append(style_label)
            continue
        transmission_label = next(
            (label for pattern, label in _TRANSMISSION_PATTERNS if pattern.search(token)), None
        )
        if transmission_label is not None:
            if transmission is None:
                transmission = transmission_label
            continue
        model_tokens.append(token)
    body_rule = next(
        (label for pattern, label in _BODY_RULE_PATTERNS if pattern.search(text.upper())), None
    )
    return {
        "brand": brand,
        "model_raw": " ".join(model_tokens),
        "displacement_cc": displacement_cc,
        "body_rule": body_rule,
        "transmission": transmission,
        "doors": doors,
        "style": " ".join(styles) if styles else None,
    }


def record_payload(record: dict[str, object]) -> str:
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _match_brand(tokens: list[str]) -> tuple[int, str]:
    if not tokens:
        return 0, ""
    if len(tokens) >= 2:
        pair = f"{tokens[0]} {tokens[1]}".upper()
        if pair in _MULTI_TOKEN_BRANDS:
            return 2, _BRAND_ALIASES.get(pair, pair)
    first = tokens[0].upper()
    if first in _BRANDS or first in _BRAND_ALIASES:
        return 1, _BRAND_ALIASES.get(first, first)
    # 未知品牌仍取第一個詞，不丟資料。
    return 1, tokens[0]


def parse_hidden_fields(html_bytes: bytes) -> dict[str, str]:
    """動態解析 ASP.NET WebForms 的 hidden inputs（__VIEWSTATE 等）。

    hidden fields 以動態解析為主；dlFtr* 實名只在 assert_form_contract
    當斷言，避免改版即壞。
    """
    scraper = _HiddenInputScraper()
    scraper.feed(html_bytes.decode("utf-8", errors="replace"))
    scraper.close()
    for required in REQUIRED_HIDDEN_FIELDS:
        if not scraper.hidden.get(required):
            raise VncsParserError(f"VNCS form is missing the {required} hidden field")
    return dict(scraper.hidden)


def assert_form_contract(html_bytes: bytes) -> None:
    """斷言 live 驗證過的下拉選單實名仍在頁面上（防改版靜默失效）。"""
    text = html_bytes.decode("utf-8", errors="replace")
    missing = [name for name, pattern in _FORM_CONTROL_PATTERNS if pattern.search(text) is None]
    if missing:
        raise VncsParserError(f"VNCS page is missing expected form controls: {missing}")


def parse_vehicles(html_bytes: bytes) -> tuple[list[dict[str, object]], int]:
    """解析 VNCS 結果表格。

    回傳 ``(records, malformed_rows)``：records 為結構化列（機車等政策外
    車輛種類直接略過、不計 malformed）；malformed_rows 是缺少必要欄位或
    數值不合理的列數，供 service 做 fail-closed 品質關卡。找不到符合
    RESULT_HEADERS 的表頭時直接 raise——視為改版，不得靜默吞掉。
    """
    tables = _parse_tables(html_bytes)
    expected_headers = list(RESULT_HEADERS)
    matching_tables = [
        table for table in tables if table and _normalize_cells(table[0]) == expected_headers
    ]
    if not matching_tables:
        raise VncsParserError("VNCS result table with the expected columns was not found")
    records: list[dict[str, object]] = []
    malformed = 0
    for table in matching_tables:
        for raw_row in table[1:]:
            cells = _normalize_cells(raw_row)
            if not any(cells):
                continue
            padded = cells[: len(RESULT_HEADERS)]
            padded += [""] * (len(RESULT_HEADERS) - len(padded))
            row = dict(zip(RESULT_HEADERS, padded, strict=True))
            kind = row["車輛種類"]
            if kind not in ALLOWED_VEHICLE_KINDS:
                continue
            if _row_is_malformed(row):
                malformed += 1
                continue
            name_parts = parse_vehicle_name(row["車型名稱"])
            code = row["車身碼或引擎碼"].upper()
            records.append(
                {
                    "vehicle_kind": kind,
                    "make": str(name_parts["brand"]),
                    "model_name": row["車型名稱"],
                    **name_parts,
                    "model_year": int(row["車型年份"]),
                    "model_group_code": row["車型組代號"],
                    "body_or_engine_code": code,
                    "is_vin": is_vin_code(code),
                    "period": row["期別"] or None,
                    "approval_date": row["核准日期"] or None,
                    "check_code": row["查核碼"] or None,
                }
            )
    return records, malformed


def parse_grid_records(rows: list[dict[str, str]]) -> tuple[list[dict[str, object]], int]:
    """把瀏覽器 DOM 提取的格線列（欄位名 → 值）轉成結構化 records。

    wdgMain 的車型名稱在顯示層被伺服器截斷（…），完整名稱放在 span title；
    由 browser.py 的 JS 先以 title 覆寫再回傳，本函式只做純映射與驗證。
    回傳 ``(records, malformed_rows)``，語意與 parse_vehicles 相同：
    政策外車輛種類略過、缺必要欄位計 malformed、找不到必要欄位即 raise。
    """
    if not rows:
        return [], 0
    required = ("車輛種類", "車型名稱", "車型年份", "車型組代號", "車身碼或引擎碼")
    sample: dict[str, str] = rows[0]
    missing = [name for name in required if name not in sample]
    if missing:
        raise VncsParserError(f"VNCS grid rows are missing expected columns: {missing}")
    records: list[dict[str, object]] = []
    malformed = 0
    for row in rows:
        kind = _normalize_cell(row.get("車輛種類", ""))
        if kind not in ALLOWED_VEHICLE_KINDS:
            continue
        name = _normalize_cell(row.get("車型名稱", ""))
        year = _normalize_cell(row.get("車型年份", ""))
        code = _normalize_cell(row.get("車身碼或引擎碼", ""))
        legacy_row = {
            "車輛種類": kind,
            "車型名稱": name,
            "車型年份": year,
            "車身碼或引擎碼": code,
        }
        if _row_is_malformed(legacy_row):
            malformed += 1
            continue
        name_parts = parse_vehicle_name(name)
        records.append(
            {
                "vehicle_kind": kind,
                "make": str(name_parts["brand"]),
                "model_name": name,
                **name_parts,
                "model_year": int(year),
                "model_group_code": _normalize_cell(row.get("車型組代號", "")),
                "body_or_engine_code": code.upper(),
                "is_vin": is_vin_code(code.upper()),
                "period": _optional_grid_cell(row.get("期別")),
                "approval_date": _optional_grid_cell(row.get("核准日期")),
                "check_code": _optional_grid_cell(row.get("查核碼")),
                "tested_rpm": _optional_grid_cell(row.get("受測轉速(rpm)")),
                "noise_limit": _optional_grid_cell(row.get("使用中原地噪音管制值")),
                "stationary_noise_db": _optional_grid_cell(row.get("噪音測值原地dB(A)")),
                "acceleration_noise_db": _optional_grid_cell(row.get("噪音測值加速dB(A)")),
                "max_power_rpm": _optional_grid_cell(row.get("最大馬力轉速(rpm)")),
                "detection_mode": _optional_grid_cell(row.get("原地檢測模式")),
            }
        )
    return records, malformed


def _normalize_cell(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", value).strip()


def _optional_grid_cell(value: str | None) -> str | None:
    text = _normalize_cell(value or "")
    return text or None


def _row_is_malformed(row: dict[str, str]) -> bool:
    code = row["車身碼或引擎碼"]
    year = row["車型年份"]
    return bool(
        not row["車型名稱"]
        or not code
        or not _CODE_RE.fullmatch(code)
        or len(code) > 32
        or not year.isdigit()
        or not MODEL_YEAR_MIN <= int(year) <= MODEL_YEAR_MAX
    )


def _normalize_cells(cells: list[str] | tuple[str, ...]) -> list[str]:
    return [_WHITESPACE_RE.sub(" ", cell).strip() for cell in cells]


class _HiddenInputScraper(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "input":
            return
        values = {key.lower(): value for key, value in attrs}
        input_type = (values.get("type") or "").lower()
        name = values.get("name")
        if input_type == "hidden" and isinstance(name, str) and name:
            self.hidden[name] = values.get("value") or ""


class _TableScraper(HTMLParser):
    """收集頁面上所有 <table> 的列內容（巢狀表時文字歸最內層）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._stack: list[_TableContext] = []

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if lowered == "table":
            self._stack.append(_TableContext())
            return
        if not self._stack:
            return
        context = self._stack[-1]
        if lowered == "tr":
            context.current_row = []
            context.row_open = True
        elif lowered in ("td", "th") and context.row_open and context.current_row is not None:
            context.cell_buffer = []

    def handle_data(self, data: str) -> None:
        if self._stack and self._stack[-1].cell_buffer is not None:
            self._stack[-1].cell_buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered == "table" and self._stack:
            finished = self._stack.pop()
            if finished.rows:
                self.tables.append(finished.rows)
            return
        if not self._stack:
            return
        context = self._stack[-1]
        if lowered in ("td", "th") and context.cell_buffer is not None:
            text = _WHITESPACE_RE.sub(" ", "".join(context.cell_buffer)).strip()
            if context.current_row is not None:
                context.current_row.append(text)
            context.cell_buffer = None
        elif lowered == "tr" and context.row_open:
            if context.current_row:
                context.rows.append(context.current_row)
            context.current_row = None
            context.row_open = False


class _TableContext:
    __slots__ = ("cell_buffer", "current_row", "row_open", "rows")

    def __init__(self) -> None:
        self.rows: list[list[str]] = []
        self.current_row: list[str] | None = None
        self.cell_buffer: list[str] | None = None
        self.row_open = False


def _parse_tables(html_bytes: bytes) -> list[list[list[str]]]:
    scraper = _TableScraper()
    scraper.feed(html_bytes.decode("utf-8", errors="replace"))
    scraper.close()
    return scraper.tables
