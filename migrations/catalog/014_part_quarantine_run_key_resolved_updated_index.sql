-- 014_part_quarantine_run_key_resolved_updated_index.sql
--
-- SOL review 第八輪 P2：migration 013 的 (run_key, updated_at) 能消除
-- filesort，但索引不含 resolved_at，無法用索引過濾「未處置」列。偏斜
-- 資料（同一 run_key 下 9,999 筆已處置 + 1 筆未處置）時，unresolved
-- 查詢必須掃過 9,999 筆已處置列（實測約 4.86 ms），而既有
-- idx_quarantine_resolved (run_key, resolved_at) 只掃 1 列（約
-- 0.0175 ms）。
--
-- 改為 (run_key, resolved_at, updated_at)：
--   - run_key 等值篩選 + resolved_at IS NULL 形成連續索引範圍，只觸及
--     未處置列；
--   - 範圍內依 (updated_at, 隱含 PK id) 排序，Backward index scan 直接
--     滿足 ORDER BY updated_at DESC, id DESC，無 filesort。
-- 偏斜情境下 rows_examined = 未處置列數（1 列），不掃已處置列。
--
-- state=all 的「未處置優先」排序（ORDER BY (resolved_at IS NOT NULL),
-- updated_at DESC, id DESC）為使用者裁決的語意，屬低頻歷史檢視，
-- filesort 是可接受設計，不為此加 generated column 或複雜查詢。
--
-- 本 migration 不含 USE，可重複執行（沿用 012/013 模式）；會移除
-- migration 013 建立的 idx_quarantine_run_key_updated。

SET SESSION lock_wait_timeout = 30;
SET SESSION innodb_lock_wait_timeout = 30;

DROP PROCEDURE IF EXISTS upgrade_partsouq_014_quarantine_run_key_resolved_updated_index;
DELIMITER //
CREATE PROCEDURE upgrade_partsouq_014_quarantine_run_key_resolved_updated_index()
BEGIN
  IF DATABASE() IS NULL OR NOT EXISTS (
    SELECT 1 FROM information_schema.TABLES
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
      AND TABLE_TYPE = 'BASE TABLE'
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 014: select database and apply catalog schema first';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
      AND INDEX_NAME = 'idx_quarantine_run_key_resolved_updated'
  ) THEN
    IF EXISTS (
      SELECT 1 FROM information_schema.STATISTICS
      WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
        AND INDEX_NAME = 'idx_quarantine_run_key_updated'
    ) THEN
      ALTER TABLE part_quarantine DROP KEY idx_quarantine_run_key_updated;
    END IF;
    ALTER TABLE part_quarantine
      ADD KEY idx_quarantine_run_key_resolved_updated (run_key, resolved_at, updated_at);
  ELSEIF EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
      AND INDEX_NAME = 'idx_quarantine_run_key_updated'
  ) THEN
    ALTER TABLE part_quarantine DROP KEY idx_quarantine_run_key_updated;
  END IF;
END//
DELIMITER ;
CALL upgrade_partsouq_014_quarantine_run_key_resolved_updated_index();
DROP PROCEDURE upgrade_partsouq_014_quarantine_run_key_resolved_updated_index;

DROP PROCEDURE IF EXISTS assert_partsouq_014_output;
DELIMITER //
CREATE PROCEDURE assert_partsouq_014_output()
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
      AND INDEX_NAME = 'idx_quarantine_run_key_resolved_updated'
  ) OR EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
      AND INDEX_NAME = 'idx_quarantine_run_key_updated'
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 014: run_key+resolved_at+updated_at index postflight failed';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_014_output();
DROP PROCEDURE assert_partsouq_014_output;