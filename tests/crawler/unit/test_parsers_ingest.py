# ruff: noqa: E501

import asyncio
from pathlib import Path

from partsouq_crawler.db.repository import Repository
from partsouq_crawler.models.crawl import FetchResult
from partsouq_crawler.parsers.base import CatalogParser, ParseError
from partsouq_crawler.parsers.brands.audi import AudiBrandAdapter
from partsouq_crawler.parsers.brands.renault import RenaultBrandAdapter
from partsouq_crawler.parsers.brands.toyota import ToyotaBrandAdapter
from partsouq_crawler.services.archive_import import ArchiveCaptureInput, ArchiveImportService
from partsouq_crawler.services.export import ExportService
from partsouq_crawler.services.ingest import IngestService
from partsouq_crawler.services.reparse import ReparseService

PARTS_HTML = b"""
<html><body>
<table class="metadata">
  <tr><th>Brand</th><td>Toyota</td></tr>
  <tr><th>Name</th><td>Camry</td></tr>
  <tr><th>Model</th><td>ACV40</td></tr>
  <tr><th>Prod Period</th><td>2006-05 ~ 2011-12</td></tr>
</table>
<ol class="breadcrumb"><li>Engine</li><li>Cooling</li><li>Pump</li><li>Housing</li></ol>
<table><tr><th>Diagram Code</th><th>Diagram Name</th><th>Diagram Range</th></tr>
  <tr><td>1603</td><td>WATER PUMP</td><td>2006-05 ~ 2011-12</td></tr>
  <tr><td>1604</td><td>THERMOSTAT</td><td>2006-05 ~ 2011-12</td></tr>
</table>
<table><tr><th>Part Number</th><th>Part Name</th><th>Diagram Code</th><th>Callout</th><th>Quantity</th><th>Part Range</th><th>Condition</th><th>Note</th></tr>
  <tr><td>00123-AB</td><td>PUMP ASSY</td><td>1603</td><td>1</td><td>1</td><td>2008-01 ~ 2010-11</td><td>ACV40..ARL</td><td>A</td></tr>
  <tr><td>00123-AB</td><td>PUMP ASSY</td><td>1603</td><td>1</td><td>1</td><td>2010-12 ~ 2011-12</td><td>ACV40..ARL</td><td>B</td></tr>
</table>
</body></html>
"""

ARCHIVED_DIAGRAM_HTML = b"""
<html><body>
<ul class="breadcrumb">
  <li>Genuine Parts Catalogs</li><li>Honda</li><li>INTEGRA Europe 17ST701</li>
  <li>1. ENGINE</li>
</ul>
<table>
  <tr><th>Brand</th><th>Name</th><th>Region</th><th>Npl</th><th>Manufactured</th></tr>
  <tr><td>HONDA</td><td>INTEGRA</td><td>Europe</td><td>17ST701</td><td>1998-2000</td></tr>
</table>
<div class="panel">
  <div class="unit-header"><h2>ALTERNATOR BRACKET</h2></div>
  <table>
    <tr><th>Number</th><th>Name</th><th>Code</th><th>Qty Required</th></tr>
    <tr><td>31110P73A01</td><td>BRACKET COMP.</td><td>1</td><td>1</td></tr>
  </table>
</div>
</body></html>
"""


def test_generic_metadata_vehicle_parser() -> None:
    parsed = CatalogParser().parse("https://partsouq.com/en/catalog/genuine/parts", PARTS_HTML)
    assert parsed.vehicle is not None
    assert parsed.vehicle.brand_raw == "Toyota"
    assert parsed.vehicle.model_raw == "ACV40"
    assert parsed.vehicle.production_from == "2006-05"


def test_toyota_adapter_aliases() -> None:
    vehicle = ToyotaBrandAdapter().adapt({"Brand": "Toyota", "Frame": "ACV40", "Engine": "2AZ"})
    assert vehicle is not None and vehicle.model_raw == "ACV40" and vehicle.options_raw == "2AZ"


def test_audi_adapter_aliases() -> None:
    vehicle = AudiBrandAdapter().adapt({"Brand": "Audi", "Sales Type": "8K2"})
    assert vehicle is not None and vehicle.model_raw == "8K2"


