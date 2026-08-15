-- PartSouq crawler — schema migration 007：共用 MySQL VIN／零件 mapping
--
-- 升級來源：已完成 catalog migration 001-006、舊版 db/nhtsa.sql，以及
-- 舊版 db/admin.sql（也接受舊 admin.sql 因 oversized index 中途停止的狀態）。
--
-- 執行前必要條件：
--   1. 停止 admin、scheduler、PartSouq crawler 與 NHTSA sync 等所有 writer。
--   2. 完整備份 MySQL；特別保留 published_parts 與 admin_*。
--   3. 由 mysql 命令列明確指定 database；本檔不包含 USE。
--
-- 本 migration 可重跑，且不猜測無法證明的關聯：legacy published_parts
-- 只有在其既有可見欄位能唯一對回一個 normalized vehicle 時才補 vehicle_id。
-- 無法唯一對應的 snapshot row 原樣保留，vehicle_id 維持 NULL，VIN mapping
-- view 會排除它；下一次完整 success publish 會由 crawler 寫入正式 vehicle_id。
-- 任一 duplicate、orphan、反向年份，或必要舊 schema 欄位／索引不符時一律 fail closed。

SET SESSION lock_wait_timeout = 30;
SET SESSION innodb_lock_wait_timeout = 30;

-- -------------------------------------------------------------------------
-- 0. 輸入契約與 writer preflight
-- -------------------------------------------------------------------------

SET @partsouq_007_database_selected := DATABASE() IS NOT NULL;
SET @partsouq_007_required_tables := (
  SELECT COUNT(*)
  FROM information_schema.TABLES
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_TYPE = 'BASE TABLE'
    AND TABLE_NAME IN (
      'brands', 'models', 'vehicles', 'categories', 'groups_t', 'parts',
      'published_parts', 'crawl_state', 'crawl_runs',
      'nhtsa_sync_runs', 'nhtsa_source_artifacts'
    )
);
SET @partsouq_007_required_columns := (
  SELECT COUNT(*)
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND (
      (TABLE_NAME = 'vehicles' AND COLUMN_NAME IN (
        'id', 'model_id', 'identity_hash', 'name', 'model_code', 'prod_period',
        'grade', 'engine'
      ))
      OR (TABLE_NAME = 'groups_t' AND COLUMN_NAME IN (
        'id', 'category_id', 'code', 'verified_row_count'
      ))
      OR (TABLE_NAME = 'parts' AND COLUMN_NAME IN (
        'id', 'group_id', 'part_number', 'name', 'range_str', 'seen_run_id'
      ))
      OR (TABLE_NAME = 'published_parts' AND COLUMN_NAME IN (
        'part_id', 'brand', 'model', 'vehicle_name', 'vehicle_code',
        'prod_period', 'part_name', 'part_number', 'category_main',
        'category_group', 'group_code', 'part_range', 'snapshot_at'
      ))
    )
);
SET @partsouq_007_vehicle_v5_index := (
  SELECT COUNT(*)
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'vehicles'
    AND INDEX_NAME = 'uq_vehicle_identity_v5'
);

DROP PROCEDURE IF EXISTS assert_partsouq_007_input;
DELIMITER //
CREATE PROCEDURE assert_partsouq_007_input()
BEGIN
  IF @partsouq_007_database_selected <> 1 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 007: select an explicit database before running';
  END IF;
  IF @partsouq_007_required_tables <> 11 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 007: required catalog/NHTSA tables are missing';
  END IF;
  IF @partsouq_007_required_columns <> 31 OR @partsouq_007_vehicle_v5_index = 0 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 007: apply catalog migrations 001-006 first';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_007_input();
DROP PROCEDURE assert_partsouq_007_input;

SET @partsouq_007_catalog_writers := (
  SELECT COUNT(*) FROM crawl_runs WHERE status = 'running'
);
SET @partsouq_007_nhtsa_writers := (
  SELECT COUNT(*) FROM nhtsa_sync_runs WHERE status = 'running'
);

DROP PROCEDURE IF EXISTS assert_partsouq_007_base_writers_stopped;
DELIMITER //
CREATE PROCEDURE assert_partsouq_007_base_writers_stopped()
BEGIN
  IF @partsouq_007_catalog_writers > 0 OR @partsouq_007_nhtsa_writers > 0 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 007: running catalog/NHTSA jobs exist; stop writers first';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_007_base_writers_stopped();
DROP PROCEDURE assert_partsouq_007_base_writers_stopped;

-- -------------------------------------------------------------------------
-- 1. NHTSA VIN decode table
-- -------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS nhtsa_vin_decodes (
  vin CHAR(17) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
  make_name VARCHAR(255) NOT NULL,
  model_name VARCHAR(512) NOT NULL,
  model_year SMALLINT UNSIGNED NOT NULL,
  engine_configuration VARCHAR(255) NULL,
  engine_model VARCHAR(255) NULL,
  displacement_l DECIMAL(14,9) NULL,
  trim_name VARCHAR(255) NULL,
  series_name VARCHAR(255) NULL,
  error_code VARCHAR(64) NOT NULL,
  error_text TEXT NULL,
  payload_json JSON NOT NULL,
  source_url TEXT NOT NULL,
  source_artifact_id BIGINT UNSIGNED NOT NULL,
  decoded_at DATETIME(6) NOT NULL,
  INDEX idx_nhtsa_vin_vehicle (make_name, model_name(191), model_year),
  CONSTRAINT fk_nhtsa_vin_artifact
    FOREIGN KEY (source_artifact_id) REFERENCES nhtsa_source_artifacts(id)
) ENGINE=InnoDB;

