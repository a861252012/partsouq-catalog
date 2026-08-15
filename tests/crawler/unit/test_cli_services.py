import asyncio
from pathlib import Path

import pytest

from partsouq_crawler.cli import build_parser, dispatch
from partsouq_crawler.config import CrawlerConfig
from partsouq_crawler.db.repository import Repository
from partsouq_crawler.models.crawl import FetchResult
from partsouq_crawler.parsers.base import CatalogParser
from partsouq_crawler.services.export import ExportService
from partsouq_crawler.services.ingest import IngestService

from .test_parsers_ingest import PARTS_HTML


def test_config_from_env_and_validation(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PARTSOUQ_DATABASE", str(tmp_path / "env.sqlite3"))
    monkeypatch.setenv("PARTSOUQ_CONCURRENCY", "2")
    monkeypatch.setenv("PARTSOUQ_DELAY_SECONDS", "1.5")
    monkeypatch.setenv("PARTSOUQ_REQUEST_TIMEOUT_SECONDS", "9")
    monkeypatch.setenv("PARTSOUQ_MAX_RETRIES", "4")
    monkeypatch.setenv("PARTSOUQ_USER_AGENT", "test-agent")
    config = CrawlerConfig.from_env(max_pages=12, max_depth=3)
    assert config.database == tmp_path / "env.sqlite3"
    assert config.concurrency == 2
    assert config.delay_seconds == 1.5
    assert config.max_retries == 4
    assert config.max_pages == 12 and config.max_depth == 3
    assert config.user_agent == "test-agent"
    assert config.public_dict()["database"] == str(tmp_path / "env.sqlite3")

    with pytest.raises(ValueError):
        CrawlerConfig(concurrency=0).validate()
    with pytest.raises(ValueError):
        CrawlerConfig(delay_seconds=-1).validate()
    with pytest.raises(ValueError):
        CrawlerConfig(request_timeout_seconds=0).validate()
    with pytest.raises(ValueError):
        CrawlerConfig(max_pages=-1).validate()
    with pytest.raises(ValueError):
        CrawlerConfig(robots_policy="maybe").validate()
    with pytest.raises(ValueError):
        CrawlerConfig(transport="maybe").validate()
    with pytest.raises(ValueError):
        CrawlerConfig(transport="browser", concurrency=2).validate()


def test_config_browser_transport_from_env(monkeypatch) -> None:
    monkeypatch.setenv("PARTSOUQ_TRANSPORT", "browser")
    monkeypatch.setenv("PARTSOUQ_BROWSER_EXECUTABLE", "/Applications/Test Browser")
    monkeypatch.setenv("PARTSOUQ_BROWSER_HEADLESS", "true")
    config = CrawlerConfig.from_env()
    assert config.transport == "browser"
    assert config.browser_executable == Path("/Applications/Test Browser")
    assert config.browser_headless is True


def test_cli_browser_transport_options() -> None:
    args = build_parser().parse_args(
        [
            "probe",
            "https://partsouq.com/en/catalog/genuine",
            "--transport",
            "browser",
            "--browser-executable",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        ]
    )
    assert args.transport == "browser"
    assert args.browser_executable.name == "Brave Browser"
    assert args.browser_headless is False


def test_cli_database_commands_and_export_service(tmp_path: Path) -> None:
    async def setup(database: Path) -> int:
        repository = await Repository.create(database)
        run_id = await repository.create_or_get_run("cli-run", [], {})
        await repository.enqueue(run_id, "https://x/problem", parent_url=None, depth=0)
        item = await repository.acquire_next(run_id, worker_id="w", lease_seconds=1, max_depth=0)
        assert item is not None
        await repository.finish_queue(item.id, "challenged", error="challenge")
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
        await IngestService(repository).ingest(
            run_id=run_id,
            response_id=response_id,
            source_url=url,
            parsed=CatalogParser().parse(url, PARTS_HTML),
        )
        jsonl = tmp_path / "direct.jsonl"
        count = await ExportService(repository).export(jsonl)
        assert count == 2 and jsonl.read_text().count("\n") == 2
        with pytest.raises(ValueError):
            await ExportService(repository).export(tmp_path / "bad.txt")
        await repository.close()
        return response_id

    async def command(arguments: list[str]) -> int:
        args = build_parser().parse_args(arguments)
        return await dispatch(args)

    database = tmp_path / "cli.sqlite3"
    response_id = asyncio.run(setup(database))
    base = ["--sqlite", str(database)]
    assert asyncio.run(command(["db-status", *base])) == 0
    assert asyncio.run(command(["crawl-status", *base, "--run-id", "cli-run"])) == 0

    problems = tmp_path / "problems.csv"
    assert (
        asyncio.run(
            command(
                [
                    "problem-urls",
                    *base,
                    "--run-id",
                    "cli-run",
                    "--format",
                    "csv",
                    "--output",
                    str(problems),
                ]
            )
        )
        == 0
    )
    assert "challenged" in problems.read_text()
    assert (
        asyncio.run(
            command(
                [
                    "requeue-problems",
                    *base,
                    "--run-id",
                    "cli-run",
                    "--status",
                    "challenged",
                ]
            )
        )
        == 0
    )

    dumped = tmp_path / "dumped.html"
    assert (
        asyncio.run(
            command(
                [
                    "dump-response",
                    *base,
                    "--response-id",
                    str(response_id),
                    "--output",
                    str(dumped),
                ]
            )
        )
        == 0
    )
    assert dumped.read_bytes() == PARTS_HTML
    assert asyncio.run(command(["reparse", *base, "--response-id", str(response_id)])) == 0

    exported = tmp_path / "fitments.csv"
    assert asyncio.run(command(["export", *base, "--output", str(exported)])) == 0
    assert "00123-AB" in exported.read_text(encoding="utf-8-sig")

    backup = tmp_path / "backup.sqlite3"
    assert asyncio.run(command(["db-backup", *base, "--output", str(backup)])) == 0
    assert backup.exists()

    snapshot = tmp_path / "partsouq-current.sqlite3"
    assert asyncio.run(command(["snapshot-publish", *base, "--output", str(snapshot)])) == 0
    assert snapshot.exists()
    assert snapshot.with_name(f"{snapshot.name}.manifest.json").exists()
