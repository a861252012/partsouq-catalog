from __future__ import annotations

import asyncio
import hashlib
import sys
from collections.abc import Sequence
from typing import Any

import pymysql

from partsouq_crawler.nhtsa.client import NhtsaBulkClient
from partsouq_crawler.nhtsa.config import NhtsaConfig
from partsouq_crawler.nhtsa.datasets import DATASET_SPECS, BulkSource
from partsouq_crawler.nhtsa.models import (
    ArtifactMember,
    NhtsaRunLease,
    ParsedRecord,
    RejectedRow,
    verified_stored_artifact_path,
)
from partsouq_crawler.nhtsa.parser import BulkArtifactParser
from partsouq_crawler.nhtsa.progress import lease_heartbeat
from partsouq_crawler.nhtsa.repository import (
    BULK_PARSER_NAME,
    BULK_PARSER_VERSION,
    NhtsaLeaseLostError,
    NhtsaMySQLRepository,
)

BATCH_SIZE = 5000


class NhtsaBulkSyncService:
    def __init__(
        self,
        repository: NhtsaMySQLRepository,
        config: NhtsaConfig,
        *,
        parser: BulkArtifactParser | None = None,
    ) -> None:
        self.repository = repository
        self.config = config
        self.parser = parser or BulkArtifactParser()
        self.writer = NhtsaRecordWriter(repository)

    async def run(
        self,
        *,
        run_key: str,
        scope_name: str,
        sources: Sequence[BulkSource],
        scheduled_job_run_id: int,
    ) -> dict[str, Any]:
        lease = self.repository.start_run(
            run_key,
            scope_name,
            [source.key for source in sources],
            scheduled_job_run_id=scheduled_job_run_id,
            expected_job_name="nhtsa-bulk",
        )
        downloaded = 0
        reused = 0
        source_rows = 0
        new_versions = 0
        rejected_rows = 0
        publishable: list[tuple[str, str, int]] = []
        active_artifact_id: int | None = None
        try:
            with lease_heartbeat(self.config, lease) as check_lease:
                async with NhtsaBulkClient(self.config) as client:
                    for source in sources:
                        check_lease()
                        spec = DATASET_SPECS[source.dataset_name]
                        print(
                            f"nhtsa bulk {source.key}: checking source",
                            file=sys.stderr,
                            flush=True,
                        )
                        current = self.repository.current_artifact(source.dataset_name, source.key)
                        current_path = await asyncio.to_thread(
                            verified_stored_artifact_path,
                            current,
                            parser_name=BULK_PARSER_NAME,
                            parser_version=BULK_PARSER_VERSION,
                        )
                        conditional_current = current if current_path is not None else None
                        download = await client.download(
                            source,
                            current_artifact=conditional_current,
                        )
                        check_lease()
                        if download.reused_artifact_id is not None:
                            refreshed = self.repository.current_artifact(
                                source.dataset_name,
                                source.key,
                            )
                            refreshed_path = await asyncio.to_thread(
                                verified_stored_artifact_path,
                                refreshed,
                                parser_name=BULK_PARSER_NAME,
                                parser_version=BULK_PARSER_VERSION,
                            )
                            if (
                                refreshed is None
                                or refreshed_path is None
                                or int(str(refreshed["id"])) != download.reused_artifact_id
                            ):
                                raise ValueError(
                                    f"{source.key} current artifact failed 304 revalidation"
                                )
                            artifact_id = download.reused_artifact_id
                            reused += 1
                            publishable.append((source.dataset_name, source.key, artifact_id))
                            source_rows += int(str(refreshed["source_rows"]))
                            print(
                                f"nhtsa bulk {source.key}: reused current artifact",
                                file=sys.stderr,
                                flush=True,
                            )
                            continue

                        if download.sha256 is None or download.path is None:
                            raise ValueError(f"{source.key} download has no content")
                        existing = self.repository.artifact_by_content(
                            source.dataset_name,
                            source.key,
                            download.sha256,
                            BULK_PARSER_VERSION,
                        )
                        if existing and existing["status"] == "imported":
                            reused += 1
                            artifact_id = int(str(existing["id"]))
                            self.repository.refresh_artifact_storage(
                                lease,
                                artifact_id,
                                download,
                            )
                            source_rows += int(str(existing["source_rows"]))
                            publishable.append((source.dataset_name, source.key, artifact_id))
                            print(
                                f"nhtsa bulk {source.key}: reused imported artifact",
                                file=sys.stderr,
                                flush=True,
                            )
                            continue
                        if existing and existing["status"] == "quarantined":
                            raise ValueError(
                                f"{source.key} content is quarantined for parser version "
                                f"{existing['parser_version']}: {existing['error_message']}"
                            )

                        artifact_id = self.repository.create_artifact(
                            lease,
                            dataset_name=source.dataset_name,
                            source_key=source.key,
                            source_url=source.url,
                            download=download,
                            parser_name=BULK_PARSER_NAME,
                            parser_version=BULK_PARSER_VERSION,
                        )
                        active_artifact_id = artifact_id
                        downloaded += 1
                        member = self.parser.inspect(download.path, source, spec)
                        current_schema = self.repository.current_schema(
                            source.dataset_name, source.key
                        )
                        if current_schema is not None and current_schema != member.schema_sha256:
                            raise ValueError(
                                f"schema drift for {source.key}: "
                                f"{current_schema} -> {member.schema_sha256}"
                            )
                        self.repository.store_member(lease, artifact_id, member)
                        self.repository.reset_artifact_import(lease, artifact_id)
                        artifact_source_rows, artifact_new_versions, artifact_rejected = (
                            self._import_artifact(
                                lease,
                                artifact_id,
                                download.path,
                                source,
                                member,
                            )
                        )
                        self.repository.complete_artifact(
                            lease,
                            artifact_id,
                            source_rows=artifact_source_rows,
                            new_versions=artifact_new_versions,
                            rejected_rows=artifact_rejected,
                        )
                        source_rows += artifact_source_rows
                        new_versions += artifact_new_versions
                        rejected_rows += artifact_rejected
                        if artifact_rejected:
                            raise ValueError(
                                f"{source.key} rejected {artifact_rejected} of "
                                f"{artifact_source_rows} source rows"
                            )
                        publishable.append((source.dataset_name, source.key, artifact_id))
                        active_artifact_id = None

                check_lease()
            self.repository.complete_run_and_publish_artifacts(
                lease,
                publishable,
                downloaded=downloaded,
                reused=reused,
                source_rows=source_rows,
                new_versions=new_versions,
                rejected_rows=rejected_rows,
            )
            return {
                "run_id": lease.id,
                "run_key": run_key,
                "scope": scope_name,
                "status": "completed",
                "artifacts_downloaded": downloaded,
                "artifacts_reused": reused,
                "source_rows": source_rows,
                "new_versions": new_versions,
                "rejected_rows": rejected_rows,
                "published_sources": len(publishable),
            }
        except asyncio.CancelledError:
            try:
                self.repository.finish_run(
                    lease,
                    status="interrupted",
                    downloaded=downloaded,
                    reused=reused,
                    source_rows=source_rows,
                    new_versions=new_versions,
                    rejected_rows=rejected_rows,
                    error_message="sync interrupted",
                )
            except Exception as finish_error:
                print(
                    f"nhtsa bulk cancellation cleanup failed: {finish_error}",
                    file=sys.stderr,
                    flush=True,
                )
            raise
        except Exception as error:
            if active_artifact_id is not None and not isinstance(error, NhtsaLeaseLostError):
                try:
                    self.repository.quarantine_artifact(lease, active_artifact_id, str(error))
                except Exception as quarantine_error:
                    print(
                        f"nhtsa bulk quarantine cleanup failed: {quarantine_error}",
                        file=sys.stderr,
                        flush=True,
                    )
            if not isinstance(error, NhtsaLeaseLostError):
                try:
                    self.repository.finish_run(
                        lease,
                        status="failed",
                        downloaded=downloaded,
                        reused=reused,
                        source_rows=source_rows,
                        new_versions=new_versions,
                        rejected_rows=rejected_rows,
                        error_message=f"{type(error).__name__}: {error}",
                    )
                except Exception as finish_error:
                    print(
                        f"nhtsa bulk terminal cleanup failed: {finish_error}",
                        file=sys.stderr,
                        flush=True,
                    )
            return {
                "run_id": lease.id,
                "run_key": run_key,
                "scope": scope_name,
                "status": "failed",
                "error_type": type(error).__name__,
                "error": str(error),
                "artifacts_downloaded": downloaded,
                "artifacts_reused": reused,
                "source_rows": source_rows,
                "new_versions": new_versions,
                "rejected_rows": rejected_rows,
                "published_sources": 0,
            }

    def _import_artifact(
        self,
        lease: NhtsaRunLease,
        artifact_id: int,
        path: Any,
        source: BulkSource,
        member: ArtifactMember,
    ) -> tuple[int, int, int]:
        spec = DATASET_SPECS[source.dataset_name]
        records: list[ParsedRecord] = []
        rejections: list[RejectedRow] = []
        source_rows = 0
        new_versions = 0
        rejected_rows = 0
        for item in self.parser.iter_records(path, source, spec, member):
            source_rows += 1
            if isinstance(item, RejectedRow):
                rejections.append(item)
                if len(rejections) >= BATCH_SIZE:
                    self.repository.insert_rejections(lease, artifact_id, rejections)
                    rejected_rows += len(rejections)
                    rejections.clear()
                continue
            records.append(item)
            if len(records) >= BATCH_SIZE:
                added, rejected = self.writer.insert(lease, artifact_id, records)
                new_versions += added
                rejected_rows += rejected
                records.clear()
                print(
                    f"nhtsa bulk {source.key}: processed {source_rows} rows",
                    file=sys.stderr,
                    flush=True,
                )
        if records:
            added, rejected = self.writer.insert(lease, artifact_id, records)
            new_versions += added
            rejected_rows += rejected
            print(
                f"nhtsa bulk {source.key}: processed {source_rows} rows",
                file=sys.stderr,
                flush=True,
            )
        if rejections:
            self.repository.insert_rejections(lease, artifact_id, rejections)
            rejected_rows += len(rejections)
        return source_rows, new_versions, rejected_rows


class NhtsaRecordWriter:
    def __init__(self, repository: NhtsaMySQLRepository) -> None:
        self.repository = repository

    def insert(
        self,
        lease: NhtsaRunLease,
        artifact_id: int,
        records: Sequence[ParsedRecord],
    ) -> tuple[int, int]:
        try:
            return self.repository.insert_records(lease, artifact_id, records), 0
        except pymysql.err.IntegrityError:
            new_versions = 0
            rejected: list[RejectedRow] = []
            for record in records:
                try:
                    new_versions += self.repository.insert_records(lease, artifact_id, [record])
                except pymysql.err.IntegrityError as error:
                    rejected.append(
                        RejectedRow(
                            member_name=record.member_name,
                            source_line=record.source_line,
                            raw_sha256=hashlib.sha256(record.payload_json.encode()).hexdigest(),
                            error_type="DuplicateNaturalKey",
                            error_message=str(error),
                            raw_text=record.payload_json,
                        )
                    )
            self.repository.insert_rejections(lease, artifact_id, rejected)
            return new_versions, len(rejected)
