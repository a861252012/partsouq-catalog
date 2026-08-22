-- 015_quarantine_index_contract_cleanup.sql
--
-- Forward-only state repair for existing volumes that may already have run
-- migration 014. The two indexes used by the quarantine FORCE INDEX queries
-- must have an exact shape; a same-name UNIQUE, prefix, DESC, functional,
-- non-BTREE, or invisible index is not equivalent.
--
-- This migration also removes three superseded indexes:
--   - idx_quarantine_run_key_updated is replaced by
--     idx_quarantine_run_key_resolved_updated.
--   - idx_quarantine_resolved is the left prefix of that replacement.
--   - idx_quarantine_group is the left prefix of uq_quarantine, which remains
--     available for fk_quarantine_group and ON DELETE CASCADE.
--
-- Shape repairs use one atomic DROP + ADD ALTER. Correct indexes are not
-- rebuilt; invisible-only repairs change metadata. Secondary index DDL is
-- required to stay INPLACE/LOCK=NONE and fails closed after 30 seconds if a
-- writer or long transaction still holds a metadata lock.
-- A migration-owned FULLTEXT index, or orphaned hidden FTS metadata from an
-- earlier repair, fails closed. Removing those artifacts requires a separate
-- table rebuild and must not be hidden inside this online migration.
--
-- This migration contains no USE statement and is idempotent.

SET SESSION lock_wait_timeout = 30;
SET SESSION innodb_lock_wait_timeout = 30;

