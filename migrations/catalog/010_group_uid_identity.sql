-- PartSouq crawler — schema migration 010：零件組以 category + code + uid 識別
--
-- 同一分類與 group code 可能有多個不同 uid 的 unit 頁。舊唯一鍵只含
-- category_id + code，會讓後一個變體覆蓋前一個。執行前先停止 catalog
-- writer；本 migration 不含 USE，可重複執行。

SET SESSION lock_wait_timeout = 30;
SET SESSION innodb_lock_wait_timeout = 30;

DROP PROCEDURE IF EXISTS upgrade_partsouq_010_group_identity;
DELIMITER //
CREATE PROCEDURE upgrade_partsouq_010_group_identity()
BEGIN
  IF DATABASE() IS NULL OR NOT EXISTS (
    SELECT 1 FROM information_schema.TABLES
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'groups_t'
      AND TABLE_TYPE = 'BASE TABLE'
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 010: select database and apply catalog schema first';
  END IF;
  IF EXISTS (SELECT 1 FROM crawl_runs WHERE status = 'running') THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 010: running catalog jobs exist; stop writers first';
  END IF;

  UPDATE groups_t SET uid = '' WHERE uid IS NULL;
  ALTER TABLE groups_t MODIFY COLUMN uid VARCHAR(32) NOT NULL DEFAULT '';

  IF COALESCE((
    SELECT GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX SEPARATOR ',')
    FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'groups_t'
      AND INDEX_NAME = 'uq_group' AND NON_UNIQUE = 0
  ), '') <> 'category_id,code,uid' THEN
    IF EXISTS (
      SELECT 1 FROM information_schema.STATISTICS
      WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'groups_t'
        AND INDEX_NAME = 'uq_group_v2'
    ) THEN
      ALTER TABLE groups_t DROP INDEX uq_group_v2;
    END IF;
    -- 先建立替代索引，再移除舊索引。舊 uq_group 同時支援
    -- fk_group_cat 的 category_id 查找，先 drop 會被 MySQL 拒絕。
    ALTER TABLE groups_t
      ADD UNIQUE KEY uq_group_v2 (category_id, code, uid);
    IF EXISTS (
      SELECT 1 FROM information_schema.STATISTICS
      WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'groups_t'
        AND INDEX_NAME = 'uq_group'
    ) THEN
      ALTER TABLE groups_t DROP INDEX uq_group;
    END IF;
    ALTER TABLE groups_t RENAME INDEX uq_group_v2 TO uq_group;
  END IF;
END//
DELIMITER ;
CALL upgrade_partsouq_010_group_identity();
DROP PROCEDURE upgrade_partsouq_010_group_identity;

DROP PROCEDURE IF EXISTS assert_partsouq_010_output;
DELIMITER //
CREATE PROCEDURE assert_partsouq_010_output()
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'groups_t'
      AND COLUMN_NAME = 'uid'
      AND (COLUMN_TYPE <> 'varchar(32)' OR IS_NULLABLE <> 'NO' OR COLUMN_DEFAULT <> '')
  ) OR COALESCE((
    SELECT GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX SEPARATOR ',')
    FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'groups_t'
      AND INDEX_NAME = 'uq_group' AND NON_UNIQUE = 0
  ), '') <> 'category_id,code,uid' THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 010: group uid identity postflight failed';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_010_output();
DROP PROCEDURE assert_partsouq_010_output;
