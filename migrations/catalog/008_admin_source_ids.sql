-- PartSouq crawler — schema migration 008：後台可追溯的來源 ID
--
-- 執行前必要條件：
--   1. 停止 admin、scheduler 與 PartSouq crawler 等所有 writer。
--   2. 完整備份 MySQL，並明確選定 database；本檔不包含 USE。
--   3. 先完成 catalog migrations 001-007。
--
-- published_parts.part_id 與 code 已由既有 snapshot 保存。本 migration
-- 另外保存 model / vehicle vid / category / group 的來源 ID。legacy snapshot
-- 不信任 part_id；只在所有既有可見欄位能唯一對回一條 normalized part
-- chain 時回填。無法唯一證明的欄位維持 NULL，等待下一次完整 success publish。
-- 本檔可安全重跑；已存在但定義不符的欄位、索引或來源鏈一律 fail closed。

SET SESSION lock_wait_timeout = 30;
SET SESSION innodb_lock_wait_timeout = 30;

-- -------------------------------------------------------------------------
-- 0. 輸入契約與 writer preflight
-- -------------------------------------------------------------------------

SET @partsouq_008_database_selected := DATABASE() IS NOT NULL;
SET @partsouq_008_required_tables := (
  SELECT COUNT(*)
  FROM information_schema.TABLES
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_TYPE = 'BASE TABLE'
    AND TABLE_NAME IN (
      'brands', 'models', 'vehicles', 'categories', 'groups_t', 'parts',
      'published_parts', 'crawl_runs'
    )
);
SET @partsouq_008_mapping_tables := (
  SELECT COUNT(*)
  FROM information_schema.TABLES
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_TYPE = 'BASE TABLE'
    AND TABLE_NAME IN ('nhtsa_vin_decodes', 'admin_vehicle_mappings')
);
SET @partsouq_008_required_columns := (
  SELECT COUNT(*)
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND (
      (TABLE_NAME = 'brands' AND COLUMN_NAME IN ('id', 'name'))
      OR (TABLE_NAME = 'models' AND COLUMN_NAME IN ('id', 'brand_id', 'name'))
      OR (TABLE_NAME = 'vehicles' AND COLUMN_NAME IN (
        'id', 'model_id', 'vid', 'name', 'model_code', 'prod_period'
      ))
      OR (TABLE_NAME = 'categories' AND COLUMN_NAME IN (
        'id', 'vehicle_id', 'name', 'cid'
      ))
      OR (TABLE_NAME = 'groups_t' AND COLUMN_NAME IN (
        'id', 'category_id', 'code', 'name', 'uid'
      ))
      OR (TABLE_NAME = 'parts' AND COLUMN_NAME IN (
        'id', 'group_id', 'part_number', 'name', 'range_str', 'code', 'note', 'quantity'
      ))
      OR (TABLE_NAME = 'published_parts' AND COLUMN_NAME IN (
        'part_id', 'vehicle_id', 'brand', 'model', 'vehicle_name', 'vehicle_code',
        'prod_period', 'part_name', 'part_number', 'category_main', 'category_group',
        'group_code', 'part_range', 'source_url', 'note', 'quantity', 'code', 'snapshot_at'
      ))
      OR (TABLE_NAME = 'crawl_runs' AND COLUMN_NAME = 'status')
    )
);
SET @partsouq_008_mapping_columns := (
  SELECT COUNT(*)
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND (
      (TABLE_NAME = 'nhtsa_vin_decodes' AND COLUMN_NAME IN (
        'vin', 'make_name', 'model_name', 'model_year', 'engine_configuration',
        'engine_model', 'displacement_l', 'trim_name', 'source_url', 'source_artifact_id'
      ))
      OR (TABLE_NAME = 'admin_vehicle_mappings' AND COLUMN_NAME IN (
        'id', 'vin', 'partsouq_vehicle_id', 'make_name', 'model_name', 'model_year',
        'source_name', 'source_reference'
      ))
    )
);

DROP PROCEDURE IF EXISTS assert_partsouq_008_input;
DELIMITER //
CREATE PROCEDURE assert_partsouq_008_input()
BEGIN
  IF @partsouq_008_database_selected <> 1 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 008: select an explicit database before running';
  END IF;
  IF @partsouq_008_required_tables <> 8
     OR @partsouq_008_required_columns <> 47
     OR @partsouq_008_mapping_tables <> 2
     OR @partsouq_008_mapping_columns <> 18 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 008: apply catalog migrations 001-007 first';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_008_input();
DROP PROCEDURE assert_partsouq_008_input;

SET @partsouq_008_catalog_writers := (
  SELECT COUNT(*) FROM crawl_runs WHERE status = 'running'
);