DROP PROCEDURE IF EXISTS upgrade_partsouq_015_quarantine_index_contract_cleanup;
DELIMITER //
CREATE PROCEDURE upgrade_partsouq_015_quarantine_index_contract_cleanup()
BEGIN
  DECLARE v_index_rows INT DEFAULT 0;
  DECLARE v_shape_valid INT DEFAULT 0;
  DECLARE v_visible INT DEFAULT 0;
  DECLARE v_pk_valid INT DEFAULT 0;
  DECLARE v_uq_valid INT DEFAULT 0;
  DECLARE v_fk_valid INT DEFAULT 0;
  DECLARE v_visible_fulltext INT DEFAULT 0;
  DECLARE v_hidden_fts INT DEFAULT 0;

  IF DATABASE() IS NULL OR NOT EXISTS (
    SELECT 1 FROM information_schema.TABLES
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
      AND TABLE_TYPE = 'BASE TABLE'
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 015: select database and apply catalog schema first';
  END IF;

  SELECT IF(
           COUNT(*) = 1
           AND MIN(NON_UNIQUE) = 0 AND MAX(NON_UNIQUE) = 0
           AND MIN(COLUMN_NAME) = 'id'
           AND COALESCE(SUM(EXPRESSION IS NOT NULL), 0) = 0
           AND COALESCE(SUM(SUB_PART IS NOT NULL), 0) = 0
           AND MIN(COLLATION) = 'A' AND MAX(COLLATION) = 'A'
           AND MIN(INDEX_TYPE) = 'BTREE' AND MAX(INDEX_TYPE) = 'BTREE'
           AND MIN(IS_VISIBLE) = 'YES' AND MAX(IS_VISIBLE) = 'YES',
           1, 0
         )
    INTO v_pk_valid
    FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
      AND INDEX_NAME = 'PRIMARY';
  SELECT IF(
           COUNT(*) = 4
           AND MIN(NON_UNIQUE) = 0 AND MAX(NON_UNIQUE) = 0
           AND GROUP_CONCAT(
             IF(EXPRESSION IS NULL, COLUMN_NAME, '<expr>') ORDER BY SEQ_IN_INDEX
           ) = 'group_id,part_number,range_str,reason'
           AND COALESCE(SUM(EXPRESSION IS NOT NULL), 0) = 0
           AND COALESCE(SUM(SUB_PART IS NOT NULL), 0) = 0
           AND GROUP_CONCAT(
             COALESCE(COLLATION, 'NULL') ORDER BY SEQ_IN_INDEX
           ) = 'A,A,A,A'
           AND MIN(INDEX_TYPE) = 'BTREE' AND MAX(INDEX_TYPE) = 'BTREE'
           AND MIN(IS_VISIBLE) = 'YES' AND MAX(IS_VISIBLE) = 'YES',
           1, 0
         )
    INTO v_uq_valid
    FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
      AND INDEX_NAME = 'uq_quarantine';
  -- The referenced PRIMARY must be exactly groups_t(id), not a legacy
  -- nonstandard prefix reference into a composite PRIMARY(id, ...).
  SELECT IF(
           COUNT(*) = 1
           AND MIN(key_columns.ORDINAL_POSITION) = 1
           AND MIN(key_columns.POSITION_IN_UNIQUE_CONSTRAINT) = 1
           AND MIN(key_columns.COLUMN_NAME) = 'group_id'
           AND MIN(key_columns.REFERENCED_TABLE_SCHEMA) = DATABASE()
           AND MIN(key_columns.REFERENCED_TABLE_NAME) = 'groups_t'
           AND MIN(key_columns.REFERENCED_COLUMN_NAME) = 'id'
           AND MIN(referential_constraints.UNIQUE_CONSTRAINT_SCHEMA) = DATABASE()
           AND MIN(referential_constraints.UNIQUE_CONSTRAINT_NAME) = 'PRIMARY'
           AND MIN(referential_constraints.UPDATE_RULE) = 'NO ACTION'
           AND MIN(referential_constraints.DELETE_RULE) = 'CASCADE'
           AND (
             SELECT COUNT(*)
             FROM information_schema.KEY_COLUMN_USAGE AS parent_key_columns
             WHERE parent_key_columns.CONSTRAINT_SCHEMA = DATABASE()
               AND parent_key_columns.TABLE_SCHEMA = DATABASE()
               AND parent_key_columns.TABLE_NAME = 'groups_t'
               AND parent_key_columns.CONSTRAINT_NAME = 'PRIMARY'
           ) = 1,
           1, 0
         )
    INTO v_fk_valid
    FROM information_schema.KEY_COLUMN_USAGE AS key_columns
    JOIN information_schema.REFERENTIAL_CONSTRAINTS AS referential_constraints
      ON referential_constraints.CONSTRAINT_SCHEMA = key_columns.CONSTRAINT_SCHEMA
     AND referential_constraints.CONSTRAINT_NAME = key_columns.CONSTRAINT_NAME
     AND referential_constraints.TABLE_NAME = key_columns.TABLE_NAME
    WHERE key_columns.CONSTRAINT_SCHEMA = DATABASE()
      AND key_columns.TABLE_SCHEMA = DATABASE()
      AND key_columns.TABLE_NAME = 'part_quarantine'
      AND key_columns.CONSTRAINT_NAME = 'fk_quarantine_group';
  IF v_pk_valid <> 1 OR v_uq_valid <> 1 OR v_fk_valid <> 1 OR EXISTS (
    SELECT 1
    FROM part_quarantine
    LEFT JOIN groups_t ON groups_t.id = part_quarantine.group_id
    WHERE groups_t.id IS NULL
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 015: quarantine key/FK/data preflight failed';
  END IF;
  IF EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
      AND INDEX_NAME IN (
        'idx_quarantine_list',
        'idx_quarantine_run_key_resolved_updated',
        'idx_quarantine_run_key_updated',
        'idx_quarantine_resolved',
        'idx_quarantine_group'
      )
      AND INDEX_TYPE = 'FULLTEXT'
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 015: FULLTEXT drift requires table rebuild';
  END IF;
  SELECT COUNT(DISTINCT INDEX_NAME)
    INTO v_visible_fulltext
    FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
      AND INDEX_TYPE = 'FULLTEXT';
  SHOW EXTENDED INDEX FROM part_quarantine
    WHERE Key_name = 'FTS_DOC_ID_INDEX' AND Column_name = 'FTS_DOC_ID';
  SET v_hidden_fts = FOUND_ROWS();
  IF v_hidden_fts > 0 AND v_visible_fulltext = 0 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 015: hidden FTS artifacts require table rebuild';
  END IF;

  -- idx_quarantine_run_key_resolved_updated (run_key, resolved_at, updated_at)
  SELECT COUNT(*),
         IF(
           COUNT(*) = 3
           AND MIN(NON_UNIQUE) = 1 AND MAX(NON_UNIQUE) = 1
           AND GROUP_CONCAT(
             IF(EXPRESSION IS NULL, COLUMN_NAME, '<expr>') ORDER BY SEQ_IN_INDEX
           ) = 'run_key,resolved_at,updated_at'
           AND COALESCE(SUM(EXPRESSION IS NOT NULL), 0) = 0
           AND COALESCE(SUM(SUB_PART IS NOT NULL), 0) = 0
           AND GROUP_CONCAT(
             COALESCE(COLLATION, 'NULL') ORDER BY SEQ_IN_INDEX
           ) = 'A,A,A'
           AND MIN(INDEX_TYPE) = 'BTREE' AND MAX(INDEX_TYPE) = 'BTREE',
           1, 0
         ),
         IF(
           COUNT(*) = 3 AND MIN(IS_VISIBLE) = 'YES' AND MAX(IS_VISIBLE) = 'YES',
           1, 0
         )
    INTO v_index_rows, v_shape_valid, v_visible
    FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
      AND INDEX_NAME = 'idx_quarantine_run_key_resolved_updated';
  IF v_index_rows = 0 THEN
    ALTER TABLE part_quarantine
      ADD KEY idx_quarantine_run_key_resolved_updated (run_key, resolved_at, updated_at),
      ALGORITHM=INPLACE, LOCK=NONE;
  ELSEIF v_shape_valid <> 1 THEN
    ALTER TABLE part_quarantine
      DROP KEY idx_quarantine_run_key_resolved_updated,
      ADD KEY idx_quarantine_run_key_resolved_updated (run_key, resolved_at, updated_at),
      ALGORITHM=INPLACE, LOCK=NONE;
  ELSEIF v_visible <> 1 THEN
    ALTER TABLE part_quarantine
      ALTER INDEX idx_quarantine_run_key_resolved_updated VISIBLE,
      ALGORITHM=INPLACE, LOCK=NONE;
  END IF;

  -- idx_quarantine_list (resolved_at, updated_at)
  SELECT COUNT(*),
         IF(
           COUNT(*) = 2
           AND MIN(NON_UNIQUE) = 1 AND MAX(NON_UNIQUE) = 1
           AND GROUP_CONCAT(
             IF(EXPRESSION IS NULL, COLUMN_NAME, '<expr>') ORDER BY SEQ_IN_INDEX
           ) = 'resolved_at,updated_at'
           AND COALESCE(SUM(EXPRESSION IS NOT NULL), 0) = 0
           AND COALESCE(SUM(SUB_PART IS NOT NULL), 0) = 0
           AND GROUP_CONCAT(
             COALESCE(COLLATION, 'NULL') ORDER BY SEQ_IN_INDEX
           ) = 'A,A'
           AND MIN(INDEX_TYPE) = 'BTREE' AND MAX(INDEX_TYPE) = 'BTREE',
           1, 0
         ),
         IF(
           COUNT(*) = 2 AND MIN(IS_VISIBLE) = 'YES' AND MAX(IS_VISIBLE) = 'YES',
           1, 0
         )
    INTO v_index_rows, v_shape_valid, v_visible
    FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
      AND INDEX_NAME = 'idx_quarantine_list';
  IF v_index_rows = 0 THEN
    ALTER TABLE part_quarantine
      ADD KEY idx_quarantine_list (resolved_at, updated_at),
      ALGORITHM=INPLACE, LOCK=NONE;
  ELSEIF v_shape_valid <> 1 THEN
    ALTER TABLE part_quarantine
      DROP KEY idx_quarantine_list,
      ADD KEY idx_quarantine_list (resolved_at, updated_at),
      ALGORITHM=INPLACE, LOCK=NONE;
  ELSEIF v_visible <> 1 THEN
    ALTER TABLE part_quarantine
      ALTER INDEX idx_quarantine_list VISIBLE,
      ALGORITHM=INPLACE, LOCK=NONE;
  END IF;

  IF EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
      AND INDEX_NAME = 'idx_quarantine_run_key_updated'
  ) THEN
    ALTER TABLE part_quarantine
      DROP KEY idx_quarantine_run_key_updated,
      ALGORITHM=INPLACE, LOCK=NONE;
  END IF;
  IF EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
      AND INDEX_NAME = 'idx_quarantine_resolved'
  ) THEN
    ALTER TABLE part_quarantine
      DROP KEY idx_quarantine_resolved,
      ALGORITHM=INPLACE, LOCK=NONE;
  END IF;
  IF EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
      AND INDEX_NAME = 'idx_quarantine_group'
  ) THEN
    ALTER TABLE part_quarantine
      DROP KEY idx_quarantine_group,
      ALGORITHM=INPLACE, LOCK=NONE;
  END IF;
