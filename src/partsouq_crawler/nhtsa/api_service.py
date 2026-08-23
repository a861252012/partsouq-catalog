from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from typing import Any

from partsouq_crawler.nhtsa.api import NhtsaApiParser, normalize_vin, vin_source_key
from partsouq_crawler.nhtsa.api_client import NhtsaApiClient
from partsouq_crawler.nhtsa.config import NhtsaConfig
from partsouq_crawler.nhtsa.datasets import CSSI_SOURCES, VPIC_FIXED_SOURCES, ApiSource
from partsouq_crawler.nhtsa.models import (
    ApiDocument,
    NhtsaRunLease,
    read_verified_stored_artifact,
    verified_stored_artifact_path,
)
from partsouq_crawler.nhtsa.progress import lease_heartbeat
from partsouq_crawler.nhtsa.repository import NhtsaLeaseLostError, NhtsaMySQLRepository
from partsouq_crawler.nhtsa.service import BATCH_SIZE, NhtsaRecordWriter

API_PARSER_NAME = "nhtsa_official_api_json"
API_PARSER_VERSION = "3"
# Fail-closed ceiling: 2 fixed vPIC sources + up to MAX_MANUFACTURER_PAGES
# manufacturer pages + ~150 vehicle-variable value lists + 12,340 per-make
# GetModelsForMakeId expansions (~13k total); anything beyond aborts the run.
API_REQUEST_BUDGET = 15000
MANUFACTURER_PAGE_SIZE = 100
MAX_MANUFACTURER_PAGES = 500
MODEL_EXPANSION_LOG_BATCH = 500


@dataclass(frozen=True, slots=True)
class ApiSourceImport:
    artifact_id: int
    document: ApiDocument
    downloaded: bool
    new_versions: int


