-- 020_artifact_sanitizer_version.sql
--
-- Record the sanitizer contract on each HTTP artifact. The CAS body remains
-- keyed only by bytes, so identical sanitized bytes may be shared across
-- sanitizer releases without losing the version used for a specific capture.

SET SESSION lock_wait_timeout = 30;
SET SESSION innodb_lock_wait_timeout = 30;

DROP PROCEDURE IF EXISTS upgrade_partsouq_020_artifact_sanitizer_version;
DELIMITER //
CREATE PROCEDURE upgrade_partsouq_020_artifact_sanitizer_version()
BEGIN
  IF DATABASE() IS NULL OR (
    SELECT COUNT(*) FROM information_schema.TABLES
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'
      AND TABLE_NAME IN (
        'crawl_runs', 'scheduled_job_runs',
        'partsouq_response_bodies', 'partsouq_http_artifacts'
      )
  ) <> 4 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 020: HTTP evidence schema is incomplete';
  END IF;
  IF EXISTS (SELECT 1 FROM crawl_runs WHERE status = 'running')
     OR EXISTS (
       SELECT 1 FROM scheduled_job_runs
       WHERE job_name = 'catalog' AND status = 'running'
     ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 020: running catalog jobs exist; stop writers first';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'partsouq_http_artifacts'
      AND COLUMN_NAME = 'sanitizer_version'
  ) THEN
    ALTER TABLE partsouq_http_artifacts
      ADD COLUMN sanitizer_version VARCHAR(64) NULL AFTER body_sha256,
      ALGORITHM=INPLACE, LOCK=NONE;
  END IF;

  IF (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'partsouq_http_artifacts'
      AND COLUMN_NAME = 'sanitizer_version'
      AND COLUMN_TYPE = 'varchar(64)'
  ) <> 1 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 020: artifact sanitizer column contract mismatch';
  END IF;

  UPDATE partsouq_http_artifacts AS artifact
  JOIN partsouq_response_bodies AS body ON body.body_sha256 = artifact.body_sha256
  SET artifact.sanitizer_version = body.sanitizer_version
  WHERE artifact.sanitizer_version IS NULL OR artifact.sanitizer_version = '';

  IF EXISTS (
    SELECT 1 FROM partsouq_http_artifacts
    WHERE sanitizer_version IS NULL OR sanitizer_version = ''
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 020: artifact sanitizer backfill is incomplete';
  END IF;

  IF EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'partsouq_http_artifacts'
      AND COLUMN_NAME = 'sanitizer_version' AND IS_NULLABLE = 'YES'
  ) THEN
    ALTER TABLE partsouq_http_artifacts
      MODIFY COLUMN sanitizer_version VARCHAR(64) NOT NULL AFTER body_sha256;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'partsouq_http_artifacts'
      AND CONSTRAINT_NAME = 'chk_partsouq_artifact_sanitizer'
  ) THEN
    ALTER TABLE partsouq_http_artifacts
      ADD CONSTRAINT chk_partsouq_artifact_sanitizer CHECK (sanitizer_version <> '');
  END IF;

  UPDATE partsouq_http_artifacts
  SET verification_status = 'rejected', verified_at = NULL
  WHERE sanitizer_version <> 'partsouq-html-public-v2'
    AND verification_status = 'verified';

  UPDATE crawl_runs AS run
  JOIN (
    SELECT DISTINCT crawl_run_id
    FROM partsouq_http_artifacts
    WHERE sanitizer_version <> 'partsouq-html-public-v2'
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
END//
DELIMITER ;
CALL upgrade_partsouq_020_artifact_sanitizer_version();
DROP PROCEDURE upgrade_partsouq_020_artifact_sanitizer_version;

DROP PROCEDURE IF EXISTS assert_partsouq_020_output;
DELIMITER //
CREATE PROCEDURE assert_partsouq_020_output()
BEGIN
  IF (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'partsouq_http_artifacts'
      AND COLUMN_NAME = 'sanitizer_version'
      AND COLUMN_TYPE = 'varchar(64)' AND IS_NULLABLE = 'NO'
  ) <> 1 OR NOT EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'partsouq_http_artifacts'
      AND CONSTRAINT_NAME = 'chk_partsouq_artifact_sanitizer'
  ) OR EXISTS (
    SELECT 1 FROM partsouq_http_artifacts
    WHERE sanitizer_version IS NULL OR sanitizer_version = ''
  ) OR EXISTS (
    SELECT 1 FROM partsouq_http_artifacts
    WHERE sanitizer_version <> 'partsouq-html-public-v2'
      AND verification_status = 'verified'
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 020: artifact sanitizer verification failed';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_020_output();
DROP PROCEDURE assert_partsouq_020_output;
