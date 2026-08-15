from __future__ import annotations

import asyncio
import hashlib
import os
import ssl
import tempfile
from contextlib import suppress
from pathlib import Path

import aiofiles
import aiohttp
import certifi

from partsouq_crawler.crawl.rate_limit import HostRateLimiter
from partsouq_crawler.nhtsa.api import NhtsaApiPolicy
from partsouq_crawler.nhtsa.config import NhtsaConfig
from partsouq_crawler.nhtsa.datasets import ApiSource
from partsouq_crawler.nhtsa.models import DownloadedArtifact


class NhtsaApiError(RuntimeError):
    pass


class NhtsaApiClient:
    def __init__(
        self,
        config: NhtsaConfig,
        *,
        policy: NhtsaApiPolicy | None = None,
    ) -> None:
        self.config = config
        self.policy = policy or NhtsaApiPolicy()
        self.rate_limiter = HostRateLimiter(config.api_delay_seconds)
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> NhtsaApiClient:
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        timeout = aiohttp.ClientTimeout(total=self.config.request_timeout_seconds)
        self.session = aiohttp.ClientSession(
            timeout=timeout,
            connector=aiohttp.TCPConnector(ssl=ssl_context),
            headers={"User-Agent": self.config.user_agent, "Accept": "application/json"},
        )
        return self

    async def __aexit__(self, *_args: object) -> None:
        if self.session is not None:
            await self.session.close()

    async def fetch(
        self,
        source: ApiSource,
        *,
        current_artifact: dict[str, object] | None,
    ) -> tuple[DownloadedArtifact, bytes | None]:
        self.policy.validate(source.url)
        if self.session is None:
            raise RuntimeError("NHTSA API client is not open")
        headers: dict[str, str] = {}
        if current_artifact:
            if current_artifact.get("etag"):
                headers["If-None-Match"] = str(current_artifact["etag"])
            if current_artifact.get("last_modified"):
                headers["If-Modified-Since"] = str(current_artifact["last_modified"])
        await self.rate_limiter.wait()
        try:
            async with self.session.get(source.url, headers=headers) as response:
                response_headers = {key.lower(): value for key, value in response.headers.items()}
                if response.status == 304:
                    if current_artifact is None:
                        raise NhtsaApiError("received 304 without a current API artifact")
                    return (
                        DownloadedArtifact(
                            http_status=304,
                            response_headers=response_headers,
                            path=None,
                            sha256=None,
                            byte_count=0,
                            reused_artifact_id=int(str(current_artifact["id"])),
                        ),
                        None,
                    )
                if response.status != 200:
                    raise NhtsaApiError(
                        f"{source.key} returned HTTP {response.status} from {source.url}"
                    )
                body = await response.read()
                if not body:
                    raise NhtsaApiError(f"{source.key} returned an empty response")
        except (aiohttp.ClientError, TimeoutError) as error:
            raise NhtsaApiError(f"failed to request {source.key}: {error}") from error

        sha256 = hashlib.sha256(body).hexdigest()
        target_dir = self.config.raw_dir / source.dataset_name
        target_dir.mkdir(parents=True, exist_ok=True)
        final_path = target_dir / f"{sha256}.json"
        if not final_path.exists():
            descriptor, name = tempfile.mkstemp(prefix=".nhtsa-api-", dir=target_dir)
            os.close(descriptor)
            temp_path = Path(name)
            try:
                async with aiofiles.open(temp_path, "wb") as output:
                    await output.write(body)
                    await output.flush()
                os.replace(temp_path, final_path)
            finally:
                with suppress(FileNotFoundError):
                    await asyncio.to_thread(os.unlink, temp_path)
        return (
            DownloadedArtifact(
                http_status=200,
                response_headers=response_headers,
                path=final_path,
                sha256=sha256,
                byte_count=len(body),
            ),
            body,
        )