class NhtsaApiSyncService:
    def __init__(
        self,
        repository: NhtsaMySQLRepository,
        config: NhtsaConfig,
        *,
        parser: NhtsaApiParser | None = None,
    ) -> None:
        self.repository = repository
        self.config = config
        self.parser = parser or NhtsaApiParser()
        self.writer = NhtsaRecordWriter(repository)
        self.request_count = 0

    async def run(
        self,
        *,
        run_key: str,
        scope_name: str,
        scheduled_job_run_id: int,
    ) -> dict[str, Any]:
        if scope_name not in {"all", "vpic", "cssi"}:
            raise ValueError(f"unsupported NHTSA API scope: {scope_name}")
        source_groups = []
        if scope_name in {"all", "vpic"}:
            source_groups.append("vpic")
        if scope_name in {"all", "cssi"}:
            source_groups.append("cssi")
        lease = self.repository.start_run(
            run_key,
            f"api-{scope_name}",
            source_groups,
            scheduled_job_run_id=scheduled_job_run_id,
            expected_job_name="nhtsa-api",
        )
        downloaded = 0
        reused = 0
        source_rows = 0
        new_versions = 0
        rejected_rows = 0
        publishable: list[tuple[str, str, int]] = []
        replace_datasets: list[str] = []
        active_artifact_id: int | None = None
        try:
            with lease_heartbeat(self.config, lease) as check_lease:
                async with NhtsaApiClient(self.config) as client:
                    if scope_name in {"all", "vpic"}:
                        variables: ApiDocument | None = None
                        makes: ApiDocument | None = None
                        for source in VPIC_FIXED_SOURCES:
                            imported = await self._sync_source(client, source, lease)
                            active_artifact_id = imported.artifact_id
                            downloaded += int(imported.downloaded)
                            reused += int(not imported.downloaded)
                            source_rows += imported.document.count
                            new_versions += imported.new_versions
                            rejected_rows += len(imported.document.rejections)
                            publishable.append(
                                (source.dataset_name, source.key, imported.artifact_id)
                            )
                            active_artifact_id = None
                            if source.dataset_name == "vpic_variables":
                                variables = imported.document
                            elif source.dataset_name == "vpic_makes":
                                makes = imported.document

                        # GetModelsForMakeYear 全量展開刻意不做：12,340 makes x ~46 年
                        # ≈ 567k requests，超出 request budget 兩個數量級。allowlist 與
                        # vpic_model_years spec 僅為抽樣決策保留（少數代表 make x 單一年份）。
                        if makes is None:
                            raise ValueError("vPIC make list was not collected")
                        total_makes = len(makes.records)
                        for offset, make_record in enumerate(makes.records, start=1):
                            make_id = make_record.external_id
                            if make_id is None or not make_id.isdigit():
                                raise ValueError(
                                    "vPIC make record has no usable Make_ID: "
                                    f"{make_record.natural_key_text}"
                                )
                            source = ApiSource(
                                key=f"vpic_models_for_make_{make_id}",
                                dataset_name="vpic_models",
                                url=(
                                    "https://vpic.nhtsa.dot.gov/api/vehicles/"
                                    f"GetModelsForMakeId/{make_id}?format=json"
                                ),
                                context=(
                                    ("Make_ID", make_id),
                                    ("Make_Name", make_record.make_name or ""),
                                ),
                            )
                            imported = await self._sync_source(client, source, lease)
                            downloaded += int(imported.downloaded)
                            reused += int(not imported.downloaded)
                            source_rows += imported.document.count
                            new_versions += imported.new_versions
                            rejected_rows += len(imported.document.rejections)
                            publishable.append(
                                (source.dataset_name, source.key, imported.artifact_id)
                            )
                            if offset % MODEL_EXPANSION_LOG_BATCH == 0:
                                print(
                                    f"nhtsa api vpic models expansion: "
                                    f"{offset}/{total_makes} makes synced",
                                    file=sys.stderr,
                                    flush=True,
                                )

                        for page in range(1, MAX_MANUFACTURER_PAGES + 1):
                            source = ApiSource(
                                key=f"vpic_manufacturers_page_{page:03d}",
                                dataset_name="vpic_manufacturers",
                                url=(
                                    "https://vpic.nhtsa.dot.gov/api/vehicles/"
                                    f"GetAllManufacturers?format=json&page={page}"
                                ),
                            )
                            imported = await self._sync_source(client, source, lease)
                            downloaded += int(imported.downloaded)
                            reused += int(not imported.downloaded)
                            source_rows += imported.document.count
                            new_versions += imported.new_versions
                            rejected_rows += len(imported.document.rejections)
                            if imported.document.count:
                                publishable.append(
                                    (source.dataset_name, source.key, imported.artifact_id)
                                )
                            if imported.document.count < MANUFACTURER_PAGE_SIZE:
                                break
                        else:
                            raise ValueError("vPIC manufacturer pagination exceeded safety limit")

                        if variables is None:
                            raise ValueError("vPIC variable list was not collected")
                        variable_ids = sorted(
                            {
                                int(record.external_id)
                                for record in variables.records
                                if record.external_id and record.external_id.isdigit()
                            }
                        )
                        for variable_id in variable_ids:
                            source = ApiSource(
                                key=f"vpic_variable_{variable_id}_values",
                                dataset_name="vpic_variable_values",
                                url=(
                                    "https://vpic.nhtsa.dot.gov/api/vehicles/"
                                    f"GetVehicleVariableValuesList/{variable_id}?format=json"
                                ),
                                context=(("Variable_ID", str(variable_id)),),
                            )
                            imported = await self._sync_source(client, source, lease)
                            downloaded += int(imported.downloaded)
                            reused += int(not imported.downloaded)
                            source_rows += imported.document.count
                            new_versions += imported.new_versions
                            rejected_rows += len(imported.document.rejections)
                            publishable.append(
                                (source.dataset_name, source.key, imported.artifact_id)
                            )
                        replace_datasets.extend(
                            (
                                "vpic_makes",
                                "vpic_models",
                                "vpic_manufacturers",
                                "vpic_variables",
                                "vpic_variable_values",
                            )
                        )

                    if scope_name in {"all", "cssi"}:
                        for source in CSSI_SOURCES:
                            imported = await self._sync_source(client, source, lease)
                            downloaded += int(imported.downloaded)
                            reused += int(not imported.downloaded)
                            source_rows += imported.document.count
                            new_versions += imported.new_versions
                            rejected_rows += len(imported.document.rejections)
                            publishable.append(
                                (source.dataset_name, source.key, imported.artifact_id)
                            )
                        replace_datasets.append("cssi_stations")

                if rejected_rows:
                    raise ValueError(f"NHTSA API sync rejected {rejected_rows} records")
                check_lease()
            self.repository.complete_run_and_publish_artifacts(
                lease,
                publishable,
                replace_datasets=replace_datasets,
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
                "api_requests": self.request_count,
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
                    f"nhtsa api cancellation cleanup failed: {finish_error}",
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
                        f"nhtsa api quarantine cleanup failed: {quarantine_error}",
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
                        f"nhtsa api terminal cleanup failed: {finish_error}",
                        file=sys.stderr,
                        flush=True,
                    )
            return {
                "run_id": lease.id,
                "run_key": run_key,
                "scope": scope_name,
                "status": "failed",
                "api_requests": self.request_count,
                "error_type": type(error).__name__,
                "error": str(error),
                "artifacts_downloaded": downloaded,
                "artifacts_reused": reused,
                "source_rows": source_rows,
                "new_versions": new_versions,
                "rejected_rows": rejected_rows,
                "published_sources": 0,
            }

    async def decode_vin(
        self,
        *,
        run_key: str,
        vin: str,
        scheduled_job_run_id: int,
    ) -> dict[str, Any]:
        normalized_vin = normalize_vin(vin)
        source = ApiSource(
            key=vin_source_key(normalized_vin),
            dataset_name="vpic_vin_decodes",
            url=(
                "https://vpic.nhtsa.dot.gov/api/vehicles/"
                f"DecodeVinValues/{normalized_vin}?format=json"
            ),
        )
        lease = self.repository.start_run(
            run_key,
            "api-vin",
            (source.key,),
            scheduled_job_run_id=scheduled_job_run_id,
            expected_job_name="nhtsa-vin",
        )
        downloaded = 0
        reused = 0
        source_rows = 0
        new_versions = 0
        rejected_rows = 0
        artifact_id: int | None = None
        try:
            with lease_heartbeat(self.config, lease) as check_lease:
                async with NhtsaApiClient(self.config) as client:
                    imported = await self._sync_source(client, source, lease)
                artifact_id = imported.artifact_id
                document = imported.document
                downloaded = int(imported.downloaded)
                reused = int(not imported.downloaded)
                source_rows = document.count
                new_versions = imported.new_versions
                rejected_rows = len(document.rejections)
                if document.count != 1 or len(document.records) != 1 or document.rejections:
                    raise ValueError(
                        "NHTSA VIN decode must return exactly one valid result; "
                        f"count={document.count}, records={len(document.records)}, "
                        f"rejections={len(document.rejections)}"
                    )
                record = document.records[0]
                payload = json.loads(record.payload_json)
                if (
                    not isinstance(payload, dict)
                    or str(payload.get("VIN") or "").upper() != normalized_vin
                ):
                    raise ValueError("NHTSA VIN decode response does not match the requested VIN")
                check_lease()
            vehicle = self.repository.complete_run_and_publish_vin_decode(
                lease,
                imported.artifact_id,
                normalized_vin,
                payload,
                downloaded=downloaded,
                reused=reused,
                source_rows=source_rows,
                new_versions=new_versions,
                rejected_rows=rejected_rows,
            )
            return {
                "run_id": lease.id,
                "run_key": run_key,
                "scope": "vin",
                "status": "completed",
                "api_requests": self.request_count,
                "artifacts_downloaded": downloaded,
                "artifacts_reused": reused,
                "source_rows": source_rows,
                "new_versions": new_versions,
                "rejected_rows": rejected_rows,
                "vehicle": vehicle,
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
                    error_message="VIN decode interrupted",
                )
            except Exception as finish_error:
                print(
                    f"nhtsa VIN cancellation cleanup failed: {finish_error}",
                    file=sys.stderr,
                    flush=True,
                )
            raise
        except Exception as error:
            if artifact_id is not None and not isinstance(error, NhtsaLeaseLostError):
                try:
                    self.repository.quarantine_artifact(
                        lease,
                        artifact_id,
                        str(error),
                        only_if_unpublished=True,
                    )
                except Exception as quarantine_error:
                    print(
                        f"nhtsa VIN quarantine cleanup failed: {quarantine_error}",
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
                        f"nhtsa VIN terminal cleanup failed: {finish_error}",
                        file=sys.stderr,
                        flush=True,
                    )
            return {
                "run_id": lease.id,
                "run_key": run_key,
                "scope": "vin",
                "status": "failed",
                "api_requests": self.request_count,
                "error_type": type(error).__name__,
                "error": str(error),
                "artifacts_downloaded": downloaded,
                "artifacts_reused": reused,
                "source_rows": source_rows,
                "new_versions": new_versions,
                "rejected_rows": rejected_rows,
            }

    async def _sync_source(
        self,
        client: NhtsaApiClient,
        source: ApiSource,
        lease: NhtsaRunLease,
    ) -> ApiSourceImport:
        self.request_count += 1
        if self.request_count > API_REQUEST_BUDGET:
            raise ValueError(f"NHTSA API request budget exceeded ({API_REQUEST_BUDGET})")
        current = self.repository.current_artifact(source.dataset_name, source.key)
        current_path = await asyncio.to_thread(
            verified_stored_artifact_path,
            current,
            parser_name=API_PARSER_NAME,
            parser_version=API_PARSER_VERSION,
        )
        conditional_current = current if current_path is not None else None
        download, body = await client.fetch(source, current_artifact=conditional_current)
        if download.reused_artifact_id is not None:
            refreshed = self.repository.current_artifact(source.dataset_name, source.key)
            body = await asyncio.to_thread(
                read_verified_stored_artifact,
                refreshed,
                parser_name=API_PARSER_NAME,
                parser_version=API_PARSER_VERSION,
            )
            if (
                refreshed is None
                or body is None
                or int(str(refreshed["id"])) != download.reused_artifact_id
            ):
                raise ValueError(f"{source.key} current API artifact failed 304 revalidation")
            document = self.parser.parse(body, source)
            return ApiSourceImport(download.reused_artifact_id, document, False, 0)
        if download.sha256 is None or download.path is None or body is None:
            raise ValueError(f"{source.key} API download has no content")
        existing = self.repository.artifact_by_content(
            source.dataset_name,
            source.key,
            download.sha256,
            API_PARSER_VERSION,
        )
        if existing and existing["status"] == "imported":
            self.repository.refresh_artifact_storage(
                lease,
                int(str(existing["id"])),
                download,
            )
            document = self.parser.parse(body, source)
            return ApiSourceImport(int(str(existing["id"])), document, False, 0)
        if existing and existing["status"] == "quarantined":
            raise ValueError(
                f"{source.key} API content is quarantined: {existing['error_message']}"
            )
        artifact_id = self.repository.create_artifact(
            lease,
            dataset_name=source.dataset_name,
            source_key=source.key,
            source_url=source.url,
            download=download,
            parser_name=API_PARSER_NAME,
            parser_version=API_PARSER_VERSION,
        )
        try:
            document = self.parser.parse(body, source)
            current_schema = self.repository.current_schema(source.dataset_name, source.key)
            current_rows = int(str(current["source_rows"])) if current else 0
            if (
                current_schema is not None
                and current_rows > 0
                and document.count > 0
                and current_schema != document.member.schema_sha256
            ):
                raise ValueError(
                    f"API schema drift for {source.key}: "
                    f"{current_schema} -> {document.member.schema_sha256}"
                )
            self.repository.store_member(lease, artifact_id, document.member)
            self.repository.reset_artifact_import(lease, artifact_id)
            new_versions = 0
            duplicate_rejections = 0
            records = list(document.records)
            for index in range(0, len(records), BATCH_SIZE):
                added, rejected = self.writer.insert(
                    lease,
                    artifact_id,
                    records[index : index + BATCH_SIZE],
                )
                new_versions += added
                duplicate_rejections += rejected
            self.repository.insert_rejections(lease, artifact_id, document.rejections)
            total_rejections = duplicate_rejections + len(document.rejections)
            self.repository.complete_artifact(
                lease,
                artifact_id,
                source_rows=document.count,
                new_versions=new_versions,
                rejected_rows=total_rejections,
            )
            if total_rejections:
                raise ValueError(f"{source.key} rejected {total_rejections} API records")
            return ApiSourceImport(artifact_id, document, True, new_versions)
        except Exception as error:
            try:
                self.repository.quarantine_artifact(lease, artifact_id, str(error))
            except Exception as quarantine_error:
                print(
                    f"nhtsa API source quarantine cleanup failed: {quarantine_error}",
                    file=sys.stderr,
                    flush=True,
                )
            raise
