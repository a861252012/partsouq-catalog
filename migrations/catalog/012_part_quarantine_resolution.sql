-- 012_part_quarantine_resolution.sql
--
-- part_quarantine 的維運處置紀錄欄位。
--
-- 政策（使用者決定）：無名稱料號列是「忽略 + 紀錄」—— 爬蟲自動寫入
-- quarantine 表作為完整紀錄，組照常標 done、發布照常進行，不阻擋任何
-- gate。resolved_at / resolution 供維運核對後標記處置狀態（純審計紀錄）：
--   1. 站方補上名稱 → 之後的 run 重新爬取，料號正常落庫；
--   2. 管理員核對後確認該列永遠無法發布 → 填 resolved_at + resolution。
-- count_quarantined()（未處置列數）可供維運查詢，不影響流程。
-- 同一料號在後續 run 再次出現時會重開處置狀態（quarantine_parts 的
-- ON DUPLICATE 會清掉 resolved_at / resolution）。
--
-- 本 migration 不含 USE，可重複執行（條件式 procedure，沿用 009/010 模式）。

SET SESSION lock_wait_timeout = 30;
SET SESSION innodb_lock_wait_timeout = 30;

DROP PROCEDURE IF EXISTS upgrade_partsouq_012_quarantine_resolution;
DELIMITER //
CREATE PROCEDURE upgrade_partsouq_012_quarantine_resolution()
BEGIN
  IF DATABASE() IS NULL OR NOT EXISTS (
    SELECT 1 FROM information_schema.TABLES
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
      AND TABLE_TYPE = 'BASE TABLE'
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 012: select database and apply catalog schema first';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
      AND COLUMN_NAME = 'resolved_at'
  ) THEN
    ALTER TABLE part_quarantine ADD COLUMN resolved_at DATETIME NULL AFTER run_key;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
      AND COLUMN_NAME = 'resolution'
  ) THEN
    ALTER TABLE part_quarantine ADD COLUMN resolution VARCHAR(255) NULL AFTER resolved_at;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
      AND INDEX_NAME = 'idx_quarantine_resolved'
  ) THEN
    ALTER TABLE part_quarantine ADD KEY idx_quarantine_resolved (run_key, resolved_at);
  END IF;
  -- SOL review P2：預設列表只篩 resolved_at 並依 updated_at 排序（沒有
  -- run_key），必須有以 (resolved_at, updated_at) 開頭的索引，否則
  -- 資料累積後會全表掃描 + filesort。
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
      AND INDEX_NAME = 'idx_quarantine_list'
  ) THEN
    ALTER TABLE part_quarantine ADD KEY idx_quarantine_list (resolved_at, updated_at);
  END IF;
END//
DELIMITER ;
CALL upgrade_partsouq_012_quarantine_resolution();
DROP PROCEDURE upgrade_partsouq_012_quarantine_resolution;

DROP PROCEDURE IF EXISTS assert_partsouq_012_output;
DELIMITER //
CREATE PROCEDURE assert_partsouq_012_output()
BEGIN
  IF (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
      AND COLUMN_NAME IN ('resolved_at', 'resolution')
  ) <> 2 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 012: quarantine resolution columns postflight failed';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
      AND INDEX_NAME = 'idx_quarantine_resolved'
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 012: quarantine resolution index postflight failed';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
      AND INDEX_NAME = 'idx_quarantine_list'
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 012: quarantine list index postflight failed';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_012_output();
DROP PROCEDURE assert_partsouq_012_output;