-- 032_bounded_snapshot_evidence_binding.sql
--
-- 將正式 snapshot 的每一個 part 直接綁定到已接受的 artifact record
-- digest。raw parts 後續被其他 crawl 覆寫 seen_run_id 或內容時，不會再
-- 影響已發布 bounded snapshot 的 evidence audit。

SET SESSION lock_wait_timeout = 30;
SET SESSION innodb_lock_wait_timeout = 30;

DROP PROCEDURE IF EXISTS assert_partsouq_032_preflight;
DELIMITER //
CREATE PROCEDURE assert_partsouq_032_preflight()
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
      SET MESSAGE_TEXT = 'migration 032: apply catalog migrations through 031 first';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_032_preflight();
DROP PROCEDURE assert_partsouq_032_preflight;

DROP PROCEDURE IF EXISTS add_partsouq_032_snapshot_digest;
DELIMITER //
CREATE PROCEDURE add_partsouq_032_snapshot_digest()
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'bounded_parts'
      AND COLUMN_NAME = 'evidence_record_sha256'
  ) THEN
    ALTER TABLE bounded_parts
      ADD COLUMN evidence_record_sha256 CHAR(64)
        CHARACTER SET ascii COLLATE ascii_bin NULL AFTER code;
  END IF;
END//
DELIMITER ;
CALL add_partsouq_032_snapshot_digest();
DROP PROCEDURE add_partsouq_032_snapshot_digest;

-- 舊版 snapshot 只在仍可由 current normalized row 證明「沒有被後續 raw
-- crawl 改寫」且 accepted artifact record 唯一時才回填。任何不唯一、缺件或
-- 欄位已變動的列一律保留 NULL，新的 formal view 會 fail closed。
UPDATE bounded_parts AS snapshot
JOIN parts AS source_part
  ON source_part.id = snapshot.part_id
 AND source_part.seen_run_id = snapshot.crawl_run_id
JOIN groups_t AS source_group
  ON source_group.id = source_part.group_id
JOIN categories AS source_category
  ON source_category.id = source_group.category_id
JOIN vehicles AS source_vehicle
  ON source_vehicle.id = source_category.vehicle_id
JOIN models AS source_model
  ON source_model.id = source_vehicle.model_id
JOIN brands AS source_brand
  ON source_brand.id = source_model.brand_id
JOIN (
    SELECT record.crawl_run_id, record.part_id, MIN(record.record_sha256) AS record_sha256
    FROM partsouq_artifact_records AS record
    JOIN partsouq_http_artifacts AS artifact
      ON artifact.id = record.artifact_id
     AND artifact.crawl_run_id = record.crawl_run_id
     AND artifact.verification_status = 'verified'
     AND artifact.capture_kind = 'live_http'
    WHERE record.record_type = 'part'
      AND record.accepted = 1
      AND record.part_id IS NOT NULL
    GROUP BY record.crawl_run_id, record.part_id
    HAVING COUNT(*) = 1
) AS accepted_record
  ON accepted_record.crawl_run_id = snapshot.crawl_run_id
 AND accepted_record.part_id = snapshot.part_id
SET snapshot.evidence_record_sha256 = accepted_record.record_sha256
WHERE snapshot.evidence_record_sha256 IS NULL
  AND source_part.group_id = snapshot.group_id
  AND CAST(source_part.part_number AS BINARY) <=> CAST(snapshot.part_number AS BINARY)
  AND CAST(source_part.name AS BINARY) <=> CAST(snapshot.part_name AS BINARY)
  AND CAST(source_part.code AS BINARY) <=> CAST(snapshot.code AS BINARY)
  AND CAST(source_part.note AS BINARY) <=> CAST(snapshot.note AS BINARY)
  AND CAST(source_part.quantity AS BINARY) <=> CAST(snapshot.quantity AS BINARY)
  AND CAST(source_part.range_str AS BINARY) <=> CAST(snapshot.part_range AS BINARY)
  AND source_part.part_from <=> snapshot.part_from
  AND source_part.part_to <=> snapshot.part_to
  AND source_category.id = snapshot.category_id
  AND CAST(source_category.cid AS BINARY) <=> CAST(snapshot.category_cid AS BINARY)
  AND CAST(source_category.name AS BINARY) <=> CAST(snapshot.category_main AS BINARY)
  AND CAST(source_group.name AS BINARY) <=> CAST(snapshot.category_group AS BINARY)
  AND CAST(source_group.code AS BINARY) <=> CAST(snapshot.group_code AS BINARY)
  AND CAST(source_group.uid AS BINARY) <=> CAST(snapshot.group_uid AS BINARY)
  AND CAST(source_group.url AS BINARY) <=> CAST(snapshot.source_url AS BINARY)
  AND source_vehicle.id = snapshot.vehicle_id
  AND source_vehicle.model_id = snapshot.model_id
  AND CAST(source_vehicle.vid AS BINARY) <=> CAST(snapshot.vehicle_vid AS BINARY)
  AND CAST(source_vehicle.name AS BINARY) <=> CAST(snapshot.vehicle_name AS BINARY)
  AND CAST(source_vehicle.model_code AS BINARY) <=> CAST(snapshot.vehicle_code AS BINARY)
  AND CAST(source_vehicle.prod_period AS BINARY) <=> CAST(snapshot.prod_period AS BINARY)
  AND source_vehicle.production_from <=> snapshot.production_from
  AND source_vehicle.production_to <=> snapshot.production_to
  AND CAST(source_vehicle.engine AS BINARY) <=> CAST(snapshot.engine AS BINARY)
  AND CAST(source_vehicle.grade AS BINARY) <=> CAST(snapshot.trim_name AS BINARY)
  AND CAST(source_brand.name AS BINARY) <=> CAST(snapshot.brand AS BINARY)
  AND CAST(source_model.name AS BINARY) <=> CAST(snapshot.model AS BINARY);

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
 AND evidence_part.evidence_record_sha256 = record.record_sha256
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

DROP PROCEDURE IF EXISTS assert_partsouq_032_output;
DELIMITER //
CREATE PROCEDURE assert_partsouq_032_output()
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'bounded_parts'
      AND COLUMN_NAME = 'evidence_record_sha256'
  ) OR NOT EXISTS (
    SELECT 1 FROM information_schema.VIEWS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'v_current_catalog_parts'
      AND LOCATE('evidence_record_sha256', LOWER(VIEW_DEFINITION)) > 0
      AND LOCATE('verified_bounded_records', LOWER(VIEW_DEFINITION)) > 0
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 032: bounded snapshot evidence binding is incomplete';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_032_output();
DROP PROCEDURE assert_partsouq_032_output;
