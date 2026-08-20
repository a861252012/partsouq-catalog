-- PartSouq crawler — schema migration 009：正式有界資料集
--
-- 執行前停止 catalog writer，並先完成 migrations 001-008。
-- 本 migration 不含 USE，可重複執行；既有 full published snapshot
-- 與 normalized membership 不會被清除。

SET SESSION lock_wait_timeout = 30;
SET SESSION innodb_lock_wait_timeout = 30;

SET @partsouq_009_required_tables := (
  SELECT COUNT(*) FROM information_schema.TABLES
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'
    AND TABLE_NAME IN (
      'brands', 'models', 'vehicles', 'categories', 'groups_t', 'parts',
      'published_parts', 'crawl_runs', 'scheduled_job_runs'
    )
);

DROP PROCEDURE IF EXISTS assert_partsouq_009_input;
DELIMITER //
CREATE PROCEDURE assert_partsouq_009_input()
BEGIN
  IF DATABASE() IS NULL OR @partsouq_009_required_tables <> 9 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 009: select database and apply schemas/migrations 001-008 first';
  END IF;
  IF EXISTS (SELECT 1 FROM crawl_runs WHERE status = 'running') THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 009: running catalog jobs exist; stop writers first';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_009_input();
DROP PROCEDURE assert_partsouq_009_input;

DROP PROCEDURE IF EXISTS upgrade_partsouq_009_columns;
DELIMITER //
CREATE PROCEDURE upgrade_partsouq_009_columns()
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'scheduled_job_runs'
      AND COLUMN_NAME = 'trigger_mode'
  ) THEN
    ALTER TABLE scheduled_job_runs
      ADD COLUMN trigger_mode VARCHAR(16) NOT NULL DEFAULT 'manual' AFTER job_name;
  ELSEIF EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'scheduled_job_runs'
      AND COLUMN_NAME = 'trigger_mode'
      AND (COLUMN_TYPE <> 'varchar(16)' OR IS_NULLABLE <> 'NO'
           OR COLUMN_DEFAULT <> 'manual')
  ) THEN
    UPDATE scheduled_job_runs
    SET trigger_mode = 'manual'
    WHERE trigger_mode IS NULL OR trigger_mode NOT IN ('manual', 'daemon', 'queue');
    ALTER TABLE scheduled_job_runs
      MODIFY COLUMN trigger_mode VARCHAR(16) NOT NULL DEFAULT 'manual' AFTER job_name;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'crawl_runs'
      AND COLUMN_NAME = 'dataset_kind'
  ) THEN
    ALTER TABLE crawl_runs
      ADD COLUMN dataset_kind VARCHAR(16) NOT NULL DEFAULT 'full' AFTER status;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'crawl_runs'
      AND COLUMN_NAME = 'target_parts'
  ) THEN
    ALTER TABLE crawl_runs ADD COLUMN target_parts INT NULL AFTER dataset_kind;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'crawl_runs'
      AND COLUMN_NAME = 'scheduled_job_run_id'
  ) THEN
    ALTER TABLE crawl_runs
      ADD COLUMN scheduled_job_run_id BIGINT UNSIGNED NULL AFTER target_parts;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts'
      AND COLUMN_NAME = 'part_number_normalized'
  ) THEN
    ALTER TABLE published_parts
      ADD COLUMN part_number_normalized VARCHAR(64) NULL AFTER part_number;
  END IF;
END//
DELIMITER ;
CALL upgrade_partsouq_009_columns();
DROP PROCEDURE upgrade_partsouq_009_columns;

UPDATE crawl_runs
SET dataset_kind = CASE
  WHEN status = 'sample' OR run_key LIKE 'sample-%' THEN 'sample'
  ELSE 'full'
END
WHERE dataset_kind = 'full';

UPDATE published_parts
SET part_number_normalized = UPPER(REGEXP_REPLACE(part_number, '[[:space:]-]+', ''))
WHERE part_number_normalized IS NULL
   OR part_number_normalized <> UPPER(REGEXP_REPLACE(part_number, '[[:space:]-]+', ''));