DROP PROCEDURE IF EXISTS upgrade_partsouq_007_nhtsa_contract;
DELIMITER //
CREATE PROCEDURE upgrade_partsouq_007_nhtsa_contract()
BEGIN
  DECLARE required_columns INT DEFAULT 0;
  DECLARE primary_columns VARCHAR(255) DEFAULT '';
  DECLARE vehicle_index_rows INT DEFAULT 0;
  DECLARE vehicle_index_non_unique INT DEFAULT 1;
  DECLARE vehicle_index_columns VARCHAR(255) DEFAULT '';
  DECLARE artifact_fk_rows INT DEFAULT 0;
  DECLARE artifact_fk_valid INT DEFAULT 0;
  DECLARE artifact_orphans BIGINT DEFAULT 0;

  SELECT COUNT(*) INTO required_columns
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'nhtsa_vin_decodes'
    AND COLUMN_NAME IN (
      'vin', 'make_name', 'model_name', 'model_year', 'engine_configuration',
      'engine_model', 'displacement_l', 'trim_name', 'series_name', 'error_code',
      'error_text', 'payload_json', 'source_url', 'source_artifact_id', 'decoded_at'
    );
  IF required_columns <> 15 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 007: nhtsa_vin_decodes has an incompatible shape';
  END IF;

  SELECT COALESCE(GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX), '')
    INTO primary_columns
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'nhtsa_vin_decodes'
    AND INDEX_NAME = 'PRIMARY';
  IF primary_columns <> 'vin' THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 007: nhtsa_vin_decodes primary key must be vin';
  END IF;

  SELECT COUNT(*), COALESCE(MAX(NON_UNIQUE), 1),
         COALESCE(GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX), '')
    INTO vehicle_index_rows, vehicle_index_non_unique, vehicle_index_columns
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'nhtsa_vin_decodes'
    AND INDEX_NAME = 'idx_nhtsa_vin_vehicle';
  IF vehicle_index_rows > 0
     AND (vehicle_index_non_unique <> 1
          OR vehicle_index_columns <> 'make_name,model_name,model_year') THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 007: idx_nhtsa_vin_vehicle definition mismatch';
  END IF;
  IF vehicle_index_rows = 0 THEN
    ALTER TABLE nhtsa_vin_decodes
      ADD INDEX idx_nhtsa_vin_vehicle (make_name, model_name(191), model_year);
  END IF;

  SELECT COUNT(*) INTO artifact_fk_rows
  FROM information_schema.TABLE_CONSTRAINTS
  WHERE CONSTRAINT_SCHEMA = DATABASE()
    AND TABLE_NAME = 'nhtsa_vin_decodes'
    AND CONSTRAINT_NAME = 'fk_nhtsa_vin_artifact'
    AND CONSTRAINT_TYPE = 'FOREIGN KEY';
  SELECT COUNT(*) INTO artifact_fk_valid
  FROM information_schema.KEY_COLUMN_USAGE
  WHERE CONSTRAINT_SCHEMA = DATABASE()
    AND TABLE_NAME = 'nhtsa_vin_decodes'
    AND CONSTRAINT_NAME = 'fk_nhtsa_vin_artifact'
    AND COLUMN_NAME = 'source_artifact_id'
    AND REFERENCED_TABLE_NAME = 'nhtsa_source_artifacts'
    AND REFERENCED_COLUMN_NAME = 'id';
  IF artifact_fk_rows > 0 AND artifact_fk_valid <> 1 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 007: fk_nhtsa_vin_artifact definition mismatch';
  END IF;
  IF artifact_fk_rows = 0 THEN
    SELECT COUNT(*) INTO artifact_orphans
    FROM nhtsa_vin_decodes AS d
    LEFT JOIN nhtsa_source_artifacts AS a ON a.id = d.source_artifact_id
    WHERE a.id IS NULL;
    IF artifact_orphans > 0 THEN
      SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'migration 007: orphan NHTSA VIN artifacts require manual repair';
    END IF;
    ALTER TABLE nhtsa_vin_decodes
      ADD CONSTRAINT fk_nhtsa_vin_artifact
      FOREIGN KEY (source_artifact_id) REFERENCES nhtsa_source_artifacts(id);
  END IF;
END//
DELIMITER ;
CALL upgrade_partsouq_007_nhtsa_contract();
DROP PROCEDURE upgrade_partsouq_007_nhtsa_contract;

-- -------------------------------------------------------------------------
-- 2. 建立缺失的後台基礎表
--
-- 舊版 admin_part_translations 的 utf8mb4 複合 unique 可能超過 InnoDB
-- 3072-byte key limit，導致舊 admin.sql 只建立第一張表便中止。因此這裡
-- 使用新版 shape 補齊完全缺失的表；已存在的舊表留待第 5 節原地升級。
-- -------------------------------------------------------------------------

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

SET @partsouq_007_admin_writers := (
  SELECT COUNT(*) FROM admin_crawl_requests WHERE status = 'running'
);
SET @partsouq_007_scheduler_writers := (
  SELECT COUNT(*) FROM scheduled_job_runs WHERE status = 'running'
);

DROP PROCEDURE IF EXISTS assert_partsouq_007_admin_writers_stopped;
DELIMITER //
CREATE PROCEDURE assert_partsouq_007_admin_writers_stopped()
BEGIN
  IF @partsouq_007_admin_writers > 0 OR @partsouq_007_scheduler_writers > 0 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 007: running admin/scheduler jobs exist; stop writers first';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_007_admin_writers_stopped();
DROP PROCEDURE assert_partsouq_007_admin_writers_stopped;

-- -------------------------------------------------------------------------
-- 3. Catalog 欄位與 normalized range backfill
-- -------------------------------------------------------------------------

DROP PROCEDURE IF EXISTS upgrade_partsouq_007_catalog_columns;
DELIMITER //
CREATE PROCEDURE upgrade_partsouq_007_catalog_columns()
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'vehicles'
      AND COLUMN_NAME = 'production_from'
  ) THEN
    ALTER TABLE vehicles
      ADD COLUMN production_from CHAR(7) NULL AFTER prod_period;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'vehicles'
      AND COLUMN_NAME = 'production_to'
  ) THEN
    ALTER TABLE vehicles
      ADD COLUMN production_to CHAR(7) NULL AFTER production_from;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'parts'
      AND COLUMN_NAME = 'part_from'
  ) THEN
    ALTER TABLE parts ADD COLUMN part_from CHAR(7) NULL AFTER range_str;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'parts'
      AND COLUMN_NAME = 'part_to'
  ) THEN
    ALTER TABLE parts ADD COLUMN part_to CHAR(7) NULL AFTER part_from;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts'
      AND COLUMN_NAME = 'vehicle_id'
  ) THEN
    ALTER TABLE published_parts ADD COLUMN vehicle_id INT NULL AFTER part_id;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts'
      AND COLUMN_NAME = 'production_from'
  ) THEN
    ALTER TABLE published_parts
      ADD COLUMN production_from CHAR(7) NULL AFTER prod_period;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts'
      AND COLUMN_NAME = 'production_to'
  ) THEN
    ALTER TABLE published_parts
      ADD COLUMN production_to CHAR(7) NULL AFTER production_from;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts'
      AND COLUMN_NAME = 'engine'
  ) THEN
    ALTER TABLE published_parts ADD COLUMN engine VARCHAR(256) NULL AFTER production_to;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts'
      AND COLUMN_NAME = 'trim_name'
  ) THEN
    ALTER TABLE published_parts ADD COLUMN trim_name VARCHAR(256) NULL AFTER engine;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts'
      AND COLUMN_NAME = 'part_from'
  ) THEN
    ALTER TABLE published_parts ADD COLUMN part_from CHAR(7) NULL AFTER part_range;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts'
      AND COLUMN_NAME = 'part_to'
  ) THEN
    ALTER TABLE published_parts ADD COLUMN part_to CHAR(7) NULL AFTER part_from;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts'
      AND COLUMN_NAME = 'source_url'
  ) THEN
    ALTER TABLE published_parts ADD COLUMN source_url VARCHAR(1024) NULL AFTER part_to;
  END IF;
END//
DELIMITER ;
CALL upgrade_partsouq_007_catalog_columns();
DROP PROCEDURE upgrade_partsouq_007_catalog_columns;

-- 所有 range 先進 temporary staging。只解析與 Python parser 相同且能明確
-- 驗證月份／順序的格式；未知、錯月或反向區間維持 NULL。
DROP TEMPORARY TABLE IF EXISTS tmp_partsouq_007_ranges;
CREATE TEMPORARY TABLE tmp_partsouq_007_ranges (
  source_kind CHAR(1) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  source_id BIGINT NOT NULL,
  raw_value VARCHAR(64) NULL,
  range_from CHAR(7) NULL,
  range_to CHAR(7) NULL,
  PRIMARY KEY (source_kind, source_id)
) ENGINE=InnoDB;

