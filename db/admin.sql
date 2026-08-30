CREATE TABLE IF NOT EXISTS admin_vehicle_mappings (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    vin_prefix VARCHAR(11) NOT NULL,
    vin CHAR(17) CHARACTER SET ascii COLLATE ascii_bin NULL,
    partsouq_vehicle_id INT NULL,
    make_name VARCHAR(128) NOT NULL,
    model_name VARCHAR(256) NOT NULL,
    model_year SMALLINT UNSIGNED NULL,
    engine VARCHAR(256) NULL,
    trim_name VARCHAR(256) NULL,
    source_name VARCHAR(64) NOT NULL DEFAULT 'manual',
    source_reference VARCHAR(1024) NULL,
    manual_mapping_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin
        GENERATED ALWAYS AS (
            IF(vin IS NULL, SHA2(CONCAT(
                UPPER(TRIM(vin_prefix)), ':',
                CHAR_LENGTH(TRIM(make_name)), ':', UPPER(TRIM(make_name)),
                CHAR_LENGTH(TRIM(model_name)), ':', UPPER(TRIM(model_name)),
                ':', COALESCE(model_year, ''),
                ':', CHAR_LENGTH(TRIM(COALESCE(engine, ''))),
                ':', UPPER(TRIM(COALESCE(engine, ''))),
                CHAR_LENGTH(TRIM(COALESCE(trim_name, ''))),
                ':', UPPER(TRIM(COALESCE(trim_name, '')))
            ), 256), NULL)
        ) STORED,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_admin_vehicle_vin (vin),
    UNIQUE KEY uq_admin_vehicle_manual_mapping (manual_mapping_key),
    KEY idx_admin_vehicle_make_model_year (make_name, model_name, model_year),
    CONSTRAINT fk_admin_vehicle_vin FOREIGN KEY (vin) REFERENCES nhtsa_vin_decodes(vin),
    CONSTRAINT fk_admin_vehicle_partsouq
        FOREIGN KEY (partsouq_vehicle_id) REFERENCES vehicles(id),
    CONSTRAINT chk_admin_vehicle_prefix_length CHECK (CHAR_LENGTH(vin_prefix) BETWEEN 3 AND 11),
    CONSTRAINT chk_admin_vehicle_confirmed_pair CHECK (
        (vin IS NULL AND partsouq_vehicle_id IS NULL)
        OR (vin IS NOT NULL AND partsouq_vehicle_id IS NOT NULL)
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS admin_part_translations (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    english_name VARCHAR(512) NOT NULL,
    chinese_name VARCHAR(512) NOT NULL,
    common_chinese_name VARCHAR(512) NULL,
    source_name VARCHAR(64) NOT NULL DEFAULT 'manual',
    source_reference VARCHAR(1024) NULL,
    translation_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin
        GENERATED ALWAYS AS (
            SHA2(CONCAT(
                CHAR_LENGTH(english_name), ':', english_name,
                CHAR_LENGTH(chinese_name), ':', chinese_name
            ), 256)
        ) STORED,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_admin_part_translation (translation_key),
    KEY idx_admin_part_translation_english (english_name(191)),
    KEY idx_admin_part_translation_chinese (chinese_name(191))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS admin_part_fitments (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    part_number VARCHAR(64) NOT NULL,
    vin_prefix VARCHAR(11) NULL,
    make_name VARCHAR(128) NOT NULL,
    model_name VARCHAR(256) NOT NULL,
    model_year_from SMALLINT UNSIGNED NULL,
    model_year_to SMALLINT UNSIGNED NULL,
    engine VARCHAR(256) NULL,
    trim_name VARCHAR(256) NULL,
    source_name VARCHAR(64) NOT NULL DEFAULT 'manual',
    source_reference VARCHAR(1024) NULL,
    fitment_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin
        GENERATED ALWAYS AS (
            SHA2(CONCAT(
                CHAR_LENGTH(part_number), ':', part_number,
                CHAR_LENGTH(COALESCE(vin_prefix, '')), ':', COALESCE(vin_prefix, ''),
                CHAR_LENGTH(make_name), ':', make_name,
                CHAR_LENGTH(model_name), ':', model_name,
                COALESCE(model_year_from, ''), ':', COALESCE(model_year_to, ''), ':',
                CHAR_LENGTH(COALESCE(engine, '')), ':', COALESCE(engine, ''),
                CHAR_LENGTH(COALESCE(trim_name, '')), ':', COALESCE(trim_name, '')
            ), 256)
        ) STORED,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_admin_part_fitment (fitment_key),
    KEY idx_admin_part_fitment_number (part_number),
    KEY idx_admin_part_fitment_vehicle (make_name, model_name, model_year_from, model_year_to),
    CONSTRAINT chk_admin_part_fitment_prefix_length CHECK (
        vin_prefix IS NULL OR CHAR_LENGTH(vin_prefix) BETWEEN 3 AND 11
    ),
    CONSTRAINT chk_admin_part_fitment_years CHECK (
        model_year_from IS NULL OR model_year_to IS NULL OR model_year_from <= model_year_to
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS scheduled_job_runs (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    parent_scheduled_job_run_id BIGINT UNSIGNED NULL,
    job_name VARCHAR(32) NOT NULL,
    trigger_mode VARCHAR(16) NOT NULL DEFAULT 'manual',
    status VARCHAR(32) NOT NULL,
    started_at DATETIME NOT NULL,
    finished_at DATETIME NULL,
    exit_code INT NULL,
    output_text MEDIUMTEXT NULL,
    KEY idx_scheduled_job_runs_name_started (job_name, started_at),
    UNIQUE KEY uq_scheduled_job_parent_stage (parent_scheduled_job_run_id, job_name),
    CONSTRAINT fk_scheduled_job_parent FOREIGN KEY (parent_scheduled_job_run_id)
        REFERENCES scheduled_job_runs(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

SET @add_scheduler_trigger_mode = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE scheduled_job_runs ADD COLUMN trigger_mode '
        'VARCHAR(16) NOT NULL DEFAULT ''manual'' AFTER job_name',
        'DO 0'
    )
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'scheduled_job_runs'
      AND COLUMN_NAME = 'trigger_mode'
);
PREPARE add_scheduler_trigger_mode FROM @add_scheduler_trigger_mode;
EXECUTE add_scheduler_trigger_mode;
DEALLOCATE PREPARE add_scheduler_trigger_mode;

CREATE TABLE IF NOT EXISTS published_parts_previous LIKE published_parts;

DROP PROCEDURE IF EXISTS ensure_published_snapshot_foreign_keys;
DELIMITER //
CREATE PROCEDURE ensure_published_snapshot_foreign_keys()
BEGIN
    DECLARE current_fk_valid INT DEFAULT 0;
    DECLARE previous_fk_valid INT DEFAULT 0;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts'
          AND COLUMN_NAME = 'crawl_run_id'
    ) OR NOT EXISTS (
        SELECT 1 FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts_previous'
          AND COLUMN_NAME = 'crawl_run_id'
    ) THEN
        SIGNAL SQLSTATE '45000'
          SET MESSAGE_TEXT = 'admin schema requires catalog migration 016';
    END IF;

    SELECT COUNT(*) = 1 INTO current_fk_valid
    FROM information_schema.KEY_COLUMN_USAGE AS key_columns
    JOIN information_schema.REFERENTIAL_CONSTRAINTS AS constraints_t
      ON constraints_t.CONSTRAINT_SCHEMA = key_columns.CONSTRAINT_SCHEMA
     AND constraints_t.TABLE_NAME = key_columns.TABLE_NAME
     AND constraints_t.CONSTRAINT_NAME = key_columns.CONSTRAINT_NAME
    WHERE key_columns.CONSTRAINT_SCHEMA = DATABASE()
      AND key_columns.TABLE_NAME = 'published_parts'
      AND key_columns.CONSTRAINT_NAME = 'fk_published_crawl_run'
      AND key_columns.COLUMN_NAME = 'crawl_run_id'
      AND key_columns.REFERENCED_TABLE_SCHEMA = DATABASE()
      AND key_columns.REFERENCED_TABLE_NAME = 'crawl_runs'
      AND key_columns.REFERENCED_COLUMN_NAME = 'id'
      AND constraints_t.UPDATE_RULE = 'NO ACTION'
      AND constraints_t.DELETE_RULE = 'NO ACTION';
    IF current_fk_valid <> 1 THEN
        IF EXISTS (
            SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
            WHERE CONSTRAINT_SCHEMA = DATABASE()
              AND TABLE_NAME = 'published_parts'
              AND CONSTRAINT_NAME = 'fk_published_crawl_run'
        ) THEN
            ALTER TABLE published_parts DROP FOREIGN KEY fk_published_crawl_run;
        END IF;
        ALTER TABLE published_parts ADD CONSTRAINT fk_published_crawl_run
          FOREIGN KEY (crawl_run_id) REFERENCES crawl_runs(id);
    END IF;

    SELECT COUNT(*) = 1 INTO previous_fk_valid
    FROM information_schema.KEY_COLUMN_USAGE AS key_columns
    JOIN information_schema.REFERENTIAL_CONSTRAINTS AS constraints_t
      ON constraints_t.CONSTRAINT_SCHEMA = key_columns.CONSTRAINT_SCHEMA
     AND constraints_t.TABLE_NAME = key_columns.TABLE_NAME
     AND constraints_t.CONSTRAINT_NAME = key_columns.CONSTRAINT_NAME
    WHERE key_columns.CONSTRAINT_SCHEMA = DATABASE()
      AND key_columns.TABLE_NAME = 'published_parts_previous'
      AND key_columns.CONSTRAINT_NAME = 'fk_published_previous_crawl_run'
      AND key_columns.COLUMN_NAME = 'crawl_run_id'
      AND key_columns.REFERENCED_TABLE_SCHEMA = DATABASE()
      AND key_columns.REFERENCED_TABLE_NAME = 'crawl_runs'
      AND key_columns.REFERENCED_COLUMN_NAME = 'id'
      AND constraints_t.UPDATE_RULE = 'NO ACTION'
      AND constraints_t.DELETE_RULE = 'NO ACTION';
    IF previous_fk_valid <> 1 THEN
        IF EXISTS (
            SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
            WHERE CONSTRAINT_SCHEMA = DATABASE()
              AND TABLE_NAME = 'published_parts_previous'
              AND CONSTRAINT_NAME = 'fk_published_previous_crawl_run'
        ) THEN
            ALTER TABLE published_parts_previous
              DROP FOREIGN KEY fk_published_previous_crawl_run;
        END IF;
        ALTER TABLE published_parts_previous
          ADD CONSTRAINT fk_published_previous_crawl_run
          FOREIGN KEY (crawl_run_id) REFERENCES crawl_runs(id);
    END IF;
END//
DELIMITER ;
CALL ensure_published_snapshot_foreign_keys();
DROP PROCEDURE ensure_published_snapshot_foreign_keys;

-- Full snapshots are retained only as raw candidates. The formal catalog must
-- expose a verified desired bounded snapshot and must never fall back to a
-- full candidate.
CREATE OR REPLACE VIEW v_current_catalog_parts_evidence_base AS
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

-- 正式 snapshot 除了逐列 evidence 外，每個納入的 group 都必須有一張
-- immutable receipt。若 quota 剛好在 unit 頁中途達標，receipt 會明確標為
-- partial；缺 receipt、artifact 不一致或收據涵蓋範圍不完整時一律不輸出。
CREATE OR REPLACE VIEW v_current_catalog_parts AS
WITH snapshot_groups AS (
    SELECT
        bounded_part.crawl_run_id,
        bounded_part.group_id,
        COUNT(*) AS snapshot_part_count
    FROM bounded_parts AS bounded_part
    GROUP BY bounded_part.crawl_run_id, bounded_part.group_id
),
receipt_artifact_counts AS (
    SELECT
        snapshot_group.crawl_run_id,
        snapshot_group.group_id,
        receipt.source_artifact_id,
        SUM(artifact_record.record_type = 'part') AS parsed_part_count,
        SUM(
            artifact_record.record_type = 'part'
            AND artifact_record.accepted = 1
        ) AS accepted_part_count,
        COUNT(DISTINCT CASE
            WHEN artifact_record.record_type = 'part' AND artifact_record.accepted = 1
            THEN artifact_record.part_id
        END) AS accepted_part_id_count,
        SUM(artifact_record.record_type = 'quarantine_part') AS skipped_record_count
    FROM snapshot_groups AS snapshot_group
    STRAIGHT_JOIN bounded_group_receipts AS receipt
      ON receipt.crawl_run_id = snapshot_group.crawl_run_id
     AND receipt.group_id = snapshot_group.group_id
    STRAIGHT_JOIN partsouq_artifact_records AS artifact_record FORCE INDEX (PRIMARY)
      ON artifact_record.artifact_id = receipt.source_artifact_id
     AND artifact_record.crawl_run_id = receipt.crawl_run_id
    GROUP BY
        snapshot_group.crawl_run_id,
        snapshot_group.group_id,
        receipt.source_artifact_id
),
receipt_snapshot_members AS (
    SELECT
        snapshot_group.crawl_run_id,
        snapshot_group.group_id,
        COUNT(DISTINCT bounded_part.part_id) AS accepted_snapshot_part_count
    FROM snapshot_groups AS snapshot_group
    STRAIGHT_JOIN bounded_group_receipts AS receipt
      ON receipt.crawl_run_id = snapshot_group.crawl_run_id
     AND receipt.group_id = snapshot_group.group_id
    STRAIGHT_JOIN partsouq_artifact_records AS artifact_record FORCE INDEX (PRIMARY)
      ON artifact_record.artifact_id = receipt.source_artifact_id
     AND artifact_record.crawl_run_id = receipt.crawl_run_id
     AND artifact_record.record_type = 'part'
     AND artifact_record.accepted = 1
    STRAIGHT_JOIN bounded_parts AS bounded_part
      ON bounded_part.crawl_run_id = snapshot_group.crawl_run_id
     AND bounded_part.group_id = snapshot_group.group_id
     AND bounded_part.part_id = artifact_record.part_id
     AND bounded_part.evidence_record_sha256 = artifact_record.record_sha256
    GROUP BY snapshot_group.crawl_run_id, snapshot_group.group_id
),
receipt_integrity AS (
    SELECT
        snapshot_group.crawl_run_id,
        snapshot_group.group_id,
        snapshot_group.snapshot_part_count,
        CASE
            WHEN receipt.source_artifact_id IS NOT NULL
             AND artifact.crawl_run_id = snapshot_group.crawl_run_id
             AND artifact.capture_kind = 'live_http'
             AND artifact.page_type = 'unit'
             AND artifact.parser_name = 'parse_parts'
             AND artifact.verification_status = 'verified'
             AND artifact.http_status = 200
             AND artifact.challenge_detected = 0
             AND LOWER(artifact.content_type) LIKE 'text/html%'
             AND artifact.malformed_row_count = 0
             AND artifact.verified_at IS NOT NULL
             AND receipt_artifact_counts.parsed_part_count = receipt.parsed_part_count
             AND receipt_artifact_counts.accepted_part_count = receipt.accepted_part_count
             AND receipt_artifact_counts.accepted_part_id_count = receipt.accepted_part_count
             AND receipt_artifact_counts.skipped_record_count = receipt.skipped_record_count
             AND artifact.parsed_record_count = (
                 receipt.parsed_part_count + receipt.skipped_record_count
             )
             AND artifact.accepted_record_count = receipt.accepted_part_count
             AND artifact.skipped_record_count = receipt.skipped_record_count
             AND receipt_snapshot_members.accepted_snapshot_part_count
                 = receipt.accepted_part_count
             AND snapshot_group.snapshot_part_count = receipt.accepted_part_count
             AND (
                 (receipt.status = 'done'
                  AND receipt.accepted_part_count = receipt.parsed_part_count)
                 OR (receipt.status = 'partial'
                     AND receipt.accepted_part_count > 0
                     AND receipt.accepted_part_count < receipt.parsed_part_count)
             )
            THEN 1 ELSE 0
        END AS is_verified
    FROM snapshot_groups AS snapshot_group
    LEFT JOIN bounded_group_receipts AS receipt
      ON receipt.crawl_run_id = snapshot_group.crawl_run_id
     AND receipt.group_id = snapshot_group.group_id
    LEFT JOIN partsouq_http_artifacts AS artifact
      ON artifact.id = receipt.source_artifact_id
     AND artifact.crawl_run_id = receipt.crawl_run_id
    LEFT JOIN receipt_artifact_counts
      ON receipt_artifact_counts.crawl_run_id = snapshot_group.crawl_run_id
     AND receipt_artifact_counts.group_id = snapshot_group.group_id
     AND receipt_artifact_counts.source_artifact_id = receipt.source_artifact_id
    LEFT JOIN receipt_snapshot_members
      ON receipt_snapshot_members.crawl_run_id = snapshot_group.crawl_run_id
     AND receipt_snapshot_members.group_id = snapshot_group.group_id
),
verified_bounded_group_receipts AS (
    SELECT
        receipt_integrity.crawl_run_id,
        COUNT(*) AS snapshot_group_count,
        SUM(receipt_integrity.snapshot_part_count) AS snapshot_part_count,
        SUM(receipt_integrity.is_verified) AS verified_group_count,
        SUM(
            CASE WHEN receipt_integrity.is_verified = 1
            THEN receipt_integrity.snapshot_part_count ELSE 0 END
        ) AS verified_part_count
    FROM receipt_integrity
    GROUP BY receipt_integrity.crawl_run_id
)
SELECT evidence_base.*
FROM v_current_catalog_parts_evidence_base AS evidence_base
JOIN verified_bounded_group_receipts AS receipt_gate
  ON receipt_gate.crawl_run_id = evidence_base.source_crawl_run_id
 AND receipt_gate.snapshot_part_count = 10000
 AND receipt_gate.verified_group_count = receipt_gate.snapshot_group_count
 AND receipt_gate.verified_part_count = receipt_gate.snapshot_part_count;

-- Compatibility readers must obey the same provenance gate; direct access to
-- published_parts would expose a scheduler-running or failed candidate.
CREATE OR REPLACE VIEW v_parts AS
SELECT
    part_id, vehicle_id, model_id, vehicle_vid,
    brand, model, vehicle_name, vehicle_code, prod_period,
    production_from, production_to, engine, trim_name,
    part_name, part_number,
    category_id, category_cid, category_main, category_group,
    group_id, group_code, group_uid,
    part_range, part_from, part_to, source_url, note, quantity, code
FROM v_current_catalog_parts;

-- Exact mapping 的候選車款數必須先依 mapping 算一次。若把相同的
-- COUNT(DISTINCT vehicle_id) 關聯子查詢放在每個零件列，10,000 筆
-- snapshot 會反覆掃描 current catalog，讓 VIN fitment API 退化成 N×N。
CREATE OR REPLACE VIEW v_vin_part_fitments AS
WITH strict_vehicle_counts AS (
    SELECT
        vehicle_mapping.id AS mapping_id,
        COUNT(DISTINCT exact_candidate.vehicle_id) AS strict_vehicle_count
    FROM admin_vehicle_mappings AS vehicle_mapping
    JOIN nhtsa_vin_decodes AS vin_decode ON vin_decode.vin = vehicle_mapping.vin
    JOIN v_current_catalog_parts AS exact_candidate
      ON exact_candidate.vehicle_id IS NOT NULL
     AND (
         exact_candidate.production_from IS NOT NULL
         OR exact_candidate.production_to IS NOT NULL
     )
     AND CAST(REGEXP_REPLACE(UPPER(exact_candidate.brand), '[^A-Z0-9]', '') AS BINARY)
         = CAST(REGEXP_REPLACE(UPPER(vin_decode.make_name), '[^A-Z0-9]', '') AS BINARY)
     AND CAST(REGEXP_REPLACE(UPPER(exact_candidate.model), '[^A-Z0-9]', '') AS BINARY)
         = CAST(REGEXP_REPLACE(UPPER(vin_decode.model_name), '[^A-Z0-9]', '') AS BINARY)
     AND CAST(REGEXP_REPLACE(UPPER(exact_candidate.engine), '[^A-Z0-9]', '') AS BINARY)
         = CAST(REGEXP_REPLACE(UPPER(vin_decode.engine_model), '[^A-Z0-9]', '') AS BINARY)
     AND CAST(REGEXP_REPLACE(UPPER(exact_candidate.trim_name), '[^A-Z0-9]', '') AS BINARY)
         = CAST(REGEXP_REPLACE(UPPER(vin_decode.trim_name), '[^A-Z0-9]', '') AS BINARY)
     AND (
         exact_candidate.production_from IS NULL
         OR vin_decode.model_year >= CAST(LEFT(exact_candidate.production_from, 4) AS UNSIGNED)
     )
     AND (
         exact_candidate.production_to IS NULL
         OR vin_decode.model_year <= CAST(LEFT(exact_candidate.production_to, 4) AS UNSIGNED)
     )
    WHERE vehicle_mapping.vin IS NOT NULL
      AND vehicle_mapping.partsouq_vehicle_id IS NOT NULL
      AND vehicle_mapping.source_name NOT IN ('manual-name-override', 'manual-sparse-override')
      AND vehicle_mapping.model_year <=> vin_decode.model_year
      AND NULLIF(TRIM(vin_decode.model_name), '') IS NOT NULL
      AND NULLIF(TRIM(vin_decode.engine_model), '') IS NOT NULL
      AND NULLIF(TRIM(vin_decode.trim_name), '') IS NOT NULL
      AND CAST(REGEXP_REPLACE(UPPER(vehicle_mapping.make_name), '[^A-Z0-9]', '') AS BINARY)
          = CAST(REGEXP_REPLACE(UPPER(vin_decode.make_name), '[^A-Z0-9]', '') AS BINARY)
      AND CAST(REGEXP_REPLACE(UPPER(vehicle_mapping.model_name), '[^A-Z0-9]', '') AS BINARY)
          = CAST(REGEXP_REPLACE(UPPER(vin_decode.model_name), '[^A-Z0-9]', '') AS BINARY)
      AND CAST(REGEXP_REPLACE(UPPER(vehicle_mapping.engine), '[^A-Z0-9]', '') AS BINARY)
          = CAST(REGEXP_REPLACE(UPPER(vin_decode.engine_model), '[^A-Z0-9]', '') AS BINARY)
      AND CAST(REGEXP_REPLACE(UPPER(vehicle_mapping.trim_name), '[^A-Z0-9]', '') AS BINARY)
          = CAST(REGEXP_REPLACE(UPPER(vin_decode.trim_name), '[^A-Z0-9]', '') AS BINARY)
    GROUP BY vehicle_mapping.id
)
SELECT mapped_fitment.*
FROM (
    SELECT
        vin_decode.vin,
        vin_decode.make_name,
        vin_decode.model_name,
        vin_decode.model_year,
        vin_decode.engine_configuration,
        vin_decode.engine_model,
        vin_decode.displacement_l,
        vin_decode.trim_name AS nhtsa_trim_name,
        vin_decode.source_url AS nhtsa_source_url,
        vin_decode.source_artifact_id AS nhtsa_source_artifact_id,
        catalog_part.part_id,
        catalog_part.dataset_scope AS catalog_dataset_scope,
        catalog_part.source_crawl_run_id AS catalog_crawl_run_id,
        catalog_part.model_id,
        catalog_part.vehicle_id,
        catalog_part.vehicle_vid,
        catalog_part.category_id,
        catalog_part.category_cid,
        catalog_part.group_id,
        catalog_part.group_uid,
        catalog_part.code,
        catalog_part.group_code,
        catalog_part.vehicle_id AS partsouq_vehicle_id,
        catalog_part.brand AS partsouq_brand,
        catalog_part.model AS partsouq_model,
        catalog_part.vehicle_name,
        catalog_part.vehicle_code,
        catalog_part.engine AS partsouq_engine,
        catalog_part.trim_name AS partsouq_trim_name,
        catalog_part.part_number,
        catalog_part.part_name,
        catalog_part.category_main,
        catalog_part.category_group,
        catalog_part.prod_period,
        catalog_part.part_range,
        CASE
            WHEN catalog_part.production_from IS NULL THEN catalog_part.part_from
            WHEN catalog_part.part_from IS NULL THEN catalog_part.production_from
            ELSE GREATEST(catalog_part.production_from, catalog_part.part_from)
        END AS fitment_from,
        CASE
            WHEN catalog_part.production_to IS NULL THEN catalog_part.part_to
            WHEN catalog_part.part_to IS NULL THEN catalog_part.production_to
            ELSE LEAST(catalog_part.production_to, catalog_part.part_to)
        END AS fitment_to,
        catalog_part.source_url,
        vehicle_mapping.id AS mapping_id,
        vehicle_mapping.source_name AS mapping_source_name,
        vehicle_mapping.source_reference AS mapping_source_reference,
        CASE
            WHEN vehicle_mapping.source_name IN ('manual-name-override', 'manual-sparse-override')
                THEN 'confirmed_manual_override'
            ELSE 'confirmed'
        END AS vehicle_mapping_status,
        CASE
            WHEN vehicle_mapping.source_name IN ('manual-name-override', 'manual-sparse-override')
                THEN 'manual_vehicle_override'
            ELSE 'compatible_by_model_year_engine_trim'
        END AS fitment_status
    FROM admin_vehicle_mappings AS vehicle_mapping
    JOIN nhtsa_vin_decodes AS vin_decode ON vin_decode.vin = vehicle_mapping.vin
    JOIN v_current_catalog_parts AS catalog_part ON catalog_part.vehicle_id = vehicle_mapping.partsouq_vehicle_id
    LEFT JOIN strict_vehicle_counts ON strict_vehicle_counts.mapping_id = vehicle_mapping.id
    WHERE vehicle_mapping.vin IS NOT NULL AND vehicle_mapping.partsouq_vehicle_id IS NOT NULL
      AND catalog_part.vehicle_id IS NOT NULL
      AND (
          (
              vehicle_mapping.source_name = 'manual-sparse-override'
              AND NULLIF(TRIM(vehicle_mapping.source_reference), '') IS NOT NULL
              AND vehicle_mapping.model_year <=> vin_decode.model_year
              AND CAST(REGEXP_REPLACE(UPPER(catalog_part.brand), '[^A-Z0-9]', '') AS BINARY)
                  = CAST(REGEXP_REPLACE(UPPER(vin_decode.make_name), '[^A-Z0-9]', '') AS BINARY)
              AND CAST(vehicle_mapping.make_name AS BINARY) <=> CAST(vin_decode.make_name AS BINARY)
              AND CAST(vehicle_mapping.model_name AS BINARY) <=> CAST(
                  COALESCE(NULLIF(TRIM(vin_decode.model_name), ''), catalog_part.model) AS BINARY
              )
              AND CAST(vehicle_mapping.engine AS BINARY) <=> CAST(
                  COALESCE(NULLIF(TRIM(vin_decode.engine_model), ''), catalog_part.engine) AS BINARY
              )
              AND CAST(vehicle_mapping.trim_name AS BINARY) <=> CAST(
                  COALESCE(NULLIF(TRIM(vin_decode.trim_name), ''), catalog_part.trim_name) AS BINARY
              )
              AND (
                  NULLIF(TRIM(vin_decode.model_name), '') IS NULL
                  OR CAST(REGEXP_REPLACE(UPPER(catalog_part.model), '[^A-Z0-9]', '') AS BINARY)
                      = CAST(REGEXP_REPLACE(UPPER(vin_decode.model_name), '[^A-Z0-9]', '') AS BINARY)
              )
              AND (
                  NULLIF(TRIM(vin_decode.engine_model), '') IS NULL
                  OR CAST(REGEXP_REPLACE(UPPER(catalog_part.engine), '[^A-Z0-9]', '') AS BINARY)
                      = CAST(REGEXP_REPLACE(UPPER(vin_decode.engine_model), '[^A-Z0-9]', '') AS BINARY)
              )
              AND (
                  NULLIF(TRIM(vin_decode.trim_name), '') IS NULL
                  OR CAST(REGEXP_REPLACE(UPPER(catalog_part.trim_name), '[^A-Z0-9]', '') AS BINARY)
                      = CAST(REGEXP_REPLACE(UPPER(vin_decode.trim_name), '[^A-Z0-9]', '') AS BINARY)
              )
          )
          OR (
              vehicle_mapping.source_name = 'manual-name-override'
              AND NULLIF(TRIM(vehicle_mapping.source_reference), '') IS NOT NULL
              AND CAST(vehicle_mapping.make_name AS BINARY) = CAST(vin_decode.make_name AS BINARY)
              AND CAST(vehicle_mapping.model_name AS BINARY) = CAST(vin_decode.model_name AS BINARY)
              AND vehicle_mapping.model_year <=> vin_decode.model_year
              AND CAST(vehicle_mapping.engine AS BINARY)
                  <=> CAST(vin_decode.engine_model AS BINARY)
              AND CAST(vehicle_mapping.trim_name AS BINARY) <=> CAST(vin_decode.trim_name AS BINARY)
              AND CAST(REGEXP_REPLACE(UPPER(catalog_part.brand), '[^A-Z0-9]', '') AS BINARY)
                  = CAST(REGEXP_REPLACE(
                      UPPER(vin_decode.make_name), '[^A-Z0-9]', ''
                  ) AS BINARY)
          )
          OR (
              vehicle_mapping.source_name NOT IN (
                  'manual-name-override', 'manual-sparse-override'
              )
              AND vehicle_mapping.model_year <=> vin_decode.model_year
              AND NULLIF(TRIM(vin_decode.model_name), '') IS NOT NULL
              AND NULLIF(TRIM(vin_decode.engine_model), '') IS NOT NULL
              AND NULLIF(TRIM(vin_decode.trim_name), '') IS NOT NULL
              AND CAST(REGEXP_REPLACE(
                  UPPER(vehicle_mapping.make_name), '[^A-Z0-9]', ''
              ) AS BINARY) = CAST(REGEXP_REPLACE(
                  UPPER(vin_decode.make_name), '[^A-Z0-9]', ''
              ) AS BINARY)
              AND CAST(REGEXP_REPLACE(
                  UPPER(vehicle_mapping.model_name), '[^A-Z0-9]', ''
              ) AS BINARY) = CAST(REGEXP_REPLACE(
                  UPPER(vin_decode.model_name), '[^A-Z0-9]', ''
              ) AS BINARY)
              AND CAST(REGEXP_REPLACE(
                  UPPER(vehicle_mapping.engine), '[^A-Z0-9]', ''
              ) AS BINARY) = CAST(REGEXP_REPLACE(
                  UPPER(vin_decode.engine_model), '[^A-Z0-9]', ''
              ) AS BINARY)
              AND CAST(REGEXP_REPLACE(
                  UPPER(vehicle_mapping.trim_name), '[^A-Z0-9]', ''
              ) AS BINARY) = CAST(REGEXP_REPLACE(
                  UPPER(vin_decode.trim_name), '[^A-Z0-9]', ''
              ) AS BINARY)
              AND CAST(REGEXP_REPLACE(UPPER(catalog_part.brand), '[^A-Z0-9]', '') AS BINARY)
                  = CAST(REGEXP_REPLACE(
                      UPPER(vin_decode.make_name), '[^A-Z0-9]', ''
                  ) AS BINARY)
              AND CAST(REGEXP_REPLACE(UPPER(catalog_part.model), '[^A-Z0-9]', '') AS BINARY)
                  = CAST(REGEXP_REPLACE(
                      UPPER(vin_decode.model_name), '[^A-Z0-9]', ''
                  ) AS BINARY)
              AND CAST(REGEXP_REPLACE(UPPER(catalog_part.engine), '[^A-Z0-9]', '') AS BINARY)
                  = CAST(REGEXP_REPLACE(
                      UPPER(vin_decode.engine_model), '[^A-Z0-9]', ''
                  ) AS BINARY)
              AND CAST(REGEXP_REPLACE(
                  UPPER(catalog_part.trim_name), '[^A-Z0-9]', ''
              ) AS BINARY) = CAST(REGEXP_REPLACE(
                  UPPER(vin_decode.trim_name), '[^A-Z0-9]', ''
              ) AS BINARY)
              AND COALESCE(strict_vehicle_counts.strict_vehicle_count, 0) = 1
          )
      )
      AND (catalog_part.production_from IS NOT NULL OR catalog_part.production_to IS NOT NULL)
      AND (
          NULLIF(TRIM(catalog_part.part_range), '') IS NULL
          OR catalog_part.part_from IS NOT NULL
          OR catalog_part.part_to IS NOT NULL
      )
      AND NULLIF(TRIM(catalog_part.part_name), '') IS NOT NULL
) AS mapped_fitment
WHERE (mapped_fitment.fitment_from IS NULL OR mapped_fitment.model_year >= CAST(LEFT(mapped_fitment.fitment_from, 4) AS UNSIGNED))
  AND (mapped_fitment.fitment_to IS NULL OR mapped_fitment.model_year <= CAST(LEFT(mapped_fitment.fitment_to, 4) AS UNSIGNED))
  AND (
      mapped_fitment.fitment_from IS NULL
      OR mapped_fitment.fitment_to IS NULL
      OR mapped_fitment.fitment_from <= mapped_fitment.fitment_to
  );

CREATE TABLE IF NOT EXISTS admin_category_labels (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    category_main VARCHAR(256) NOT NULL,
    category_group VARCHAR(256) NOT NULL DEFAULT '',
    category_small VARCHAR(256) NOT NULL DEFAULT '',
    chinese_label VARCHAR(512) NOT NULL,
    common_chinese_label VARCHAR(512) NULL,
    source_name VARCHAR(64) NOT NULL DEFAULT 'manual',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_admin_category_label (category_main, category_group, category_small)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS admin_reconciliation_items (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    channel VARCHAR(32) NOT NULL,
    subject_key VARCHAR(512) NOT NULL,
    left_value JSON NULL,
    right_value JSON NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'open',
    resolution_note TEXT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    resolved_at DATETIME NULL,
    KEY idx_admin_reconciliation_status (channel, status, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS admin_crawl_requests (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    job_name VARCHAR(32) NOT NULL,
    requested_scope VARCHAR(64) NOT NULL DEFAULT 'all',
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    requested_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at DATETIME NULL,
    finished_at DATETIME NULL,
    error_message TEXT NULL,
    KEY idx_admin_crawl_request_status (status, requested_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

SET @add_crawl_run_schedule_fk = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE crawl_runs ADD CONSTRAINT fk_crawl_run_schedule '
        'FOREIGN KEY (scheduled_job_run_id) REFERENCES scheduled_job_runs(id)',
        'DO 0'
    )
    FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE()
      AND CONSTRAINT_NAME = 'fk_crawl_run_schedule'
      AND CONSTRAINT_TYPE = 'FOREIGN KEY'
);
PREPARE add_crawl_run_schedule_fk FROM @add_crawl_run_schedule_fk;
EXECUTE add_crawl_run_schedule_fk;
DEALLOCATE PREPARE add_crawl_run_schedule_fk;

SET @add_bounded_crawl_run_fk = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE bounded_parts ADD CONSTRAINT fk_bounded_crawl_run '
        'FOREIGN KEY (crawl_run_id) REFERENCES crawl_runs(id)',
        'DO 0'
    )
    FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE()
      AND CONSTRAINT_NAME = 'fk_bounded_crawl_run'
      AND CONSTRAINT_TYPE = 'FOREIGN KEY'
);
PREPARE add_bounded_crawl_run_fk FROM @add_bounded_crawl_run_fk;
EXECUTE add_bounded_crawl_run_fk;
DEALLOCATE PREPARE add_bounded_crawl_run_fk;
