from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from partsouq_crawler.crawl.challenge import detect_challenge
from partsouq_crawler.db.repository import Repository
from partsouq_crawler.models.crawl import FetchResult
from partsouq_crawler.parsers.base import CatalogParser, ParseError
from partsouq_crawler.services.ingest import IngestService


@dataclass(frozen=True, slots=True)
class ArchiveCaptureInput:
    source_url: str
    archive_source: str
    captured_at: str
    input_path: Path | None = None
    http_status: int = 200
    response_headers: dict[str, str] = field(default_factory=dict)
    collection_name: str | None = None
    warc_filename: str | None = None
    warc_offset: int | None = None
    warc_length: int | None = None
    archive_digest: str | None = None
    truncation_reason: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


class ArchiveImportService:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    async def import_html(
        self,
        *,
        run_key: str,
        capture: ArchiveCaptureInput,
    ) -> dict[str, object]:
        if capture.input_path is None:
            raise ValueError("archive input path is required")
        return await self.import_bytes(
            run_key=run_key,
            capture=capture,
            body=capture.input_path.read_bytes(),
        )

    async def import_bytes(
        self,
        *,
        run_key: str,
        capture: ArchiveCaptureInput,
        body: bytes,
    ) -> dict[str, object]:
        config: dict[str, object] = {
            "source_mode": "historical_archive",
            "archive_source": capture.archive_source,
            "current_or_complete": False,
        }
        run_id = await self.repository.create_or_get_run(run_key, [capture.source_url], config)
        await self.repository.set_run_status(run_id, "running")
        result = FetchResult(
            requested_url=capture.source_url,
            final_url=capture.source_url,
            status=capture.http_status,
            headers=capture.response_headers or {"Content-Type": "text/html; charset=utf-8"},
            body=body,
            elapsed_ms=0,
            attempt=1,
        )
        challenge = detect_challenge(result.status, result.headers, result.body)
        response_id, body_sha256 = await self.repository.store_response(
            run_id,
            None,
            result,
            challenged=challenge.challenged,
            challenge_reason=challenge.reason,
        )
        await self.repository.add_archive_capture(
            response_id=response_id,
            archive_source=capture.archive_source,
            collection_name=capture.collection_name,
            captured_at=capture.captured_at,
            warc_filename=capture.warc_filename,
            warc_offset=capture.warc_offset,
            warc_length=capture.warc_length,
            archive_digest=capture.archive_digest,
            truncation_reason=capture.truncation_reason,
            metadata=capture.metadata,
        )

        records_inserted = 0
        parts_parsed = 0
        vehicle_parsed = False
        diagrams_parsed = 0
        error: str | None = None
        if challenge.challenged:
            error = challenge.reason or "challenge_capture"
        else:
            try:
                parsed = CatalogParser().parse(capture.source_url, body)
                parts_parsed = len(parsed.parts)
                vehicle_parsed = parsed.vehicle is not None
                diagrams_parsed = len(parsed.diagrams)
                records_inserted = await IngestService(self.repository).ingest(
                    run_id=run_id,
                    response_id=response_id,
                    source_url=capture.source_url,
                    parsed=parsed,
                    verified_fitments=False,
                    fitment_derivation=f"historical_archive_{capture.archive_source}",
                )
            except (LookupError, OSError, ParseError, ValueError) as parse_error:
                error = str(parse_error)
                await self.repository.add_parse_failure(
                    response_id,
                    "catalog_parser",
                    "historical_archive",
                    parse_error,
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
            "archive_source": capture.archive_source,
            "captured_at": capture.captured_at,
            "response_id": response_id,
            "body_sha256": body_sha256,
            "truncation_reason": capture.truncation_reason,
            "cloudflare_challenge": challenge.challenged,
            "parts_parsed": parts_parsed,
            "vehicle_parsed": vehicle_parsed,
            "diagrams_parsed": diagrams_parsed,
            "records_inserted": records_inserted,
            "error": error,
            "current_or_complete": False,
        }
