-- 033_bounded_snapshot_immutable.sql
--
-- bounded_parts 是已發布的正式 snapshot。允許直接 UPDATE 會讓 evidence
-- digest 仍可通過 view 的關聯檢查，卻輸出遭改寫的欄位；因此只能以整批
-- DELETE + INSERT 發布下一份 snapshot。

SET SESSION lock_wait_timeout = 30;
SET SESSION innodb_lock_wait_timeout = 30;

DROP PROCEDURE IF EXISTS assert_partsouq_033_preflight;
DELIMITER //
CREATE PROCEDURE assert_partsouq_033_preflight()
BEGIN
  IF DATABASE() IS NULL OR NOT EXISTS (
    SELECT 1 FROM information_schema.TABLES
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_TYPE = 'BASE TABLE'
      AND TABLE_NAME = 'bounded_parts'
  ) OR NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'bounded_parts'
      AND COLUMN_NAME = 'evidence_record_sha256'
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 033: apply catalog migrations through 032 first';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_033_preflight();
DROP PROCEDURE assert_partsouq_033_preflight;

DROP TRIGGER IF EXISTS prevent_bounded_parts_update;
CREATE TRIGGER prevent_bounded_parts_update
BEFORE UPDATE ON bounded_parts
FOR EACH ROW
  SIGNAL SQLSTATE '45000'
    SET MESSAGE_TEXT = 'bounded_parts snapshot is immutable; publish a replacement snapshot';

DROP PROCEDURE IF EXISTS assert_partsouq_033_output;
DELIMITER //
CREATE PROCEDURE assert_partsouq_033_output()
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.TRIGGERS
    WHERE TRIGGER_SCHEMA = DATABASE()
      AND TRIGGER_NAME = 'prevent_bounded_parts_update'
      AND EVENT_OBJECT_TABLE = 'bounded_parts'
      AND ACTION_TIMING = 'BEFORE'
      AND EVENT_MANIPULATION = 'UPDATE'
      AND LOCATE('SIGNAL SQLSTATE', UPPER(ACTION_STATEMENT)) > 0
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 033: bounded snapshot immutability is incomplete';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_033_output();
DROP PROCEDURE assert_partsouq_033_output;
