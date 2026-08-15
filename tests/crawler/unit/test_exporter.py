from pathlib import Path

from partsouq_crawler.exporters.csv_exporter import spreadsheet_safe, write_csv


def test_spreadsheet_formula_injection_protection() -> None:
    assert spreadsheet_safe("=1+1") == "'=1+1"
    assert spreadsheet_safe("+SUM(A1)") == "'+SUM(A1)"
    assert spreadsheet_safe("-2") == "'-2"
    assert spreadsheet_safe("@cmd") == "'@cmd"
    assert spreadsheet_safe("safe") == "safe"


def test_csv_uses_utf8_bom_and_safe_values(tmp_path: Path) -> None:
    path = tmp_path / "safe.csv"
    assert write_csv(path, [{"Number": "001", "Note": "=HYPERLINK('x')"}]) == 1
    content = path.read_text(encoding="utf-8-sig")
    assert "'=HYPERLINK" in content