INSERT INTO tmp_partsouq_007_ranges(source_kind, source_id, raw_value)
SELECT 'V', id, prod_period FROM vehicles
WHERE production_from IS NULL AND production_to IS NULL;
INSERT INTO tmp_partsouq_007_ranges(source_kind, source_id, raw_value)
SELECT 'P', id, range_str FROM parts
WHERE part_from IS NULL AND part_to IS NULL;
INSERT INTO tmp_partsouq_007_ranges(source_kind, source_id, raw_value)
SELECT 'R', part_id, prod_period FROM published_parts
WHERE production_from IS NULL AND production_to IS NULL;
INSERT INTO tmp_partsouq_007_ranges(source_kind, source_id, raw_value)
SELECT 'F', part_id, part_range FROM published_parts
WHERE part_from IS NULL AND part_to IS NULL;

-- PartSouq month range: 01.2017 - 12.2020
UPDATE tmp_partsouq_007_ranges
SET range_from = CONCAT(
      RIGHT(REGEXP_SUBSTR(TRIM(raw_value), '(0[1-9]|1[0-2])[.][0-9]{4}', 1, 1), 4),
      '-',
      LEFT(REGEXP_SUBSTR(TRIM(raw_value), '(0[1-9]|1[0-2])[.][0-9]{4}', 1, 1), 2)
    ),
    range_to = CONCAT(
      RIGHT(REGEXP_SUBSTR(TRIM(raw_value), '(0[1-9]|1[0-2])[.][0-9]{4}', 1, 2), 4),
      '-',
      LEFT(REGEXP_SUBSTR(TRIM(raw_value), '(0[1-9]|1[0-2])[.][0-9]{4}', 1, 2), 2)
    )
WHERE raw_value IS NOT NULL
  AND TRIM(raw_value) REGEXP
      '^(0[1-9]|1[0-2])[.][0-9]{4}[[:space:]]*-[[:space:]]*(0[1-9]|1[0-2])[.][0-9]{4}$'
  AND CAST(RIGHT(
        REGEXP_SUBSTR(TRIM(raw_value), '(0[1-9]|1[0-2])[.][0-9]{4}', 1, 1), 4
      ) AS UNSIGNED) BETWEEN 1886 AND 2100
  AND CAST(RIGHT(
        REGEXP_SUBSTR(TRIM(raw_value), '(0[1-9]|1[0-2])[.][0-9]{4}', 1, 2), 4
      ) AS UNSIGNED) BETWEEN 1886 AND 2100
  AND CONCAT(
        RIGHT(REGEXP_SUBSTR(TRIM(raw_value), '(0[1-9]|1[0-2])[.][0-9]{4}', 1, 1), 4),
        '-',
        LEFT(REGEXP_SUBSTR(TRIM(raw_value), '(0[1-9]|1[0-2])[.][0-9]{4}', 1, 1), 2)
      ) <= CONCAT(
        RIGHT(REGEXP_SUBSTR(TRIM(raw_value), '(0[1-9]|1[0-2])[.][0-9]{4}', 1, 2), 4),
        '-',
        LEFT(REGEXP_SUBSTR(TRIM(raw_value), '(0[1-9]|1[0-2])[.][0-9]{4}', 1, 2), 2)
      );

-- Open PartSouq month ranges: 01.2017 - / - 12.2020
UPDATE tmp_partsouq_007_ranges
SET range_from = CONCAT(SUBSTRING(TRIM(raw_value), 4, 4), '-', LEFT(TRIM(raw_value), 2))
WHERE range_from IS NULL AND range_to IS NULL
  AND raw_value IS NOT NULL
  AND TRIM(raw_value) REGEXP
      '^(0[1-9]|1[0-2])[.][0-9]{4}[[:space:]]*-[[:space:]]*$'
  AND CAST(RIGHT(
        REGEXP_SUBSTR(TRIM(raw_value), '(0[1-9]|1[0-2])[.][0-9]{4}', 1, 1), 4
      ) AS UNSIGNED) BETWEEN 1886 AND 2100;

UPDATE tmp_partsouq_007_ranges
SET range_to = CONCAT(
      RIGHT(REGEXP_SUBSTR(TRIM(raw_value), '(0[1-9]|1[0-2])[.][0-9]{4}', 1, 1), 4),
      '-',
      LEFT(REGEXP_SUBSTR(TRIM(raw_value), '(0[1-9]|1[0-2])[.][0-9]{4}', 1, 1), 2)
    )
WHERE range_from IS NULL AND range_to IS NULL
  AND raw_value IS NOT NULL
  AND TRIM(raw_value) REGEXP
      '^-[[:space:]]*(0[1-9]|1[0-2])[.][0-9]{4}$'
  AND CAST(RIGHT(
        REGEXP_SUBSTR(TRIM(raw_value), '(0[1-9]|1[0-2])[.][0-9]{4}', 1, 1), 4
      ) AS UNSIGNED) BETWEEN 1886 AND 2100;

-- Year range: 2017 - 2020 / 2017 - / - 2020 / 2017
UPDATE tmp_partsouq_007_ranges
SET range_from = CONCAT(LEFT(TRIM(raw_value), 4), '-01'),
    range_to = CONCAT(RIGHT(TRIM(raw_value), 4), '-12')
WHERE range_from IS NULL AND range_to IS NULL
  AND raw_value IS NOT NULL
  AND TRIM(raw_value) REGEXP '^[0-9]{4}[[:space:]]*-[[:space:]]*[0-9]{4}$'
  AND CAST(LEFT(TRIM(raw_value), 4) AS UNSIGNED) BETWEEN 1886 AND 2100
  AND CAST(RIGHT(TRIM(raw_value), 4) AS UNSIGNED) BETWEEN 1886 AND 2100
  AND LEFT(TRIM(raw_value), 4) <= RIGHT(TRIM(raw_value), 4);

UPDATE tmp_partsouq_007_ranges
SET range_from = CONCAT(LEFT(TRIM(raw_value), 4), '-01')
WHERE range_from IS NULL AND range_to IS NULL
  AND raw_value IS NOT NULL
  AND TRIM(raw_value) REGEXP '^[0-9]{4}[[:space:]]*-[[:space:]]*$'
  AND CAST(LEFT(TRIM(raw_value), 4) AS UNSIGNED) BETWEEN 1886 AND 2100;

UPDATE tmp_partsouq_007_ranges
SET range_to = CONCAT(RIGHT(TRIM(raw_value), 4), '-12')
WHERE range_from IS NULL AND range_to IS NULL
  AND raw_value IS NOT NULL
  AND TRIM(raw_value) REGEXP '^-[[:space:]]*[0-9]{4}$'
  AND CAST(RIGHT(TRIM(raw_value), 4) AS UNSIGNED) BETWEEN 1886 AND 2100;

UPDATE tmp_partsouq_007_ranges
SET range_from = CONCAT(TRIM(raw_value), '-01'),
    range_to = CONCAT(TRIM(raw_value), '-12')
WHERE range_from IS NULL AND range_to IS NULL
  AND raw_value IS NOT NULL
  AND TRIM(raw_value) REGEXP '^[0-9]{4}$'
  AND CAST(TRIM(raw_value) AS UNSIGNED) BETWEEN 1886 AND 2100;

