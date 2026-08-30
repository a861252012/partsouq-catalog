-- 036_bounded_group_receipt_contract.sql
--
-- 將正式 bounded snapshot 的 group receipt 從可被下一輪爬取覆寫的
-- groups_t.fetched_* 分離。每個已發布 group 都必須能對應到同一 run 的
-- verified unit artifact；quota 在 unit 頁中途達標時，明確標為 partial。

SET SESSION lock_wait_timeout = 30;
SET SESSION innodb_lock_wait_timeout = 30;

DROP PROCEDURE IF EXISTS assert_partsouq_036_preflight;
DELIMITER //
CREATE PROCEDURE assert_partsouq_036_preflight()
BEGIN
  IF DATABASE() IS NULL OR (
    SELECT COUNT(*) FROM information_schema.TABLES
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'
      AND TABLE_NAME IN (
        'bounded_parts', 'crawl_runs', 'groups_t',
        'partsouq_http_artifacts', 'partsouq_artifact_records'
      )
  ) <> 5 OR NOT (
    EXISTS (
      SELECT 1 FROM information_schema.VIEWS
      WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'v_current_catalog_parts'
    ) OR EXISTS (
      SELECT 1 FROM information_schema.VIEWS
      WHERE TABLE_SCHEMA = DATABASE()
        AND TABLE_NAME = 'v_current_catalog_parts_evidence_base'
    )
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 036: apply catalog migrations through 035 first';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_036_preflight();
DROP PROCEDURE assert_partsouq_036_preflight;

CREATE TABLE IF NOT EXISTS bounded_group_receipts (
  crawl_run_id         INT NOT NULL,
  group_id             INT NOT NULL,
  source_artifact_id   BIGINT UNSIGNED NOT NULL,
  status               VARCHAR(16) NOT NULL,
  parsed_part_count    INT UNSIGNED NOT NULL,
  accepted_part_count  INT UNSIGNED NOT NULL,
  skipped_record_count INT UNSIGNED NOT NULL,
  recorded_at          DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (crawl_run_id, group_id),
  UNIQUE KEY uq_bounded_group_receipt_artifact (source_artifact_id),
  KEY idx_bounded_group_receipt_group (group_id),
  CONSTRAINT fk_bounded_group_receipt_run FOREIGN KEY (crawl_run_id)
    REFERENCES crawl_runs(id) ON DELETE CASCADE,
  CONSTRAINT fk_bounded_group_receipt_artifact FOREIGN KEY (source_artifact_id, crawl_run_id)
    REFERENCES partsouq_http_artifacts(id, crawl_run_id),
  CONSTRAINT chk_bounded_group_receipt_status CHECK (
    status IN ('done', 'partial')
  ),
  CONSTRAINT chk_bounded_group_receipt_counts CHECK (
    accepted_part_count <= parsed_part_count
    AND (
      (status = 'done' AND accepted_part_count = parsed_part_count)
      OR (
        status = 'partial'
        AND accepted_part_count > 0
        AND accepted_part_count < parsed_part_count
      )
    )
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- db/catalog.sql 在 036 前已建立 receipt table 的 fresh volume，不能只靠
-- CREATE TABLE IF NOT EXISTS 取得新 CHECK。無論初始 table 是否存在，都將
-- 此一未發布 receipt 的狀態契約收斂成同一個定義；若舊資料違反新契約，DDL
-- 會拒絕升級，避免把 partial 誤標為 done。
DROP PROCEDURE IF EXISTS converge_partsouq_036_receipt_count_check;
DELIMITER //
CREATE PROCEDURE converge_partsouq_036_receipt_count_check()
BEGIN
  IF EXISTS (
    SELECT 1
    FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE()
      AND TABLE_NAME = 'bounded_group_receipts'
      AND CONSTRAINT_NAME = 'chk_bounded_group_receipt_counts'
      AND CONSTRAINT_TYPE = 'CHECK'
  ) THEN
    ALTER TABLE bounded_group_receipts
      DROP CHECK chk_bounded_group_receipt_counts;
  END IF;
  ALTER TABLE bounded_group_receipts
    ADD CONSTRAINT chk_bounded_group_receipt_counts CHECK (
      accepted_part_count <= parsed_part_count
      AND (
        (status = 'done' AND accepted_part_count = parsed_part_count)
        OR (
          status = 'partial'
          AND accepted_part_count > 0
          AND accepted_part_count < parsed_part_count
        )
      )
    );
END//
DELIMITER ;
CALL converge_partsouq_036_receipt_count_check();
DROP PROCEDURE converge_partsouq_036_receipt_count_check;

DROP PROCEDURE IF EXISTS add_partsouq_036_bounded_indexes;
DELIMITER //
CREATE PROCEDURE add_partsouq_036_bounded_indexes()
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'bounded_parts'
      AND INDEX_NAME = 'idx_bounded_run_group'
  ) THEN
    ALTER TABLE bounded_parts
      ADD KEY idx_bounded_run_group (crawl_run_id, group_id);
  END IF;
END//
DELIMITER ;
CALL add_partsouq_036_bounded_indexes();
DROP PROCEDURE add_partsouq_036_bounded_indexes;

-- 既有資料只在 snapshot part 的 evidence digest、artifact 內 accepted
-- subset 與 artifact 全部 parser record 三者都可對回時才回填。每個 group
-- 有多個可能 artifact 時不猜測，留給下方 formal view fail closed。
INSERT IGNORE INTO bounded_group_receipts (
  crawl_run_id,
  group_id,
  source_artifact_id,
  status,
  parsed_part_count,
  accepted_part_count,
  skipped_record_count
)
WITH snapshot_groups AS (
  SELECT
    bounded_part.crawl_run_id,
    bounded_part.group_id,
    COUNT(*) AS snapshot_part_count
  FROM bounded_parts AS bounded_part
  GROUP BY bounded_part.crawl_run_id, bounded_part.group_id
),
snapshot_artifact_matches AS (
  SELECT
    bounded_part.crawl_run_id,
    bounded_part.group_id,
    artifact_record.artifact_id,
    COUNT(DISTINCT bounded_part.part_id) AS matched_snapshot_part_count
  FROM bounded_parts AS bounded_part
  JOIN partsouq_artifact_records AS artifact_record
    ON artifact_record.crawl_run_id = bounded_part.crawl_run_id
   AND artifact_record.record_type = 'part'
   AND artifact_record.accepted = 1
   AND artifact_record.part_id = bounded_part.part_id
   AND artifact_record.record_sha256 = bounded_part.evidence_record_sha256
  GROUP BY
    bounded_part.crawl_run_id,
    bounded_part.group_id,
    artifact_record.artifact_id
),
artifact_record_counts AS (
  SELECT
    snapshot_artifact_match.crawl_run_id,
    snapshot_artifact_match.group_id,
    snapshot_artifact_match.artifact_id,
    SUM(artifact_record.record_type = 'part') AS parsed_part_count,
    SUM(
      artifact_record.record_type = 'part'
      AND artifact_record.accepted = 1
    ) AS accepted_part_count,
    COUNT(DISTINCT CASE
      WHEN artifact_record.record_type = 'part' AND artifact_record.accepted = 1
      THEN artifact_record.part_id
    END) AS accepted_part_id_count,
    SUM(artifact_record.record_type = 'quarantine_part') AS skipped_record_count
  FROM snapshot_artifact_matches AS snapshot_artifact_match
  JOIN partsouq_artifact_records AS artifact_record
    ON artifact_record.artifact_id = snapshot_artifact_match.artifact_id
   AND artifact_record.crawl_run_id = snapshot_artifact_match.crawl_run_id
  GROUP BY
    snapshot_artifact_match.crawl_run_id,
    snapshot_artifact_match.group_id,
    snapshot_artifact_match.artifact_id
),
candidate_receipts AS (
  SELECT
    snapshot_group.crawl_run_id,
    snapshot_group.group_id,
    artifact_record_count.artifact_id,
    artifact_record_count.parsed_part_count,
    artifact_record_count.accepted_part_count,
    artifact_record_count.skipped_record_count,
    CASE
      WHEN artifact_record_count.parsed_part_count
           = artifact_record_count.accepted_part_count
        THEN 'done'
      ELSE 'partial'
    END AS status,
    COUNT(*) OVER (
      PARTITION BY snapshot_group.crawl_run_id, snapshot_group.group_id
    ) AS candidate_count
  FROM snapshot_groups AS snapshot_group
  JOIN snapshot_artifact_matches AS snapshot_artifact_match
    ON snapshot_artifact_match.crawl_run_id = snapshot_group.crawl_run_id
   AND snapshot_artifact_match.group_id = snapshot_group.group_id
   AND snapshot_artifact_match.matched_snapshot_part_count = snapshot_group.snapshot_part_count
  JOIN artifact_record_counts AS artifact_record_count
    ON artifact_record_count.crawl_run_id = snapshot_artifact_match.crawl_run_id
   AND artifact_record_count.group_id = snapshot_artifact_match.group_id
   AND artifact_record_count.artifact_id = snapshot_artifact_match.artifact_id
   AND artifact_record_count.accepted_part_count = snapshot_group.snapshot_part_count
   AND artifact_record_count.accepted_part_id_count = snapshot_group.snapshot_part_count
  JOIN partsouq_http_artifacts AS artifact
    ON artifact.id = artifact_record_count.artifact_id
   AND artifact.crawl_run_id = snapshot_group.crawl_run_id
   AND artifact.capture_kind = 'live_http'
   AND artifact.page_type = 'unit'
   AND artifact.parser_name = 'parse_parts'
   AND artifact.verification_status = 'verified'
   AND artifact.http_status = 200
   AND artifact.challenge_detected = 0
   AND LOWER(artifact.content_type) LIKE 'text/html%'
   AND artifact.malformed_row_count = 0
   AND artifact.verified_at IS NOT NULL
   AND artifact.parsed_record_count = (
     artifact_record_count.parsed_part_count
     + artifact_record_count.skipped_record_count
   )
   AND artifact.accepted_record_count = artifact_record_count.accepted_part_count
   AND artifact.skipped_record_count = artifact_record_count.skipped_record_count
)
SELECT
  candidate_receipt.crawl_run_id,
  candidate_receipt.group_id,
  candidate_receipt.artifact_id,
  candidate_receipt.status,
  candidate_receipt.parsed_part_count,
  candidate_receipt.accepted_part_count,
  candidate_receipt.skipped_record_count
FROM candidate_receipts AS candidate_receipt
WHERE candidate_receipt.candidate_count = 1;

-- 既有資料庫的 v_current 是完整的 evidence gate。MySQL 8 可以用 RENAME
-- TABLE 改名 view，並保留原本的 DEFINER 與 SQL SECURITY；不能重建成新
-- migration 執行帳號的權限語意。新 volume 的 admin.sql 已預先建立 base，
-- 重跑不改寫。
DROP PROCEDURE IF EXISTS prepare_partsouq_036_current_view;
DELIMITER //
CREATE PROCEDURE prepare_partsouq_036_current_view()
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.VIEWS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'v_current_catalog_parts_evidence_base'
  ) THEN
    IF NOT EXISTS (
      SELECT 1 FROM information_schema.VIEWS
      WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'v_current_catalog_parts'
    ) THEN
      SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'migration 036: current bounded view is missing';
    END IF;
    RENAME TABLE v_current_catalog_parts
      TO v_current_catalog_parts_evidence_base;
  END IF;
END//
DELIMITER ;
CALL prepare_partsouq_036_current_view();
DROP PROCEDURE prepare_partsouq_036_current_view;

CREATE OR REPLACE VIEW v_current_catalog_parts AS
WITH snapshot_groups AS (
    SELECT
        bounded_part.crawl_run_id,
        bounded_part.group_id,
        COUNT(*) AS snapshot_part_count
    FROM bounded_parts AS bounded_part
    GROUP BY bounded_part.crawl_run_id, bounded_part.group_id
),
receipt_artifact_counts AS (
    SELECT
        snapshot_group.crawl_run_id,
        snapshot_group.group_id,
        receipt.source_artifact_id,
        SUM(artifact_record.record_type = 'part') AS parsed_part_count,
        SUM(
            artifact_record.record_type = 'part'
            AND artifact_record.accepted = 1
        ) AS accepted_part_count,
        COUNT(DISTINCT CASE
            WHEN artifact_record.record_type = 'part' AND artifact_record.accepted = 1
            THEN artifact_record.part_id
        END) AS accepted_part_id_count,
        SUM(artifact_record.record_type = 'quarantine_part') AS skipped_record_count
    FROM snapshot_groups AS snapshot_group
    STRAIGHT_JOIN bounded_group_receipts AS receipt
      ON receipt.crawl_run_id = snapshot_group.crawl_run_id
     AND receipt.group_id = snapshot_group.group_id
    STRAIGHT_JOIN partsouq_artifact_records AS artifact_record FORCE INDEX (PRIMARY)
      ON artifact_record.artifact_id = receipt.source_artifact_id
     AND artifact_record.crawl_run_id = receipt.crawl_run_id
    GROUP BY
        snapshot_group.crawl_run_id,
        snapshot_group.group_id,
        receipt.source_artifact_id
),
receipt_snapshot_members AS (
    SELECT
        snapshot_group.crawl_run_id,
        snapshot_group.group_id,
        COUNT(DISTINCT bounded_part.part_id) AS accepted_snapshot_part_count
    FROM snapshot_groups AS snapshot_group
    STRAIGHT_JOIN bounded_group_receipts AS receipt
      ON receipt.crawl_run_id = snapshot_group.crawl_run_id
     AND receipt.group_id = snapshot_group.group_id
    STRAIGHT_JOIN partsouq_artifact_records AS artifact_record FORCE INDEX (PRIMARY)
      ON artifact_record.artifact_id = receipt.source_artifact_id
     AND artifact_record.crawl_run_id = receipt.crawl_run_id
     AND artifact_record.record_type = 'part'
     AND artifact_record.accepted = 1
    STRAIGHT_JOIN bounded_parts AS bounded_part
      ON bounded_part.crawl_run_id = snapshot_group.crawl_run_id
     AND bounded_part.group_id = snapshot_group.group_id
     AND bounded_part.part_id = artifact_record.part_id
     AND bounded_part.evidence_record_sha256 = artifact_record.record_sha256
    GROUP BY snapshot_group.crawl_run_id, snapshot_group.group_id
),
receipt_integrity AS (
    SELECT
        snapshot_group.crawl_run_id,
        snapshot_group.group_id,
        snapshot_group.snapshot_part_count,
        CASE
            WHEN receipt.source_artifact_id IS NOT NULL
             AND artifact.crawl_run_id = snapshot_group.crawl_run_id
             AND artifact.capture_kind = 'live_http'
             AND artifact.page_type = 'unit'
             AND artifact.parser_name = 'parse_parts'
             AND artifact.verification_status = 'verified'
             AND artifact.http_status = 200
             AND artifact.challenge_detected = 0
             AND LOWER(artifact.content_type) LIKE 'text/html%'
             AND artifact.malformed_row_count = 0
             AND artifact.verified_at IS NOT NULL
             AND receipt_artifact_counts.parsed_part_count = receipt.parsed_part_count
             AND receipt_artifact_counts.accepted_part_count = receipt.accepted_part_count
             AND receipt_artifact_counts.accepted_part_id_count = receipt.accepted_part_count
             AND receipt_artifact_counts.skipped_record_count = receipt.skipped_record_count
             AND artifact.parsed_record_count = (
                 receipt.parsed_part_count + receipt.skipped_record_count
             )
             AND artifact.accepted_record_count = receipt.accepted_part_count
             AND artifact.skipped_record_count = receipt.skipped_record_count
             AND receipt_snapshot_members.accepted_snapshot_part_count
                 = receipt.accepted_part_count
             AND snapshot_group.snapshot_part_count = receipt.accepted_part_count
             AND (
                 (receipt.status = 'done'
                  AND receipt.accepted_part_count = receipt.parsed_part_count)
                 OR (receipt.status = 'partial'
                     AND receipt.accepted_part_count > 0
                     AND receipt.accepted_part_count < receipt.parsed_part_count)
             )
            THEN 1 ELSE 0
        END AS is_verified
    FROM snapshot_groups AS snapshot_group
    LEFT JOIN bounded_group_receipts AS receipt
      ON receipt.crawl_run_id = snapshot_group.crawl_run_id
     AND receipt.group_id = snapshot_group.group_id
    LEFT JOIN partsouq_http_artifacts AS artifact
      ON artifact.id = receipt.source_artifact_id
     AND artifact.crawl_run_id = receipt.crawl_run_id
    LEFT JOIN receipt_artifact_counts
      ON receipt_artifact_counts.crawl_run_id = snapshot_group.crawl_run_id
     AND receipt_artifact_counts.group_id = snapshot_group.group_id
     AND receipt_artifact_counts.source_artifact_id = receipt.source_artifact_id
    LEFT JOIN receipt_snapshot_members
      ON receipt_snapshot_members.crawl_run_id = snapshot_group.crawl_run_id
     AND receipt_snapshot_members.group_id = snapshot_group.group_id
),
verified_bounded_group_receipts AS (
    SELECT
        receipt_integrity.crawl_run_id,
        COUNT(*) AS snapshot_group_count,
        SUM(receipt_integrity.snapshot_part_count) AS snapshot_part_count,
        SUM(receipt_integrity.is_verified) AS verified_group_count,
        SUM(
            CASE WHEN receipt_integrity.is_verified = 1
            THEN receipt_integrity.snapshot_part_count ELSE 0 END
        ) AS verified_part_count
    FROM receipt_integrity
    GROUP BY receipt_integrity.crawl_run_id
)
SELECT evidence_base.*
FROM v_current_catalog_parts_evidence_base AS evidence_base
JOIN verified_bounded_group_receipts AS receipt_gate
  ON receipt_gate.crawl_run_id = evidence_base.source_crawl_run_id
 AND receipt_gate.snapshot_part_count = 10000
 AND receipt_gate.verified_group_count = receipt_gate.snapshot_group_count
 AND receipt_gate.verified_part_count = receipt_gate.snapshot_part_count;

DROP TRIGGER IF EXISTS prevent_bounded_group_receipt_update;
DELIMITER //
CREATE TRIGGER prevent_bounded_group_receipt_update
BEFORE UPDATE ON bounded_group_receipts
FOR EACH ROW
BEGIN
  IF EXISTS (
    SELECT 1
    FROM crawl_runs
    WHERE crawl_runs.id = OLD.crawl_run_id
      AND crawl_runs.status = 'bounded_success'
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'bounded group receipt is immutable after snapshot publication';
  END IF;
END//
DELIMITER ;

DROP TRIGGER IF EXISTS prevent_bounded_group_receipt_delete;
DELIMITER //
CREATE TRIGGER prevent_bounded_group_receipt_delete
BEFORE DELETE ON bounded_group_receipts
FOR EACH ROW
BEGIN
  IF EXISTS (
    SELECT 1
    FROM crawl_runs
    WHERE crawl_runs.id = OLD.crawl_run_id
      AND crawl_runs.status = 'bounded_success'
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'bounded group receipt is immutable after snapshot publication';
  END IF;
END//
DELIMITER ;

DROP PROCEDURE IF EXISTS assert_partsouq_036_output;
DELIMITER //
CREATE PROCEDURE assert_partsouq_036_output()
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.TABLES
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_TYPE = 'BASE TABLE'
      AND TABLE_NAME = 'bounded_group_receipts'
      AND ENGINE = 'InnoDB'
  ) OR (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'bounded_group_receipts'
  ) <> 8 OR (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'bounded_group_receipts'
      AND (
        (COLUMN_NAME = 'crawl_run_id' AND COLUMN_TYPE = 'int' AND IS_NULLABLE = 'NO')
        OR (COLUMN_NAME = 'group_id' AND COLUMN_TYPE = 'int' AND IS_NULLABLE = 'NO')
        OR (
          COLUMN_NAME = 'source_artifact_id'
          AND COLUMN_TYPE = 'bigint unsigned'
          AND IS_NULLABLE = 'NO'
        )
        OR (COLUMN_NAME = 'status' AND COLUMN_TYPE = 'varchar(16)' AND IS_NULLABLE = 'NO')
        OR (
          COLUMN_NAME = 'parsed_part_count'
          AND COLUMN_TYPE = 'int unsigned'
          AND IS_NULLABLE = 'NO'
        )
        OR (
          COLUMN_NAME = 'accepted_part_count'
          AND COLUMN_TYPE = 'int unsigned'
          AND IS_NULLABLE = 'NO'
        )
        OR (
          COLUMN_NAME = 'skipped_record_count'
          AND COLUMN_TYPE = 'int unsigned'
          AND IS_NULLABLE = 'NO'
        )
        OR (
          COLUMN_NAME = 'recorded_at'
          AND COLUMN_TYPE = 'datetime(6)'
          AND IS_NULLABLE = 'NO'
        )
      )
  ) <> 8 OR (
    SELECT COUNT(*) FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'bounded_group_receipts'
      AND INDEX_NAME = 'PRIMARY'
  ) <> 2 OR (
    SELECT COUNT(*) FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'bounded_group_receipts'
      AND INDEX_NAME = 'PRIMARY'
      AND (
        (SEQ_IN_INDEX = 1 AND COLUMN_NAME = 'crawl_run_id')
        OR (SEQ_IN_INDEX = 2 AND COLUMN_NAME = 'group_id')
      )
  ) <> 2 OR (
    SELECT COUNT(*) FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'bounded_group_receipts'
      AND INDEX_NAME = 'uq_bounded_group_receipt_artifact'
      AND NON_UNIQUE = 0
      AND SEQ_IN_INDEX = 1
      AND COLUMN_NAME = 'source_artifact_id'
  ) <> 1 OR NOT EXISTS (
    SELECT 1 FROM information_schema.REFERENTIAL_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE()
      AND TABLE_NAME = 'bounded_group_receipts'
      AND CONSTRAINT_NAME = 'fk_bounded_group_receipt_run'
      AND DELETE_RULE = 'CASCADE'
  ) OR NOT EXISTS (
    SELECT 1 FROM information_schema.REFERENTIAL_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE()
      AND TABLE_NAME = 'bounded_group_receipts'
      AND CONSTRAINT_NAME = 'fk_bounded_group_receipt_artifact'
  ) OR (
    SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE()
      AND TABLE_NAME = 'bounded_group_receipts'
      AND CONSTRAINT_TYPE = 'CHECK'
      AND CONSTRAINT_NAME IN (
        'chk_bounded_group_receipt_status',
        'chk_bounded_group_receipt_counts'
      )
      AND ENFORCED = 'YES'
  ) <> 2 OR NOT EXISTS (
    SELECT 1
    FROM information_schema.CHECK_CONSTRAINTS AS check_constraint
    JOIN information_schema.TABLE_CONSTRAINTS AS table_constraint
      ON table_constraint.CONSTRAINT_SCHEMA = check_constraint.CONSTRAINT_SCHEMA
     AND table_constraint.CONSTRAINT_NAME = check_constraint.CONSTRAINT_NAME
    WHERE check_constraint.CONSTRAINT_SCHEMA = DATABASE()
      AND table_constraint.TABLE_SCHEMA = DATABASE()
      AND table_constraint.TABLE_NAME = 'bounded_group_receipts'
      AND table_constraint.CONSTRAINT_TYPE = 'CHECK'
      AND table_constraint.ENFORCED = 'YES'
      AND check_constraint.CONSTRAINT_NAME = 'chk_bounded_group_receipt_counts'
      AND LOCATE('accepted_part_count', LOWER(check_constraint.CHECK_CLAUSE)) > 0
      AND LOCATE('parsed_part_count', LOWER(check_constraint.CHECK_CLAUSE)) > 0
      AND LOCATE('status', LOWER(check_constraint.CHECK_CLAUSE)) > 0
      AND LOCATE('done', LOWER(check_constraint.CHECK_CLAUSE)) > 0
      AND LOCATE('partial', LOWER(check_constraint.CHECK_CLAUSE)) > 0
  ) OR NOT EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'bounded_parts'
      AND INDEX_NAME = 'idx_bounded_run_group'
      AND SEQ_IN_INDEX = 1
      AND COLUMN_NAME = 'crawl_run_id'
  ) OR NOT EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'bounded_parts'
      AND INDEX_NAME = 'idx_bounded_run_group'
      AND SEQ_IN_INDEX = 2
      AND COLUMN_NAME = 'group_id'
  ) OR NOT EXISTS (
    SELECT 1 FROM information_schema.VIEWS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'v_current_catalog_parts_evidence_base'
      AND LOCATE('verified_bounded_evidence', LOWER(VIEW_DEFINITION)) > 0
      AND LOCATE('verified_bounded_records', LOWER(VIEW_DEFINITION)) > 0
      AND LOCATE('catalog_desired_bounded_scope', LOWER(VIEW_DEFINITION)) > 0
      AND LOCATE('evidence_record_sha256', LOWER(VIEW_DEFINITION)) > 0
  ) OR NOT EXISTS (
    SELECT 1 FROM information_schema.VIEWS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'v_current_catalog_parts'
      AND LOCATE('v_current_catalog_parts_evidence_base', LOWER(VIEW_DEFINITION)) > 0
      AND LOCATE('bounded_group_receipts', LOWER(VIEW_DEFINITION)) > 0
      AND LOCATE('receipt_integrity', LOWER(VIEW_DEFINITION)) > 0
      AND LOCATE('verified_bounded_group_receipts', LOWER(VIEW_DEFINITION)) > 0
  ) OR NOT EXISTS (
    SELECT 1 FROM information_schema.TRIGGERS
    WHERE TRIGGER_SCHEMA = DATABASE()
      AND TRIGGER_NAME = 'prevent_bounded_group_receipt_update'
      AND EVENT_OBJECT_TABLE = 'bounded_group_receipts'
      AND ACTION_TIMING = 'BEFORE'
      AND EVENT_MANIPULATION = 'UPDATE'
      AND LOCATE('old.crawl_run_id', LOWER(ACTION_STATEMENT)) > 0
      AND LOCATE('status = ''bounded_success''', LOWER(ACTION_STATEMENT)) > 0
      AND LOCATE('signal sqlstate', LOWER(ACTION_STATEMENT)) > 0
  ) OR NOT EXISTS (
    SELECT 1 FROM information_schema.TRIGGERS
    WHERE TRIGGER_SCHEMA = DATABASE()
      AND TRIGGER_NAME = 'prevent_bounded_group_receipt_delete'
      AND EVENT_OBJECT_TABLE = 'bounded_group_receipts'
      AND ACTION_TIMING = 'BEFORE'
      AND EVENT_MANIPULATION = 'DELETE'
      AND LOCATE('old.crawl_run_id', LOWER(ACTION_STATEMENT)) > 0
      AND LOCATE('status = ''bounded_success''', LOWER(ACTION_STATEMENT)) > 0
      AND LOCATE('signal sqlstate', LOWER(ACTION_STATEMENT)) > 0
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 036: bounded group receipt contract is incomplete';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_036_output();
DROP PROCEDURE assert_partsouq_036_output;
