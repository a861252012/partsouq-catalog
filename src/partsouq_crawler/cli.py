from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from pathlib import Path
from typing import Any

import pymysql

from partsouq_catalog.admission import AdmissionLockBusy
from partsouq_crawler.config import DEFAULT_SEED, CrawlerConfig
from partsouq_crawler.crawl.challenge import detect_challenge
from partsouq_crawler.crawl.engine import CrawlerEngine
from partsouq_crawler.crawl.fetcher import FetchError
from partsouq_crawler.crawl.robots import parse_robots
from partsouq_crawler.crawl.sitemap import parse_sitemap
from partsouq_crawler.crawl.transport import create_fetch_transport
from partsouq_crawler.db.repository import Repository
from partsouq_crawler.logging import CrawlLogger
from partsouq_crawler.nhtsa.api_service import NhtsaApiSyncService
from partsouq_crawler.nhtsa.config import NhtsaConfig
from partsouq_crawler.nhtsa.datasets import BULK_SOURCES_BY_SCOPE
from partsouq_crawler.nhtsa.repository import NhtsaMySQLRepository
from partsouq_crawler.nhtsa.service import NhtsaBulkSyncService
from partsouq_crawler.parsers.base import CatalogParser, ParseError
from partsouq_crawler.services.archive_import import ArchiveCaptureInput, ArchiveImportService
from partsouq_crawler.services.common_crawl_import import CommonCrawlImportService
from partsouq_crawler.services.export import ExportService
from partsouq_crawler.services.ingest import IngestService
from partsouq_crawler.services.reparse import ReparseService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="partsouq-crawler")
    subparsers = parser.add_subparsers(dest="command", required=True)

    probe = subparsers.add_parser("probe", help="Fetch one URL once and store its response")
    probe.add_argument("url")
    _database_argument(probe)
    probe.add_argument("--run-id", default="partsouq-live-probe")
    probe.add_argument("--user-agent")
    probe.add_argument("--timeout", type=float, default=30.0)
    _transport_arguments(probe)

    crawl = subparsers.add_parser("crawl-all", help="Resume or start full discovery crawl")
    _database_argument(crawl)
    crawl.add_argument("--run-id", required=True)
    crawl.add_argument("--seed-url", default=DEFAULT_SEED)
    crawl.add_argument("--max-pages", type=int, default=0)
    crawl.add_argument("--max-depth", type=int, default=0)
    crawl.add_argument("--concurrency", type=int, default=1)
    crawl.add_argument("--delay", type=float, default=5.0)
    crawl.add_argument("--timeout", type=float, default=30.0)
    crawl.add_argument("--retry-count", type=int, default=3)
    crawl.add_argument("--robots-policy", choices=("require", "ignore"), default="require")
    crawl.add_argument("--user-agent")
    crawl.add_argument("--json-log", action="store_true")
    _transport_arguments(crawl)

    status = subparsers.add_parser("crawl-status")
    _database_argument(status)
    status.add_argument("--run-id", required=True)

    db_status = subparsers.add_parser("db-status")
    _database_argument(db_status)

    problems = subparsers.add_parser("problem-urls")
    _database_argument(problems)
    problems.add_argument("--run-id", required=True)
    problems.add_argument("--format", choices=("jsonl", "csv"), default="jsonl")
    problems.add_argument("--output", type=Path)

    requeue = subparsers.add_parser("requeue-problems")
    _database_argument(requeue)
    requeue.add_argument("--run-id", required=True)
    requeue.add_argument(
        "--status", action="append", choices=("failed", "parse_failed", "challenged"), required=True
    )

    dump = subparsers.add_parser("dump-response")
    _database_argument(dump)
    dump.add_argument("--response-id", type=int)
    dump.add_argument("--url")
    dump.add_argument("--sha256")
    dump.add_argument("--output", type=Path, required=True)

    reparse = subparsers.add_parser("reparse")
    _database_argument(reparse)
    reparse.add_argument("--response-id", type=int)
    reparse.add_argument("--run-id", type=int)
    reparse.add_argument("--page-type")

    export = subparsers.add_parser("export")
    _database_argument(export)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--include-compatibility-hints", action="store_true")
    export.add_argument("--include-unverified-fitments", action="store_true")

    backup = subparsers.add_parser("db-backup")
    _database_argument(backup)
    backup.add_argument("--output", type=Path, required=True)

    publish = subparsers.add_parser(
        "snapshot-publish",
        help="Validate and atomically publish a read-only database snapshot",
    )
    _database_argument(publish)
    publish.add_argument("--output", type=Path, required=True)

    archive_import = subparsers.add_parser(
        "archive-import",
        help="Import a lawfully obtained historical HTML capture without contacting PartSouq",
    )
    _database_argument(archive_import)
    archive_import.add_argument("--run-id", required=True)
    archive_import.add_argument("--input", type=Path, required=True)
    archive_import.add_argument("--source-url", required=True)
    archive_import.add_argument(
        "--archive-source",
        choices=("common_crawl", "wayback", "owned_export"),
        required=True,
    )
    archive_import.add_argument("--captured-at", required=True)
    archive_import.add_argument("--collection")
    archive_import.add_argument("--warc-filename")
    archive_import.add_argument("--warc-offset", type=int)
    archive_import.add_argument("--warc-length", type=int)
    archive_import.add_argument("--archive-digest")
    archive_import.add_argument("--truncation-reason")

    common_crawl_import = subparsers.add_parser(
        "common-crawl-import",
        help="Download diagram WARC records listed in Common Crawl index files",
    )
    _database_argument(common_crawl_import)
    common_crawl_import.add_argument("--run-id", required=True)
    common_crawl_import.add_argument("--index", type=Path, action="append", required=True)
    common_crawl_import.add_argument("--max-records", type=int, default=0)
    common_crawl_import.add_argument("--delay", type=float, default=0.25)
    common_crawl_import.add_argument("--timeout", type=float, default=60.0)

    nhtsa_sync = subparsers.add_parser(
        "nhtsa-sync-bulk",
        help="Import official NHTSA bulk datasets into MySQL",
    )
    _nhtsa_arguments(nhtsa_sync)
    nhtsa_sync.add_argument("--run-id", default="nhtsa-official-bulk")
    nhtsa_sync.add_argument(
        "--scope",
        choices=tuple(BULK_SOURCES_BY_SCOPE),
        default="all",
    )

    nhtsa_api = subparsers.add_parser(
        "nhtsa-sync-api",
        help="Sync allowlisted NHTSA vPIC and CSSI APIs into MySQL",
    )
    _nhtsa_arguments(nhtsa_api)
    nhtsa_api.add_argument("--run-id", default="nhtsa-official-api")
    nhtsa_api.add_argument("--scope", choices=("all", "vpic", "cssi"), default="all")

    nhtsa_vin = subparsers.add_parser(
        "nhtsa-decode-vin",
        help="Decode one user-supplied 17-character VIN with the official vPIC API",
    )
    _nhtsa_arguments(nhtsa_vin)
    nhtsa_vin.add_argument("vin")
    nhtsa_vin.add_argument("--run-id", default="nhtsa-official-vin")

    nhtsa_status = subparsers.add_parser("nhtsa-status")
    _nhtsa_arguments(nhtsa_status, include_runtime=False)
    return parser


