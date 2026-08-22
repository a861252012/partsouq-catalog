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
--
-- 第九輪 review P2：只檢查 INDEX_NAME 不夠——同名但欄位順序錯誤或
-- INVISIBLE 的索引會讓 FORCE INDEX 回 MySQL 1176。本 migration 對
-- idx_quarantine_run_key_resolved_updated 與 idx_quarantine_list 都驗
-- 完整 signature（GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX)）與
-- IS_VISIBLE = 'YES'，不符時 drop 重建；postflight 再斷言一次。

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
  -- idx_quarantine_run_key_resolved_updated (run_key, resolved_at, updated_at)
  IF EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
      AND INDEX_NAME = 'idx_quarantine_run_key_resolved_updated'
  ) AND NOT (
    (SELECT GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX)
       FROM information_schema.STATISTICS
       WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
         AND INDEX_NAME = 'idx_quarantine_run_key_resolved_updated')
      = 'run_key,resolved_at,updated_at'
    AND (SELECT MAX(IS_VISIBLE)
       FROM information_schema.STATISTICS
       WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
         AND INDEX_NAME = 'idx_quarantine_run_key_resolved_updated') = 'YES'
  ) THEN
    ALTER TABLE part_quarantine DROP KEY idx_quarantine_run_key_resolved_updated;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
      AND INDEX_NAME = 'idx_quarantine_run_key_resolved_updated'
  ) THEN
    ALTER TABLE part_quarantine
      ADD KEY idx_quarantine_run_key_resolved_updated (run_key, resolved_at, updated_at);
  END IF;
  IF EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
      AND INDEX_NAME = 'idx_quarantine_run_key_updated'
  ) THEN
    ALTER TABLE part_quarantine DROP KEY idx_quarantine_run_key_updated;
  END IF;
  -- idx_quarantine_list (resolved_at, updated_at)，012 建立；unresolved
  -- 無 run_key 路徑 FORCE INDEX 它，同樣必須驗 signature 與可見性。
  IF EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
      AND INDEX_NAME = 'idx_quarantine_list'
  ) AND NOT (
    (SELECT GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX)
       FROM information_schema.STATISTICS
       WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
         AND INDEX_NAME = 'idx_quarantine_list')
      = 'resolved_at,updated_at'
    AND (SELECT MAX(IS_VISIBLE)
       FROM information_schema.STATISTICS
       WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
         AND INDEX_NAME = 'idx_quarantine_list') = 'YES'
  ) THEN
    ALTER TABLE part_quarantine DROP KEY idx_quarantine_list;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
      AND INDEX_NAME = 'idx_quarantine_list'
  ) THEN
    ALTER TABLE part_quarantine ADD KEY idx_quarantine_list (resolved_at, updated_at);
  END IF;
END//
DELIMITER ;
CALL upgrade_partsouq_014_quarantine_run_key_resolved_updated_index();
DROP PROCEDURE upgrade_partsouq_014_quarantine_run_key_resolved_updated_index;

DROP PROCEDURE IF EXISTS assert_partsouq_014_output;
DELIMITER //
CREATE PROCEDURE assert_partsouq_014_output()
BEGIN
  DECLARE v_sig VARCHAR(255);
  DECLARE v_vis VARCHAR(3);
  DECLARE v_list_sig VARCHAR(255);
  DECLARE v_list_vis VARCHAR(3);
  SELECT GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX), MAX(IS_VISIBLE)
    INTO v_sig, v_vis
    FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
      AND INDEX_NAME = 'idx_quarantine_run_key_resolved_updated';
  SELECT GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX), MAX(IS_VISIBLE)
    INTO v_list_sig, v_list_vis
    FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
      AND INDEX_NAME = 'idx_quarantine_list';
  IF v_sig IS NULL OR v_sig <> 'run_key,resolved_at,updated_at' OR v_vis <> 'YES'
     OR v_list_sig IS NULL OR v_list_sig <> 'resolved_at,updated_at' OR v_list_vis <> 'YES'
     OR EXISTS (
       SELECT 1 FROM information_schema.STATISTICS
       WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
         AND INDEX_NAME = 'idx_quarantine_run_key_updated'
     ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 014: index signature/visibility postflight failed';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_014_output();
DROP PROCEDURE assert_partsouq_014_output;