def test_renault_adapter_aliases() -> None:
    vehicle = RenaultBrandAdapter().adapt({"Brand": "Renault", "Vehicle Type": "X98"})
    assert vehicle is not None and vehicle.model_raw == "X98"


def test_part_rows_and_multiple_diagrams() -> None:
    parsed = CatalogParser().parse("https://partsouq.com/en/catalog/genuine/parts", PARTS_HTML)
    assert len(parsed.diagrams) == 2
    assert len(parsed.parts) == 2
    assert parsed.parts[0].number_raw == "00123-AB"
    assert parsed.parts[0].condition_raw == "ACV40..ARL"


def test_real_part_page_shape_links_vehicle_diagram_and_part() -> None:
    parsed = CatalogParser().parse(
        "https://partsouq.com/en/catalog/genuine/diagram?c=Honda&number=31110P73A01",
        ARCHIVED_DIAGRAM_HTML,
    )
    assert parsed.vehicle is not None
    assert parsed.vehicle.brand_raw == "HONDA"
    assert parsed.vehicle.name_raw == "INTEGRA"
    assert parsed.vehicle.model_raw == "17ST701"
    assert parsed.vehicle.prod_period_raw == "1998-2000"
    assert len(parsed.diagrams) == 1
    assert parsed.diagrams[0].name_raw == "ALTERNATOR BRACKET"
    assert parsed.parts[0].diagram_name_raw == "ALTERNATOR BRACKET"
    assert parsed.parts[0].callout_raw == "1"
    assert parsed.parts[0].quantity_raw == "1"


def test_category_depth_over_three_is_preserved() -> None:
    parsed = CatalogParser().parse("https://partsouq.com/en/catalog/genuine/parts", PARTS_HTML)
    assert parsed.taxonomies[0].path == ("Engine", "Cooling", "Pump", "Housing")


def test_terminal_part_page_without_rows_is_parse_failure() -> None:
    try:
        CatalogParser().parse(
            "https://partsouq.com/en/catalog/genuine/parts", b"<html><h1>Parts</h1></html>"
        )
    except ParseError as error:
        assert "no parseable part rows" in str(error)
    else:
        raise AssertionError("terminal page did not fail")