DROP PROCEDURE IF EXISTS assert_partsouq_008_writers_stopped;
DELIMITER //
CREATE PROCEDURE assert_partsouq_008_writers_stopped()
BEGIN
  IF @partsouq_008_catalog_writers > 0 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 008: running catalog jobs exist; stop writers first';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_008_writers_stopped();
DROP PROCEDURE assert_partsouq_008_writers_stopped;

-- -------------------------------------------------------------------------
-- 1. Add nullable source-ID columns for legacy-safe backfill
-- -------------------------------------------------------------------------

DROP PROCEDURE IF EXISTS upgrade_partsouq_008_columns;
DELIMITER //
CREATE PROCEDURE upgrade_partsouq_008_columns()
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts'
      AND COLUMN_NAME = 'model_id'
  ) THEN
    ALTER TABLE published_parts ADD COLUMN model_id INT NULL AFTER vehicle_id;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts'
      AND COLUMN_NAME = 'vehicle_vid'
  ) THEN
    ALTER TABLE published_parts ADD COLUMN vehicle_vid VARCHAR(32) NULL AFTER model_id;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts'
      AND COLUMN_NAME = 'category_id'
  ) THEN
    ALTER TABLE published_parts ADD COLUMN category_id INT NULL AFTER part_number;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts'
      AND COLUMN_NAME = 'category_cid'
  ) THEN
    ALTER TABLE published_parts ADD COLUMN category_cid VARCHAR(32) NULL AFTER category_id;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts'
      AND COLUMN_NAME = 'group_id'
  ) THEN
    ALTER TABLE published_parts ADD COLUMN group_id INT NULL AFTER category_group;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts'
      AND COLUMN_NAME = 'group_uid'
  ) THEN
    ALTER TABLE published_parts ADD COLUMN group_uid VARCHAR(32) NULL AFTER group_code;
  END IF;
END//
DELIMITER ;
CALL upgrade_partsouq_008_columns();
DROP PROCEDURE upgrade_partsouq_008_columns;

SET @partsouq_008_column_contracts := (
  SELECT COUNT(*)
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts'
    AND (
      (COLUMN_NAME = 'model_id' AND DATA_TYPE = 'int' AND IS_NULLABLE = 'YES')
      OR (COLUMN_NAME = 'vehicle_vid' AND DATA_TYPE = 'varchar'
          AND CHARACTER_MAXIMUM_LENGTH = 32 AND IS_NULLABLE = 'YES')
      OR (COLUMN_NAME = 'category_id' AND DATA_TYPE = 'int' AND IS_NULLABLE = 'YES')
      OR (COLUMN_NAME = 'category_cid' AND DATA_TYPE = 'varchar'
          AND CHARACTER_MAXIMUM_LENGTH = 32 AND IS_NULLABLE = 'YES')
      OR (COLUMN_NAME = 'group_id' AND DATA_TYPE = 'int' AND IS_NULLABLE = 'YES')
      OR (COLUMN_NAME = 'group_uid' AND DATA_TYPE = 'varchar'
          AND CHARACTER_MAXIMUM_LENGTH = 32 AND IS_NULLABLE = 'YES')
    )
);

DROP PROCEDURE IF EXISTS assert_partsouq_008_column_contracts;
DELIMITER //
CREATE PROCEDURE assert_partsouq_008_column_contracts()
BEGIN
  IF @partsouq_008_column_contracts <> 6 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 008: source-ID column definition mismatch';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_008_column_contracts();
DROP PROCEDURE assert_partsouq_008_column_contracts;

-- -------------------------------------------------------------------------
-- 2. Add deterministic lookup indexes
-- -------------------------------------------------------------------------