def _database_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sqlite", type=Path, default=Path("output/partsouq-live.sqlite3"))


def _transport_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--transport", choices=("http", "browser"), default="http")
    parser.add_argument("--browser-executable", type=Path)
    parser.add_argument("--browser-headless", action="store_true")


def _nhtsa_arguments(parser: argparse.ArgumentParser, *, include_runtime: bool = True) -> None:
    if include_runtime:
        parser.add_argument("--raw-dir", type=Path)
        parser.add_argument("--user-agent")
        parser.add_argument("--timeout", type=float)
        parser.add_argument("--api-delay", type=float)


async def dispatch(args: argparse.Namespace) -> int:
    if args.command.startswith("nhtsa-"):
        return await _dispatch_nhtsa(args)
    repository = await Repository.create(args.sqlite)
    try:
        if args.command == "probe":
            return await _probe(repository, args)
        if args.command == "crawl-all":
            config = CrawlerConfig.from_env(
                database=args.sqlite,
                concurrency=args.concurrency,
                delay_seconds=args.delay,
                request_timeout_seconds=args.timeout,
                max_retries=args.retry_count,
                max_pages=args.max_pages,
                max_depth=args.max_depth,
                robots_policy=args.robots_policy,
                user_agent=args.user_agent,
                log_json=args.json_log,
                transport=args.transport,
                browser_executable=args.browser_executable,
                browser_headless=args.browser_headless,
            )
            engine = CrawlerEngine(
                repository=repository,
                config=config,
                run_key=args.run_id,
                seed_url=args.seed_url,
                logger=CrawlLogger(json_mode=args.json_log),
            )
            return await engine.run()
        if args.command == "crawl-status":
            _print_json(await repository.status_report(args.run_id))
            return 0
        if args.command == "db-status":
            _print_json(await repository.db_status())
            return 0
        if args.command == "problem-urls":
            rows = await repository.problem_urls(args.run_id)
            _write_problem_rows(rows, args.format, args.output)
            return 0
        if args.command == "requeue-problems":
            count = await repository.requeue_problems(args.run_id, args.status)
            _print_json({"requeued": count, "statuses": args.status})
            return 0
        if args.command == "dump-response":
            return await _dump_response(repository, args)
        if args.command == "reparse":
            reparse_report = await ReparseService(repository).run(
                response_id=args.response_id,
                run_id=args.run_id,
                page_type=args.page_type,
            )
            _print_json(reparse_report)
            return 0 if reparse_report["failed"] == 0 else 1
        if args.command == "export":
            count = await ExportService(repository).export(
                args.output,
                include_compatibility_hints=args.include_compatibility_hints,
                include_unverified_fitments=args.include_unverified_fitments,
            )
            _print_json({"output": str(args.output), "rows": count})
            return 0
        if args.command == "db-backup":
            await repository.backup(args.output)
            _print_json({"backup": str(args.output)})
            return 0
        if args.command == "snapshot-publish":
            manifest = await repository.publish_snapshot(args.output)
            _print_json({"snapshot": str(args.output), "manifest": manifest})
            return 0
        if args.command == "archive-import":
            archive_report = await ArchiveImportService(repository).import_html(
                run_key=args.run_id,
                capture=ArchiveCaptureInput(
                    input_path=args.input,
                    source_url=args.source_url,
                    archive_source=args.archive_source,
                    captured_at=args.captured_at,
                    collection_name=args.collection,
                    warc_filename=args.warc_filename,
                    warc_offset=args.warc_offset,
                    warc_length=args.warc_length,
                    archive_digest=args.archive_digest,
                    truncation_reason=args.truncation_reason,
                ),
            )
            _print_json(archive_report)
            return 0 if archive_report["error"] is None else 1
        if args.command == "common-crawl-import":
            common_crawl_report = await CommonCrawlImportService(repository).run(
                run_key=args.run_id,
                index_paths=args.index,
                max_records=args.max_records,
                delay_seconds=args.delay,
                timeout_seconds=args.timeout,
            )
            _print_json(common_crawl_report)
            return 0 if common_crawl_report["failed"] == 0 else 1
        raise ValueError(f"unsupported command: {args.command}")
    finally:
        await repository.close()


