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

-- Full snapshot is authoritative only after both the crawl and its sole
-- daemon scheduler run have completed successfully. Legacy snapshots without
-- crawl_run_id stay hidden; a qualified bounded 10,000-row snapshot remains
-- visible until a traceable full snapshot is ready.
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
    p.crawl_run_id AS source_crawl_run_id,
    p.part_id, p.vehicle_id, p.model_id, p.vehicle_vid, p.brand, p.model,
    p.vehicle_name, p.vehicle_code, p.prod_period, p.production_from,
    p.production_to, p.engine, p.trim_name, p.part_name, p.part_number,
    p.part_number_normalized, p.category_id, p.category_cid, p.category_main,
    p.category_group, p.group_id, p.group_code, p.group_uid, p.part_range,
    p.part_from, p.part_to, p.source_url, p.note, p.quantity, p.code, p.snapshot_at
FROM published_parts AS p
JOIN qualified_full_runs ON qualified_full_runs.crawl_run_id = p.crawl_run_id
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
JOIN qualified_full_runs ON qualified_full_runs.crawl_run_id = previous.crawl_run_id
),
formal_full_parts AS (
SELECT formal_current_parts.*
FROM formal_current_parts
UNION ALL
SELECT formal_previous_parts.*
FROM formal_previous_parts
WHERE NOT EXISTS (SELECT 1 FROM formal_current_parts)
)
SELECT
    formal_full_parts.*
FROM formal_full_parts
UNION ALL
SELECT
    'bounded' AS dataset_scope,
    b.crawl_run_id AS source_crawl_run_id,
    b.part_id, b.vehicle_id, b.model_id, b.vehicle_vid, b.brand, b.model,
    b.vehicle_name, b.vehicle_code, b.prod_period, b.production_from,
    b.production_to, b.engine, b.trim_name, b.part_name, b.part_number,
    b.part_number_normalized, b.category_id, b.category_cid, b.category_main,
    b.category_group, b.group_id, b.group_code, b.group_uid, b.part_range,
    b.part_from, b.part_to, b.source_url, b.note, b.quantity, b.code, b.snapshot_at
FROM bounded_parts AS b
JOIN crawl_runs AS current_run
  ON current_run.id = b.crawl_run_id
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

CREATE OR REPLACE VIEW v_vin_part_fitments AS
SELECT mapped.*
FROM (
    SELECT
        d.vin,
        d.make_name,
        d.model_name,
        d.model_year,
        d.engine_configuration,
        d.engine_model,
        d.displacement_l,
        d.trim_name AS nhtsa_trim_name,
        d.source_url AS nhtsa_source_url,
        d.source_artifact_id AS nhtsa_source_artifact_id,
        p.part_id,
        p.dataset_scope AS catalog_dataset_scope,
        p.source_crawl_run_id AS catalog_crawl_run_id,
        p.model_id,
        p.vehicle_id,
        p.vehicle_vid,
        p.category_id,
        p.category_cid,
        p.group_id,
        p.group_uid,
        p.code,
        p.group_code,
        p.vehicle_id AS partsouq_vehicle_id,
        p.brand AS partsouq_brand,
        p.model AS partsouq_model,
        p.vehicle_name,
        p.vehicle_code,
        p.engine AS partsouq_engine,
        p.trim_name AS partsouq_trim_name,
        p.part_number,
        p.part_name,
        p.category_main,
        p.category_group,
        p.prod_period,
        p.part_range,
        CASE
            WHEN p.production_from IS NULL THEN p.part_from
            WHEN p.part_from IS NULL THEN p.production_from
            ELSE GREATEST(p.production_from, p.part_from)
        END AS fitment_from,
        CASE
            WHEN p.production_to IS NULL THEN p.part_to
            WHEN p.part_to IS NULL THEN p.production_to
            ELSE LEAST(p.production_to, p.part_to)
        END AS fitment_to,
        p.source_url,
        m.id AS mapping_id,
        m.source_name AS mapping_source_name,
        m.source_reference AS mapping_source_reference,
        CASE
            WHEN m.source_name = 'manual-name-override' THEN 'confirmed_manual_override'
            ELSE 'confirmed'
        END AS vehicle_mapping_status,
        CASE
            WHEN m.source_name = 'manual-name-override' THEN 'manual_vehicle_override'
            ELSE 'compatible_by_model_year_engine_trim'
        END AS fitment_status
    FROM admin_vehicle_mappings AS m
    JOIN nhtsa_vin_decodes AS d ON d.vin = m.vin
    JOIN v_current_catalog_parts AS p ON p.vehicle_id = m.partsouq_vehicle_id
    WHERE m.vin IS NOT NULL AND m.partsouq_vehicle_id IS NOT NULL
      AND p.vehicle_id IS NOT NULL
      AND CAST(m.make_name AS BINARY) = CAST(d.make_name AS BINARY)
      AND CAST(m.model_name AS BINARY) = CAST(d.model_name AS BINARY)
      AND m.model_year <=> d.model_year
      AND CAST(m.engine AS BINARY)
          <=> CAST(CONCAT_WS(' / ', d.engine_configuration, d.engine_model) AS BINARY)
      AND CAST(m.trim_name AS BINARY) <=> CAST(d.trim_name AS BINARY)
      AND NULLIF(TRIM(d.engine_configuration), '') IS NOT NULL
      AND d.displacement_l IS NOT NULL
      AND NULLIF(TRIM(d.trim_name), '') IS NOT NULL
      AND (
          m.source_name = 'manual-name-override'
          OR (
              CAST(REGEXP_REPLACE(UPPER(p.brand), '[^A-Z0-9]', '') AS BINARY)
                  = CAST(REGEXP_REPLACE(UPPER(d.make_name), '[^A-Z0-9]', '') AS BINARY)
              AND CAST(REGEXP_REPLACE(UPPER(p.model), '[^A-Z0-9]', '') AS BINARY)
                  = CAST(REGEXP_REPLACE(UPPER(d.model_name), '[^A-Z0-9]', '') AS BINARY)
              AND NULLIF(TRIM(d.engine_model), '') IS NOT NULL
              AND CAST(REGEXP_REPLACE(UPPER(p.engine), '[^A-Z0-9]', '') AS BINARY)
                  = CAST(REGEXP_REPLACE(UPPER(d.engine_model), '[^A-Z0-9]', '') AS BINARY)
              AND CAST(REGEXP_REPLACE(UPPER(p.trim_name), '[^A-Z0-9]', '') AS BINARY)
                  = CAST(REGEXP_REPLACE(UPPER(d.trim_name), '[^A-Z0-9]', '') AS BINARY)
          )
      )
      AND (p.production_from IS NOT NULL OR p.production_to IS NOT NULL)
      AND (
          NULLIF(TRIM(p.part_range), '') IS NULL
          OR p.part_from IS NOT NULL
          OR p.part_to IS NOT NULL
      )
      AND NULLIF(TRIM(p.part_name), '') IS NOT NULL
) AS mapped
WHERE (mapped.fitment_from IS NULL OR mapped.model_year >= CAST(LEFT(mapped.fitment_from, 4) AS UNSIGNED))
  AND (mapped.fitment_to IS NULL OR mapped.model_year <= CAST(LEFT(mapped.fitment_to, 4) AS UNSIGNED))
  AND (
      mapped.fitment_from IS NULL
      OR mapped.fitment_to IS NULL
      OR mapped.fitment_from <= mapped.fitment_to
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
