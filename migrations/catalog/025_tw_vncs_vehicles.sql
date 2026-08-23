-- 025_tw_vncs_vehicles.sql
--
-- 台灣 MOENV VNCS 汽油車/柴油車車輛主檔（含真實 17 碼 VIN）與極簡同步
-- run ledger。唯一鍵為「條件唯一」：只有 VIN 參與 uq_vncs_vin——generated
-- column 把非 VIN 列映射成 NULL，MySQL 多 NULL 不衝突；非 VIN 引擎號碼
-- 可能一碼多車，只建一般索引 idx_vncs_code，絕不靜默丟資料。

SET SESSION lock_wait_timeout = 30;
SET SESSION innodb_lock_wait_timeout = 30;

DROP PROCEDURE IF EXISTS upgrade_partsouq_025_tw_vncs_vehicles;
DELIMITER //
CREATE PROCEDURE upgrade_partsouq_025_tw_vncs_vehicles()
BEGIN
  IF DATABASE() IS NULL OR (
    SELECT COUNT(*) FROM information_schema.TABLES
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'
      AND TABLE_NAME = 'scheduled_job_runs'
  ) <> 1 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 025: apply the catalog base schema first';
  END IF;

  CREATE TABLE IF NOT EXISTS tw_vncs_vehicles (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    vehicle_kind ENUM('汽油車', '柴油車') NOT NULL,
    make VARCHAR(128) NOT NULL,
    model_raw VARCHAR(512) NOT NULL,
    displacement_cc SMALLINT UNSIGNED NULL,
    body_rule VARCHAR(64) NULL,
    transmission VARCHAR(32) NULL,
    doors TINYINT UNSIGNED NULL,
    style VARCHAR(128) NULL,
    model_year SMALLINT UNSIGNED NOT NULL,
    model_group_code VARCHAR(64) NOT NULL DEFAULT '',
    body_or_engine_code VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    is_vin BOOLEAN NOT NULL,
    vin_code VARCHAR(32) GENERATED ALWAYS AS (
      CASE WHEN is_vin THEN body_or_engine_code END
    ) STORED,
    period VARCHAR(32) NULL,
    approval_date VARCHAR(32) NULL,
    check_code VARCHAR(64) NULL,
    source_url TEXT NOT NULL,
    payload_json JSON NOT NULL,
    first_seen_at DATETIME(6) NOT NULL,
    last_synced_at DATETIME(6) NOT NULL,
    UNIQUE KEY uq_vncs_vin (vin_code),
    KEY idx_vncs_code (body_or_engine_code, model_year),
    KEY idx_vncs_vehicle (make, model_raw(191), model_year),
    CONSTRAINT chk_vncs_vehicle_kind CHECK (vehicle_kind IN ('汽油車', '柴油車'))
  ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

  CREATE TABLE IF NOT EXISTS vncs_sync_runs (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    scheduled_job_run_id BIGINT UNSIGNED NULL,
    run_key VARCHAR(191) NOT NULL,
    status ENUM('running', 'completed', 'failed', 'interrupted') NOT NULL,
    rows_seen BIGINT UNSIGNED NOT NULL DEFAULT 0,
    rows_upserted BIGINT UNSIGNED NOT NULL DEFAULT 0,
    malformed_rows BIGINT UNSIGNED NOT NULL DEFAULT 0,
    started_at DATETIME(6) NOT NULL,
    ended_at DATETIME(6) NULL,
    error_message TEXT NULL,
    INDEX idx_vncs_sync_runs_key (run_key, id),
    CONSTRAINT fk_vncs_sync_scheduled_job
      FOREIGN KEY (scheduled_job_run_id) REFERENCES scheduled_job_runs(id),
    CONSTRAINT chk_vncs_sync_terminal CHECK (
      (status = 'running' AND ended_at IS NULL)
      OR (status IN ('completed', 'failed', 'interrupted') AND ended_at IS NOT NULL)
    )
  ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
END//
DELIMITER ;
CALL upgrade_partsouq_025_tw_vncs_vehicles();
DROP PROCEDURE upgrade_partsouq_025_tw_vncs_vehicles;

DROP PROCEDURE IF EXISTS assert_partsouq_025_output;
DELIMITER //
CREATE PROCEDURE assert_partsouq_025_output()
BEGIN
  IF (
    SELECT COUNT(*) FROM information_schema.TABLES
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'
      AND TABLE_NAME IN ('tw_vncs_vehicles', 'vncs_sync_runs')
  ) <> 2 OR NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tw_vncs_vehicles'
      AND COLUMN_NAME = 'vin_code'
      AND COLUMN_TYPE = 'varchar(32)'
      AND IS_NULLABLE = 'YES'
      AND EXTRA LIKE '%STORED GENERATED%'
  ) OR NOT EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tw_vncs_vehicles'
      AND INDEX_NAME = 'uq_vncs_vin' AND NON_UNIQUE = 0
  ) OR NOT EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tw_vncs_vehicles'
      AND INDEX_NAME = 'idx_vncs_code' AND SEQ_IN_INDEX IN (1, 2)
  ) OR NOT EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'tw_vncs_vehicles'
      AND CONSTRAINT_NAME = 'chk_vncs_vehicle_kind'
      AND CONSTRAINT_TYPE = 'CHECK' AND ENFORCED = 'YES'
  ) OR NOT EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'vncs_sync_runs'
      AND CONSTRAINT_NAME = 'chk_vncs_sync_terminal'
      AND CONSTRAINT_TYPE = 'CHECK' AND ENFORCED = 'YES'
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 025: output contract mismatch';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_025_output();
DROP PROCEDURE assert_partsouq_025_output;