END//
DELIMITER ;
CALL upgrade_partsouq_015_quarantine_index_contract_cleanup();
DROP PROCEDURE upgrade_partsouq_015_quarantine_index_contract_cleanup;

DROP PROCEDURE IF EXISTS assert_partsouq_015_output;
DELIMITER //
CREATE PROCEDURE assert_partsouq_015_output()
BEGIN
  DECLARE v_run_valid INT DEFAULT 0;
  DECLARE v_list_valid INT DEFAULT 0;
  DECLARE v_pk_valid INT DEFAULT 0;
  DECLARE v_uq_valid INT DEFAULT 0;
  DECLARE v_fk_valid INT DEFAULT 0;

  SELECT IF(
           COUNT(*) = 1
           AND MIN(NON_UNIQUE) = 0 AND MAX(NON_UNIQUE) = 0
           AND MIN(COLUMN_NAME) = 'id'
           AND COALESCE(SUM(EXPRESSION IS NOT NULL), 0) = 0
           AND COALESCE(SUM(SUB_PART IS NOT NULL), 0) = 0
           AND MIN(COLLATION) = 'A' AND MAX(COLLATION) = 'A'
           AND MIN(INDEX_TYPE) = 'BTREE' AND MAX(INDEX_TYPE) = 'BTREE'
           AND MIN(IS_VISIBLE) = 'YES' AND MAX(IS_VISIBLE) = 'YES',
           1, 0
         )
    INTO v_pk_valid
    FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
      AND INDEX_NAME = 'PRIMARY';

  SELECT IF(
           COUNT(*) = 3
           AND MIN(NON_UNIQUE) = 1 AND MAX(NON_UNIQUE) = 1
           AND GROUP_CONCAT(
             IF(EXPRESSION IS NULL, COLUMN_NAME, '<expr>') ORDER BY SEQ_IN_INDEX
           ) = 'run_key,resolved_at,updated_at'
           AND COALESCE(SUM(EXPRESSION IS NOT NULL), 0) = 0
           AND COALESCE(SUM(SUB_PART IS NOT NULL), 0) = 0
           AND GROUP_CONCAT(
             COALESCE(COLLATION, 'NULL') ORDER BY SEQ_IN_INDEX
           ) = 'A,A,A'
           AND MIN(INDEX_TYPE) = 'BTREE' AND MAX(INDEX_TYPE) = 'BTREE'
           AND MIN(IS_VISIBLE) = 'YES' AND MAX(IS_VISIBLE) = 'YES',
           1, 0
         )
    INTO v_run_valid
    FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
      AND INDEX_NAME = 'idx_quarantine_run_key_resolved_updated';
  SELECT IF(
           COUNT(*) = 2
           AND MIN(NON_UNIQUE) = 1 AND MAX(NON_UNIQUE) = 1
           AND GROUP_CONCAT(
             IF(EXPRESSION IS NULL, COLUMN_NAME, '<expr>') ORDER BY SEQ_IN_INDEX
           ) = 'resolved_at,updated_at'
           AND COALESCE(SUM(EXPRESSION IS NOT NULL), 0) = 0
           AND COALESCE(SUM(SUB_PART IS NOT NULL), 0) = 0
           AND GROUP_CONCAT(
             COALESCE(COLLATION, 'NULL') ORDER BY SEQ_IN_INDEX
           ) = 'A,A'
           AND MIN(INDEX_TYPE) = 'BTREE' AND MAX(INDEX_TYPE) = 'BTREE'
           AND MIN(IS_VISIBLE) = 'YES' AND MAX(IS_VISIBLE) = 'YES',
           1, 0
         )
    INTO v_list_valid
    FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
      AND INDEX_NAME = 'idx_quarantine_list';
  SELECT IF(
           COUNT(*) = 4
           AND MIN(NON_UNIQUE) = 0 AND MAX(NON_UNIQUE) = 0
           AND GROUP_CONCAT(
             IF(EXPRESSION IS NULL, COLUMN_NAME, '<expr>') ORDER BY SEQ_IN_INDEX
           ) = 'group_id,part_number,range_str,reason'
           AND COALESCE(SUM(EXPRESSION IS NOT NULL), 0) = 0
           AND COALESCE(SUM(SUB_PART IS NOT NULL), 0) = 0
           AND GROUP_CONCAT(
             COALESCE(COLLATION, 'NULL') ORDER BY SEQ_IN_INDEX
           ) = 'A,A,A,A'
           AND MIN(INDEX_TYPE) = 'BTREE' AND MAX(INDEX_TYPE) = 'BTREE'
           AND MIN(IS_VISIBLE) = 'YES' AND MAX(IS_VISIBLE) = 'YES',
           1, 0
         )
    INTO v_uq_valid
    FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
      AND INDEX_NAME = 'uq_quarantine';
  SELECT IF(
           COUNT(*) = 1
           AND MIN(key_columns.ORDINAL_POSITION) = 1
           AND MIN(key_columns.POSITION_IN_UNIQUE_CONSTRAINT) = 1
           AND MIN(key_columns.COLUMN_NAME) = 'group_id'
           AND MIN(key_columns.REFERENCED_TABLE_SCHEMA) = DATABASE()
           AND MIN(key_columns.REFERENCED_TABLE_NAME) = 'groups_t'
           AND MIN(key_columns.REFERENCED_COLUMN_NAME) = 'id'
           AND MIN(referential_constraints.UNIQUE_CONSTRAINT_SCHEMA) = DATABASE()
           AND MIN(referential_constraints.UNIQUE_CONSTRAINT_NAME) = 'PRIMARY'
           AND MIN(referential_constraints.UPDATE_RULE) = 'NO ACTION'
           AND MIN(referential_constraints.DELETE_RULE) = 'CASCADE'
           AND (
             SELECT COUNT(*)
             FROM information_schema.KEY_COLUMN_USAGE AS parent_key_columns
             WHERE parent_key_columns.CONSTRAINT_SCHEMA = DATABASE()
               AND parent_key_columns.TABLE_SCHEMA = DATABASE()
               AND parent_key_columns.TABLE_NAME = 'groups_t'
               AND parent_key_columns.CONSTRAINT_NAME = 'PRIMARY'
           ) = 1,
           1, 0
         )
    INTO v_fk_valid
    FROM information_schema.KEY_COLUMN_USAGE AS key_columns
    JOIN information_schema.REFERENTIAL_CONSTRAINTS AS referential_constraints
      ON referential_constraints.CONSTRAINT_SCHEMA = key_columns.CONSTRAINT_SCHEMA
     AND referential_constraints.CONSTRAINT_NAME = key_columns.CONSTRAINT_NAME
     AND referential_constraints.TABLE_NAME = key_columns.TABLE_NAME
    WHERE key_columns.CONSTRAINT_SCHEMA = DATABASE()
      AND key_columns.TABLE_SCHEMA = DATABASE()
      AND key_columns.TABLE_NAME = 'part_quarantine'
      AND key_columns.CONSTRAINT_NAME = 'fk_quarantine_group';
  IF v_run_valid <> 1 OR v_list_valid <> 1 OR v_pk_valid <> 1
     OR v_uq_valid <> 1 OR v_fk_valid <> 1
     OR EXISTS (
       SELECT 1
       FROM part_quarantine
       LEFT JOIN groups_t ON groups_t.id = part_quarantine.group_id
       WHERE groups_t.id IS NULL
     )
     OR EXISTS (
       SELECT 1 FROM information_schema.STATISTICS
       WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine'
         AND INDEX_NAME IN (
           'idx_quarantine_run_key_updated',
           'idx_quarantine_resolved',
           'idx_quarantine_group'
         )
     ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 015: quarantine index/FK postflight failed';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_015_output();
DROP PROCEDURE assert_partsouq_015_output;
