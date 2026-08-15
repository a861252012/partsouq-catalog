from __future__ import annotations

import hashlib
import os
import ssl
import tempfile
from pathlib import Path

import aiofiles
import aiohttp
import certifi

from partsouq_crawler.nhtsa.config import NhtsaConfig
from partsouq_crawler.nhtsa.datasets import BulkSource
from partsouq_crawler.nhtsa.models import DownloadedArtifact


class NhtsaDownloadError(RuntimeError):
    pass


class NhtsaBulkClient:
    def __init__(self, config: NhtsaConfig) -> None:
        self.config = config
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> NhtsaBulkClient:
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        timeout = aiohttp.ClientTimeout(total=self.config.request_timeout_seconds)
        self.session = aiohttp.ClientSession(
            timeout=timeout,
            connector=aiohttp.TCPConnector(ssl=ssl_context),
            headers={"User-Agent": self.config.user_agent, "Accept": "*/*"},
        )
        return self

    async def __aexit__(self, *_args: object) -> None:
        if self.session is not None:
            await self.session.close()

    async def download(
        self,
        source: BulkSource,
        *,
        current_artifact: dict[str, object] | None,
    ) -> DownloadedArtifact:
        if self.session is None:
            raise RuntimeError("NHTSA client is not open")
        headers: dict[str, str] = {}
        if current_artifact:
            if current_artifact.get("etag"):
                headers["If-None-Match"] = str(current_artifact["etag"])
            if current_artifact.get("last_modified"):
                headers["If-Modified-Since"] = str(current_artifact["last_modified"])

        target_dir = self.config.raw_dir / source.dataset_name
        target_dir.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            async with self.session.get(source.url, headers=headers) as response:
                response_headers = {key.lower(): value for key, value in response.headers.items()}
                if response.status == 304:
                    if current_artifact is None:
                        raise NhtsaDownloadError("received 304 without a current artifact")
                    return DownloadedArtifact(
                        http_status=304,
                        response_headers=response_headers,
                        path=None,
                        sha256=None,
                        byte_count=0,
                        reused_artifact_id=int(str(current_artifact["id"])),
                    )
                if response.status != 200:
                    raise NhtsaDownloadError(
                        f"{source.key} returned HTTP {response.status} from {source.url}"
                    )

                descriptor, name = tempfile.mkstemp(prefix=".nhtsa-download-", dir=target_dir)
                os.close(descriptor)
                temp_path = Path(name)
                digest = hashlib.sha256()
                byte_count = 0
                async with aiofiles.open(temp_path, "wb") as output:
                    async for chunk in response.content.iter_chunked(1024 * 1024):
                        await output.write(chunk)
                        digest.update(chunk)
                        byte_count += len(chunk)
                    await output.flush()
                if byte_count == 0:
                    raise NhtsaDownloadError(f"{source.key} downloaded an empty response")

                sha256 = digest.hexdigest()
                suffix = ".zip" if source.is_zip else Path(source.expected_member).suffix
                final_path = target_dir / f"{sha256}{suffix}"
                if final_path.exists():
                    temp_path.unlink()
                else:
                    os.replace(temp_path, final_path)
                temp_path = None
                return DownloadedArtifact(
                    http_status=200,
                    response_headers=response_headers,
                    path=final_path,
                    sha256=sha256,
                    byte_count=byte_count,
                )
        except (aiohttp.ClientError, TimeoutError, OSError) as error:
            raise NhtsaDownloadError(f"failed to download {source.key}: {error}") from error
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