-- ISO month/date range accepted by the shared parser.
UPDATE tmp_partsouq_007_ranges
SET range_from = REGEXP_SUBSTR(
      TRIM(raw_value), '[0-9]{4}-(0[1-9]|1[0-2])', 1, 1
    ),
    range_to = REGEXP_SUBSTR(
      TRIM(raw_value), '[0-9]{4}-(0[1-9]|1[0-2])', 1, 2
    )
WHERE range_from IS NULL AND range_to IS NULL
  AND raw_value IS NOT NULL
  AND TRIM(raw_value) REGEXP
      '^[0-9]{4}-(0[1-9]|1[0-2])(-[0-9]{2})?[[:space:]]+-[[:space:]]+[0-9]{4}-(0[1-9]|1[0-2])(-[0-9]{2})?$'
  AND CAST(LEFT(
        REGEXP_SUBSTR(TRIM(raw_value), '[0-9]{4}-(0[1-9]|1[0-2])', 1, 1), 4
      ) AS UNSIGNED) BETWEEN 1886 AND 2100
  AND CAST(LEFT(
        REGEXP_SUBSTR(TRIM(raw_value), '[0-9]{4}-(0[1-9]|1[0-2])', 1, 2), 4
      ) AS UNSIGNED) BETWEEN 1886 AND 2100
  AND REGEXP_SUBSTR(TRIM(raw_value), '[0-9]{4}-(0[1-9]|1[0-2])', 1, 1)
      <= REGEXP_SUBSTR(TRIM(raw_value), '[0-9]{4}-(0[1-9]|1[0-2])', 1, 2);

UPDATE tmp_partsouq_007_ranges
SET range_from = REGEXP_SUBSTR(
      TRIM(raw_value), '[0-9]{4}-(0[1-9]|1[0-2])', 1, 1
    )
WHERE range_from IS NULL AND range_to IS NULL
  AND raw_value IS NOT NULL
  AND TRIM(raw_value) REGEXP
      '^[0-9]{4}-(0[1-9]|1[0-2])(-[0-9]{2})?[[:space:]]+-[[:space:]]*$'
  AND CAST(LEFT(
        REGEXP_SUBSTR(TRIM(raw_value), '[0-9]{4}-(0[1-9]|1[0-2])', 1, 1), 4
      ) AS UNSIGNED) BETWEEN 1886 AND 2100;

UPDATE tmp_partsouq_007_ranges
SET range_from = LEFT(TRIM(raw_value), 7),
    range_to = LEFT(TRIM(raw_value), 7)
WHERE range_from IS NULL AND range_to IS NULL
  AND raw_value IS NOT NULL
  AND TRIM(raw_value) REGEXP '^[0-9]{4}-(0[1-9]|1[0-2])(-[0-9]{2})?$'
  AND CAST(LEFT(TRIM(raw_value), 4) AS UNSIGNED) BETWEEN 1886 AND 2100;

UPDATE vehicles AS v
JOIN tmp_partsouq_007_ranges AS r
  ON r.source_kind = 'V' AND r.source_id = v.id
SET v.production_from = r.range_from,
    v.production_to = r.range_to
WHERE v.production_from IS NULL AND v.production_to IS NULL;

UPDATE parts AS p
JOIN tmp_partsouq_007_ranges AS r
  ON r.source_kind = 'P' AND r.source_id = p.id
SET p.part_from = r.range_from,
    p.part_to = r.range_to
WHERE p.part_from IS NULL AND p.part_to IS NULL;

UPDATE published_parts AS p
JOIN tmp_partsouq_007_ranges AS r
  ON r.source_kind = 'R' AND r.source_id = p.part_id
SET p.production_from = r.range_from,
    p.production_to = r.range_to
WHERE p.production_from IS NULL AND p.production_to IS NULL;

UPDATE published_parts AS p
JOIN tmp_partsouq_007_ranges AS r
  ON r.source_kind = 'F' AND r.source_id = p.part_id
SET p.part_from = r.range_from,
    p.part_to = r.range_to
WHERE p.part_from IS NULL AND p.part_to IS NULL;

DROP TEMPORARY TABLE tmp_partsouq_007_ranges;

-- -------------------------------------------------------------------------
-- 4. Safe legacy published_parts vehicle backfill
-- -------------------------------------------------------------------------

DROP TEMPORARY TABLE IF EXISTS tmp_partsouq_007_published_vehicle;
CREATE TEMPORARY TABLE tmp_partsouq_007_published_vehicle (
  part_id INT NOT NULL PRIMARY KEY,
  vehicle_id INT NOT NULL,
  source_url VARCHAR(1024) NULL
) ENGINE=InnoDB;

-- 不信任 legacy published_parts.part_id。migration 004 的首次 snapshot 可能
-- 使用 ROW_NUMBER()；改用當時已發布的全部可見欄位反查，且只接受唯一
-- distinct vehicle。任何同外觀但不同 engine/grade 的車款會保持 NULL。
INSERT INTO tmp_partsouq_007_published_vehicle(part_id, vehicle_id, source_url)
SELECT
  pp.part_id,
  MIN(v.id) AS vehicle_id,
  CASE
    WHEN COUNT(DISTINCT COALESCE(g.url, '')) = 1 THEN MIN(g.url)
    ELSE NULL
  END AS source_url
FROM published_parts AS pp
JOIN parts AS p
  ON CAST(p.part_number AS BINARY) = CAST(pp.part_number AS BINARY)
 AND CAST(p.name AS BINARY) <=> CAST(pp.part_name AS BINARY)
 AND CAST(p.range_str AS BINARY) = CAST(pp.part_range AS BINARY)
 AND CAST(p.code AS BINARY) <=> CAST(pp.code AS BINARY)
 AND CAST(p.note AS BINARY) <=> CAST(pp.note AS BINARY)
 AND CAST(p.quantity AS BINARY) <=> CAST(pp.quantity AS BINARY)
JOIN groups_t AS g
  ON g.id = p.group_id
 AND CAST(g.code AS BINARY) = CAST(pp.group_code AS BINARY)
 AND CAST(g.name AS BINARY) <=> CAST(pp.category_group AS BINARY)
JOIN categories AS c
  ON c.id = g.category_id
 AND CAST(c.name AS BINARY) = CAST(pp.category_main AS BINARY)
JOIN vehicles AS v
  ON v.id = c.vehicle_id
 AND CAST(v.name AS BINARY) = CAST(pp.vehicle_name AS BINARY)
 AND CAST(v.model_code AS BINARY) = CAST(pp.vehicle_code AS BINARY)
 AND CAST(v.prod_period AS BINARY) <=> CAST(pp.prod_period AS BINARY)
JOIN models AS m
  ON m.id = v.model_id
 AND CAST(m.name AS BINARY) = CAST(pp.model AS BINARY)
JOIN brands AS b
  ON b.id = m.brand_id
 AND CAST(b.name AS BINARY) = CAST(pp.brand AS BINARY)
WHERE pp.vehicle_id IS NULL
GROUP BY pp.part_id
HAVING COUNT(DISTINCT v.id) = 1;

UPDATE published_parts AS pp
JOIN tmp_partsouq_007_published_vehicle AS matched ON matched.part_id = pp.part_id
JOIN vehicles AS v ON v.id = matched.vehicle_id
SET pp.vehicle_id = matched.vehicle_id,
    pp.production_from = COALESCE(pp.production_from, v.production_from),
    pp.production_to = COALESCE(pp.production_to, v.production_to),
    pp.engine = COALESCE(pp.engine, v.engine),
    pp.trim_name = COALESCE(pp.trim_name, v.grade),
    pp.source_url = COALESCE(pp.source_url, matched.source_url)
