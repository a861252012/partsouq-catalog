from __future__ import annotations

import pytest

from partsouq_crawler.vncs.parser import (
    RESULT_HEADERS,
    VncsParserError,
    assert_form_contract,
    is_vin_code,
    parse_grid_records,
    parse_hidden_fields,
    parse_vehicle_name,
    parse_vehicles,
)

VIN = "KNAPX81BDV7443274"

INITIAL_PAGE_HTML = """<!DOCTYPE html>
<html><head><title>VNCS</title></head><body>
<form id="form1" action="VNCSEXLRPT.aspx" method="post">
<input type="hidden" name="__VIEWSTATE" value="/wEPDwULLTE1234567890==" />
<input type="hidden" name="__VIEWSTATEGENERATOR" value="C2EE9ABB" />
<input type="hidden" name="__EVENTVALIDATION" value="/wEdAAEeVvAlKx21vA==" />
<select name="dlFtrMOBTYPE"><option>汽油車</option><option>柴油車</option>
<option>機車</option></select>
<select name="dlFtrPERIOD"><option>第一期</option><option>第六期</option></select>
<select name="dlFtrTESTTYPE"><option>新車型</option><option>逐車</option>
<option>沿用</option></select>
<input type="submit" name="btnQuery" value="查詢" />
</form></body></html>"""

RESULT_TABLE_HTML = f"""<!DOCTYPE html>
<html><body>
<div class="other-table"><table><tr><td>干擾用的表格</td></tr></table></div>
<table id="resultGrid">
<tr><th>{"</th><th>".join(RESULT_HEADERS)}</th></tr>
<tr><td>汽油車</td><td>TOYOTA COROLLA ALTIS 1800 4D 自排</td>
<td>2024</td><td>T2-A24</td><td>{VIN}</td><td>六期</td>
<td>113/05/22</td><td>A1</td></tr>
<tr><td>柴油車</td><td>CMC VERYCA 1200 2D 手排</td>
<td>2023</td><td>C5-D23</td><td>12345678</td><td>六期</td>
<td>112/11/03</td><td>B2</td></tr>
<tr><td>機車</td><td>KYMCO MANY 110</td><td>2024</td><td>M7-X24</td>
<td>LC0TCKS10X0000001</td><td>七期</td><td>113/01/15</td><td>C3</td></tr>
<tr><td>汽油車</td><td>HONDA FIT 1500 5D CVT</td>
<td>2025</td><td>H3-F25</td><td></td><td>七期</td><td></td><td></td></tr>
<tr><td>汽油車</td><td></td><td>2025</td><td>X1-Z25</td>
<td>JHMAP123456700001</td><td>七期</td><td>114/02/07</td><td>D4</td></tr>
</table>
</body></html>"""


def _row(**overrides: str) -> str:
    cells = {
        "車輛種類": "汽油車",
        "車型名稱": "TOYOTA COROLLA ALTIS 1800 4D 自排",
        "車型年份": "2024",
        "車型組代號": "T2-A24",
        "車身碼或引擎碼": VIN,
        "期別": "六期",
        "核准日期": "113/05/22",
        "查核碼": "A1",
    }
    cells.update(overrides)
    head = "</th><th>".join(RESULT_HEADERS)
    body = "</td><td>".join(cells[name] for name in RESULT_HEADERS)
    return f"<table><tr><th>{head}</th></tr><tr><td>{body}</td></tr></table>"


def test_parse_hidden_fields_extracts_all_aspnet_state_dynamically() -> None:
    fields = parse_hidden_fields(INITIAL_PAGE_HTML.encode())

    assert fields["__VIEWSTATE"] == "/wEPDwULLTE1234567890=="
    assert fields["__VIEWSTATEGENERATOR"] == "C2EE9ABB"
    assert fields["__EVENTVALIDATION"] == "/wEdAAEeVvAlKx21vA=="


@pytest.mark.parametrize(
    "missing_field",
    ("__VIEWSTATE", "__EVENTVALIDATION"),
)
def test_parse_hidden_fields_fails_closed_without_required_state(missing_field: str) -> None:
    marker = f'name="{missing_field}"'
    broken_html = INITIAL_PAGE_HTML.replace(marker, 'name="__OTHER_STATE"')

    with pytest.raises(VncsParserError, match=missing_field):
        parse_hidden_fields(broken_html.encode())


def test_assert_form_contract_accepts_live_control_names() -> None:
    assert_form_contract(INITIAL_PAGE_HTML.encode())