DROP PROCEDURE IF EXISTS upgrade_partsouq_008_indexes;
DELIMITER //
CREATE PROCEDURE upgrade_partsouq_008_indexes()
BEGIN
  DECLARE index_rows INT DEFAULT 0;
  DECLARE index_valid INT DEFAULT 0;

  SELECT COUNT(*) INTO index_rows
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts'
    AND INDEX_NAME = 'idx_published_model';
  SELECT COUNT(*) INTO index_valid
  FROM (
    SELECT INDEX_NAME
    FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts'
      AND INDEX_NAME = 'idx_published_model'
    GROUP BY INDEX_NAME
    HAVING GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) = 'model_id'
       AND MIN(NON_UNIQUE) = 1 AND MAX(NON_UNIQUE) = 1
  ) AS valid_model_index;
  IF index_rows > 0 AND index_valid <> 1 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 008: idx_published_model definition mismatch';
  END IF;
  IF index_rows = 0 THEN
    ALTER TABLE published_parts ADD KEY idx_published_model (model_id);
  END IF;

  SELECT COUNT(*) INTO index_rows
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts'
    AND INDEX_NAME = 'idx_published_category';
  SELECT COUNT(*) INTO index_valid
  FROM (
    SELECT INDEX_NAME
    FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts'
      AND INDEX_NAME = 'idx_published_category'
    GROUP BY INDEX_NAME
    HAVING GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) = 'category_id'
       AND MIN(NON_UNIQUE) = 1 AND MAX(NON_UNIQUE) = 1
  ) AS valid_category_index;
  IF index_rows > 0 AND index_valid <> 1 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 008: idx_published_category definition mismatch';
  END IF;
  IF index_rows = 0 THEN
    ALTER TABLE published_parts ADD KEY idx_published_category (category_id);
  END IF;

  SELECT COUNT(*) INTO index_rows
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts'
    AND INDEX_NAME = 'idx_published_group';
  SELECT COUNT(*) INTO index_valid
  FROM (
    SELECT INDEX_NAME
    FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts'
      AND INDEX_NAME = 'idx_published_group'
    GROUP BY INDEX_NAME
    HAVING GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) = 'group_id'
       AND MIN(NON_UNIQUE) = 1 AND MAX(NON_UNIQUE) = 1
  ) AS valid_group_index;
  IF index_rows > 0 AND index_valid <> 1 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 008: idx_published_group definition mismatch';
  END IF;
  IF index_rows = 0 THEN
    ALTER TABLE published_parts ADD KEY idx_published_group (group_id);
  END IF;
END//
DELIMITER ;
CALL upgrade_partsouq_008_indexes();
DROP PROCEDURE upgrade_partsouq_008_indexes;

-- -------------------------------------------------------------------------
-- 3. Safe legacy backfill from the complete visible snapshot identity
-- -------------------------------------------------------------------------

DROP TEMPORARY TABLE IF EXISTS tmp_partsouq_008_source_ids;
CREATE TEMPORARY TABLE tmp_partsouq_008_source_ids (
  snapshot_part_id INT NOT NULL PRIMARY KEY,
  vehicle_id INT NOT NULL,
  model_id INT NOT NULL,
  vehicle_vid VARCHAR(32) NULL,
  category_id INT NOT NULL,
  category_cid VARCHAR(32) NULL,
  group_id INT NOT NULL,
  group_uid VARCHAR(32) NULL
) ENGINE=InnoDB;

-- part_id 可能是 legacy ROW_NUMBER()，所以只作 snapshot row key，不參與 JOIN。
-- 所有當時已發布的可見欄位都必須精確相符，且只能找到一個 normalized
-- part / vehicle / category / group chain；否則不進 staging，也不回填。
INSERT INTO tmp_partsouq_008_source_ids(
  snapshot_part_id, vehicle_id, model_id, vehicle_vid,
  category_id, category_cid, group_id, group_uid
)
SELECT
  pp.part_id,
  MIN(v.id),
  MIN(m.id),
  MIN(v.vid),
  MIN(c.id),
  MIN(c.cid),
  MIN(g.id),
  MIN(g.uid)
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
WHERE pp.vehicle_id IS NULL OR pp.vehicle_id = v.id
GROUP BY pp.part_id
HAVING COUNT(DISTINCT p.id) = 1
   AND COUNT(DISTINCT v.id) = 1
   AND COUNT(DISTINCT m.id) = 1
   AND COUNT(DISTINCT c.id) = 1
   AND COUNT(DISTINCT g.id) = 1;

UPDATE published_parts AS pp
JOIN tmp_partsouq_008_source_ids AS matched
  ON matched.snapshot_part_id = pp.part_id
SET pp.vehicle_id = COALESCE(pp.vehicle_id, matched.vehicle_id),
    pp.model_id = COALESCE(pp.model_id, matched.model_id),
    pp.vehicle_vid = COALESCE(pp.vehicle_vid, matched.vehicle_vid),
    pp.category_id = COALESCE(pp.category_id, matched.category_id),
    pp.category_cid = COALESCE(pp.category_cid, matched.category_cid),
    pp.group_id = COALESCE(pp.group_id, matched.group_id),
    pp.group_uid = COALESCE(pp.group_uid, matched.group_uid);

DROP TEMPORARY TABLE tmp_partsouq_008_source_ids;

-- -------------------------------------------------------------------------
-- 4. Validate every populated source chain and refresh current view
-- -------------------------------------------------------------------------

