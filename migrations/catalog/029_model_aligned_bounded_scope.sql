-- 029_model_aligned_bounded_scope.sql
--
-- bounded run 可保存單一品牌、型號與實際年份下限。既有 run 的三欄維持
-- NULL，沿用原本未限定車款的語意；新 scope 必須三欄一起存在。

SET SESSION lock_wait_timeout = 30;
SET SESSION innodb_lock_wait_timeout = 30;

DROP PROCEDURE IF EXISTS upgrade_partsouq_029_model_aligned_bounded_scope;
DELIMITER //
CREATE PROCEDURE upgrade_partsouq_029_model_aligned_bounded_scope()
BEGIN
  IF DATABASE() IS NULL OR NOT EXISTS (
    SELECT 1 FROM information_schema.TABLES
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'crawl_runs'
      AND TABLE_TYPE = 'BASE TABLE'
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 029: apply the catalog base schema first';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'crawl_runs'
      AND COLUMN_NAME = 'scope_brand'
  ) THEN
    ALTER TABLE crawl_runs
      ADD COLUMN scope_brand VARCHAR(64) NULL AFTER target_parts;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'crawl_runs'
      AND COLUMN_NAME = 'scope_model'
  ) THEN
    ALTER TABLE crawl_runs
      ADD COLUMN scope_model VARCHAR(128) NULL AFTER scope_brand;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'crawl_runs'
      AND COLUMN_NAME = 'scope_vehicle_year_floor'
  ) THEN
    ALTER TABLE crawl_runs
      ADD COLUMN scope_vehicle_year_floor SMALLINT UNSIGNED NULL AFTER scope_model;
  END IF;

  IF EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'crawl_runs'
      AND CONSTRAINT_NAME = 'chk_crawl_run_model_scope'
      AND CONSTRAINT_TYPE = 'CHECK'
  ) THEN
    ALTER TABLE crawl_runs DROP CHECK chk_crawl_run_model_scope;
  END IF;
  ALTER TABLE crawl_runs
    ADD CONSTRAINT chk_crawl_run_model_scope CHECK (
      (
        scope_brand IS NULL
        AND scope_model IS NULL
        AND scope_vehicle_year_floor IS NULL
      ) OR (
        NULLIF(TRIM(scope_brand), '') IS NOT NULL
        AND NULLIF(TRIM(scope_model), '') IS NOT NULL
        AND scope_vehicle_year_floor IS NOT NULL
        AND scope_vehicle_year_floor BETWEEN 1886 AND 2100
      )
    );
END//
DELIMITER ;
CALL upgrade_partsouq_029_model_aligned_bounded_scope();
DROP PROCEDURE upgrade_partsouq_029_model_aligned_bounded_scope;

DROP PROCEDURE IF EXISTS assert_partsouq_029_output;
DELIMITER //
CREATE PROCEDURE assert_partsouq_029_output()
BEGIN
  IF (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'crawl_runs'
      AND (
        (COLUMN_NAME = 'scope_brand' AND COLUMN_TYPE = 'varchar(64)' AND IS_NULLABLE = 'YES')
        OR (COLUMN_NAME = 'scope_model' AND COLUMN_TYPE = 'varchar(128)' AND IS_NULLABLE = 'YES')
        OR (
          COLUMN_NAME = 'scope_vehicle_year_floor'
          AND COLUMN_TYPE = 'smallint unsigned'
          AND IS_NULLABLE = 'YES'
        )
      )
  ) <> 3 OR NOT EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS AS constraints_t
    JOIN information_schema.CHECK_CONSTRAINTS AS checks_t
      ON checks_t.CONSTRAINT_SCHEMA = constraints_t.CONSTRAINT_SCHEMA
     AND checks_t.CONSTRAINT_NAME = constraints_t.CONSTRAINT_NAME
    WHERE constraints_t.CONSTRAINT_SCHEMA = DATABASE()
      AND constraints_t.TABLE_NAME = 'crawl_runs'
      AND constraints_t.CONSTRAINT_NAME = 'chk_crawl_run_model_scope'
      AND constraints_t.CONSTRAINT_TYPE = 'CHECK'
      AND constraints_t.ENFORCED = 'YES'
      AND LOCATE('scope_brand', LOWER(checks_t.CHECK_CLAUSE)) > 0
      AND LOCATE('scope_model', LOWER(checks_t.CHECK_CLAUSE)) > 0
      AND LOCATE('scope_vehicle_year_floor', LOWER(checks_t.CHECK_CLAUSE)) > 0
      AND LOCATE('trim', LOWER(checks_t.CHECK_CLAUSE)) > 0
      AND LOCATE('1886', LOWER(checks_t.CHECK_CLAUSE)) > 0
      AND LOCATE('2100', LOWER(checks_t.CHECK_CLAUSE)) > 0
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 029: output contract mismatch';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_029_output();
DROP PROCEDURE assert_partsouq_029_output;