@pytest.mark.parametrize(
    "removed_control",
    ("dlFtrMOBTYPE", "dlFtrPERIOD", "dlFtrTESTTYPE"),
)
def test_assert_form_contract_rejects_missing_controls(removed_control: str) -> None:
    broken_html = INITIAL_PAGE_HTML.replace(f'name="{removed_control}"', 'name="dlRenamed"')

    with pytest.raises(VncsParserError, match=removed_control):
        assert_form_contract(broken_html.encode())


def test_parse_vehicles_extracts_rows_and_excludes_motorcycles() -> None:
    records, malformed = parse_vehicles(RESULT_TABLE_HTML.encode())

    kinds = [str(record["vehicle_kind"]) for record in records]
    assert kinds == ["汽油車", "柴油車"]
    assert malformed == 2
    first = records[0]
    assert first["make"] == "TOYOTA"
    assert first["model_raw"] == "COROLLA ALTIS"
    assert first["displacement_cc"] == 1800
    assert first["transmission"] == "AT"
    assert first["doors"] == 4
    assert first["model_year"] == 2024
    assert first["model_group_code"] == "T2-A24"
    assert first["body_or_engine_code"] == VIN
    assert first["is_vin"] is True
    assert first["period"] == "六期"
    assert first["approval_date"] == "113/05/22"
    assert first["check_code"] == "A1"
    second = records[1]
    assert second["make"] == "CMC"
    assert second["body_or_engine_code"] == "12345678"
    assert second["is_vin"] is False
    assert second["transmission"] == "MT"
    assert second["doors"] == 2


def test_parse_vehicles_raises_when_expected_table_is_absent() -> None:
    html = b"<html><body><table><tr><td>no result table here</td></tr></table></body></html>"

    with pytest.raises(VncsParserError, match="result table"):
        parse_vehicles(html)


def test_parse_vehicles_handles_nested_tables_and_blank_rows() -> None:
    inner_row = _row(車型名稱="LUXGEN S5 1800 4D Turbo", 車身碼或引擎碼="LZZTEST00X0000999")
    wrapped_html = (
        "<html><body><table><tr><td>"
        "<table><tr><td>inner noise</td></tr></table>"
        "</td></tr></table>" + inner_row + "</body></html>"
    )

    records, malformed = parse_vehicles(wrapped_html.encode())

    assert malformed == 0
    assert [str(record["model_raw"]) for record in records] == ["S5"]
    assert records[0]["style"] == "Turbo"


def test_is_vin_code_rejects_non_vin_engine_codes() -> None:
    assert is_vin_code(VIN)
    assert is_vin_code("zzztest00x0000999".upper())
    assert not is_vin_code("12345678")
    assert not is_vin_code("KNAPX81BDV74432I")  # 含 I，非合法 VIN 字元集
    assert not is_vin_code(VIN[:-1])