DROP PROCEDURE IF EXISTS finalize_partsouq_009_published_column;
DELIMITER //
CREATE PROCEDURE finalize_partsouq_009_published_column()
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts'
      AND COLUMN_NAME = 'part_number_normalized' AND IS_NULLABLE = 'YES'
  ) THEN
    ALTER TABLE published_parts
      MODIFY part_number_normalized VARCHAR(64) NOT NULL;
  END IF;
END//
DELIMITER ;
CALL finalize_partsouq_009_published_column();
DROP PROCEDURE finalize_partsouq_009_published_column;

DROP PROCEDURE IF EXISTS upgrade_partsouq_009_indexes;
DELIMITER //
CREATE PROCEDURE upgrade_partsouq_009_indexes()
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'crawl_runs'
      AND INDEX_NAME = 'idx_crawl_run_schedule'
  ) THEN
    ALTER TABLE crawl_runs
      ADD KEY idx_crawl_run_schedule (scheduled_job_run_id);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts'
      AND INDEX_NAME = 'idx_published_part_number_normalized'
  ) THEN
    ALTER TABLE published_parts
      ADD KEY idx_published_part_number_normalized (part_number_normalized);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts'
      AND INDEX_NAME = 'idx_published_snapshot_page'
  ) THEN
    ALTER TABLE published_parts
      ADD KEY idx_published_snapshot_page (snapshot_at, part_id);
  END IF;
END//
DELIMITER ;
CALL upgrade_partsouq_009_indexes();
DROP PROCEDURE upgrade_partsouq_009_indexes;