WHERE pp.vehicle_id IS NULL;

DROP TEMPORARY TABLE tmp_partsouq_007_published_vehicle;

SET @partsouq_007_bad_vehicle_ranges := (
  SELECT COUNT(*) FROM vehicles
  WHERE (production_from IS NOT NULL
         AND (production_from NOT REGEXP '^[0-9]{4}-(0[1-9]|1[0-2])$'
              OR CAST(LEFT(production_from, 4) AS UNSIGNED) NOT BETWEEN 1886 AND 2100))
     OR (production_to IS NOT NULL
         AND (production_to NOT REGEXP '^[0-9]{4}-(0[1-9]|1[0-2])$'
              OR CAST(LEFT(production_to, 4) AS UNSIGNED) NOT BETWEEN 1886 AND 2100))
     OR (production_from IS NOT NULL AND production_to IS NOT NULL
         AND production_from > production_to)
);
SET @partsouq_007_bad_part_ranges := (
  SELECT COUNT(*) FROM parts
  WHERE (part_from IS NOT NULL
         AND (part_from NOT REGEXP '^[0-9]{4}-(0[1-9]|1[0-2])$'
              OR CAST(LEFT(part_from, 4) AS UNSIGNED) NOT BETWEEN 1886 AND 2100))
     OR (part_to IS NOT NULL
         AND (part_to NOT REGEXP '^[0-9]{4}-(0[1-9]|1[0-2])$'
              OR CAST(LEFT(part_to, 4) AS UNSIGNED) NOT BETWEEN 1886 AND 2100))
     OR (part_from IS NOT NULL AND part_to IS NOT NULL AND part_from > part_to)
);
SET @partsouq_007_bad_published_ranges := (
  SELECT COUNT(*) FROM published_parts
  WHERE (production_from IS NOT NULL
         AND (production_from NOT REGEXP '^[0-9]{4}-(0[1-9]|1[0-2])$'
              OR CAST(LEFT(production_from, 4) AS UNSIGNED) NOT BETWEEN 1886 AND 2100))
     OR (production_to IS NOT NULL
         AND (production_to NOT REGEXP '^[0-9]{4}-(0[1-9]|1[0-2])$'
              OR CAST(LEFT(production_to, 4) AS UNSIGNED) NOT BETWEEN 1886 AND 2100))
     OR (production_from IS NOT NULL AND production_to IS NOT NULL
         AND production_from > production_to)
     OR (part_from IS NOT NULL
         AND (part_from NOT REGEXP '^[0-9]{4}-(0[1-9]|1[0-2])$'
              OR CAST(LEFT(part_from, 4) AS UNSIGNED) NOT BETWEEN 1886 AND 2100))
     OR (part_to IS NOT NULL
         AND (part_to NOT REGEXP '^[0-9]{4}-(0[1-9]|1[0-2])$'
              OR CAST(LEFT(part_to, 4) AS UNSIGNED) NOT BETWEEN 1886 AND 2100))
     OR (part_from IS NOT NULL AND part_to IS NOT NULL AND part_from > part_to)
);

DROP PROCEDURE IF EXISTS assert_partsouq_007_ranges;
DELIMITER //
CREATE PROCEDURE assert_partsouq_007_ranges()
BEGIN
  IF @partsouq_007_bad_vehicle_ranges > 0
     OR @partsouq_007_bad_part_ranges > 0
     OR @partsouq_007_bad_published_ranges > 0 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 007: invalid normalized date ranges require manual repair';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_007_ranges();
DROP PROCEDURE assert_partsouq_007_ranges;

