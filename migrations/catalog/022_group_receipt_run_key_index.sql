-- 022_group_receipt_run_key_index.sql
--
-- Resume compatibility checks look up terminal group receipts by run_key.
-- Keep that safety gate indexed as the catalog grows, permanently reject
-- unfinished bounded runs created with non-terminal or non-canonical receipts,
-- and make the run evidence status contract byte-exact.

SET SESSION lock_wait_timeout = 30;
SET SESSION innodb_lock_wait_timeout = 30;

DROP PROCEDURE IF EXISTS upgrade_partsouq_022_group_receipt_run_key_index;
DELIMITER //
CREATE PROCEDURE upgrade_partsouq_022_group_receipt_run_key_index()
BEGIN
  IF DATABASE() IS NULL OR (
    SELECT COUNT(*) FROM information_schema.TABLES
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME IN ('groups_t', 'crawl_runs', 'partsouq_http_artifacts')
      AND TABLE_TYPE = 'BASE TABLE'
  ) <> 3 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 022: select database and apply catalog schema first';
  END IF;

  IF EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'groups_t'
      AND INDEX_NAME = 'idx_group_fetched_run_key'
  ) AND NOT (
    SELECT COUNT(*) = 1
      AND MIN(NON_UNIQUE) = 1 AND MAX(NON_UNIQUE) = 1
      AND MIN(COLUMN_NAME) = 'fetched_run_key'
      AND COALESCE(SUM(EXPRESSION IS NOT NULL), 0) = 0
      AND COALESCE(SUM(SUB_PART IS NOT NULL), 0) = 0
      AND MIN(COLLATION) = 'A' AND MAX(COLLATION) = 'A'
      AND MIN(INDEX_TYPE) = 'BTREE' AND MAX(INDEX_TYPE) = 'BTREE'
      AND MIN(IS_VISIBLE) = 'YES' AND MAX(IS_VISIBLE) = 'YES'
    FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'groups_t'
      AND INDEX_NAME = 'idx_group_fetched_run_key'
  ) THEN
    ALTER TABLE groups_t DROP KEY idx_group_fetched_run_key,
      ALGORITHM=INPLACE, LOCK=NONE;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'groups_t'
      AND INDEX_NAME = 'idx_group_fetched_run_key'
  ) THEN
    ALTER TABLE groups_t ADD KEY idx_group_fetched_run_key (fetched_run_key),
      ALGORITHM=INPLACE, LOCK=NONE;
  END IF;

  UPDATE crawl_runs
  SET evidence_status = 'rejected',
      evidence_manifest_sha256 = NULL,
      evidence_dataset_sha256 = NULL,
      evidence_artifact_count = 0,
      evidence_record_count = 0,
      evidence_original_bytes = 0,
      evidence_stored_bytes = 0,
      evidence_verified_at = NULL
  WHERE BINARY evidence_status = BINARY 'rejected'
     OR NOT (
       BINARY evidence_status = BINARY 'missing'
       OR BINARY evidence_status = BINARY 'collecting'
       OR BINARY evidence_status = BINARY 'verified'
     );

  IF EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'crawl_runs'
      AND CONSTRAINT_NAME = 'chk_crawl_run_verified_evidence'
  ) THEN
    ALTER TABLE crawl_runs DROP CHECK chk_crawl_run_verified_evidence;
  END IF;
  IF EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'crawl_runs'
      AND CONSTRAINT_NAME = 'chk_crawl_run_evidence_status'
  ) THEN
    ALTER TABLE crawl_runs DROP CHECK chk_crawl_run_evidence_status;
  END IF;

  IF EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'crawl_runs'
      AND COLUMN_NAME = 'evidence_status'
      AND (
        COLUMN_TYPE <> 'varchar(16)' OR IS_NULLABLE <> 'NO'
        OR COLUMN_DEFAULT <> 'missing'
        OR CHARACTER_SET_NAME <> 'ascii' OR COLLATION_NAME <> 'ascii_bin'
      )
  ) THEN
    ALTER TABLE crawl_runs
      MODIFY COLUMN evidence_status
        VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT 'missing'
        AFTER error_msg;
  END IF;

  ALTER TABLE crawl_runs ADD CONSTRAINT chk_crawl_run_evidence_status CHECK (
    BINARY evidence_status = BINARY 'missing'
    OR BINARY evidence_status = BINARY 'collecting'
    OR BINARY evidence_status = BINARY 'verified'
    OR BINARY evidence_status = BINARY 'rejected'
  );
  ALTER TABLE crawl_runs ADD CONSTRAINT chk_crawl_run_verified_evidence CHECK (
    BINARY evidence_status <> BINARY 'verified' OR (
      evidence_manifest_sha256 REGEXP '^[0-9a-f]{64}$'
      AND evidence_dataset_sha256 REGEXP '^[0-9a-f]{64}$'
      AND evidence_artifact_count > 0
      AND evidence_record_count > 0
      AND evidence_original_bytes > 0
      AND evidence_stored_bytes > 0
      AND evidence_verified_at IS NOT NULL
    )
  );

  UPDATE partsouq_http_artifacts
  SET verification_status = 'rejected', verified_at = NULL
  WHERE BINARY verification_status = BINARY 'rejected'
     OR NOT (
       BINARY verification_status = BINARY 'pending'
       OR BINARY verification_status = BINARY 'verified'
       OR BINARY verification_status = BINARY 'superseded'
     );

  IF EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'partsouq_http_artifacts'
      AND CONSTRAINT_NAME = 'chk_partsouq_artifact_verified_sanitizer'
  ) THEN
    ALTER TABLE partsouq_http_artifacts
      DROP CHECK chk_partsouq_artifact_verified_sanitizer;
  END IF;
  IF EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'partsouq_http_artifacts'
      AND CONSTRAINT_NAME = 'chk_partsouq_artifact_status'
  ) THEN
    ALTER TABLE partsouq_http_artifacts DROP CHECK chk_partsouq_artifact_status;
  END IF;

  IF EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'partsouq_http_artifacts'
      AND COLUMN_NAME = 'verification_status'
      AND (
        COLUMN_TYPE <> 'varchar(16)' OR IS_NULLABLE <> 'NO'
        OR COLUMN_DEFAULT <> 'pending'
        OR CHARACTER_SET_NAME <> 'ascii' OR COLLATION_NAME <> 'ascii_bin'
      )
  ) THEN
    ALTER TABLE partsouq_http_artifacts
      MODIFY COLUMN verification_status
        VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT 'pending'
        AFTER accepted_records_sha256;
  END IF;

  ALTER TABLE partsouq_http_artifacts
    ADD CONSTRAINT chk_partsouq_artifact_verified_sanitizer CHECK (
      BINARY verification_status <> BINARY 'verified'
      OR BINARY sanitizer_version = BINARY 'partsouq-html-public-v2'
    );
  ALTER TABLE partsouq_http_artifacts
    ADD CONSTRAINT chk_partsouq_artifact_status CHECK (
      (
        BINARY verification_status = BINARY 'pending'
        OR BINARY verification_status = BINARY 'verified'
        OR BINARY verification_status = BINARY 'superseded'
        OR BINARY verification_status = BINARY 'rejected'
      )
      AND (
        BINARY verification_status <> BINARY 'verified'
        OR verified_at IS NOT NULL
      )
    );

  UPDATE crawl_runs AS run
  JOIN (
    SELECT DISTINCT crawl_run_id
    FROM partsouq_http_artifacts
    WHERE BINARY sanitizer_version <> BINARY 'partsouq-html-public-v2'
       OR NOT (
         BINARY verification_status = BINARY 'verified'
         OR BINARY verification_status = BINARY 'superseded'
       )
  ) AS incompatible_artifact ON incompatible_artifact.crawl_run_id = run.id
  SET run.evidence_status = 'rejected',
      run.evidence_manifest_sha256 = NULL,
      run.evidence_dataset_sha256 = NULL,
      run.evidence_artifact_count = 0,
      run.evidence_record_count = 0,
      run.evidence_original_bytes = 0,
      run.evidence_stored_bytes = 0,
      run.evidence_verified_at = NULL
  WHERE run.dataset_kind = 'bounded'
    AND run.status IN ('running', 'error', 'interrupted');

  UPDATE crawl_runs AS run
  JOIN (
    SELECT DISTINCT fetched_run_key
    FROM groups_t
    WHERE fetched_run_key IS NOT NULL
      AND (
        fetched_status IS NULL OR NOT (
          BINARY fetched_status = BINARY 'done'
          OR BINARY fetched_status = BINARY 'not_found'
        )
        OR NULLIF(TRIM(url), '') IS NULL
        OR NOT (
          REGEXP_LIKE(
            url,
            '^https://partsouq[.]com/en/catalog/genuine/unit[?]((c|model|vid|cid|cname|uid|q)=[^&#]*&)*(c|model|vid|cid|cname|uid|q)=[^&#]*$',
            'c'
          )
          AND REGEXP_LIKE(url, '(^|[?&])uid=[^&#]+(&|$)', 'c')
          AND REGEXP_LIKE(uid, '^[A-Za-z0-9._~-]+$', 'c')
          AND REGEXP_INSTR(url, '(^|[?&])uid=', 1, 2, 0, 'c') = 0
          AND BINARY SUBSTRING_INDEX(
            REGEXP_SUBSTR(url, '(^|[?&])uid=[^&#]+', 1, 1, 'c'),
            'uid=', -1
          ) = BINARY uid
        )
      )
  ) AS incompatible ON incompatible.fetched_run_key = run.run_key
  SET run.evidence_status = 'rejected',
      run.evidence_manifest_sha256 = NULL,
      run.evidence_dataset_sha256 = NULL,
      run.evidence_artifact_count = 0,
      run.evidence_record_count = 0,
      run.evidence_original_bytes = 0,
      run.evidence_stored_bytes = 0,
      run.evidence_verified_at = NULL
  WHERE run.dataset_kind = 'bounded'
    AND run.status IN ('running', 'error', 'interrupted');