CREATE TABLE IF NOT EXISTS bounded_parts (
  part_id        INT NOT NULL PRIMARY KEY,
  crawl_run_id   INT NOT NULL,
  vehicle_id     INT NOT NULL,
  model_id       INT NOT NULL,
  vehicle_vid    VARCHAR(32) NOT NULL,
  brand          VARCHAR(64) NOT NULL,
  model          VARCHAR(128) NOT NULL,
  vehicle_name   VARCHAR(256) NOT NULL,
  vehicle_code   VARCHAR(128) NOT NULL,
  prod_period    VARCHAR(64) NULL,
  production_from CHAR(7) NULL,
  production_to   CHAR(7) NULL,
  engine         VARCHAR(256) NULL,
  trim_name      VARCHAR(256) NULL,
  part_name      VARCHAR(512) NOT NULL,
  part_number    VARCHAR(64) NOT NULL,
  part_number_normalized VARCHAR(64) NOT NULL,
  category_id    INT NOT NULL,
  category_cid   VARCHAR(32) NOT NULL,
  category_main  VARCHAR(256) NOT NULL,
  category_group VARCHAR(256) NOT NULL,
  group_id       INT NOT NULL,
  group_code     VARCHAR(16) NOT NULL,
  group_uid      VARCHAR(32) NOT NULL,
  part_range     VARCHAR(64) NOT NULL,
  part_from      CHAR(7) NULL,
  part_to        CHAR(7) NULL,
  source_url     VARCHAR(1024) NOT NULL,
  note           TEXT NULL,
  quantity       VARCHAR(16) NULL,
  code           VARCHAR(64) NOT NULL,
  snapshot_at    DATETIME NOT NULL,
  KEY idx_bounded_run (crawl_run_id),
  KEY idx_bounded_part_number (part_number),
  KEY idx_bounded_part_number_normalized (part_number_normalized),
  KEY idx_bounded_brand_model (brand, model),
  KEY idx_bounded_snapshot_page (snapshot_at, part_id),
  CONSTRAINT chk_bounded_production_from CHECK (
    production_from IS NULL OR (
      production_from REGEXP '^[0-9]{4}-(0[1-9]|1[0-2])$'
      AND CAST(LEFT(production_from, 4) AS UNSIGNED) BETWEEN 1886 AND 2100
    )
  ),
  CONSTRAINT chk_bounded_production_to CHECK (
    production_to IS NULL OR (
      production_to REGEXP '^[0-9]{4}-(0[1-9]|1[0-2])$'
      AND CAST(LEFT(production_to, 4) AS UNSIGNED) BETWEEN 1886 AND 2100
    )
  ),
  CONSTRAINT chk_bounded_production_order CHECK (
    production_from IS NULL OR production_to IS NULL OR production_from <= production_to
  ),
  CONSTRAINT chk_bounded_part_from CHECK (
    part_from IS NULL OR (
      part_from REGEXP '^[0-9]{4}-(0[1-9]|1[0-2])$'
      AND CAST(LEFT(part_from, 4) AS UNSIGNED) BETWEEN 1886 AND 2100
    )
  ),
  CONSTRAINT chk_bounded_part_to CHECK (
    part_to IS NULL OR (
      part_to REGEXP '^[0-9]{4}-(0[1-9]|1[0-2])$'
      AND CAST(LEFT(part_to, 4) AS UNSIGNED) BETWEEN 1886 AND 2100
    )
  ),
  CONSTRAINT chk_bounded_part_order CHECK (
    part_from IS NULL OR part_to IS NULL OR part_from <= part_to
  ),
  CONSTRAINT chk_bounded_range_overlap_start CHECK (
    part_to IS NULL OR production_from IS NULL OR part_to >= production_from
  ),
  CONSTRAINT chk_bounded_range_overlap_end CHECK (
    production_to IS NULL OR part_from IS NULL OR production_to >= part_from
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

DROP PROCEDURE IF EXISTS upgrade_partsouq_009_constraints;
DELIMITER //
CREATE PROCEDURE upgrade_partsouq_009_constraints()
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'crawl_runs'
      AND CONSTRAINT_NAME = 'chk_crawl_run_target'
  ) THEN
    ALTER TABLE crawl_runs ADD CONSTRAINT chk_crawl_run_target
      CHECK (target_parts IS NULL OR target_parts > 0);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'bounded_parts'
      AND CONSTRAINT_NAME = 'chk_bounded_range_overlap_start'
  ) THEN
    ALTER TABLE bounded_parts ADD CONSTRAINT chk_bounded_range_overlap_start
      CHECK (part_to IS NULL OR production_from IS NULL OR part_to >= production_from);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'bounded_parts'
      AND CONSTRAINT_NAME = 'chk_bounded_range_overlap_end'
  ) THEN
    ALTER TABLE bounded_parts ADD CONSTRAINT chk_bounded_range_overlap_end
      CHECK (production_to IS NULL OR part_from IS NULL OR production_to >= part_from);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.REFERENTIAL_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE()
      AND CONSTRAINT_NAME = 'fk_crawl_run_schedule'
  ) THEN
    ALTER TABLE crawl_runs ADD CONSTRAINT fk_crawl_run_schedule
      FOREIGN KEY (scheduled_job_run_id) REFERENCES scheduled_job_runs(id);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.REFERENTIAL_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE()
      AND CONSTRAINT_NAME = 'fk_bounded_crawl_run'
  ) THEN
    ALTER TABLE bounded_parts ADD CONSTRAINT fk_bounded_crawl_run
      FOREIGN KEY (crawl_run_id) REFERENCES crawl_runs(id);
  END IF;
END//
DELIMITER ;
CALL upgrade_partsouq_009_constraints();
DROP PROCEDURE upgrade_partsouq_009_constraints;