DROP PROCEDURE IF EXISTS upgrade_partsouq_007_catalog_contracts;
DELIMITER //
CREATE PROCEDURE upgrade_partsouq_007_catalog_contracts()
BEGIN
  DECLARE index_rows INT DEFAULT 0;
  DECLARE index_columns VARCHAR(255) DEFAULT '';
  DECLARE index_non_unique INT DEFAULT 1;
  DECLARE null_rows BIGINT DEFAULT 0;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'vehicles'
      AND CONSTRAINT_NAME = 'chk_vehicle_production_from' AND CONSTRAINT_TYPE = 'CHECK'
  ) THEN
    ALTER TABLE vehicles ADD CONSTRAINT chk_vehicle_production_from CHECK (
      production_from IS NULL OR (
        production_from REGEXP '^[0-9]{4}-(0[1-9]|1[0-2])$'
        AND CAST(LEFT(production_from, 4) AS UNSIGNED) BETWEEN 1886 AND 2100
      )
    );
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'vehicles'
      AND CONSTRAINT_NAME = 'chk_vehicle_production_to' AND CONSTRAINT_TYPE = 'CHECK'
  ) THEN
    ALTER TABLE vehicles ADD CONSTRAINT chk_vehicle_production_to CHECK (
      production_to IS NULL OR (
        production_to REGEXP '^[0-9]{4}-(0[1-9]|1[0-2])$'
        AND CAST(LEFT(production_to, 4) AS UNSIGNED) BETWEEN 1886 AND 2100
      )
    );
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'vehicles'
      AND CONSTRAINT_NAME = 'chk_vehicle_production_order' AND CONSTRAINT_TYPE = 'CHECK'
  ) THEN
    ALTER TABLE vehicles ADD CONSTRAINT chk_vehicle_production_order CHECK (
      production_from IS NULL OR production_to IS NULL OR production_from <= production_to
    );
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'parts'
      AND CONSTRAINT_NAME = 'chk_part_from' AND CONSTRAINT_TYPE = 'CHECK'
  ) THEN
    ALTER TABLE parts ADD CONSTRAINT chk_part_from CHECK (
      part_from IS NULL OR (
        part_from REGEXP '^[0-9]{4}-(0[1-9]|1[0-2])$'
        AND CAST(LEFT(part_from, 4) AS UNSIGNED) BETWEEN 1886 AND 2100
      )
    );
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'parts'
      AND CONSTRAINT_NAME = 'chk_part_to' AND CONSTRAINT_TYPE = 'CHECK'
  ) THEN
    ALTER TABLE parts ADD CONSTRAINT chk_part_to CHECK (
      part_to IS NULL OR (
        part_to REGEXP '^[0-9]{4}-(0[1-9]|1[0-2])$'
        AND CAST(LEFT(part_to, 4) AS UNSIGNED) BETWEEN 1886 AND 2100
      )
    );
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'parts'
      AND CONSTRAINT_NAME = 'chk_part_range_order' AND CONSTRAINT_TYPE = 'CHECK'
  ) THEN
    ALTER TABLE parts ADD CONSTRAINT chk_part_range_order CHECK (
      part_from IS NULL OR part_to IS NULL OR part_from <= part_to
    );
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts'
      AND CONSTRAINT_NAME = 'chk_published_production_from' AND CONSTRAINT_TYPE = 'CHECK'
  ) THEN
    ALTER TABLE published_parts ADD CONSTRAINT chk_published_production_from CHECK (
      production_from IS NULL OR (
        production_from REGEXP '^[0-9]{4}-(0[1-9]|1[0-2])$'
        AND CAST(LEFT(production_from, 4) AS UNSIGNED) BETWEEN 1886 AND 2100
      )
    );
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts'
      AND CONSTRAINT_NAME = 'chk_published_production_to' AND CONSTRAINT_TYPE = 'CHECK'
  ) THEN
    ALTER TABLE published_parts ADD CONSTRAINT chk_published_production_to CHECK (
      production_to IS NULL OR (
        production_to REGEXP '^[0-9]{4}-(0[1-9]|1[0-2])$'
        AND CAST(LEFT(production_to, 4) AS UNSIGNED) BETWEEN 1886 AND 2100
      )
    );
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts'
      AND CONSTRAINT_NAME = 'chk_published_production_order' AND CONSTRAINT_TYPE = 'CHECK'
  ) THEN
    ALTER TABLE published_parts ADD CONSTRAINT chk_published_production_order CHECK (
      production_from IS NULL OR production_to IS NULL OR production_from <= production_to
    );
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts'
      AND CONSTRAINT_NAME = 'chk_published_part_from' AND CONSTRAINT_TYPE = 'CHECK'
  ) THEN
    ALTER TABLE published_parts ADD CONSTRAINT chk_published_part_from CHECK (
      part_from IS NULL OR (
        part_from REGEXP '^[0-9]{4}-(0[1-9]|1[0-2])$'
        AND CAST(LEFT(part_from, 4) AS UNSIGNED) BETWEEN 1886 AND 2100
      )
    );
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts'
      AND CONSTRAINT_NAME = 'chk_published_part_to' AND CONSTRAINT_TYPE = 'CHECK'
  ) THEN
    ALTER TABLE published_parts ADD CONSTRAINT chk_published_part_to CHECK (
      part_to IS NULL OR (
        part_to REGEXP '^[0-9]{4}-(0[1-9]|1[0-2])$'
        AND CAST(LEFT(part_to, 4) AS UNSIGNED) BETWEEN 1886 AND 2100
      )
    );
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts'
      AND CONSTRAINT_NAME = 'chk_published_part_order' AND CONSTRAINT_TYPE = 'CHECK'
  ) THEN
    ALTER TABLE published_parts ADD CONSTRAINT chk_published_part_order CHECK (
      part_from IS NULL OR part_to IS NULL OR part_from <= part_to
    );
  END IF;

  SELECT COUNT(*), COALESCE(MAX(NON_UNIQUE), 1),
         COALESCE(GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX), '')
    INTO index_rows, index_non_unique, index_columns
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts'
    AND INDEX_NAME = 'idx_published_vehicle';
  IF index_rows > 0 AND (index_non_unique <> 1 OR index_columns <> 'vehicle_id') THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 007: idx_published_vehicle definition mismatch';
  END IF;
  IF index_rows = 0 THEN
    ALTER TABLE published_parts ADD INDEX idx_published_vehicle (vehicle_id);
  END IF;

  SELECT COUNT(*) INTO null_rows FROM parts WHERE name IS NULL;
  IF null_rows = 0 AND EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'parts'
      AND COLUMN_NAME = 'name' AND IS_NULLABLE = 'YES'
  ) THEN
    ALTER TABLE parts MODIFY name VARCHAR(512) NOT NULL;
  END IF;

  SELECT COUNT(*) INTO null_rows FROM published_parts WHERE part_name IS NULL;
  IF null_rows = 0 AND EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts'
      AND COLUMN_NAME = 'part_name' AND IS_NULLABLE = 'YES'
  ) THEN
    ALTER TABLE published_parts MODIFY part_name VARCHAR(512) NOT NULL;
  END IF;

  -- Fresh／fully matched snapshots use the target NOT NULL contract. Legacy
  -- ambiguous rows deliberately keep this column nullable until republished.
  SELECT COUNT(*) INTO null_rows FROM published_parts WHERE vehicle_id IS NULL;
  IF null_rows = 0 AND EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts'
      AND COLUMN_NAME = 'vehicle_id' AND IS_NULLABLE = 'YES'
  ) THEN
    ALTER TABLE published_parts MODIFY vehicle_id INT NOT NULL;
  END IF;
END//
DELIMITER ;
CALL upgrade_partsouq_007_catalog_contracts();
DROP PROCEDURE upgrade_partsouq_007_catalog_contracts;

-- -------------------------------------------------------------------------
-- 5. Existing admin tables: columns, generated keys, unique/FK/checks
-- -------------------------------------------------------------------------

DROP PROCEDURE IF EXISTS upgrade_partsouq_007_admin_columns;
DELIMITER //
CREATE PROCEDURE upgrade_partsouq_007_admin_columns()
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'admin_vehicle_mappings'
      AND COLUMN_NAME = 'vin'
  ) THEN
    ALTER TABLE admin_vehicle_mappings
      ADD COLUMN vin CHAR(17) CHARACTER SET ascii COLLATE ascii_bin NULL AFTER vin_prefix;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'admin_vehicle_mappings'
      AND COLUMN_NAME = 'partsouq_vehicle_id'
  ) THEN
    ALTER TABLE admin_vehicle_mappings
      ADD COLUMN partsouq_vehicle_id INT NULL AFTER vin;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'admin_vehicle_mappings'
      AND COLUMN_NAME = 'manual_mapping_key'
  ) THEN
    ALTER TABLE admin_vehicle_mappings
      ADD COLUMN manual_mapping_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin
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
      ) STORED AFTER source_reference;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'admin_part_translations'
      AND COLUMN_NAME = 'translation_key'
  ) THEN
    ALTER TABLE admin_part_translations
      ADD COLUMN translation_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin
      GENERATED ALWAYS AS (
        SHA2(CONCAT(
          CHAR_LENGTH(english_name), ':', english_name,
          CHAR_LENGTH(chinese_name), ':', chinese_name
        ), 256)
      ) STORED AFTER source_reference;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'admin_part_fitments'
      AND COLUMN_NAME = 'fitment_key'
  ) THEN
    ALTER TABLE admin_part_fitments
      ADD COLUMN fitment_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin
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
      ) STORED AFTER source_reference;
  END IF;
END//
DELIMITER ;
CALL upgrade_partsouq_007_admin_columns();
DROP PROCEDURE upgrade_partsouq_007_admin_columns;

