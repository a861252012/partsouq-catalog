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
        'confirmed' AS vehicle_mapping_status,
        'compatible_by_model_year' AS fitment_status
    FROM admin_vehicle_mappings AS m
    JOIN nhtsa_vin_decodes AS d ON d.vin = m.vin
    JOIN published_parts AS p ON p.vehicle_id = m.partsouq_vehicle_id
    WHERE m.vin IS NOT NULL AND m.partsouq_vehicle_id IS NOT NULL
      AND p.vehicle_id IS NOT NULL
      AND CAST(m.make_name AS BINARY) = CAST(d.make_name AS BINARY)
      AND CAST(m.model_name AS BINARY) = CAST(d.model_name AS BINARY)
      AND m.model_year <=> d.model_year
      AND (
          NULLIF(TRIM(p.prod_period), '') IS NULL
          OR p.production_from IS NOT NULL
          OR p.production_to IS NOT NULL
      )
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

CREATE TABLE IF NOT EXISTS scheduled_job_runs (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    job_name VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL,
    started_at DATETIME NOT NULL,
    finished_at DATETIME NULL,
    exit_code INT NULL,
    output_text MEDIUMTEXT NULL,
    KEY idx_scheduled_job_runs_name_started (job_name, started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