async def _dispatch_nhtsa(args: argparse.Namespace) -> int:
    config = NhtsaConfig.from_env(
        raw_dir=getattr(args, "raw_dir", None),
        user_agent=getattr(args, "user_agent", None),
        request_timeout_seconds=getattr(args, "timeout", None),
        api_delay_seconds=getattr(args, "api_delay", None),
    )
    try:
        repository = NhtsaMySQLRepository.create(config)
    except pymysql.MySQLError as error:
        _print_json({"status": "failed", "error_type": type(error).__name__, "error": str(error)})
        return 1
    try:
        if args.command == "nhtsa-status":
            _print_json(repository.status_report())
            return 0
        if args.command == "nhtsa-sync-bulk":
            sources = BULK_SOURCES_BY_SCOPE[args.scope]
            report = await NhtsaBulkSyncService(repository, config).run(
                run_key=args.run_id,
                scope_name=args.scope,
                sources=sources,
            )
            _print_json(report)
            return 0 if report["status"] == "completed" else 1
        if args.command == "nhtsa-sync-api":
            report = await NhtsaApiSyncService(repository, config).run(
                run_key=args.run_id,
                scope_name=args.scope,
            )
            _print_json(report)
            return 0 if report["status"] == "completed" else 1
        if args.command == "nhtsa-decode-vin":
            try:
                report = await NhtsaApiSyncService(repository, config).decode_vin(
                    run_key=args.run_id,
                    vin=args.vin,
                )
            except ValueError as error:
                _print_json(
                    {"status": "failed", "error_type": type(error).__name__, "error": str(error)}
                )
                return 1
            _print_json(report)
            return 0 if report["status"] == "completed" else 1
        raise ValueError(f"unsupported NHTSA command: {args.command}")
    except AdmissionLockBusy as error:
        _print_json({"status": "deferred", "error_type": type(error).__name__})
        return 75
    finally:
        repository.close()


