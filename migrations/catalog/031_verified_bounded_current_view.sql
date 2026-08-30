-- 031_verified_bounded_current_view.sql
--
-- full crawl 尚未保存等同 bounded 的 live HTTP evidence。它可以保留在
-- normalized / published candidate tables 供日後重建，但不可作為後台或 VIN
-- mapping 的正式資料來源，也不可壓過已驗證的 desired bounded snapshot。

SET SESSION lock_wait_timeout = 30;
SET SESSION innodb_lock_wait_timeout = 30;

DROP PROCEDURE IF EXISTS assert_partsouq_031_preflight;
DELIMITER //
CREATE PROCEDURE assert_partsouq_031_preflight()
BEGIN
  IF DATABASE() IS NULL OR (
    SELECT COUNT(*) FROM information_schema.TABLES
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'
      AND TABLE_NAME IN (
        'bounded_parts', 'crawl_runs', 'scheduled_job_runs',
        'partsouq_response_bodies', 'partsouq_http_artifacts',
        'partsouq_artifact_records', 'catalog_desired_bounded_scope'
      )
  ) <> 7 OR NOT EXISTS (
    SELECT 1 FROM information_schema.VIEWS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'v_current_catalog_parts'
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 031: apply catalog migrations through 030 first';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_031_preflight();
DROP PROCEDURE assert_partsouq_031_preflight;

CREATE OR REPLACE VIEW v_current_catalog_parts AS
WITH verified_bounded_evidence AS (
SELECT
    artifact.crawl_run_id,
    COUNT(*) AS artifact_count,
    SUM(
        artifact.capture_kind = 'live_http'
        AND evidence_job.job_name = 'catalog'
        AND evidence_job.trigger_mode = 'daemon'
        AND evidence_job.finished_at IS NOT NULL
        AND artifact.fetched_at >= evidence_run.started_at
        AND artifact.fetched_at >= evidence_job.started_at
        AND artifact.fetched_at <= evidence_job.finished_at + INTERVAL 5 MINUTE
        AND (
          (
            artifact.scheduled_job_run_id = evidence_run.scheduled_job_run_id
            AND evidence_job.status = 'completed'
            AND evidence_job.exit_code = 0
          )
          OR (
            artifact.scheduled_job_run_id <> evidence_run.scheduled_job_run_id
            AND evidence_job.status = 'failed'
            AND evidence_job.exit_code IS NOT NULL
            AND evidence_job.exit_code <> 0
          )
        )
        AND artifact.http_status = 200
        AND artifact.challenge_detected = 0
        AND LOWER(artifact.content_type) LIKE 'text/html%'
        AND artifact.malformed_row_count = 0
        AND artifact.verified_at IS NOT NULL
    ) AS live_artifact_count,
    COUNT(DISTINCT artifact.page_type) AS page_type_count,
    SUM(artifact.accepted_record_count) AS accepted_record_count,
    SUM(body.original_bytes) AS original_bytes,
    SUM(body.stored_bytes) AS stored_bytes
FROM (
    SELECT DISTINCT crawl_run_id
    FROM bounded_parts
) AS active_snapshot
STRAIGHT_JOIN partsouq_http_artifacts AS artifact
  FORCE INDEX (idx_partsouq_artifact_run_status)
  ON artifact.crawl_run_id = active_snapshot.crawl_run_id
 AND artifact.verification_status = 'verified'
STRAIGHT_JOIN crawl_runs AS evidence_run
  ON evidence_run.id = artifact.crawl_run_id
STRAIGHT_JOIN scheduled_job_runs AS evidence_job
  ON evidence_job.id = artifact.scheduled_job_run_id
STRAIGHT_JOIN partsouq_response_bodies AS body
  ON body.body_sha256 = artifact.body_sha256
GROUP BY artifact.crawl_run_id
),
verified_bounded_records AS (
SELECT
    record.crawl_run_id,
    COUNT(*) AS accepted_record_count,
    COUNT(DISTINCT record.part_id) AS accepted_part_count
FROM (
    SELECT DISTINCT crawl_run_id
    FROM bounded_parts
) AS active_record_snapshot
STRAIGHT_JOIN partsouq_artifact_records AS record
  FORCE INDEX (idx_partsouq_record_run_accepted)
  ON record.crawl_run_id = active_record_snapshot.crawl_run_id
 AND record.accepted = 1
STRAIGHT_JOIN partsouq_http_artifacts AS artifact
  ON artifact.id = record.artifact_id
 AND artifact.crawl_run_id = record.crawl_run_id
 AND artifact.verification_status = 'verified'
 AND artifact.capture_kind = 'live_http'
STRAIGHT_JOIN bounded_parts AS evidence_part
  ON evidence_part.crawl_run_id = record.crawl_run_id
 AND evidence_part.part_id = record.part_id
WHERE record.record_type = 'part'
GROUP BY record.crawl_run_id
)
SELECT
    'bounded' AS dataset_scope,
    bounded_parts.crawl_run_id AS source_crawl_run_id,
    bounded_parts.part_id, bounded_parts.vehicle_id, bounded_parts.model_id,
    bounded_parts.vehicle_vid, bounded_parts.brand, bounded_parts.model,
    bounded_parts.vehicle_name, bounded_parts.vehicle_code, bounded_parts.prod_period,
    bounded_parts.production_from, bounded_parts.production_to, bounded_parts.engine,
    bounded_parts.trim_name, bounded_parts.part_name, bounded_parts.part_number,
    bounded_parts.part_number_normalized, bounded_parts.category_id,
    bounded_parts.category_cid, bounded_parts.category_main,
    bounded_parts.category_group, bounded_parts.group_id, bounded_parts.group_code,
    bounded_parts.group_uid, bounded_parts.part_range, bounded_parts.part_from,
    bounded_parts.part_to, bounded_parts.source_url, bounded_parts.note,
    bounded_parts.quantity, bounded_parts.code, bounded_parts.snapshot_at
FROM bounded_parts
JOIN crawl_runs AS current_run
  ON current_run.id = bounded_parts.crawl_run_id
 AND current_run.dataset_kind = 'bounded'
 AND current_run.status = 'bounded_success'
 AND current_run.target_parts = 10000
 AND current_run.parts_ok = 10000
 AND current_run.finished_at IS NOT NULL
 AND current_run.error_msg IS NULL
 AND current_run.evidence_status = 'verified'
 AND current_run.evidence_manifest_sha256 REGEXP '^[0-9a-f]{64}$'
 AND current_run.evidence_dataset_sha256 REGEXP '^[0-9a-f]{64}$'
 AND current_run.evidence_artifact_count > 0
 AND current_run.evidence_record_count = 10000
 AND current_run.evidence_original_bytes > 0
 AND current_run.evidence_stored_bytes > 0
 AND current_run.evidence_verified_at IS NOT NULL
JOIN catalog_desired_bounded_scope AS desired_scope
  ON desired_scope.singleton_id = 1
 AND CAST(current_run.scope_brand AS BINARY) = CAST(desired_scope.scope_brand AS BINARY)
 AND CAST(current_run.scope_model AS BINARY) = CAST(desired_scope.scope_model AS BINARY)
 AND current_run.scope_vehicle_year_floor = desired_scope.scope_vehicle_year_floor
JOIN verified_bounded_evidence AS verified_evidence
  ON verified_evidence.crawl_run_id = current_run.id
 AND verified_evidence.artifact_count = current_run.evidence_artifact_count
 AND verified_evidence.live_artifact_count = verified_evidence.artifact_count
 AND verified_evidence.page_type_count = 6
 AND verified_evidence.accepted_record_count = current_run.evidence_record_count
 AND verified_evidence.original_bytes = current_run.evidence_original_bytes
 AND verified_evidence.stored_bytes = current_run.evidence_stored_bytes
JOIN verified_bounded_records AS verified_records
  ON verified_records.crawl_run_id = current_run.id
 AND verified_records.accepted_record_count = current_run.evidence_record_count
 AND verified_records.accepted_part_count = current_run.evidence_record_count
JOIN scheduled_job_runs AS scheduler_run
  ON scheduler_run.id = current_run.scheduled_job_run_id
 AND scheduler_run.job_name = 'catalog'
 AND scheduler_run.trigger_mode = 'daemon'
 AND scheduler_run.status = 'completed'
 AND scheduler_run.finished_at IS NOT NULL
 AND scheduler_run.exit_code = 0
JOIN (
    SELECT scheduled_job_run_id, COUNT(*) AS linked_crawl_runs
    FROM crawl_runs
    WHERE scheduled_job_run_id IS NOT NULL
    GROUP BY scheduled_job_run_id
) AS scheduler_links
  ON scheduler_links.scheduled_job_run_id = scheduler_run.id
 AND scheduler_links.linked_crawl_runs = 1
JOIN (
    SELECT COUNT(*) AS row_count,
           MIN(crawl_run_id) AS min_crawl_run_id,
           MAX(crawl_run_id) AS max_crawl_run_id
    FROM bounded_parts
) AS snapshot
  ON snapshot.row_count = 10000
 AND snapshot.min_crawl_run_id = current_run.id
 AND snapshot.max_crawl_run_id = current_run.id;

DROP PROCEDURE IF EXISTS assert_partsouq_031_output;
DELIMITER //
CREATE PROCEDURE assert_partsouq_031_output()
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.VIEWS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'v_current_catalog_parts'
      AND LOCATE('bounded_parts', LOWER(VIEW_DEFINITION)) > 0
      AND LOCATE('verified_bounded_evidence', LOWER(VIEW_DEFINITION)) > 0
      AND LOCATE('verified_bounded_records', LOWER(VIEW_DEFINITION)) > 0
      AND LOCATE('catalog_desired_bounded_scope', LOWER(VIEW_DEFINITION)) > 0
      AND LOCATE('formal_full_parts', LOWER(VIEW_DEFINITION)) = 0
      AND LOCATE('published_parts', LOWER(VIEW_DEFINITION)) = 0
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 031: formal view must expose only verified bounded data';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_031_output();
DROP PROCEDURE assert_partsouq_031_output;