END//
DELIMITER ;
CALL upgrade_partsouq_022_group_receipt_run_key_index();
DROP PROCEDURE upgrade_partsouq_022_group_receipt_run_key_index;

DROP PROCEDURE IF EXISTS assert_partsouq_022_output;
DELIMITER //
CREATE PROCEDURE assert_partsouq_022_output()
BEGIN
  IF (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'crawl_runs'
      AND COLUMN_NAME = 'evidence_status'
      AND COLUMN_TYPE = 'varchar(16)' AND IS_NULLABLE = 'NO'
      AND COLUMN_DEFAULT = 'missing'
      AND CHARACTER_SET_NAME = 'ascii' AND COLLATION_NAME = 'ascii_bin'
  ) <> 1 OR NOT EXISTS (
    SELECT 1 FROM information_schema.CHECK_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE()
      AND CONSTRAINT_NAME = 'chk_crawl_run_evidence_status'
      AND LOCATE('charset binary', LOWER(CHECK_CLAUSE)) > 0
      AND LOCATE('missing', LOWER(CHECK_CLAUSE)) > 0
      AND LOCATE('collecting', LOWER(CHECK_CLAUSE)) > 0
      AND LOCATE('verified', LOWER(CHECK_CLAUSE)) > 0
      AND LOCATE('rejected', LOWER(CHECK_CLAUSE)) > 0
  ) OR NOT EXISTS (
    SELECT 1 FROM information_schema.CHECK_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE()
      AND CONSTRAINT_NAME = 'chk_crawl_run_verified_evidence'
      AND LOCATE('charset binary', LOWER(CHECK_CLAUSE)) > 0
      AND LOCATE('evidence_manifest_sha256', LOWER(CHECK_CLAUSE)) > 0
      AND LOCATE('evidence_dataset_sha256', LOWER(CHECK_CLAUSE)) > 0
  ) OR (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'partsouq_http_artifacts'
      AND COLUMN_NAME = 'verification_status'
      AND COLUMN_TYPE = 'varchar(16)' AND IS_NULLABLE = 'NO'
      AND COLUMN_DEFAULT = 'pending'
      AND CHARACTER_SET_NAME = 'ascii' AND COLLATION_NAME = 'ascii_bin'
  ) <> 1 OR NOT EXISTS (
    SELECT 1 FROM information_schema.CHECK_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE()
      AND CONSTRAINT_NAME = 'chk_partsouq_artifact_status'
      AND LOCATE('charset binary', LOWER(CHECK_CLAUSE)) > 0
      AND LOCATE('pending', LOWER(CHECK_CLAUSE)) > 0
      AND LOCATE('verified', LOWER(CHECK_CLAUSE)) > 0
      AND LOCATE('superseded', LOWER(CHECK_CLAUSE)) > 0
      AND LOCATE('rejected', LOWER(CHECK_CLAUSE)) > 0
  ) OR NOT EXISTS (
    SELECT 1 FROM information_schema.CHECK_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE()
      AND CONSTRAINT_NAME = 'chk_partsouq_artifact_verified_sanitizer'
      AND LOCATE('charset binary', LOWER(CHECK_CLAUSE)) > 0
      AND LOCATE('sanitizer_version', LOWER(CHECK_CLAUSE)) > 0
  ) OR NOT (
    SELECT COUNT(*) = 1
      AND MIN(NON_UNIQUE) = 1 AND MAX(NON_UNIQUE) = 1
      AND MIN(COLUMN_NAME) = 'fetched_run_key'
      AND COALESCE(SUM(EXPRESSION IS NOT NULL), 0) = 0
      AND COALESCE(SUM(SUB_PART IS NOT NULL), 0) = 0
      AND MIN(COLLATION) = 'A' AND MAX(COLLATION) = 'A'
      AND MIN(INDEX_TYPE) = 'BTREE' AND MAX(INDEX_TYPE) = 'BTREE'
      AND MIN(IS_VISIBLE) = 'YES' AND MAX(IS_VISIBLE) = 'YES'
    FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'groups_t'
      AND INDEX_NAME = 'idx_group_fetched_run_key'
  ) OR EXISTS (
    SELECT 1
    FROM crawl_runs AS run
    JOIN partsouq_http_artifacts AS artifact ON artifact.crawl_run_id = run.id
    WHERE run.dataset_kind = 'bounded'
      AND run.status IN ('running', 'error', 'interrupted')
      AND BINARY run.evidence_status <> BINARY 'rejected'
      AND (
        BINARY artifact.sanitizer_version <> BINARY 'partsouq-html-public-v2'
        OR NOT (
          BINARY artifact.verification_status = BINARY 'verified'
          OR BINARY artifact.verification_status = BINARY 'superseded'
        )
      )
  ) OR EXISTS (
    SELECT 1
    FROM crawl_runs AS run
    JOIN groups_t ON groups_t.fetched_run_key = run.run_key
    WHERE run.dataset_kind = 'bounded'
      AND run.status IN ('running', 'error', 'interrupted')
      AND BINARY run.evidence_status <> BINARY 'rejected'
      AND (
        groups_t.fetched_status IS NULL
        OR NOT (
          BINARY groups_t.fetched_status = BINARY 'done'
          OR BINARY groups_t.fetched_status = BINARY 'not_found'
        )
        OR NULLIF(TRIM(groups_t.url), '') IS NULL
        OR NOT (
          REGEXP_LIKE(
            groups_t.url,
            '^https://partsouq[.]com/en/catalog/genuine/unit[?]((c|model|vid|cid|cname|uid|q)=[^&#]*&)*(c|model|vid|cid|cname|uid|q)=[^&#]*$',
            'c'
          )
          AND REGEXP_LIKE(groups_t.url, '(^|[?&])uid=[^&#]+(&|$)', 'c')
          AND REGEXP_LIKE(groups_t.uid, '^[A-Za-z0-9._~-]+$', 'c')
          AND REGEXP_INSTR(
            groups_t.url, '(^|[?&])uid=', 1, 2, 0, 'c'
          ) = 0
          AND BINARY SUBSTRING_INDEX(
            REGEXP_SUBSTR(
              groups_t.url, '(^|[?&])uid=[^&#]+', 1, 1, 'c'
            ),
            'uid=', -1
          ) = BINARY groups_t.uid
        )
      )
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 022: group receipt index verification failed';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_022_output();
DROP PROCEDURE assert_partsouq_022_output;
