-- 013_part_quarantine_run_key_updated_index.sql
--
-- SOL review 第七輪 P2：unresolved + run_key 篩選的列表查詢
-- （WHERE run_key = ? AND resolved_at IS NULL ORDER BY updated_at DESC,
-- id DESC）原本只靠 idx_quarantine_resolved (run_key, resolved_at)，
-- 該索引不含 updated_at，EXPLAIN 為 idx_quarantine_resolved +
-- Using filesort。
--
-- 新增 (run_key, updated_at) 索引：run_key 等值篩選後，索引本身依
-- (updated_at, 隱含 PK id) 排序，Backward index scan 直接滿足
-- ORDER BY updated_at DESC, id DESC，無 filesort。unresolved（預設
-- 查詢）加 run_key 與否都不再 filesort。
--
-- state=all 的「未處置優先」排序（ORDER BY (resolved_at IS NOT NULL),
-- updated_at DESC, id DESC）為使用者裁決的語意，屬低頻歷史檢視，
-- filesort 是可接受設計，不為此加 generated column 或複雜查詢。
--
-- 本 migration 不含 USE，可重複執行（沿用 012 模式）。

SET SESSION lock_wait_timeout = 30;
SET SESSION innodb_lock_wait_timeout = 30;

DROP PROCEDURE IF EXISTS upgrade_partsouq_013_quarantine_run_key_updated_index;
DELIMITER //
CREATE PROCEDURE upgrade_partsouq_013_quarantine_run_key_updated_index()
BEGIN
  IF DATABASE() IS NULL OR NOT EXISTS (
    SELECT 1 FROM information_schema.TABLES
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
      AND TABLE_TYPE = 'BASE TABLE'
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 013: select database and apply catalog schema first';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
      AND INDEX_NAME = 'idx_quarantine_run_key_updated'
  ) THEN
    ALTER TABLE part_quarantine ADD KEY idx_quarantine_run_key_updated (run_key, updated_at);
  END IF;
END//
DELIMITER ;
CALL upgrade_partsouq_013_quarantine_run_key_updated_index();
DROP PROCEDURE upgrade_partsouq_013_quarantine_run_key_updated_index;

DROP PROCEDURE IF EXISTS assert_partsouq_013_output;
DELIMITER //
CREATE PROCEDURE assert_partsouq_013_output()
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
      AND INDEX_NAME = 'idx_quarantine_run_key_updated'
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 013: quarantine run_key+updated_at index postflight failed';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_013_output();
DROP PROCEDURE assert_partsouq_013_output;
