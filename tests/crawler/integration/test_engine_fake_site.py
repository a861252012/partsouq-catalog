import asyncio
import gzip
import os
import signal
from collections import Counter
from pathlib import Path

from aiohttp import web

from partsouq_crawler.db.repository import Repository

from ..helpers import crawl_fake, fake_site, robots_response


def test_1000_unique_catalog_urls_complete_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        requests: Counter[str] = Counter()

        async def handler(request: web.Request) -> web.Response:
            requests[request.path] += 1
            if request.path == "/robots.txt":
                return robots_response()
            if request.path == "/":
                links = "".join(f'<a href="/p/{index}">{index}</a>' for index in range(1, 1000))
                return web.Response(text=f"<html>{links}</html>", content_type="text/html")
            return web.Response(text="<html><h1>Catalog page</h1></html>", content_type="text/html")

        async with fake_site(handler) as base:
            code, status = await crawl_fake(
                tmp_path / "thousand.sqlite3", f"{base}/", concurrency=4
            )
        assert code == 0
        assert status["queue"]["done"] == 1000
        assert status["queue"]["pending"] == 0
        assert status["strict_complete"] is True
        assert len([path for path in requests if path != "/robots.txt"]) == 1000
        assert all(count == 1 for path, count in requests.items() if path != "/robots.txt")

    asyncio.run(scenario())


def test_resume_500_then_1000_does_not_refetch_done(tmp_path: Path) -> None:
    async def scenario() -> None:
        requests: Counter[str] = Counter()

        async def handler(request: web.Request) -> web.Response:
            requests[request.path] += 1
            if request.path == "/robots.txt":
                return robots_response()
            if request.path == "/":
                links = "".join(f'<a href="/p/{index}">{index}</a>' for index in range(1, 1000))
                return web.Response(text=f"<html>{links}</html>", content_type="text/html")
            return web.Response(text="<html>page</html>", content_type="text/html")

        database = tmp_path / "resume.sqlite3"
        async with fake_site(handler) as base:
            first_code, first = await crawl_fake(
                database, f"{base}/", run_key="resume", max_pages=500
            )
            assert first_code == 0
            assert first["status"] == "paused"
            assert first["queue"]["done"] == 500
            assert first["queue"]["pending"] == 500
            second_code, second = await crawl_fake(
                database, f"{base}/", run_key="resume", max_pages=0
            )
        assert second_code == 0
        assert second["queue"]["done"] == 1000
        assert second["queue"]["pending"] == 0
        assert second["strict_complete"] is True
        assert requests["/robots.txt"] == 1
        assert all(count == 1 for path, count in requests.items() if path != "/robots.txt")

    asyncio.run(scenario())


def test_duplicate_graph_fetches_each_url_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        requests: Counter[str] = Counter()

        async def handler(request: web.Request) -> web.Response:
            requests[request.path] += 1
            if request.path == "/robots.txt":
                return robots_response()
            if request.path == "/":
                return web.Response(text='<a href="/a">a</a><a href="/a">a</a><a href="/b">b</a>')
            if request.path == "/a":
                return web.Response(text='<a href="/b">b</a><a href="/">cycle</a>')
            return web.Response(text="done")

        async with fake_site(handler) as base:
            _, status = await crawl_fake(tmp_path / "duplicate.sqlite3", f"{base}/")
        assert status["queue"]["done"] == 3
        assert requests["/"] == requests["/a"] == requests["/b"] == 1

    asyncio.run(scenario())


def test_cloudflare_challenge_blocks_and_preserves_body(tmp_path: Path) -> None:
    async def scenario() -> None:
        body = b"<title>Just a moment...</title>Enable JavaScript and cookies to continue"

        async def handler(request: web.Request) -> web.Response:
            if request.path == "/robots.txt":
                return robots_response()
            return web.Response(
                body=body,
                status=403,
                headers={"cf-mitigated": "challenge", "server": "cloudflare"},
                content_type="text/html",
            )

        database = tmp_path / "challenge.sqlite3"
        async with fake_site(handler) as base:
            code, status = await crawl_fake(database, f"{base}/")
        assert code == 2
        assert status["status"] == "blocked"
        assert status["queue"]["challenged"] == 1
        assert status["strict_complete"] is False
        repository = await Repository.create(database)
        rows = await repository.find_responses()
        challenge_rows = [row for row in rows if row["is_cloudflare_challenge"]]
        assert len(challenge_rows) == 1
        assert repository.restore_body(challenge_rows[0]) == body
        await repository.close()

    asyncio.run(scenario())


def test_parser_failure_is_terminal_gap(tmp_path: Path) -> None:
    async def scenario() -> None:
        async def handler(request: web.Request) -> web.Response:
            if request.path == "/robots.txt":
                return robots_response()
            return web.Response(text="<html><h1>Parts</h1></html>", content_type="text/html")

        database = tmp_path / "parse-failure.sqlite3"
        async with fake_site(handler) as base:
            code, status = await crawl_fake(database, f"{base}/en/catalog/genuine/parts")
        assert code == 1
        assert status["status"] == "completed_with_gaps"
        assert status["queue"]["parse_failed"] == 1
        repository = await Repository.create(database)
        assert (await repository.table_counts())["parse_failures"] == 1
        await repository.close()

    asyncio.run(scenario())