async def _probe(repository: Repository, args: argparse.Namespace) -> int:
    config = CrawlerConfig.from_env(
        database=args.sqlite,
        request_timeout_seconds=args.timeout,
        max_retries=0,
        user_agent=args.user_agent,
        delay_seconds=0,
        transport=args.transport,
        browser_executable=args.browser_executable,
        browser_headless=args.browser_headless,
    )
    run_id = await repository.create_or_get_run(args.run_id, [args.url], config.public_dict())
    await repository.set_run_status(run_id, "running")
    try:
        async with create_fetch_transport(config, delay_seconds=0) as fetcher:
            result = await fetcher.fetch_once(args.url)
    except FetchError as error:
        await repository.set_run_status(run_id, "failed", ended=True)
        _print_json({"url": args.url, "error": str(error)})
        return 1

    challenge = detect_challenge(result.status, result.headers, result.body)
    response_id, sha256 = await repository.store_response(
        run_id,
        None,
        result,
        challenged=challenge.challenged,
        challenge_reason=challenge.reason,
    )
    parsed_ok = False
    records = 0
    if not challenge.challenged and result.status < 400:
        try:
            if result.final_url.endswith("/robots.txt"):
                parse_robots(result.final_url, result.body, result.charset or "utf-8")
            elif result.final_url.endswith((".xml", ".xml.gz")) or "xml" in (
                result.content_type or ""
            ):
                parse_sitemap(result.body, compressed=result.final_url.endswith(".gz"))
            elif "html" in (result.content_type or ""):
                parsed = CatalogParser().parse(
                    result.final_url, result.body, result.charset or "utf-8"
                )
                records = await IngestService(repository).ingest(
                    run_id=run_id,
                    response_id=response_id,
                    source_url=result.final_url,
                    parsed=parsed,
                )
            else:
                raise ParseError("unsupported probe content type")
            parsed_ok = True
        except (OSError, ParseError, ValueError) as error:
            await repository.add_parse_failure(response_id, "catalog_parser", "probe", error)
    status = "blocked" if challenge.challenged else "completed"
    await repository.set_run_status(run_id, status, blocked_reason=challenge.reason, ended=True)
    _print_json(
        {
            "url": args.url,
            "http_status": result.status,
            "content_type": result.content_type,
            "cf_mitigated": result.headers.get("cf-mitigated"),
            "cloudflare_challenge": challenge.challenged,
            "challenge_reason": challenge.reason,
            "body_sha256": sha256,
            "response_id": response_id,
            "parsed": parsed_ok,
            "normalized_records_inserted": records,
        }
    )
    return 2 if challenge.challenged else (0 if result.status < 400 else 1)


async def _dump_response(repository: Repository, args: argparse.Namespace) -> int:
    provided = sum(value is not None for value in (args.response_id, args.url, args.sha256))
    if provided != 1:
        raise ValueError("provide exactly one of --response-id, --url, or --sha256")
    rows = await repository.find_responses(
        response_id=args.response_id, url=args.url, sha256=args.sha256
    )
    if not rows:
        raise KeyError("response not found")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(repository.restore_body(rows[-1]))
    _print_json({"response_id": rows[-1]["id"], "output": str(args.output)})
    return 0


def _write_problem_rows(
    rows: list[dict[str, object]], format_name: str, output: Path | None
) -> None:
    if format_name == "jsonl":
        text = "".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in rows)
    else:
        import io

        buffer = io.StringIO()
        if rows:
            writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        text = buffer.getvalue()
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        code = asyncio.run(dispatch(args))
    except (KeyError, ValueError) as error:
        parser.error(str(error))
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main()
