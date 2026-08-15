from __future__ import annotations

import asyncio
import io
import json
import re
import ssl
from pathlib import Path
from typing import TypedDict, cast
from urllib.parse import parse_qs, urlsplit

import aiohttp
import certifi
from warcio.archiveiterator import ArchiveIterator

from partsouq_crawler.db.repository import Repository
from partsouq_crawler.services.archive_import import ArchiveCaptureInput, ArchiveImportService

COLLECTION_PATTERN = re.compile(r"crawl-data/(CC-MAIN-\d{4}-\d{2})/")
VIN_PATTERN = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$", re.IGNORECASE)
ALLOWED_PATH = "/en/catalog/genuine/diagram"


class CommonCrawlIndexRecord(TypedDict):
    url: str
    filename: str
    offset: int
    length: int
    collection: str
    timestamp: str
    digest: str


class ExtractedWarcResponse(TypedDict):
    source_url: str
    captured_at: str
    record_id: str | None
    payload_digest: str | None
    truncation_reason: str | None
    http_status: int
    headers: dict[str, str]
    body: bytes


class CommonCrawlImportService:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository
        self.archive_import = ArchiveImportService(repository)

    async def run(
        self,
        *,
        run_key: str,
        index_paths: list[Path],
        max_records: int = 0,
        delay_seconds: float = 0.25,
        timeout_seconds: float = 60.0,
    ) -> dict[str, object]:
        run_id = await self.repository.create_or_get_run(
            run_key,
            [],
            {
                "source_mode": "historical_archive",
                "archive_source": "common_crawl",
                "allowed_path": ALLOWED_PATH,
                "current_or_complete": False,
            },
        )
        await self.repository.set_run_status(run_id, "running")
        records = self._load_records(index_paths)
        if max_records:
            records = records[:max_records]

        imported = 0
        skipped_existing = 0
        failed = 0
        parts_parsed = 0
        records_inserted = 0
        failures: list[dict[str, object]] = []
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        async with aiohttp.ClientSession(
            timeout=timeout,
            auto_decompress=False,
            connector=aiohttp.TCPConnector(ssl=ssl_context),
            headers={"User-Agent": "partsouq-crawler/0.1 archive-import"},
        ) as session:
            for position, record in enumerate(records, start=1):
                if await self.repository.archive_capture_exists(
                    archive_source="common_crawl",
                    collection_name=record["collection"],
                    warc_filename=record["filename"],
                    warc_offset=record["offset"],
                    warc_length=record["length"],
                ):
                    skipped_existing += 1
                    continue
                if imported or failed:
                    await asyncio.sleep(delay_seconds)
                try:
                    warc_bytes = await self._download_record(session, record)
                    extracted = await asyncio.to_thread(self._extract_response, warc_bytes)
                    report = await self.archive_import.import_bytes(
                        run_key=run_key,
                        capture=ArchiveCaptureInput(
                            source_url=extracted["source_url"],
                            archive_source="common_crawl",
                            captured_at=extracted["captured_at"],
                            http_status=extracted["http_status"],
                            response_headers=extracted["headers"],
                            collection_name=record["collection"],
                            warc_filename=record["filename"],
                            warc_offset=record["offset"],
                            warc_length=record["length"],
                            archive_digest=extracted["payload_digest"] or record["digest"],
                            truncation_reason=extracted["truncation_reason"],
                            metadata={
                                "warc_record_id": extracted["record_id"],
                                "index_timestamp": record["timestamp"],
                            },
                        ),
                        body=extracted["body"],
                    )
                    imported += 1
                    parts_parsed += cast(int, report["parts_parsed"])
                    records_inserted += cast(int, report["records_inserted"])
                except (aiohttp.ClientError, OSError, RuntimeError, ValueError) as error:
                    failed += 1
                    if len(failures) < 20:
                        failures.append(
                            {
                                "position": position,
                                "error_type": type(error).__name__,
                                "error": str(error),
                            }
                        )

        await self.repository.set_run_status(
            run_id,
            "completed_with_gaps",
            blocked_reason="historical_archive_not_current_or_complete",
            ended=True,
        )
        return {
            "run_id": run_id,
            "run_key": run_key,
            "source_mode": "historical_archive",
            "archive_source": "common_crawl",
            "index_records_selected": len(records),
            "imported": imported,
            "skipped_existing": skipped_existing,
            "failed": failed,
            "parts_parsed": parts_parsed,
            "records_inserted": records_inserted,
            "failures": failures,
            "current_or_complete": False,
        }

    @staticmethod
    def _load_records(index_paths: list[Path]) -> list[CommonCrawlIndexRecord]:
        records: list[CommonCrawlIndexRecord] = []
        seen_locations: set[tuple[str, int, int]] = set()
        for path in index_paths:
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                try:
                    raw = json.loads(line)
                    url = str(raw["url"])
                    filename = str(raw["filename"])
                    offset = int(raw["offset"])
                    length = int(raw["length"])
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                    raise ValueError(
                        f"invalid index record {path}:{line_number}: {error}"
                    ) from error
                parsed = urlsplit(url)
                if parsed.hostname != "partsouq.com" or parsed.path != ALLOWED_PATH:
                    continue
                if any(
                    VIN_PATTERN.fullmatch(value) for value in parse_qs(parsed.query).get("q", [])
                ):
                    continue
                match = COLLECTION_PATTERN.search(filename)
                if match is None:
                    raise ValueError(f"collection missing in {path}:{line_number}")
                location = (filename, offset, length)
                if location in seen_locations:
                    continue
                seen_locations.add(location)
                records.append(
                    {
                        "url": url,
                        "filename": filename,
                        "offset": offset,
                        "length": length,
                        "collection": match.group(1),
                        "timestamp": str(raw.get("timestamp") or ""),
                        "digest": str(raw.get("digest") or ""),
                    }
                )
        return records

    @staticmethod
    async def _download_record(
        session: aiohttp.ClientSession,
        record: CommonCrawlIndexRecord,
    ) -> bytes:
        offset = int(record["offset"])
        length = int(record["length"])
        url = f"https://data.commoncrawl.org/{record['filename']}"
        headers = {"Range": f"bytes={offset}-{offset + length - 1}"}
        async with session.get(url, headers=headers) as response:
            body = await response.read()
            if response.status not in {200, 206}:
                raise RuntimeError(f"Common Crawl returned HTTP {response.status}")
            if len(body) != length:
                raise RuntimeError(
                    f"WARC range length mismatch: expected {length}, got {len(body)}"
                )
            return body

    @staticmethod
    def _extract_response(warc_bytes: bytes) -> ExtractedWarcResponse:
        for record in ArchiveIterator(io.BytesIO(warc_bytes)):
            if record.rec_type != "response" or record.http_headers is None:
                continue
            body = record.content_stream().read()
            headers = {name: value for name, value in record.http_headers.headers}
            captured_at = record.rec_headers.get_header("WARC-Date")
            if not captured_at:
                raise ValueError("WARC response has no capture time")
            source_url = record.rec_headers.get_header("WARC-Target-URI")
            if not source_url:
                raise ValueError("WARC response has no target URL")
            return {
                "source_url": source_url,
                "captured_at": captured_at,
                "record_id": record.rec_headers.get_header("WARC-Record-ID"),
                "payload_digest": record.rec_headers.get_header("WARC-Payload-Digest"),
                "truncation_reason": record.rec_headers.get_header("WARC-Truncated"),
                "http_status": int(record.http_headers.get_statuscode()),
                "headers": headers,
                "body": body,
            }
        raise ValueError("WARC range contains no response record")
