-- 023_partsouq_http_diagnostics.sql
--
-- Keep HTTP 404 and zero-parse HTML available for secret-safe operations
-- diagnosis without weakening or polluting the verified evidence contract.

SET SESSION lock_wait_timeout = 30;
SET SESSION innodb_lock_wait_timeout = 30;

DROP PROCEDURE IF EXISTS upgrade_partsouq_023_http_diagnostics;
DELIMITER //
CREATE PROCEDURE upgrade_partsouq_023_http_diagnostics()
BEGIN
  IF DATABASE() IS NULL OR (
    SELECT COUNT(*) FROM information_schema.TABLES
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'
      AND TABLE_NAME IN ('crawl_runs', 'scheduled_job_runs', 'groups_t')
  ) <> 3 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 023: apply the catalog evidence schema first';
  END IF;

  CREATE TABLE IF NOT EXISTS partsouq_http_diagnostics (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    crawl_run_id INT NOT NULL,
    scheduled_job_run_id BIGINT UNSIGNED NOT NULL,
    group_id INT NOT NULL,
    reason VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    public_source_url VARCHAR(1024) NOT NULL,
    source_url_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    raw_body_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    body_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    compression VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    body_blob MEDIUMBLOB NOT NULL,
    original_bytes INT UNSIGNED NOT NULL,
    stored_bytes INT UNSIGNED NOT NULL,
    sanitizer_version VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    http_status SMALLINT UNSIGNED NOT NULL,
    content_type VARCHAR(128) NOT NULL,
    fetched_at DATETIME(6) NOT NULL,
    elapsed_ms INT UNSIGNED NOT NULL,
    attempt SMALLINT UNSIGNED NOT NULL,
    parser_name VARCHAR(128) NOT NULL,
    parser_version VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    parser_context_json JSON NOT NULL,
    parser_context_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
      ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_partsouq_diagnostic_group_reason (crawl_run_id, group_id, reason),
    KEY idx_partsouq_diagnostic_run_reason (crawl_run_id, reason, updated_at),
    KEY idx_partsouq_diagnostic_schedule (scheduled_job_run_id),
    KEY idx_partsouq_diagnostic_body (body_sha256),
    CONSTRAINT fk_partsouq_diagnostic_run FOREIGN KEY (crawl_run_id)
      REFERENCES crawl_runs(id),
    CONSTRAINT fk_partsouq_diagnostic_schedule FOREIGN KEY (scheduled_job_run_id)
      REFERENCES scheduled_job_runs(id),
    CONSTRAINT fk_partsouq_diagnostic_group FOREIGN KEY (group_id)
      REFERENCES groups_t(id),
    CONSTRAINT chk_partsouq_diagnostic_reason CHECK (
      (BINARY reason = BINARY 'http_not_found' AND http_status = 404)
      OR (BINARY reason = BINARY 'empty_parse' AND http_status = 200)
    ),
    CONSTRAINT chk_partsouq_diagnostic_public_url CHECK (
      public_source_url LIKE 'https://partsouq.com/en/catalog/genuine/unit?%'
      AND LOWER(public_source_url) NOT REGEXP '(^|[?&])ssd='
      AND public_source_url NOT LIKE '%#%'
    ),
    CONSTRAINT chk_partsouq_diagnostic_hashes CHECK (
      source_url_sha256 REGEXP '^[0-9a-f]{64}$'
      AND raw_body_sha256 REGEXP '^[0-9a-f]{64}$'
      AND body_sha256 REGEXP '^[0-9a-f]{64}$'
      AND parser_context_sha256 REGEXP '^[0-9a-f]{64}$'
    ),
    CONSTRAINT chk_partsouq_diagnostic_context CHECK (
      JSON_VALID(parser_context_json)
      AND LOWER(CAST(parser_context_json AS CHAR)) NOT REGEXP
        '("ssd"[[:space:]]*:|ssd=|cf_clearance|phpsessid|authorization|set-cookie)'
    ),
    CONSTRAINT chk_partsouq_diagnostic_http CHECK (
      content_type <> '' AND elapsed_ms >= 0 AND attempt > 0
    ),
    CONSTRAINT chk_partsouq_diagnostic_storage CHECK (
      BINARY compression = BINARY 'zlib'
      AND original_bytes > 0
      AND stored_bytes > 0
      AND stored_bytes = OCTET_LENGTH(body_blob)
    ),
    CONSTRAINT chk_partsouq_diagnostic_parser CHECK (
      parser_name = 'parse_parts' AND parser_version <> ''
    ),
    CONSTRAINT chk_partsouq_diagnostic_sanitizer CHECK (
      BINARY sanitizer_version = BINARY 'partsouq-html-public-v2'
    )
  ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
END//
DELIMITER ;
CALL upgrade_partsouq_023_http_diagnostics();
DROP PROCEDURE upgrade_partsouq_023_http_diagnostics;

DROP PROCEDURE IF EXISTS assert_partsouq_023_output;
DELIMITER //
CREATE PROCEDURE assert_partsouq_023_output()
BEGIN
  IF (
    SELECT COUNT(*) FROM information_schema.TABLES
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'partsouq_http_diagnostics'
      AND TABLE_TYPE = 'BASE TABLE'
  ) <> 1 OR (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'partsouq_http_diagnostics'
  ) <> 25 OR (
    SELECT COUNT(*) FROM information_schema.REFERENTIAL_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE()
      AND TABLE_NAME = 'partsouq_http_diagnostics'
      AND CONSTRAINT_NAME IN (
        'fk_partsouq_diagnostic_run', 'fk_partsouq_diagnostic_schedule',
        'fk_partsouq_diagnostic_group'
      )
  ) <> 3 OR (
    SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE()
      AND TABLE_NAME = 'partsouq_http_diagnostics'
      AND CONSTRAINT_TYPE = 'CHECK'
      AND CONSTRAINT_NAME IN (
        'chk_partsouq_diagnostic_reason', 'chk_partsouq_diagnostic_public_url',
        'chk_partsouq_diagnostic_hashes', 'chk_partsouq_diagnostic_context',
        'chk_partsouq_diagnostic_http', 'chk_partsouq_diagnostic_storage',
        'chk_partsouq_diagnostic_parser', 'chk_partsouq_diagnostic_sanitizer'
      )
  ) <> 8 OR NOT EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'partsouq_http_diagnostics'
      AND INDEX_NAME = 'uq_partsouq_diagnostic_group_reason' AND NON_UNIQUE = 0
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 023: output contract mismatch';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_023_output();
DROP PROCEDURE assert_partsouq_023_output;
