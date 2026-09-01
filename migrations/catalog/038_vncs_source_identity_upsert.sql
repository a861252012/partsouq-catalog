-- 038_vncs_source_identity_upsert.sql
--
-- 讓 VNCS 排程重跑具備冪等性。025 的設計刻意讓非 VIN 引擎碼列不參與
-- 唯一約束（一碼多車），代價是重跑會整批追加重複列，必須人工清理後
-- 才能再同步；排程化之後這個維運負擔不可接受。
--
-- 本 migration 以「完整內容指紋」建立唯一鍵：JSON_ARRAY 封裝全部語意
-- 欄位（含 NULL），SHA2 後作為 STORED generated column。一碼多車的列
-- 在 model_raw／approval_date／doors 等欄位上必然不同，指紋互異，仍
-- 絕不靜默丟資料；只有逐欄完全相同的重複列會合併為一次 upsert 更新。
-- upsert_vehicles 既有 ON DUPLICATE KEY UPDATE 無需修改即自動生效。
--
-- MySQL 8 的 DDL 具原子性：欄位與唯一索引會一起成立。dirty retry 時
-- 若欄位已存在即視為本步驟已套用，直接跳到 output 檢查。

SET SESSION lock_wait_timeout = 30;
SET SESSION innodb_lock_wait_timeout = 30;

DROP PROCEDURE IF EXISTS apply_partsouq_038;
DELIMITER //
CREATE PROCEDURE apply_partsouq_038()
BEGIN
  DECLARE column_exists INT DEFAULT 0;
  IF NOT EXISTS (
    SELECT 1 FROM catalog_schema_ledger
    WHERE change_key = 'migration:037' AND state = 'applied'
  ) OR NOT EXISTS (
    SELECT 1 FROM information_schema.TABLES
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tw_vncs_vehicles'
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 038: apply catalog migrations through 037 first';
  END IF;
  SELECT COUNT(*) INTO column_exists
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tw_vncs_vehicles'
    AND COLUMN_NAME = 'source_identity_sha256';
  IF column_exists = 0 THEN
    IF EXISTS (
      SELECT 1 FROM (
        SELECT SHA2(CONVERT(JSON_ARRAY(
          vehicle_kind, make, model_raw, displacement_cc, body_rule, transmission,
          doors, style, model_year, model_group_code, body_or_engine_code,
          is_vin, period, approval_date, check_code
        ) USING utf8mb4), 256) AS source_identity
        FROM tw_vncs_vehicles
      ) AS identities
      GROUP BY source_identity
      HAVING COUNT(*) > 1
      LIMIT 1
    ) THEN
      SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'migration 038: tw_vncs_vehicles contains duplicate rows; clean them first';
    END IF;
    SET @ddl = CONCAT(
      'ALTER TABLE tw_vncs_vehicles',
      ' ADD COLUMN source_identity_sha256 CHAR(64) GENERATED ALWAYS AS (',
      'SHA2(CONVERT(JSON_ARRAY(',
      'vehicle_kind, make, model_raw, displacement_cc, body_rule, transmission,',
      'doors, style, model_year, model_group_code, body_or_engine_code,',
      'is_vin, period, approval_date, check_code',
      ') USING utf8mb4), 256)) STORED NOT NULL,',
      ' ADD UNIQUE KEY uq_vncs_source_identity (source_identity_sha256)'
    );
    PREPARE partsouq_038_stmt FROM @ddl;
    EXECUTE partsouq_038_stmt;
    DEALLOCATE PREPARE partsouq_038_stmt;
  END IF;
END//
DELIMITER ;
CALL apply_partsouq_038();
DROP PROCEDURE apply_partsouq_038;

DROP PROCEDURE IF EXISTS assert_partsouq_038_output;
DELIMITER //
CREATE PROCEDURE assert_partsouq_038_output()
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tw_vncs_vehicles'
      AND COLUMN_NAME = 'source_identity_sha256'
      AND LOWER(GENERATION_EXPRESSION) LIKE '%json_array%'
      AND LOWER(EXTRA) LIKE '%stored%generated%'
  ) OR NOT EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tw_vncs_vehicles'
      AND INDEX_NAME = 'uq_vncs_source_identity' AND NON_UNIQUE = 0
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 038: VNCS source identity contract is incomplete';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_038_output();
DROP PROCEDURE assert_partsouq_038_output;