SET @partsouq_008_invalid_source_chains := (
  SELECT COUNT(*)
  FROM published_parts AS pp
  LEFT JOIN vehicles AS v ON v.id = pp.vehicle_id
  LEFT JOIN models AS m ON m.id = pp.model_id
  LEFT JOIN brands AS b ON b.id = m.brand_id
  LEFT JOIN categories AS c ON c.id = pp.category_id
  LEFT JOIN groups_t AS g ON g.id = pp.group_id
  WHERE (pp.model_id IS NOT NULL AND (
           pp.vehicle_id IS NULL
           OR m.id IS NULL
           OR v.id IS NULL
           OR v.model_id <> pp.model_id
           OR NOT (CAST(m.name AS BINARY) <=> CAST(pp.model AS BINARY))
           OR NOT (CAST(b.name AS BINARY) <=> CAST(pp.brand AS BINARY))
         ))
     OR (pp.vehicle_vid IS NOT NULL AND (
           pp.vehicle_id IS NULL
           OR v.id IS NULL
           OR NOT (CAST(v.vid AS BINARY) <=> CAST(pp.vehicle_vid AS BINARY))
         ))
     OR (pp.category_id IS NOT NULL AND (
           pp.vehicle_id IS NULL
           OR pp.model_id IS NULL
           OR c.id IS NULL
           OR c.vehicle_id <> pp.vehicle_id
           OR NOT (CAST(c.name AS BINARY) <=> CAST(pp.category_main AS BINARY))
         ))
     OR (pp.category_cid IS NOT NULL AND (
           pp.category_id IS NULL
           OR c.id IS NULL
           OR NOT (CAST(c.cid AS BINARY) <=> CAST(pp.category_cid AS BINARY))
         ))
     OR (pp.group_id IS NOT NULL AND (
           pp.category_id IS NULL
           OR pp.vehicle_id IS NULL
           OR pp.model_id IS NULL
           OR g.id IS NULL
           OR g.category_id <> pp.category_id
           OR NOT (CAST(g.code AS BINARY) <=> CAST(pp.group_code AS BINARY))
           OR NOT (CAST(g.name AS BINARY) <=> CAST(pp.category_group AS BINARY))
         ))
     OR (pp.group_uid IS NOT NULL AND (
           pp.group_id IS NULL
           OR g.id IS NULL
           OR NOT (CAST(g.uid AS BINARY) <=> CAST(pp.group_uid AS BINARY))
         ))
);

DROP PROCEDURE IF EXISTS assert_partsouq_008_source_chains;
DELIMITER //
CREATE PROCEDURE assert_partsouq_008_source_chains()
BEGIN
  IF @partsouq_008_invalid_source_chains > 0 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 008: populated source-ID chain is inconsistent';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_008_source_chains();
DROP PROCEDURE assert_partsouq_008_source_chains;

CREATE OR REPLACE VIEW v_parts AS
SELECT
  part_id, vehicle_id, model_id, vehicle_vid,
  brand, model, vehicle_name, vehicle_code, prod_period,
  production_from, production_to, engine, trim_name,
  part_name, part_number,
  category_id, category_cid, category_main, category_group,
  group_id, group_code, group_uid,
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

SET @partsouq_008_source_columns_done := (
  SELECT COUNT(*)
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts'
    AND COLUMN_NAME IN (
      'model_id', 'vehicle_vid', 'category_id', 'category_cid', 'group_id', 'group_uid'
    )
);
SET @partsouq_008_source_indexes_done := (
  SELECT COUNT(DISTINCT INDEX_NAME)
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts'
    AND INDEX_NAME IN (
      'idx_published_model', 'idx_published_category', 'idx_published_group'
    )
);
SET @partsouq_008_views_done := (
  SELECT COUNT(*)
  FROM information_schema.VIEWS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME IN ('v_parts', 'v_vin_part_fitments')
);
SET @partsouq_008_mapping_view_source_columns_done := (
  SELECT COUNT(*)
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'v_vin_part_fitments'
    AND COLUMN_NAME IN (
      'part_id', 'model_id', 'vehicle_id', 'vehicle_vid', 'category_id', 'category_cid',
      'group_id', 'group_uid', 'code', 'group_code'
    )
);

DROP PROCEDURE IF EXISTS assert_partsouq_008_postflight;
DELIMITER //
CREATE PROCEDURE assert_partsouq_008_postflight()
BEGIN
  IF @partsouq_008_source_columns_done <> 6
     OR @partsouq_008_source_indexes_done <> 3
     OR @partsouq_008_views_done <> 2
     OR @partsouq_008_mapping_view_source_columns_done <> 10 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 008: source-ID schema/view postflight failed';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_008_postflight();
DROP PROCEDURE assert_partsouq_008_postflight;

SELECT
  COUNT(*) AS published_rows,
  COALESCE(SUM(model_id IS NOT NULL), 0) AS safely_mapped_model_rows,
  COALESCE(SUM(category_id IS NOT NULL), 0) AS safely_mapped_category_rows,
  COALESCE(SUM(group_id IS NOT NULL), 0) AS safely_mapped_group_rows,
  COALESCE(SUM(model_id IS NULL OR category_id IS NULL OR group_id IS NULL), 0)
    AS preserved_unmatched_source_rows
FROM published_parts;
