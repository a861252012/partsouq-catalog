-- 017_partsouq_http_evidence.sql
--
-- Persist reproducible PartSouq live HTTP evidence for the formal bounded
-- dataset.  Only deterministic secret-sanitized replay HTML is stored in the
-- zlib CAS; raw response bytes, request/response headers, cookies and raw ssd
-- values are deliberately excluded.  The raw response is represented only by
-- its SHA-256.  Stop catalog writers before applying.
--
-- This migration contains no USE statement and is idempotent.

SET SESSION lock_wait_timeout = 30;
SET SESSION innodb_lock_wait_timeout = 30;

DROP PROCEDURE IF EXISTS upgrade_partsouq_017_http_evidence;
DELIMITER //
CREATE PROCEDURE upgrade_partsouq_017_http_evidence()
BEGIN
  IF DATABASE() IS NULL OR (
    SELECT COUNT(*) FROM information_schema.TABLES
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'
      AND TABLE_NAME IN ('parts', 'crawl_runs', 'scheduled_job_runs')
  ) <> 3 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 017: select database and apply catalog/admin schemas first';
  END IF;
  IF EXISTS (SELECT 1 FROM crawl_runs WHERE status = 'running')
     OR EXISTS (
       SELECT 1 FROM scheduled_job_runs
       WHERE job_name = 'catalog' AND status = 'running'
     ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 017: running catalog jobs exist; stop writers first';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'crawl_runs'
      AND COLUMN_NAME = 'evidence_status'
  ) THEN
    ALTER TABLE crawl_runs
      ADD COLUMN evidence_status VARCHAR(16) NOT NULL DEFAULT 'missing' AFTER error_msg,
      ADD COLUMN evidence_manifest_sha256
        CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL AFTER evidence_status,
      ADD COLUMN evidence_dataset_sha256
        CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL AFTER evidence_manifest_sha256,
      ADD COLUMN evidence_artifact_count INT UNSIGNED NOT NULL DEFAULT 0
        AFTER evidence_dataset_sha256,
      ADD COLUMN evidence_record_count INT UNSIGNED NOT NULL DEFAULT 0
        AFTER evidence_artifact_count,
      ADD COLUMN evidence_original_bytes BIGINT UNSIGNED NOT NULL DEFAULT 0
        AFTER evidence_record_count,
      ADD COLUMN evidence_stored_bytes BIGINT UNSIGNED NOT NULL DEFAULT 0
        AFTER evidence_original_bytes,
      ADD COLUMN evidence_verified_at DATETIME(6) NULL AFTER evidence_stored_bytes,
      ALGORITHM=INPLACE, LOCK=NONE;
  ELSEIF (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'crawl_runs'
      AND (
        (COLUMN_NAME = 'evidence_status' AND COLUMN_TYPE = 'varchar(16)'
          AND IS_NULLABLE = 'NO' AND COLUMN_DEFAULT = 'missing')
        OR (COLUMN_NAME IN ('evidence_manifest_sha256', 'evidence_dataset_sha256')
          AND COLUMN_TYPE = 'char(64)' AND IS_NULLABLE = 'YES'
          AND CHARACTER_SET_NAME = 'ascii' AND COLLATION_NAME = 'ascii_bin')
        OR (COLUMN_NAME IN ('evidence_artifact_count', 'evidence_record_count')
          AND COLUMN_TYPE = 'int unsigned' AND IS_NULLABLE = 'NO'
          AND COLUMN_DEFAULT = '0')
        OR (COLUMN_NAME IN ('evidence_original_bytes', 'evidence_stored_bytes')
          AND COLUMN_TYPE = 'bigint unsigned' AND IS_NULLABLE = 'NO'
          AND COLUMN_DEFAULT = '0')
        OR (COLUMN_NAME = 'evidence_verified_at' AND COLUMN_TYPE = 'datetime(6)'
          AND IS_NULLABLE = 'YES')
      )
  ) <> 8 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 017: crawl_runs evidence column contract mismatch';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'crawl_runs'
      AND INDEX_NAME = 'idx_crawl_run_evidence'
  ) THEN
    ALTER TABLE crawl_runs
      ADD KEY idx_crawl_run_evidence (evidence_status, id),
      ALGORITHM=INPLACE, LOCK=NONE;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'crawl_runs'
      AND CONSTRAINT_NAME = 'chk_crawl_run_evidence_status'
  ) THEN
    ALTER TABLE crawl_runs ADD CONSTRAINT chk_crawl_run_evidence_status CHECK (
      evidence_status IN ('missing', 'collecting', 'verified', 'rejected')
    );
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'crawl_runs'
      AND CONSTRAINT_NAME = 'chk_crawl_run_verified_evidence'
  ) THEN
    ALTER TABLE crawl_runs ADD CONSTRAINT chk_crawl_run_verified_evidence CHECK (
      evidence_status <> 'verified' OR (
        evidence_manifest_sha256 REGEXP '^[0-9a-f]{64}$'
        AND evidence_dataset_sha256 REGEXP '^[0-9a-f]{64}$'
        AND evidence_artifact_count > 0
        AND evidence_record_count > 0
        AND evidence_original_bytes > 0
        AND evidence_stored_bytes > 0
        AND evidence_verified_at IS NOT NULL
      )
    );
  END IF;

  CREATE TABLE IF NOT EXISTS partsouq_response_bodies (
    body_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    compression VARCHAR(16) NOT NULL DEFAULT 'zlib',
    body_blob LONGBLOB NOT NULL,
    original_bytes BIGINT UNSIGNED NOT NULL,
    stored_bytes BIGINT UNSIGNED NOT NULL,
    sanitizer_version VARCHAR(64) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (body_sha256),
    CONSTRAINT chk_partsouq_body_sha256 CHECK (body_sha256 REGEXP '^[0-9a-f]{64}$'),
    CONSTRAINT chk_partsouq_body_compression CHECK (compression = 'zlib'),
    CONSTRAINT chk_partsouq_body_sizes CHECK (
      original_bytes > 0 AND stored_bytes > 0
      AND stored_bytes = OCTET_LENGTH(body_blob)
    )
  ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

  CREATE TABLE IF NOT EXISTS partsouq_http_artifacts (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    crawl_run_id INT NOT NULL,
    scheduled_job_run_id BIGINT UNSIGNED NOT NULL,
    capture_kind VARCHAR(16) NOT NULL,
    page_type VARCHAR(32) NOT NULL,
    public_source_url VARCHAR(1024) NOT NULL,
    source_url_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    raw_body_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    body_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    http_status SMALLINT UNSIGNED NOT NULL,
    content_type VARCHAR(128) NOT NULL,
    challenge_detected TINYINT(1) NOT NULL DEFAULT 0,
    fetched_at DATETIME(6) NOT NULL,
    elapsed_ms INT UNSIGNED NOT NULL,
    attempt SMALLINT UNSIGNED NOT NULL,
    parser_name VARCHAR(128) NOT NULL,
    parser_version VARCHAR(64) NOT NULL,
    parser_context_json JSON NOT NULL,
    parser_context_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    malformed_row_count INT UNSIGNED NOT NULL DEFAULT 0,
    skipped_record_count INT UNSIGNED NOT NULL DEFAULT 0,
    parsed_record_count INT UNSIGNED NOT NULL,
    parsed_records_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    accepted_record_count INT UNSIGNED NOT NULL,
    accepted_records_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    verification_status VARCHAR(16) NOT NULL DEFAULT 'pending',
    verified_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_partsouq_artifact_identity (
      crawl_run_id, scheduled_job_run_id, source_url_sha256, raw_body_sha256, body_sha256,
      parser_name, parser_version, parser_context_sha256
    ),
    UNIQUE KEY uq_partsouq_artifact_run (id, crawl_run_id),
    KEY idx_partsouq_artifact_run_status (
      crawl_run_id, verification_status, capture_kind
    ),
    KEY idx_partsouq_artifact_schedule (scheduled_job_run_id),
    KEY idx_partsouq_artifact_body (body_sha256),
    CONSTRAINT fk_partsouq_artifact_run FOREIGN KEY (crawl_run_id)
      REFERENCES crawl_runs(id),
    CONSTRAINT fk_partsouq_artifact_schedule FOREIGN KEY (scheduled_job_run_id)
      REFERENCES scheduled_job_runs(id),
    CONSTRAINT fk_partsouq_artifact_body FOREIGN KEY (body_sha256)
      REFERENCES partsouq_response_bodies(body_sha256),
    CONSTRAINT chk_partsouq_artifact_capture CHECK (
      capture_kind IN ('live_http', 'fixture')
    ),
    CONSTRAINT chk_partsouq_artifact_page_type CHECK (
      page_type IN ('genuine', 'locate', 'pick', 'vehicle', 'category', 'unit')
    ),
    CONSTRAINT chk_partsouq_artifact_public_url CHECK (
      (
        public_source_url = 'https://partsouq.com/en/catalog/genuine'
        OR public_source_url LIKE 'https://partsouq.com/en/catalog/genuine/%'
      )
      AND LOWER(public_source_url) NOT REGEXP '(^|[?&])ssd='
      AND public_source_url NOT LIKE '%#%'
    ),
    CONSTRAINT chk_partsouq_artifact_hashes CHECK (
      source_url_sha256 REGEXP '^[0-9a-f]{64}$'
      AND raw_body_sha256 REGEXP '^[0-9a-f]{64}$'
      AND body_sha256 REGEXP '^[0-9a-f]{64}$'
      AND parser_context_sha256 REGEXP '^[0-9a-f]{64}$'
      AND parsed_records_sha256 REGEXP '^[0-9a-f]{64}$'
      AND accepted_records_sha256 REGEXP '^[0-9a-f]{64}$'
    ),
    CONSTRAINT chk_partsouq_artifact_context CHECK (
      JSON_VALID(parser_context_json)
      AND LOWER(CAST(parser_context_json AS CHAR)) NOT REGEXP
        '("ssd"[[:space:]]*:|ssd=|cf_clearance|phpsessid|authorization|set-cookie)'
    ),
    CONSTRAINT chk_partsouq_artifact_http CHECK (
      content_type <> '' AND elapsed_ms >= 0 AND attempt > 0
    ),
    CONSTRAINT chk_partsouq_artifact_counts CHECK (
      parsed_record_count > 0
      AND accepted_record_count <= parsed_record_count
    ),
    CONSTRAINT chk_partsouq_artifact_status CHECK (
      verification_status IN ('pending', 'verified', 'superseded', 'rejected')
      AND (verification_status <> 'verified' OR verified_at IS NOT NULL)
    )
  ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

  CREATE TABLE IF NOT EXISTS partsouq_artifact_records (
    artifact_id BIGINT UNSIGNED NOT NULL,
    crawl_run_id INT NOT NULL,
    record_type VARCHAR(32) NOT NULL,
    natural_key_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    parent_natural_key_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
    record_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    accepted TINYINT(1) NOT NULL DEFAULT 0,
    part_id INT NULL,
    PRIMARY KEY (artifact_id, record_type, natural_key_sha256),
    KEY idx_partsouq_record_run_accepted (crawl_run_id, accepted, part_id),
    KEY idx_partsouq_record_part (part_id),
    CONSTRAINT fk_partsouq_record_artifact FOREIGN KEY (artifact_id, crawl_run_id)
      REFERENCES partsouq_http_artifacts(id, crawl_run_id) ON DELETE CASCADE,
    CONSTRAINT fk_partsouq_record_part FOREIGN KEY (part_id) REFERENCES parts(id),
    CONSTRAINT chk_partsouq_record_hashes CHECK (
      natural_key_sha256 REGEXP '^[0-9a-f]{64}$'
      AND (
        parent_natural_key_sha256 IS NULL
        OR parent_natural_key_sha256 REGEXP '^[0-9a-f]{64}$'
      )
      AND record_sha256 REGEXP '^[0-9a-f]{64}$'
    ),
    CONSTRAINT chk_partsouq_record_chain CHECK (
      record_type IN (
        'brand', 'model', 'vehicle', 'category', 'group', 'part', 'quarantine_part'
      )
      AND (
        (record_type = 'brand' AND parent_natural_key_sha256 IS NULL)
        OR (record_type <> 'brand' AND parent_natural_key_sha256 IS NOT NULL)
      )
    ),
    CONSTRAINT chk_partsouq_record_acceptance CHECK (
      (accepted = 1 AND record_type = 'part' AND part_id IS NOT NULL)
      OR (accepted = 0 AND part_id IS NULL)
    )
  ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
END//
DELIMITER ;
CALL upgrade_partsouq_017_http_evidence();
DROP PROCEDURE upgrade_partsouq_017_http_evidence;

DROP PROCEDURE IF EXISTS assert_partsouq_017_output;
DELIMITER //
CREATE PROCEDURE assert_partsouq_017_output()
BEGIN
  IF (
    SELECT COUNT(*) FROM information_schema.TABLES
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'
      AND TABLE_NAME IN (
        'partsouq_response_bodies', 'partsouq_http_artifacts',
        'partsouq_artifact_records'
      )
  ) <> 3
  OR (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'crawl_runs'
      AND COLUMN_NAME IN (
        'evidence_status', 'evidence_manifest_sha256', 'evidence_dataset_sha256',
        'evidence_artifact_count', 'evidence_record_count',
        'evidence_original_bytes', 'evidence_stored_bytes', 'evidence_verified_at'
      )
  ) <> 8
  OR (
    SELECT COUNT(*) FROM information_schema.REFERENTIAL_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND CONSTRAINT_NAME IN (
      'fk_partsouq_artifact_run', 'fk_partsouq_artifact_schedule',
      'fk_partsouq_artifact_body', 'fk_partsouq_record_artifact',
      'fk_partsouq_record_part'
    )
  ) <> 5
  OR (
    SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND CONSTRAINT_NAME IN (
      'chk_crawl_run_evidence_status', 'chk_crawl_run_verified_evidence',
      'chk_partsouq_body_sha256', 'chk_partsouq_body_compression',
      'chk_partsouq_body_sizes', 'chk_partsouq_artifact_capture',
      'chk_partsouq_artifact_page_type', 'chk_partsouq_artifact_public_url',
      'chk_partsouq_artifact_hashes', 'chk_partsouq_artifact_context',
      'chk_partsouq_artifact_http', 'chk_partsouq_artifact_counts',
      'chk_partsouq_artifact_status', 'chk_partsouq_record_hashes',
      'chk_partsouq_record_chain', 'chk_partsouq_record_acceptance'
    )
  ) <> 16 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 017: HTTP evidence schema verification failed';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_017_output();
DROP PROCEDURE assert_partsouq_017_output;
