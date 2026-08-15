from __future__ import annotations

from partsouq_crawler.crawl.classifier import classify_page
from partsouq_crawler.db.repository import Repository
from partsouq_crawler.parsers.base import CatalogParser, ParseError
from partsouq_crawler.services.ingest import IngestService


class ReparseService:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository
        self.parser = CatalogParser()
        self.ingest = IngestService(repository)

    async def run(
        self,
        *,
        response_id: int | None = None,
        run_id: int | None = None,
        page_type: str | None = None,
    ) -> dict[str, int]:
        responses = await self.repository.find_responses(response_id=response_id, run_id=run_id)
        report = {"selected": len(responses), "parsed": 0, "failed": 0, "records_inserted": 0}
        for row in responses:
            if int(row["http_status"]) >= 400 or row["is_cloudflare_challenge"]:
                continue
            body = self.repository.restore_body(row)
            detected = classify_page(row["final_url"], body, row["charset"] or "utf-8")
            if page_type and detected != page_type:
                continue
            try:
                parsed = self.parser.parse(row["final_url"], body, row["charset"] or "utf-8")
                inserted = await self.ingest.ingest(
                    run_id=int(row["run_id"]),
                    response_id=int(row["id"]),
                    source_url=row["final_url"],
                    parsed=parsed,
                )
            except ParseError as error:
                await self.repository.add_parse_failure(
                    int(row["id"]), "catalog_parser", detected, error
                )
                report["failed"] += 1
                continue
            report["parsed"] += 1
            report["records_inserted"] += inserted
        return report
