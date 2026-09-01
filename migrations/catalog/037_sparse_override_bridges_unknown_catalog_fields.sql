-- 037_sparse_override_bridges_unknown_catalog_fields.sql
--
-- 修正 manual-sparse-override 的層間不一致。候選查詢與 mapping 建立都接受
-- 「decode 欄位有值、快照同欄位 NULL」的 sparse 組合，但 v_vin_part_fitments
-- 卻在相同組合上拒絕，導致快照 engine／trim 未發布（站方頁面本來就沒有）
-- 時，透過任何合法路徑都產生不了 fitment。
--
-- 快照欄位 NULL 代表站方未發布，不是「不符」。人工確認的 mapping（必附
-- source_reference 依據）攜帶 decode 值時，予以橋接；快照欄位有值但不符
-- decode 時仍然拒絕，fail-closed 語意不變。

SET SESSION lock_wait_timeout = 30;
SET SESSION innodb_lock_wait_timeout = 30;

DROP PROCEDURE IF EXISTS assert_partsouq_037_preflight;
DELIMITER //
CREATE PROCEDURE assert_partsouq_037_preflight()
BEGIN
  IF DATABASE() IS NULL OR NOT EXISTS (
    SELECT 1 FROM information_schema.VIEWS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'v_current_catalog_parts'
  ) OR NOT EXISTS (
    SELECT 1 FROM catalog_schema_ledger
    WHERE change_key = 'migration:036' AND state = 'applied'
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 037: apply catalog migrations through 036 first';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_037_preflight();
DROP PROCEDURE assert_partsouq_037_preflight;

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
    JOIN v_current_catalog_parts AS catalog_part
      ON catalog_part.vehicle_id = vehicle_mapping.partsouq_vehicle_id
    LEFT JOIN strict_vehicle_counts
      ON strict_vehicle_counts.mapping_id = vehicle_mapping.id
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
                  OR (
                      NULLIF(TRIM(catalog_part.model), '') IS NULL
                      AND NULLIF(TRIM(vehicle_mapping.model_name), '') IS NOT NULL
                  )
              )
              AND (
                  NULLIF(TRIM(vin_decode.engine_model), '') IS NULL
                  OR CAST(REGEXP_REPLACE(UPPER(catalog_part.engine), '[^A-Z0-9]', '') AS BINARY)
                      = CAST(REGEXP_REPLACE(UPPER(vin_decode.engine_model), '[^A-Z0-9]', '') AS BINARY)
                  OR (
                      NULLIF(TRIM(catalog_part.engine), '') IS NULL
                      AND NULLIF(TRIM(vehicle_mapping.engine), '') IS NOT NULL
                  )
              )
              AND (
                  NULLIF(TRIM(vin_decode.trim_name), '') IS NULL
                  OR CAST(REGEXP_REPLACE(UPPER(catalog_part.trim_name), '[^A-Z0-9]', '') AS BINARY)
                      = CAST(REGEXP_REPLACE(UPPER(vin_decode.trim_name), '[^A-Z0-9]', '') AS BINARY)
                  OR (
                      NULLIF(TRIM(catalog_part.trim_name), '') IS NULL
                      AND NULLIF(TRIM(vehicle_mapping.trim_name), '') IS NOT NULL
                  )
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

DROP PROCEDURE IF EXISTS assert_partsouq_037_output;
DELIMITER //
CREATE PROCEDURE assert_partsouq_037_output()
BEGIN
  -- MySQL 會把 view 定義改寫成含 schema 限定的反引號形式，因此 assert 片段
  -- 以別名開頭（不含 schema 名），並沿用正規化後的壓縮空格與括號。
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.VIEWS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'v_vin_part_fitments'
      AND LOCATE('strict_vehicle_counts', LOWER(VIEW_DEFINITION)) > 0
      AND LOCATE(
        '`catalog_part`.`model`),'''') is null) and '
        '(nullif(trim(`vehicle_mapping`.`model_name`),'''') is not null',
        LOWER(VIEW_DEFINITION)
      ) > 0
      AND LOCATE(
        '`catalog_part`.`engine`),'''') is null) and '
        '(nullif(trim(`vehicle_mapping`.`engine`),'''') is not null',
        LOWER(VIEW_DEFINITION)
      ) > 0
      AND LOCATE(
        '`catalog_part`.`trim_name`),'''') is null) and '
        '(nullif(trim(`vehicle_mapping`.`trim_name`),'''') is not null',
        LOWER(VIEW_DEFINITION)
      ) > 0
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 037: sparse override bridge view is incomplete';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_037_output();
DROP PROCEDURE assert_partsouq_037_output;