DROP PROCEDURE IF EXISTS assert_partsouq_009_output;
DELIMITER //
CREATE PROCEDURE assert_partsouq_009_output()
BEGIN
  IF (SELECT COUNT(*) FROM information_schema.COLUMNS
      WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'crawl_runs'
        AND COLUMN_NAME IN ('dataset_kind', 'target_parts', 'scheduled_job_run_id')) <> 3
     OR (SELECT COUNT(*) FROM information_schema.COLUMNS
         WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'crawl_runs'
           AND (
             (COLUMN_NAME = 'dataset_kind' AND COLUMN_TYPE = 'varchar(16)'
              AND IS_NULLABLE = 'NO')
             OR (COLUMN_NAME = 'target_parts' AND COLUMN_TYPE = 'int'
                 AND IS_NULLABLE = 'YES')
             OR (COLUMN_NAME = 'scheduled_job_run_id'
                 AND COLUMN_TYPE = 'bigint unsigned' AND IS_NULLABLE = 'YES')
           )) <> 3
     OR (SELECT COUNT(*) FROM information_schema.COLUMNS
         WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'scheduled_job_runs'
           AND COLUMN_NAME = 'trigger_mode' AND COLUMN_TYPE = 'varchar(16)'
           AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT = 'manual') <> 1
     OR (SELECT COUNT(*) FROM information_schema.COLUMNS
         WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'bounded_parts'
           AND COLUMN_NAME IN (
             'part_id', 'crawl_run_id', 'vehicle_id', 'model_id', 'vehicle_vid',
             'brand', 'model', 'vehicle_name', 'vehicle_code', 'prod_period',
             'production_from', 'production_to', 'engine', 'trim_name', 'part_name',
             'part_number', 'part_number_normalized', 'category_id', 'category_cid',
             'category_main', 'category_group', 'group_id', 'group_code', 'group_uid',
             'part_range', 'part_from', 'part_to', 'source_url', 'note', 'quantity',
             'code', 'snapshot_at'
           )) <> 32
     OR (SELECT COUNT(*) FROM information_schema.COLUMNS
         WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts'
           AND COLUMN_NAME = 'part_number_normalized' AND DATA_TYPE = 'varchar'
           AND CHARACTER_MAXIMUM_LENGTH = 64 AND IS_NULLABLE = 'NO') <> 1
     OR (SELECT COUNT(DISTINCT INDEX_NAME) FROM information_schema.STATISTICS
         WHERE TABLE_SCHEMA = DATABASE()
           AND (
             (TABLE_NAME = 'published_parts' AND INDEX_NAME IN (
               'idx_published_part_number_normalized', 'idx_published_snapshot_page'
             ))
             OR (TABLE_NAME = 'bounded_parts'
                 AND INDEX_NAME = 'idx_bounded_part_number_normalized')
           )) <> 3
     OR (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
         WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'bounded_parts'
           AND CONSTRAINT_TYPE = 'CHECK'
           AND CONSTRAINT_NAME IN (
             'chk_bounded_range_overlap_start', 'chk_bounded_range_overlap_end'
           )) <> 2
     OR (SELECT COUNT(*) FROM information_schema.REFERENTIAL_CONSTRAINTS
         WHERE CONSTRAINT_SCHEMA = DATABASE()
           AND CONSTRAINT_NAME IN ('fk_crawl_run_schedule', 'fk_bounded_crawl_run')) <> 2 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 009: bounded dataset schema verification failed';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_009_output();
DROP PROCEDURE assert_partsouq_009_output;

-- Full published snapshot is authoritative. Until it exists, the latest
-- successful bounded snapshot is the current server-grade catalog source.
CREATE OR REPLACE VIEW v_current_catalog_parts AS
SELECT
  'full' AS dataset_scope,
  CAST(NULL AS SIGNED) AS source_crawl_run_id,
  p.part_id, p.vehicle_id, p.model_id, p.vehicle_vid, p.brand, p.model,
  p.vehicle_name, p.vehicle_code, p.prod_period, p.production_from,
  p.production_to, p.engine, p.trim_name, p.part_name, p.part_number,
  p.part_number_normalized, p.category_id, p.category_cid, p.category_main,
  p.category_group, p.group_id, p.group_code, p.group_uid, p.part_range,
  p.part_from, p.part_to, p.source_url, p.note, p.quantity, p.code, p.snapshot_at
FROM published_parts AS p
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
JOIN scheduled_job_runs AS scheduler_run
  ON scheduler_run.id = current_run.scheduled_job_run_id
 AND scheduler_run.job_name = 'catalog'
 AND scheduler_run.trigger_mode = 'daemon'
 AND scheduler_run.status = 'completed'
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
WHERE NOT EXISTS (SELECT 1 FROM published_parts);

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
WHERE (mapped.fitment_from IS NULL
       OR mapped.model_year >= CAST(LEFT(mapped.fitment_from, 4) AS UNSIGNED))
  AND (mapped.fitment_to IS NULL
       OR mapped.model_year <= CAST(LEFT(mapped.fitment_to, 4) AS UNSIGNED))
  AND (
    mapped.fitment_from IS NULL
    OR mapped.fitment_to IS NULL
    OR mapped.fitment_from <= mapped.fitment_to
  );