def test_http_500_retries_then_completes(tmp_path: Path, monkeypatch) -> None:
    async def scenario() -> None:
        requests: Counter[str] = Counter()

        async def handler(request: web.Request) -> web.Response:
            requests[request.path] += 1
            if request.path == "/robots.txt":
                return robots_response()
            if requests[request.path] == 1:
                return web.Response(status=500, text="temporary")
            return web.Response(text="<html>ok</html>", content_type="text/html")

        monkeypatch.setattr(
            "partsouq_crawler.crawl.engine.retry_delay", lambda *_args, **_kwargs: 0
        )
        database = tmp_path / "retry.sqlite3"
        async with fake_site(handler) as base:
            code, status = await crawl_fake(database, f"{base}/", max_retries=1)
        assert code == 0 and status["queue"]["done"] == 1
        assert requests["/"] == 2
        repository = await Repository.create(database)
        rows = await repository.find_responses()
        root_statuses = [
            row["http_status"]
            for row in rows
            if row["requested_url"].rstrip("/").endswith(str(base).split(":")[-1])
        ]
        assert root_statuses == [500, 200]
        await repository.close()

    asyncio.run(scenario())


def test_http_429_pauses_and_respects_retry_after(tmp_path: Path) -> None:
    async def scenario() -> None:
        async def handler(request: web.Request) -> web.Response:
            if request.path == "/robots.txt":
                return robots_response()
            return web.Response(status=429, text="slow down", headers={"Retry-After": "0"})

        async with fake_site(handler) as base:
            code, status = await crawl_fake(tmp_path / "rate.sqlite3", f"{base}/")
        assert code == 0
        assert status["status"] == "paused"
        assert status["queue"]["pending"] == 1
        assert status["strict_complete"] is False

    asyncio.run(scenario())


def test_http_404_is_gone_terminal_and_strict_complete(tmp_path: Path) -> None:
    async def scenario() -> None:
        async def handler(request: web.Request) -> web.Response:
            if request.path == "/robots.txt":
                return robots_response()
            return web.Response(status=404, text="gone")

        async with fake_site(handler) as base:
            code, status = await crawl_fake(tmp_path / "gone.sqlite3", f"{base}/missing")
        assert code == 0
        assert status["queue"]["gone"] == 1
        assert status["strict_complete"] is True

    asyncio.run(scenario())


def test_robots_unavailable_blocks_without_catalog_request(tmp_path: Path) -> None:
    async def scenario() -> None:
        requests: Counter[str] = Counter()

        async def handler(request: web.Request) -> web.Response:
            requests[request.path] += 1
            return web.Response(status=503, text="unavailable")

        async with fake_site(handler) as base:
            code, status = await crawl_fake(tmp_path / "robots.sqlite3", f"{base}/")
        assert code == 2
        assert status["status"] == "blocked"
        assert status["blocked_reason"] == "robots_unavailable"
        assert requests == Counter({"/robots.txt": 1})

    asyncio.run(scenario())


def test_nested_and_gzip_sitemaps_discover_catalog(tmp_path: Path) -> None:
    async def scenario() -> None:
        async def handler(request: web.Request) -> web.Response:
            origin = f"http://{request.host}"
            if request.path == "/robots.txt":
                return web.Response(
                    text=f"User-agent: *\nAllow: /\nSitemap: {origin}/sitemap.xml\n"
                )
            if request.path == "/sitemap.xml":
                body = (
                    f"<sitemapindex><sitemap><loc>{origin}/nested.xml.gz</loc>"
                    "</sitemap></sitemapindex>"
                ).encode()
                return web.Response(body=body, content_type="application/xml")
            if request.path == "/nested.xml.gz":
                body = gzip.compress(
                    f"<urlset><url><loc>{origin}/catalog</loc></url></urlset>".encode()
                )
                return web.Response(body=body, content_type="application/gzip")
            return web.Response(text="<html>catalog</html>", content_type="text/html")

        async with fake_site(handler) as base:
            code, status = await crawl_fake(tmp_path / "sitemaps.sqlite3", f"{base}/seed")
        assert code == 0
        assert status["queue"]["done"] == 4

    asyncio.run(scenario())


def test_sigint_pauses_then_resume_completes(tmp_path: Path) -> None:
    async def scenario() -> None:
        requests: Counter[str] = Counter()

        async def handler(request: web.Request) -> web.Response:
            requests[request.path] += 1
            if request.path == "/robots.txt":
                return robots_response()
            if request.path == "/":
                links = "".join(f'<a href="/p/{index}">{index}</a>' for index in range(100))
                return web.Response(text=links, content_type="text/html")
            await asyncio.sleep(0.01)
            return web.Response(text="page", content_type="text/html")

        database = tmp_path / "signal.sqlite3"
        async with fake_site(handler) as base:
            loop = asyncio.get_running_loop()
            loop.call_later(0.05, os.kill, os.getpid(), signal.SIGINT)
            first_code, first = await crawl_fake(database, f"{base}/", run_key="signal")
            assert first_code == 0 and first["status"] == "paused"
            assert first["queue"]["pending"] > 0
            second_code, second = await crawl_fake(database, f"{base}/", run_key="signal")
        assert second_code == 0
        assert second["queue"]["done"] == 101
        assert second["strict_complete"] is True
        assert all(count == 1 for path, count in requests.items() if path != "/robots.txt")

    asyncio.run(scenario())
