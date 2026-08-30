-- 028_sparse_vin_part_fitments.sql
--
-- 既有資料庫不會重跑 db/admin.sql。這個 migration 只更新 VIN 零件適配
-- view，讓已人工確認的 partial NHTSA decode 可以產生適配結果；任何年份、
-- 品牌或車款快照不一致時仍不回傳零件。

SET SESSION lock_wait_timeout = 30;
SET SESSION innodb_lock_wait_timeout = 30;

CREATE OR REPLACE VIEW v_vin_part_fitments AS
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
              AND 1 = (
                  SELECT COUNT(DISTINCT exact_candidate.vehicle_id)
                  FROM v_current_catalog_parts AS exact_candidate
                  WHERE exact_candidate.vehicle_id IS NOT NULL
                    AND (
                        exact_candidate.production_from IS NOT NULL
                        OR exact_candidate.production_to IS NOT NULL
                    )
                    AND CAST(REGEXP_REPLACE(
                        UPPER(exact_candidate.brand), '[^A-Z0-9]', ''
                    ) AS BINARY) = CAST(REGEXP_REPLACE(
                        UPPER(vin_decode.make_name), '[^A-Z0-9]', ''
                    ) AS BINARY)
                    AND CAST(REGEXP_REPLACE(
                        UPPER(exact_candidate.model), '[^A-Z0-9]', ''
                    ) AS BINARY) = CAST(REGEXP_REPLACE(
                        UPPER(vin_decode.model_name), '[^A-Z0-9]', ''
                    ) AS BINARY)
                    AND CAST(REGEXP_REPLACE(
                        UPPER(exact_candidate.engine), '[^A-Z0-9]', ''
                    ) AS BINARY) = CAST(REGEXP_REPLACE(
                        UPPER(vin_decode.engine_model), '[^A-Z0-9]', ''
                    ) AS BINARY)
                    AND CAST(REGEXP_REPLACE(
                        UPPER(exact_candidate.trim_name), '[^A-Z0-9]', ''
                    ) AS BINARY) = CAST(REGEXP_REPLACE(
                        UPPER(vin_decode.trim_name), '[^A-Z0-9]', ''
                    ) AS BINARY)
                    AND (
                        exact_candidate.production_from IS NULL
                        OR vin_decode.model_year >= CAST(
                            LEFT(exact_candidate.production_from, 4) AS UNSIGNED
                        )
                    )
                    AND (
                        exact_candidate.production_to IS NULL
                        OR vin_decode.model_year <= CAST(
                            LEFT(exact_candidate.production_to, 4) AS UNSIGNED
                        )
                    )
              )
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
