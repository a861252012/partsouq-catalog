-- 019_verified_bounded_catalog_view.sql
--
-- Fail closed when a bounded snapshot has no sealed, live HTTP evidence.
-- The immutable 016 view predates the evidence schema introduced by 017, so
-- this migration rebuilds the view after both contracts exist.

SET SESSION lock_wait_timeout = 30;
SET SESSION innodb_lock_wait_timeout = 30;

DROP PROCEDURE IF EXISTS assert_partsouq_019_preflight;
DELIMITER //
CREATE PROCEDURE assert_partsouq_019_preflight()
BEGIN
  IF DATABASE() IS NULL OR (
    SELECT COUNT(*) FROM information_schema.TABLES
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'
      AND TABLE_NAME IN (
        'bounded_parts', 'crawl_runs', 'scheduled_job_runs',
        'partsouq_response_bodies', 'partsouq_http_artifacts',
        'partsouq_artifact_records'
      )
  ) <> 6 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 019: catalog provenance/evidence schema is incomplete';
  END IF;
  IF (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'crawl_runs'
      AND COLUMN_NAME IN (
        'evidence_status', 'evidence_manifest_sha256', 'evidence_dataset_sha256',
        'evidence_artifact_count', 'evidence_record_count',
        'evidence_original_bytes', 'evidence_stored_bytes', 'evidence_verified_at'
      )
  ) <> 8 OR NOT EXISTS (
    SELECT 1 FROM information_schema.VIEWS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'v_current_catalog_parts'
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 019: migration 016/017 contracts are incomplete';
  END IF;
  IF EXISTS (SELECT 1 FROM crawl_runs WHERE status = 'running')
     OR EXISTS (
       SELECT 1 FROM scheduled_job_runs
       WHERE job_name = 'catalog' AND status = 'running'
     ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 019: running catalog jobs exist; stop writers first';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_019_preflight();
DROP PROCEDURE assert_partsouq_019_preflight;

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
),
qualified_full_runs AS (
SELECT full_run.id AS crawl_run_id
FROM crawl_runs AS full_run
JOIN scheduled_job_runs AS full_scheduler_run
  ON full_scheduler_run.id = full_run.scheduled_job_run_id
 AND full_scheduler_run.job_name = 'catalog'
 AND full_scheduler_run.trigger_mode = 'daemon'
 AND full_scheduler_run.status = 'completed'
 AND full_scheduler_run.finished_at IS NOT NULL
 AND full_scheduler_run.exit_code = 0
JOIN (
    SELECT scheduled_job_run_id, COUNT(*) AS linked_crawl_runs
    FROM crawl_runs
    WHERE scheduled_job_run_id IS NOT NULL
    GROUP BY scheduled_job_run_id
) AS full_scheduler_links
  ON full_scheduler_links.scheduled_job_run_id = full_scheduler_run.id
 AND full_scheduler_links.linked_crawl_runs = 1
WHERE full_run.dataset_kind = 'full'
  AND full_run.target_parts IS NULL
  AND full_run.status = 'success'
  AND full_run.finished_at IS NOT NULL
  AND full_run.error_msg IS NULL
),
formal_current_parts AS (
SELECT
    'full' AS dataset_scope,
    published_parts.crawl_run_id AS source_crawl_run_id,
    published_parts.part_id, published_parts.vehicle_id, published_parts.model_id,
    published_parts.vehicle_vid, published_parts.brand, published_parts.model,
    published_parts.vehicle_name, published_parts.vehicle_code,
    published_parts.prod_period, published_parts.production_from,
    published_parts.production_to, published_parts.engine, published_parts.trim_name,
    published_parts.part_name, published_parts.part_number,
    published_parts.part_number_normalized, published_parts.category_id,
    published_parts.category_cid, published_parts.category_main,
    published_parts.category_group, published_parts.group_id,
    published_parts.group_code, published_parts.group_uid, published_parts.part_range,
    published_parts.part_from, published_parts.part_to, published_parts.source_url,
    published_parts.note, published_parts.quantity, published_parts.code,
    published_parts.snapshot_at
FROM published_parts
JOIN qualified_full_runs
  ON qualified_full_runs.crawl_run_id = published_parts.crawl_run_id
),
formal_previous_parts AS (
SELECT
    'full' AS dataset_scope,
    previous.crawl_run_id AS source_crawl_run_id,
    previous.part_id, previous.vehicle_id, previous.model_id, previous.vehicle_vid,
    previous.brand, previous.model, previous.vehicle_name, previous.vehicle_code,
    previous.prod_period, previous.production_from, previous.production_to,
    previous.engine, previous.trim_name, previous.part_name, previous.part_number,
    previous.part_number_normalized, previous.category_id, previous.category_cid,
    previous.category_main, previous.category_group, previous.group_id,
    previous.group_code, previous.group_uid, previous.part_range, previous.part_from,
    previous.part_to, previous.source_url, previous.note, previous.quantity,
    previous.code, previous.snapshot_at
FROM published_parts_previous AS previous
JOIN qualified_full_runs
  ON qualified_full_runs.crawl_run_id = previous.crawl_run_id
),
formal_full_parts AS (
SELECT formal_current_parts.*
FROM formal_current_parts
UNION ALL
SELECT formal_previous_parts.*
FROM formal_previous_parts
WHERE NOT EXISTS (SELECT 1 FROM formal_current_parts)
)
SELECT formal_full_parts.*
FROM formal_full_parts
UNION ALL
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
 AND snapshot.max_crawl_run_id = current_run.id
WHERE NOT EXISTS (SELECT 1 FROM formal_full_parts);

DROP PROCEDURE IF EXISTS assert_partsouq_019_output;
DELIMITER //
CREATE PROCEDURE assert_partsouq_019_output()
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.VIEWS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'v_current_catalog_parts'
      AND LOCATE('verified_bounded_evidence', LOWER(VIEW_DEFINITION)) > 0
      AND LOCATE('verified_bounded_records', LOWER(VIEW_DEFINITION)) > 0
      AND LOCATE('partsouq_http_artifacts', LOWER(VIEW_DEFINITION)) > 0
      AND LOCATE('partsouq_artifact_records', LOWER(VIEW_DEFINITION)) > 0
      AND LOCATE('evidence_status', LOWER(VIEW_DEFINITION)) > 0
      AND LOCATE('live_http', LOWER(VIEW_DEFINITION)) > 0
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 019: verified bounded view postflight failed';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_019_output();
DROP PROCEDURE assert_partsouq_019_output;