def test_ingest_preserves_same_part_different_ranges_and_provenance(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = await Repository.create(tmp_path / "ingest.sqlite3")
        run_id = await repository.create_or_get_run("run", [], {})
        response = FetchResult(
            requested_url="https://partsouq.com/en/catalog/genuine/parts",
            final_url="https://partsouq.com/en/catalog/genuine/parts",
            status=200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            body=PARTS_HTML,
            elapsed_ms=1,
            attempt=1,
        )
        response_id, _ = await repository.store_response(
            run_id, None, response, challenged=False, challenge_reason=None
        )
        parsed = CatalogParser().parse(response.final_url, PARTS_HTML)
        inserted = await IngestService(repository).ingest(
            run_id=run_id,
            response_id=response_id,
            source_url=response.final_url,
            parsed=parsed,
        )
        assert inserted > 0
        counts = await repository.table_counts()
        assert counts["part_numbers"] == 1
        assert counts["part_occurrences"] == 2
        assert counts["fitments"] == 2
        assert counts["taxonomy_nodes"] == 4
        assert await repository.missing_provenance_count() == 0
        second_insert = await IngestService(repository).ingest(
            run_id=run_id,
            response_id=response_id,
            source_url=response.final_url,
            parsed=parsed,
        )
        assert second_insert == 0
        assert (await repository.table_counts())["part_occurrences"] == 2
        await repository.close()

    asyncio.run(scenario())


def test_search_compatibility_does_not_create_verified_fitment(tmp_path: Path) -> None:
    async def scenario() -> None:
        body = b'<div data-part-number="2710166J01" data-brand="Suzuki" data-model="Escudo" data-compatibility="Escudo Grand Vitara"></div>'
        url = "https://partsouq.com/en/search/all?q=2710166J01"
        repository = await Repository.create(tmp_path / "hint.sqlite3")
        run_id = await repository.create_or_get_run("run", [], {})
        response = FetchResult(url, url, 200, {"Content-Type": "text/html"}, body, 1, 1)
        response_id, _ = await repository.store_response(
            run_id, None, response, challenged=False, challenge_reason=None
        )
        parsed = CatalogParser().parse(url, body)
        await IngestService(repository).ingest(
            run_id=run_id, response_id=response_id, source_url=url, parsed=parsed
        )
        counts = await repository.table_counts()
        assert counts["compatibility_hints"] == 1
        assert counts["fitments"] == 0
        await repository.close()

    asyncio.run(scenario())


def test_part_relations_do_not_transitively_close(tmp_path: Path) -> None:
    async def scenario() -> None:
        body = b"""
        <div data-relation-type="superseded_by" data-from-part="A" data-to-part="B"></div>
        <div data-relation-type="superseded_by" data-from-part="B" data-to-part="C"></div>
        """
        url = "https://partsouq.com/en/search/all?q=A"
        repository = await Repository.create(tmp_path / "relations.sqlite3")
        run_id = await repository.create_or_get_run("run", [], {})
        response = FetchResult(url, url, 200, {"Content-Type": "text/html"}, body, 1, 1)
        response_id, _ = await repository.store_response(
            run_id, None, response, challenged=False, challenge_reason=None
        )
        await IngestService(repository).ingest(
            run_id=run_id,
            response_id=response_id,
            source_url=url,
            parsed=CatalogParser().parse(url, body),
        )
        cursor = await repository.connection.execute("SELECT COUNT(*) FROM part_relations")
        assert (await cursor.fetchone())[0] == 2
        await repository.close()

    asyncio.run(scenario())


def test_reparse_is_idempotent(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = await Repository.create(tmp_path / "reparse.sqlite3")
        run_id = await repository.create_or_get_run("run", [], {})
        url = "https://partsouq.com/en/catalog/genuine/parts"
        response = FetchResult(
            url,
            url,
            200,
            {"Content-Type": "text/html; charset=utf-8"},
            PARTS_HTML,
            1,
            1,
        )
        response_id, _ = await repository.store_response(
            run_id, None, response, challenged=False, challenge_reason=None
        )
        service = ReparseService(repository)
        first = await service.run(response_id=response_id)
        counts_after_first = await repository.table_counts()
        second = await service.run(response_id=response_id)
        counts_after_second = await repository.table_counts()
        assert first["records_inserted"] > 0
        assert second["records_inserted"] == 0
        assert counts_after_first == counts_after_second
        await repository.close()

    asyncio.run(scenario())


def test_archive_import_preserves_capture_and_does_not_verify_fitment(tmp_path: Path) -> None:
    async def scenario() -> None:
        input_path = tmp_path / "capture.html"
        input_path.write_bytes(ARCHIVED_DIAGRAM_HTML)
        repository = await Repository.create(tmp_path / "archive.sqlite3")
        report = await ArchiveImportService(repository).import_html(
            run_key="archive-test",
            capture=ArchiveCaptureInput(
                input_path=input_path,
                source_url=(
                    "https://partsouq.com/en/catalog/genuine/diagram?c=Honda&number=31110P73A01"
                ),
                archive_source="wayback",
                captured_at="2021-07-24T01:23:19Z",
                archive_digest="sha1:test",
            ),
        )
        counts = await repository.table_counts()
        assert report["parts_parsed"] == 1
        assert report["current_or_complete"] is False
        assert counts["archive_captures"] == 1
        assert counts["vehicle_configurations"] == 1
        assert counts["diagrams"] == 1
        assert counts["part_occurrences"] == 1
        assert counts["fitments"] == 1
        cursor = await repository.connection.execute("SELECT is_verified, derivation FROM fitments")
        fitment = await cursor.fetchone()
        assert fitment is not None
        assert fitment["is_verified"] == 0
        assert fitment["derivation"] == "historical_archive_wayback"
        assert await ExportService(repository).rows() == []
        archive_rows = await ExportService(repository).rows(include_unverified_fitments=True)
        assert len(archive_rows) == 1
        assert archive_rows[0]["Source mode"] == "historical_archive"
        assert archive_rows[0]["Captured at"] == "2021-07-24T01:23:19Z"
        await repository.close()

    asyncio.run(scenario())