@pytest.mark.parametrize(
    ("raw_name", "expected"),
    (
        (
            "TOYOTA COROLLA ALTIS 1800 4D 自排",
            {
                "brand": "TOYOTA",
                "model_raw": "COROLLA ALTIS",
                "displacement_cc": 1800,
                "body_rule": None,
                "transmission": "AT",
                "doors": 4,
                "style": None,
            },
        ),
        (
            "HONDA FIT 1500 5D CVT",
            {
                "brand": "HONDA",
                "model_raw": "FIT",
                "displacement_cc": 1500,
                "body_rule": None,
                "transmission": "CVT",
                "doors": 5,
                "style": None,
            },
        ),
        (
            "CMC VERYCA 1200 2D 手排",
            {
                "brand": "CMC",
                "model_raw": "VERYCA",
                "displacement_cc": 1200,
                "body_rule": None,
                "transmission": "MT",
                "doors": 2,
                "style": None,
            },
        ),
        (
            "LUXGEN S5 1800 4D Turbo",
            {
                "brand": "LUXGEN",
                "model_raw": "S5",
                "displacement_cc": 1800,
                "body_rule": None,
                "transmission": None,
                "doors": 4,
                "style": "Turbo",
            },
        ),
        (
            "HONDA CR-V 1500CC 5D CVT",
            {
                "brand": "HONDA",
                "model_raw": "CR-V",
                "displacement_cc": 1500,
                "body_rule": None,
                "transmission": "CVT",
                "doors": 5,
                "style": None,
            },
        ),
        (
            "MITSUBISHI OUTLANDER 2400 5D 油電",
            {
                "brand": "MITSUBISHI",
                "model_raw": "OUTLANDER",
                "displacement_cc": 2400,
                "body_rule": None,
                "transmission": None,
                "doors": 5,
                "style": "Hybrid",
            },
        ),
        (
            "TOYOTA CAMRY HYBRID 2500 4D 自排",
            {
                "brand": "TOYOTA",
                "model_raw": "CAMRY",
                "displacement_cc": 2500,
                "body_rule": None,
                "transmission": "AT",
                "doors": 4,
                "style": "Hybrid",
            },
        ),
        (
            "LAND ROVER DISCOVERY 3000 5D 自排",
            {
                "brand": "LAND ROVER",
                "model_raw": "DISCOVERY",
                "displacement_cc": 3000,
                "body_rule": None,
                "transmission": "AT",
                "doors": 5,
                "style": None,
            },
        ),
        (
            "BENZ C200 2000 4D 自排",
            {
                "brand": "MERCEDES-BENZ",
                "model_raw": "C200",
                "displacement_cc": 2000,
                "body_rule": None,
                "transmission": "AT",
                "doors": 4,
                "style": None,
            },
        ),
        (
            "BMW 318I 2000 4D 自排",
            {
                "brand": "BMW",
                "model_raw": "318I",
                "displacement_cc": 2000,
                "body_rule": None,
                "transmission": "AT",
                "doors": 4,
                "style": None,
            },
        ),
        (
            "NISSAN TIIDA 1800 5D",
            {
                "brand": "NISSAN",
                "model_raw": "TIIDA",
                "displacement_cc": 1800,
                "body_rule": None,
                "transmission": None,
                "doors": 5,
                "style": None,
            },
        ),
        (
            "MAZDA 3 2000 5D 自排",
            {
                "brand": "MAZDA",
                "model_raw": "3",
                "displacement_cc": 2000,
                "body_rule": None,
                "transmission": "AT",
                "doors": 5,
                "style": None,
            },
        ),
        (
            "TESLA MODEL 3 純電",
            {
                "brand": "TESLA",
                "model_raw": "MODEL 3",
                "displacement_cc": None,
                "body_rule": None,
                "transmission": None,
                "doors": None,
                "style": "EV",
            },
        ),
        (
            "FORD FOCUS 2000 5D 渦輪柴油",
            {
                "brand": "FORD",
                "model_raw": "FOCUS",
                "displacement_cc": 2000,
                "body_rule": None,
                "transmission": None,
                "doors": 5,
                "style": "Turbo",
            },
        ),
        (
            "HYUNDAI ELANTRA 1600 4D 自排",
            {
                "brand": "HYUNDAI",
                "model_raw": "ELANTRA",
                "displacement_cc": 1600,
                "body_rule": None,
                "transmission": "AT",
                "doors": 4,
                "style": None,
            },
        ),
        (
            "KIA CARENS 1800 5D",
            {
                "brand": "KIA",
                "model_raw": "CARENS",
                "displacement_cc": 1800,
                "body_rule": None,
                "transmission": None,
                "doors": 5,
                "style": None,
            },
        ),
        (
            "SUZUKI SWIFT 1400 5D 無段變速",
            {
                "brand": "SUZUKI",
                "model_raw": "SWIFT",
                "displacement_cc": 1400,
                "body_rule": None,
                "transmission": "CVT",
                "doors": 5,
                "style": None,
            },
        ),
        (
            "SUBARU FORESTER 2500 5D 自排",
            {
                "brand": "SUBARU",
                "model_raw": "FORESTER",
                "displacement_cc": 2500,
                "body_rule": None,
                "transmission": "AT",
                "doors": 5,
                "style": None,
            },
        ),
        (
            "LEXUS ES300H 2500 4D 自排",
            {
                "brand": "LEXUS",
                "model_raw": "ES300H",
                "displacement_cc": 2500,
                "body_rule": None,
                "transmission": "AT",
                "doors": 4,
                "style": None,
            },
        ),
        (
            "TOYOTA WISH 1800 5D 自排 休旅",
            {
                "brand": "TOYOTA",
                "model_raw": "WISH 休旅",
                "displacement_cc": 1800,
                "body_rule": "SUV",
                "transmission": "AT",
                "doors": 5,
                "style": None,
            },
        ),
        (
            "CMC DELICA 2500 4WD 箱型",
            {
                "brand": "CMC",
                "model_raw": "DELICA 4WD 箱型",
                "displacement_cc": 2500,
                "body_rule": "VAN",
                "transmission": None,
                "doors": None,
                "style": None,
            },
        ),
        (
            "LUXGEN U7 2200 5D Turbo 休旅",
            {
                "brand": "LUXGEN",
                "model_raw": "U7 休旅",
                "displacement_cc": 2200,
                "body_rule": "SUV",
                "transmission": None,
                "doors": 5,
                "style": "Turbo",
            },
        ),
        (
            "NISSAN SUPER SENTRA 1800 4D 自動手排",
            {
                "brand": "NISSAN",
                "model_raw": "SUPER SENTRA",
                "displacement_cc": 1800,
                "body_rule": None,
                "transmission": "AMT",
                "doors": 4,
                "style": None,
            },
        ),
        (
            "VOLKSWAGEN GOLF 1800 5-D TDI",
            {
                "brand": "VOLKSWAGEN",
                "model_raw": "GOLF TDI",
                "displacement_cc": 1800,
                "body_rule": None,
                "transmission": None,
                "doors": 5,
                "style": None,
            },
        ),
    ),
)
def test_parse_vehicle_name_heuristics(raw_name: str, expected: dict[str, object]) -> None:
    assert parse_vehicle_name(raw_name) == expected