SET @partsouq_007_manual_mapping_duplicates := (
  SELECT COUNT(*) FROM (
    SELECT manual_mapping_key
    FROM admin_vehicle_mappings
    WHERE manual_mapping_key IS NOT NULL
    GROUP BY manual_mapping_key
    HAVING COUNT(*) > 1
  ) AS duplicate_manual_mapping
);
SET @partsouq_007_vin_duplicates := (
  SELECT COUNT(*) FROM (
    SELECT vin
    FROM admin_vehicle_mappings
    WHERE vin IS NOT NULL
    GROUP BY vin
    HAVING COUNT(*) > 1
  ) AS duplicate_vin_mapping
);
SET @partsouq_007_translation_duplicates := (
  SELECT COUNT(*) FROM (
    SELECT translation_key
    FROM admin_part_translations
    GROUP BY translation_key
    HAVING COUNT(*) > 1
  ) AS duplicate_translation
);
SET @partsouq_007_fitment_duplicates := (
  SELECT COUNT(*) FROM (
    SELECT fitment_key
    FROM admin_part_fitments
    GROUP BY fitment_key
    HAVING COUNT(*) > 1
  ) AS duplicate_fitment
);
SET @partsouq_007_bad_fitment_years := (
  SELECT COUNT(*) FROM admin_part_fitments
  WHERE model_year_from IS NOT NULL AND model_year_to IS NOT NULL
    AND model_year_from > model_year_to
);
SET @partsouq_007_bad_confirmed_pairs := (
  SELECT COUNT(*) FROM admin_vehicle_mappings
  WHERE (vin IS NULL AND partsouq_vehicle_id IS NOT NULL)
     OR (vin IS NOT NULL AND partsouq_vehicle_id IS NULL)
);
SET @partsouq_007_vin_orphans := (
  SELECT COUNT(*)
  FROM admin_vehicle_mappings AS m
  LEFT JOIN nhtsa_vin_decodes AS d ON d.vin = m.vin
  WHERE m.vin IS NOT NULL AND d.vin IS NULL
);
SET @partsouq_007_vehicle_orphans := (
  SELECT COUNT(*)
  FROM admin_vehicle_mappings AS m
  LEFT JOIN vehicles AS v ON v.id = m.partsouq_vehicle_id
  WHERE m.partsouq_vehicle_id IS NOT NULL AND v.id IS NULL
);

DROP PROCEDURE IF EXISTS assert_partsouq_007_admin_data;
DELIMITER //
CREATE PROCEDURE assert_partsouq_007_admin_data()
BEGIN
  IF @partsouq_007_manual_mapping_duplicates > 0
     OR @partsouq_007_vin_duplicates > 0
     OR @partsouq_007_translation_duplicates > 0
     OR @partsouq_007_fitment_duplicates > 0 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 007: duplicate admin rows require manual merge';
  END IF;
  IF @partsouq_007_bad_fitment_years > 0 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 007: reversed admin fitment years require manual repair';
  END IF;
  IF @partsouq_007_bad_confirmed_pairs > 0
     OR @partsouq_007_vin_orphans > 0
     OR @partsouq_007_vehicle_orphans > 0 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 007: invalid/orphan VIN mappings require manual repair';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_007_admin_data();
DROP PROCEDURE assert_partsouq_007_admin_data;

DROP PROCEDURE IF EXISTS upgrade_partsouq_007_admin_contracts;
DELIMITER //
CREATE PROCEDURE upgrade_partsouq_007_admin_contracts()
BEGIN
  DECLARE index_rows INT DEFAULT 0;
  DECLARE index_non_unique INT DEFAULT 1;
  DECLARE index_columns VARCHAR(255) DEFAULT '';
  DECLARE temp_index_rows INT DEFAULT 0;
  DECLARE temp_index_non_unique INT DEFAULT 1;
  DECLARE temp_index_columns VARCHAR(255) DEFAULT '';
  DECLARE fk_rows INT DEFAULT 0;
  DECLARE fk_valid INT DEFAULT 0;

  -- VIN 一對一 confirmed mapping。
  SELECT COUNT(*), COALESCE(MAX(NON_UNIQUE), 1),
         COALESCE(GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX), '')
    INTO index_rows, index_non_unique, index_columns
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'admin_vehicle_mappings'
    AND INDEX_NAME = 'uq_admin_vehicle_vin';
  IF index_rows > 0 AND (index_non_unique <> 0 OR index_columns <> 'vin') THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 007: uq_admin_vehicle_vin definition mismatch';
  END IF;
  IF index_rows = 0 THEN
    ALTER TABLE admin_vehicle_mappings ADD UNIQUE KEY uq_admin_vehicle_vin (vin);
  END IF;

  SELECT COUNT(*), COALESCE(MAX(NON_UNIQUE), 1),
         COALESCE(GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX), '')
    INTO index_rows, index_non_unique, index_columns
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'admin_vehicle_mappings'
    AND INDEX_NAME = 'uq_admin_vehicle_manual_mapping';
  IF index_rows > 0
     AND (index_non_unique <> 0 OR index_columns <> 'manual_mapping_key') THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 007: uq_admin_vehicle_manual_mapping definition mismatch';
  END IF;
  IF index_rows = 0 THEN
    ALTER TABLE admin_vehicle_mappings
      ADD UNIQUE KEY uq_admin_vehicle_manual_mapping (manual_mapping_key);
  END IF;

  IF EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'admin_vehicle_mappings'
      AND INDEX_NAME = 'uq_admin_vehicle_prefix'
  ) THEN
    ALTER TABLE admin_vehicle_mappings DROP INDEX uq_admin_vehicle_prefix;
  END IF;

  -- 舊 translation unique 與新版 hash unique 使用同名；先建立 temporary
  -- hash index，再無空窗切換名稱。中途停止後重跑亦可完成 swap。
  SELECT COUNT(*), COALESCE(MAX(NON_UNIQUE), 1),
         COALESCE(GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX), '')
    INTO index_rows, index_non_unique, index_columns
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'admin_part_translations'
    AND INDEX_NAME = 'uq_admin_part_translation';
  SELECT COUNT(*), COALESCE(MAX(NON_UNIQUE), 1),
         COALESCE(GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX), '')
    INTO temp_index_rows, temp_index_non_unique, temp_index_columns
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'admin_part_translations'
    AND INDEX_NAME = 'uq_admin_part_translation_v7';
  IF temp_index_rows > 0
     AND (temp_index_non_unique <> 0 OR temp_index_columns <> 'translation_key') THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 007: temporary translation index definition mismatch';
  END IF;
  IF NOT (index_rows = 1 AND index_non_unique = 0 AND index_columns = 'translation_key') THEN
    IF temp_index_rows = 0 THEN
      ALTER TABLE admin_part_translations
        ADD UNIQUE KEY uq_admin_part_translation_v7 (translation_key);
    END IF;
    IF index_rows > 0 THEN
      ALTER TABLE admin_part_translations DROP INDEX uq_admin_part_translation;
    END IF;
    ALTER TABLE admin_part_translations
      RENAME INDEX uq_admin_part_translation_v7 TO uq_admin_part_translation;
  ELSEIF temp_index_rows > 0 THEN
    ALTER TABLE admin_part_translations DROP INDEX uq_admin_part_translation_v7;
  END IF;

  SELECT COUNT(*), COALESCE(MAX(NON_UNIQUE), 1),
         COALESCE(GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX), '')
    INTO index_rows, index_non_unique, index_columns
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'admin_part_fitments'
    AND INDEX_NAME = 'uq_admin_part_fitment';
  IF index_rows > 0 AND (index_non_unique <> 0 OR index_columns <> 'fitment_key') THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 007: uq_admin_part_fitment definition mismatch';
  END IF;
  IF index_rows = 0 THEN
    ALTER TABLE admin_part_fitments ADD UNIQUE KEY uq_admin_part_fitment (fitment_key);
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'admin_vehicle_mappings'
      AND CONSTRAINT_NAME = 'chk_admin_vehicle_prefix_length' AND CONSTRAINT_TYPE = 'CHECK'
  ) THEN
    ALTER TABLE admin_vehicle_mappings
      ADD CONSTRAINT chk_admin_vehicle_prefix_length
      CHECK (CHAR_LENGTH(vin_prefix) BETWEEN 3 AND 11);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'admin_vehicle_mappings'
      AND CONSTRAINT_NAME = 'chk_admin_vehicle_confirmed_pair' AND CONSTRAINT_TYPE = 'CHECK'
  ) THEN
    ALTER TABLE admin_vehicle_mappings
      ADD CONSTRAINT chk_admin_vehicle_confirmed_pair CHECK (
        (vin IS NULL AND partsouq_vehicle_id IS NULL)
        OR (vin IS NOT NULL AND partsouq_vehicle_id IS NOT NULL)
      );
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'admin_part_fitments'
      AND CONSTRAINT_NAME = 'chk_admin_part_fitment_prefix_length'
      AND CONSTRAINT_TYPE = 'CHECK'
  ) THEN
    ALTER TABLE admin_part_fitments
      ADD CONSTRAINT chk_admin_part_fitment_prefix_length CHECK (
        vin_prefix IS NULL OR CHAR_LENGTH(vin_prefix) BETWEEN 3 AND 11
      );
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'admin_part_fitments'
      AND CONSTRAINT_NAME = 'chk_admin_part_fitment_years' AND CONSTRAINT_TYPE = 'CHECK'
  ) THEN
    ALTER TABLE admin_part_fitments
      ADD CONSTRAINT chk_admin_part_fitment_years CHECK (
        model_year_from IS NULL OR model_year_to IS NULL OR model_year_from <= model_year_to
      );
  END IF;

  SELECT COUNT(*) INTO fk_rows
  FROM information_schema.TABLE_CONSTRAINTS
  WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'admin_vehicle_mappings'
    AND CONSTRAINT_NAME = 'fk_admin_vehicle_vin' AND CONSTRAINT_TYPE = 'FOREIGN KEY';
  SELECT COUNT(*) INTO fk_valid
  FROM information_schema.KEY_COLUMN_USAGE
  WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'admin_vehicle_mappings'
    AND CONSTRAINT_NAME = 'fk_admin_vehicle_vin' AND COLUMN_NAME = 'vin'
    AND REFERENCED_TABLE_NAME = 'nhtsa_vin_decodes' AND REFERENCED_COLUMN_NAME = 'vin';
  IF fk_rows > 0 AND fk_valid <> 1 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 007: fk_admin_vehicle_vin definition mismatch';
  END IF;
  IF fk_rows = 0 THEN
    ALTER TABLE admin_vehicle_mappings
      ADD CONSTRAINT fk_admin_vehicle_vin
      FOREIGN KEY (vin) REFERENCES nhtsa_vin_decodes(vin);
  END IF;

  SELECT COUNT(*) INTO fk_rows
  FROM information_schema.TABLE_CONSTRAINTS
  WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'admin_vehicle_mappings'
    AND CONSTRAINT_NAME = 'fk_admin_vehicle_partsouq' AND CONSTRAINT_TYPE = 'FOREIGN KEY';
  SELECT COUNT(*) INTO fk_valid
  FROM information_schema.KEY_COLUMN_USAGE
  WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'admin_vehicle_mappings'
    AND CONSTRAINT_NAME = 'fk_admin_vehicle_partsouq'
    AND COLUMN_NAME = 'partsouq_vehicle_id'
    AND REFERENCED_TABLE_NAME = 'vehicles' AND REFERENCED_COLUMN_NAME = 'id';
  IF fk_rows > 0 AND fk_valid <> 1 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 007: fk_admin_vehicle_partsouq definition mismatch';
  END IF;
  IF fk_rows = 0 THEN
    ALTER TABLE admin_vehicle_mappings
      ADD CONSTRAINT fk_admin_vehicle_partsouq
      FOREIGN KEY (partsouq_vehicle_id) REFERENCES vehicles(id);
  END IF;
