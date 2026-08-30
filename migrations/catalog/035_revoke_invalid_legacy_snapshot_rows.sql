-- 035_revoke_invalid_legacy_snapshot_rows.sql
--
-- migration 032 只會在 source part 的原始料號等欄位仍與 snapshot 相同時
-- 回填 evidence digest。若舊 snapshot 的預先正規化料號曾被改寫，digest
-- 仍可能被回填。這裡只移除已綁 evidence digest、卻無法由 snapshot 原始
-- 料號重算的列；正式 view 必須精確 10,000 筆，因此受影響的舊 snapshot
-- 會 fail closed，等待新的 verified bounded run 取代。raw parts 不會被修改。

SET SESSION lock_wait_timeout = 30;
SET SESSION innodb_lock_wait_timeout = 30;

DROP PROCEDURE IF EXISTS assert_partsouq_035_preflight;
DELIMITER //
CREATE PROCEDURE assert_partsouq_035_preflight()
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
  ) OR NOT EXISTS (
    SELECT 1 FROM information_schema.TRIGGERS
    WHERE TRIGGER_SCHEMA = DATABASE()
      AND TRIGGER_NAME = 'prevent_bounded_parts_update'
      AND EVENT_OBJECT_TABLE = 'bounded_parts'
      AND ACTION_TIMING = 'BEFORE'
      AND EVENT_MANIPULATION = 'UPDATE'
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 035: apply catalog migrations through 034 first';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_035_preflight();
DROP PROCEDURE assert_partsouq_035_preflight;

DELETE FROM bounded_parts
WHERE evidence_record_sha256 IS NOT NULL
  AND NOT (
    CAST(part_number_normalized AS BINARY)
    <=> CAST(UPPER(REGEXP_REPLACE(part_number, '[[:space:]-]+', '')) AS BINARY)
  );

DROP PROCEDURE IF EXISTS assert_partsouq_035_output;
DELIMITER //
CREATE PROCEDURE assert_partsouq_035_output()
BEGIN
  IF EXISTS (
    SELECT 1 FROM bounded_parts
    WHERE evidence_record_sha256 IS NOT NULL
      AND NOT (
        CAST(part_number_normalized AS BINARY)
        <=> CAST(UPPER(REGEXP_REPLACE(part_number, '[[:space:]-]+', '')) AS BINARY)
      )
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 035: invalid legacy bounded snapshot row remains';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_035_output();
DROP PROCEDURE assert_partsouq_035_output;
