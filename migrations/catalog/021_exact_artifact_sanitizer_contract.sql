-- 021_exact_artifact_sanitizer_contract.sql
--
-- Make sanitizer-version comparisons case-sensitive and prevent a verified
-- artifact from drifting away from the currently supported evidence contract.

SET SESSION lock_wait_timeout = 30;
SET SESSION innodb_lock_wait_timeout = 30;

DROP PROCEDURE IF EXISTS upgrade_partsouq_021_exact_artifact_sanitizer_contract;
DELIMITER //
CREATE PROCEDURE upgrade_partsouq_021_exact_artifact_sanitizer_contract()
BEGIN
  IF DATABASE() IS NULL OR NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'partsouq_http_artifacts'
      AND COLUMN_NAME = 'sanitizer_version'
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 021: artifact sanitizer schema is incomplete';
  END IF;
  IF EXISTS (SELECT 1 FROM crawl_runs WHERE status = 'running')
     OR EXISTS (
       SELECT 1 FROM scheduled_job_runs
       WHERE job_name = 'catalog' AND status = 'running'
     ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 021: running catalog jobs exist; stop writers first';
  END IF;

  UPDATE partsouq_http_artifacts
  SET verification_status = 'rejected', verified_at = NULL
  WHERE BINARY sanitizer_version <> BINARY 'partsouq-html-public-v2'
    AND verification_status = 'verified';

  UPDATE crawl_runs AS run
  JOIN (
    SELECT DISTINCT crawl_run_id
    FROM partsouq_http_artifacts
    WHERE BINARY sanitizer_version <> BINARY 'partsouq-html-public-v2'
  ) AS incompatible ON incompatible.crawl_run_id = run.id
  SET run.evidence_status = 'rejected',
      run.evidence_manifest_sha256 = NULL,
      run.evidence_dataset_sha256 = NULL,
      run.evidence_artifact_count = 0,
      run.evidence_record_count = 0,
      run.evidence_original_bytes = 0,
      run.evidence_stored_bytes = 0,
      run.evidence_verified_at = NULL
  WHERE run.evidence_status IN ('collecting', 'verified');

  IF EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'partsouq_http_artifacts'
      AND COLUMN_NAME = 'sanitizer_version'
      AND (
        COLUMN_TYPE <> 'varchar(64)' OR IS_NULLABLE <> 'NO'
        OR CHARACTER_SET_NAME <> 'ascii' OR COLLATION_NAME <> 'ascii_bin'
      )
  ) THEN
    ALTER TABLE partsouq_http_artifacts
      MODIFY COLUMN sanitizer_version
        VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL
        AFTER body_sha256;
  END IF;

  IF EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'partsouq_http_artifacts'
      AND CONSTRAINT_NAME = 'chk_partsouq_artifact_verified_sanitizer'
  ) THEN
    ALTER TABLE partsouq_http_artifacts
      DROP CHECK chk_partsouq_artifact_verified_sanitizer;
  END IF;
  ALTER TABLE partsouq_http_artifacts
    ADD CONSTRAINT chk_partsouq_artifact_verified_sanitizer CHECK (
      verification_status <> 'verified'
      OR BINARY sanitizer_version = BINARY 'partsouq-html-public-v2'
    );
END//
DELIMITER ;
CALL upgrade_partsouq_021_exact_artifact_sanitizer_contract();
DROP PROCEDURE upgrade_partsouq_021_exact_artifact_sanitizer_contract;

DROP PROCEDURE IF EXISTS assert_partsouq_021_output;
DELIMITER //
CREATE PROCEDURE assert_partsouq_021_output()
BEGIN
  IF (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'partsouq_http_artifacts'
      AND COLUMN_NAME = 'sanitizer_version'
      AND COLUMN_TYPE = 'varchar(64)' AND IS_NULLABLE = 'NO'
      AND CHARACTER_SET_NAME = 'ascii' AND COLLATION_NAME = 'ascii_bin'
  ) <> 1 OR NOT EXISTS (
    SELECT 1 FROM information_schema.CHECK_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE()
      AND CONSTRAINT_NAME = 'chk_partsouq_artifact_verified_sanitizer'
      AND LOCATE('verification_status', LOWER(CHECK_CLAUSE)) > 0
      AND LOCATE('sanitizer_version', LOWER(CHECK_CLAUSE)) > 0
      AND LOCATE('partsouq-html-public-v2', LOWER(CHECK_CLAUSE)) > 0
      AND LOCATE('charset binary', LOWER(CHECK_CLAUSE)) > 0
  ) OR EXISTS (
    SELECT 1 FROM partsouq_http_artifacts
    WHERE BINARY sanitizer_version <> BINARY 'partsouq-html-public-v2'
      AND verification_status = 'verified'
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 021: exact sanitizer contract verification failed';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_021_output();
DROP PROCEDURE assert_partsouq_021_output;