def test_parse_vehicle_name_keeps_unknown_brand_information() -> None:
    parsed = parse_vehicle_name("ACME ROADSTER 900 2D 手排")

    assert parsed["brand"] == "ACME"
    assert parsed["model_raw"] == "ROADSTER"


# ---------------------------------------------------------------------------
# 使用中噪音格線（wdgMain）DOM 列 → records 的映射（瀏覽器路線）
# ---------------------------------------------------------------------------


def _grid_row(**overrides: str) -> dict[str, str]:
    row = {
        "車輛種類": "汽油車",
        "車型名稱": "KIA SPORTAGE PE A1 2WD 1598c.c. A8 5D",
        "車型年份": "2027",
        "受測轉速(rpm)": "3750",
        "使用中原地噪音管制值": "93",
        "車型組代號": "C7G124-A02",
        "車身碼或引擎碼": VIN,
        "噪音測值原地dB(A)": "73",
        "噪音測值加速dB(A)": "68",
        "最大馬力轉速(rpm)": "5500",
        "核准日期": "2026/04/14",
        "查核碼": "",
        "期別": "六期",
        "原地檢測模式": "",
    }
    row.update(overrides)
    return row


def test_parse_grid_records_maps_in_use_noise_columns() -> None:
    records, malformed = parse_grid_records([_grid_row()])

    assert malformed == 0
    assert len(records) == 1
    record = records[0]
    assert record["vehicle_kind"] == "汽油車"
    assert record["make"] == "KIA"
    assert record["model_raw"] == "SPORTAGE PE A1 2WD A8"
    assert record["model_year"] == 2027
    assert record["model_group_code"] == "C7G124-A02"
    assert record["body_or_engine_code"] == VIN
    assert record["is_vin"] is True
    assert record["period"] == "六期"
    assert record["approval_date"] == "2026/04/14"
    assert record["check_code"] is None
    assert record["tested_rpm"] == "3750"
    assert record["noise_limit"] == "93"
    assert record["stationary_noise_db"] == "73"
    assert record["acceleration_noise_db"] == "68"
    assert record["max_power_rpm"] == "5500"
    assert record["detection_mode"] is None


def test_parse_grid_records_flags_engine_codes_and_skips_motorcycles() -> None:
    rows = [
        _grid_row(車身碼或引擎碼="R1152PH00142", 查核碼="R1152PH00142"),
        _grid_row(車輛種類="機車", 車型名稱="KYMCO MANY 110", 車身碼或引擎碼="LC0TCKS10X0000001"),
    ]

    records, malformed = parse_grid_records(rows)

    assert malformed == 0
    assert len(records) == 1
    assert records[0]["is_vin"] is False
    assert records[0]["check_code"] == "R1152PH00142"


def test_parse_grid_records_counts_malformed_rows() -> None:
    rows = [
        _grid_row(),
        _grid_row(車型名稱=""),
        _grid_row(車型年份="20X7"),
    ]

    records, malformed = parse_grid_records(rows)

    assert malformed == 2
    assert len(records) == 1


def test_parse_grid_records_rejects_unexpected_column_set() -> None:
    with pytest.raises(VncsParserError, match="missing expected columns"):
        parse_grid_records([{"車輛種類": "汽油車"}])


def test_parse_grid_records_accepts_empty_page() -> None:
    assert parse_grid_records([]) == ([], 0)
