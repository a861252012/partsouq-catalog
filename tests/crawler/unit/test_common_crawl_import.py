import asyncio
import io
import json
from pathlib import Path

from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter

from partsouq_crawler.db.repository import Repository
from partsouq_crawler.services.common_crawl_import import CommonCrawlImportService

DIAGRAM_URL = "https://partsouq.com/en/catalog/genuine/diagram?c=Honda&number=31110P73A01"
DIAGRAM_HTML = b"""
<html><body>
<ul class="breadcrumb">
  <li>Genuine Parts Catalogs</li><li>Honda</li><li>INTEGRA</li><li>ENGINE</li>
</ul>
<table>
  <tr><th>Brand</th><th>Name</th><th>Npl</th><th>Manufactured</th></tr>
  <tr><td>HONDA</td><td>INTEGRA</td><td>17ST701</td><td>1998-2000</td></tr>
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


def _warc_response() -> bytes:
    output = io.BytesIO()
    writer = WARCWriter(output, gzip=True)
    http_headers = StatusAndHeaders(
        "200 OK",
        [("Content-Type", "text/html; charset=utf-8")],
        protocol="HTTP/1.1",
    )
    record = writer.create_warc_record(
        DIAGRAM_URL,
        "response",
        payload=io.BytesIO(DIAGRAM_HTML),
        http_headers=http_headers,
    )
    writer.write_record(record)
    return output.getvalue()


def test_common_crawl_import_is_resumable(monkeypatch: object, tmp_path: Path) -> None:
    async def scenario() -> None:
        warc = _warc_response()
        index_path = tmp_path / "index.ndjson"
        index_path.write_text(
            json.dumps(
                {
                    "url": DIAGRAM_URL,
                    "status": "200",
                    "digest": "TEST",
                    "timestamp": "20210724012319",
                    "filename": ("crawl-data/CC-MAIN-2021-49/segments/test/warc/test.warc.gz"),
                    "offset": "100",
                    "length": str(len(warc)),
                }
            )
            + "\n",
            encoding="utf-8",
        )

        async def fake_download(*_args: object, **_kwargs: object) -> bytes:
            return warc

        monkeypatch.setattr(
            CommonCrawlImportService,
            "_download_record",
            staticmethod(fake_download),
        )
        repository = await Repository.create(tmp_path / "archive.sqlite3")
        service = CommonCrawlImportService(repository)
        first = await service.run(
            run_key="common-crawl-test",
            index_paths=[index_path],
            delay_seconds=0,
        )
        second = await service.run(
            run_key="common-crawl-test",
            index_paths=[index_path],
            delay_seconds=0,
        )
        counts = await repository.table_counts()
        assert first["imported"] == 1
        assert first["failed"] == 0
        assert second["imported"] == 0
        assert second["skipped_existing"] == 1
        assert counts["http_responses"] == 1
        assert counts["archive_captures"] == 1
        assert counts["part_numbers"] == 1
        assert counts["part_occurrences"] == 1
        assert counts["fitments"] == 1
        cursor = await repository.connection.execute("SELECT is_verified FROM fitments")
        row = await cursor.fetchone()
        assert row is not None and row["is_verified"] == 0
        await repository.close()

    asyncio.run(scenario())