END//
DELIMITER ;
CALL upgrade_partsouq_007_admin_contracts();
DROP PROCEDURE upgrade_partsouq_007_admin_contracts;

-- -------------------------------------------------------------------------
-- 6. Current views
-- -------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_parts AS
SELECT
  vehicle_id, brand, model, vehicle_name, vehicle_code, prod_period,
  production_from, production_to, engine, trim_name,
  part_name, part_number, category_main, category_group, group_code,
  part_range, part_from, part_to, source_url, note, quantity, code
FROM published_parts;

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
  WHERE m.vin IS NOT NULL
    AND m.partsouq_vehicle_id IS NOT NULL
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
WHERE (mapped.fitment_from IS NULL
       OR mapped.model_year >= CAST(LEFT(mapped.fitment_from, 4) AS UNSIGNED))
  AND (mapped.fitment_to IS NULL
       OR mapped.model_year <= CAST(LEFT(mapped.fitment_to, 4) AS UNSIGNED))
  AND (
    mapped.fitment_from IS NULL
    OR mapped.fitment_to IS NULL
    OR mapped.fitment_from <= mapped.fitment_to
  );

-- -------------------------------------------------------------------------
-- 7. Postflight：任何必要物件未完成都停止；最後只回報，不刪 legacy row。
-- -------------------------------------------------------------------------

SET @partsouq_007_catalog_columns_done := (
  SELECT COUNT(*)
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND (
      (TABLE_NAME = 'vehicles' AND COLUMN_NAME IN ('production_from', 'production_to'))
      OR (TABLE_NAME = 'parts' AND COLUMN_NAME IN ('part_from', 'part_to'))
      OR (TABLE_NAME = 'published_parts' AND COLUMN_NAME IN (
        'vehicle_id', 'production_from', 'production_to', 'engine', 'trim_name',
        'part_from', 'part_to', 'source_url'
      ))
    )
);
SET @partsouq_007_admin_columns_done := (
  SELECT COUNT(*)
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND (
      (TABLE_NAME = 'admin_vehicle_mappings'
       AND COLUMN_NAME IN ('vin', 'partsouq_vehicle_id', 'manual_mapping_key'))
      OR (TABLE_NAME = 'admin_part_translations' AND COLUMN_NAME = 'translation_key')
      OR (TABLE_NAME = 'admin_part_fitments' AND COLUMN_NAME = 'fitment_key')
    )
);
SET @partsouq_007_views_done := (
  SELECT COUNT(*)
  FROM information_schema.VIEWS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME IN ('v_parts', 'v_vin_part_fitments')
);
SET @partsouq_007_nhtsa_table_done := (
  SELECT COUNT(*)
  FROM information_schema.TABLES
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'nhtsa_vin_decodes'
    AND TABLE_TYPE = 'BASE TABLE'
);

DROP PROCEDURE IF EXISTS assert_partsouq_007_postflight;
DELIMITER //
CREATE PROCEDURE assert_partsouq_007_postflight()
BEGIN
  IF @partsouq_007_catalog_columns_done <> 12 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 007: catalog column postflight failed';
  END IF;
  IF @partsouq_007_admin_columns_done <> 5 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 007: admin column postflight failed';
  END IF;
  IF @partsouq_007_views_done <> 2 OR @partsouq_007_nhtsa_table_done <> 1 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 007: table/view postflight failed';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_007_postflight();
DROP PROCEDURE assert_partsouq_007_postflight;

SELECT
  COUNT(*) AS published_rows,
  COALESCE(SUM(vehicle_id IS NOT NULL), 0) AS safely_mapped_vehicle_rows,
  COALESCE(SUM(vehicle_id IS NULL), 0) AS preserved_unmatched_vehicle_rows
FROM published_parts;
