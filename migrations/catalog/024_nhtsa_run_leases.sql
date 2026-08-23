-- Fence every NHTSA publisher with a scheduler link and expiring token lease.
-- New current pointers must identify the completed run that published them.

SET SESSION lock_wait_timeout = 30;
SET SESSION innodb_lock_wait_timeout = 30;

DROP PROCEDURE IF EXISTS upgrade_partsouq_024_nhtsa_run_leases;
DELIMITER //
CREATE PROCEDURE upgrade_partsouq_024_nhtsa_run_leases()
BEGIN
  IF DATABASE() IS NULL OR (
    SELECT COUNT(*) FROM information_schema.TABLES
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'
      AND TABLE_NAME IN (
        'scheduled_job_runs', 'nhtsa_sync_runs', 'nhtsa_current_artifacts'
      )
  ) <> 3 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 024: apply catalog and NHTSA base schemas first';
  END IF;

  IF EXISTS (
    SELECT 1 FROM nhtsa_sync_runs WHERE BINARY status = BINARY 'running'
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 024: NHTSA writers must be stopped first';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'scheduled_job_runs'
      AND COLUMN_NAME = 'parent_scheduled_job_run_id'
  ) THEN
    ALTER TABLE scheduled_job_runs
      ADD COLUMN parent_scheduled_job_run_id BIGINT UNSIGNED NULL AFTER id;
  END IF;

  IF EXISTS (
    SELECT parent_scheduled_job_run_id, job_name
    FROM scheduled_job_runs
    WHERE parent_scheduled_job_run_id IS NOT NULL
    GROUP BY parent_scheduled_job_run_id, job_name
    HAVING COUNT(*) > 1
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 024: duplicate scheduler parent stage';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'scheduled_job_runs'
      AND INDEX_NAME = 'uq_scheduled_job_parent_stage'
  ) THEN
    ALTER TABLE scheduled_job_runs
      ADD UNIQUE KEY uq_scheduled_job_parent_stage (
        parent_scheduled_job_run_id, job_name
      );
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.REFERENTIAL_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'scheduled_job_runs'
      AND CONSTRAINT_NAME = 'fk_scheduled_job_parent'
  ) THEN
    ALTER TABLE scheduled_job_runs
      ADD CONSTRAINT fk_scheduled_job_parent
      FOREIGN KEY (parent_scheduled_job_run_id) REFERENCES scheduled_job_runs(id);
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'nhtsa_sync_runs'
      AND COLUMN_NAME = 'scheduled_job_run_id'
  ) THEN
    ALTER TABLE nhtsa_sync_runs
      ADD COLUMN scheduled_job_run_id BIGINT UNSIGNED NULL AFTER id;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'nhtsa_sync_runs'
      AND COLUMN_NAME = 'lease_slot'
  ) THEN
    ALTER TABLE nhtsa_sync_runs
      ADD COLUMN lease_slot VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NULL
      AFTER source_keys_json;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'nhtsa_sync_runs'
      AND COLUMN_NAME = 'lease_token'
  ) THEN
    ALTER TABLE nhtsa_sync_runs
      ADD COLUMN lease_token CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL
      AFTER lease_slot;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'nhtsa_sync_runs'
      AND COLUMN_NAME = 'heartbeat_at'
  ) THEN
    ALTER TABLE nhtsa_sync_runs
      ADD COLUMN heartbeat_at DATETIME(6) NULL AFTER updated_at;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'nhtsa_sync_runs'
      AND COLUMN_NAME = 'lease_expires_at'
  ) THEN
    ALTER TABLE nhtsa_sync_runs
      ADD COLUMN lease_expires_at DATETIME(6) NULL AFTER heartbeat_at;
  END IF;

  UPDATE nhtsa_sync_runs
  SET ended_at = COALESCE(ended_at, updated_at, started_at),
      lease_slot = NULL, lease_token = NULL, lease_expires_at = NULL
  WHERE BINARY status IN (
    BINARY 'completed', BINARY 'failed', BINARY 'interrupted'
  );

  IF EXISTS (
    SELECT scheduled_job_run_id
    FROM nhtsa_sync_runs
    WHERE scheduled_job_run_id IS NOT NULL
    GROUP BY scheduled_job_run_id
    HAVING COUNT(*) > 1
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 024: duplicate NHTSA scheduler link';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'nhtsa_sync_runs'
      AND INDEX_NAME = 'uq_nhtsa_sync_scheduled_job'
  ) THEN
    ALTER TABLE nhtsa_sync_runs
      ADD UNIQUE KEY uq_nhtsa_sync_scheduled_job (scheduled_job_run_id);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'nhtsa_sync_runs'
      AND INDEX_NAME = 'uq_nhtsa_sync_lease_slot'
  ) THEN
    ALTER TABLE nhtsa_sync_runs
      ADD UNIQUE KEY uq_nhtsa_sync_lease_slot (lease_slot);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'nhtsa_sync_runs'
      AND INDEX_NAME = 'idx_nhtsa_sync_lease_expiry'
  ) THEN
    ALTER TABLE nhtsa_sync_runs
      ADD KEY idx_nhtsa_sync_lease_expiry (status, lease_expires_at, id);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.REFERENTIAL_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'nhtsa_sync_runs'
      AND CONSTRAINT_NAME = 'fk_nhtsa_sync_scheduled_job'
  ) THEN
    ALTER TABLE nhtsa_sync_runs
      ADD CONSTRAINT fk_nhtsa_sync_scheduled_job
      FOREIGN KEY (scheduled_job_run_id) REFERENCES scheduled_job_runs(id);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'nhtsa_sync_runs'
      AND CONSTRAINT_NAME = 'chk_nhtsa_sync_status_lease'
      AND CONSTRAINT_TYPE = 'CHECK'
  ) THEN
    ALTER TABLE nhtsa_sync_runs
      ADD CONSTRAINT chk_nhtsa_sync_status_lease CHECK (
        (
          BINARY status = BINARY 'running'
          AND scheduled_job_run_id IS NOT NULL
          AND lease_slot IS NOT NULL
          AND BINARY lease_slot = BINARY 'writer'
          AND lease_token IS NOT NULL
          AND lease_token REGEXP '^[0-9a-f]{64}$'
          AND heartbeat_at IS NOT NULL
          AND lease_expires_at IS NOT NULL
          AND lease_expires_at > heartbeat_at
          AND ended_at IS NULL
        ) OR (
          BINARY status IN (
            BINARY 'completed', BINARY 'failed', BINARY 'interrupted'
          )
          AND lease_slot IS NULL
          AND lease_token IS NULL
          AND lease_expires_at IS NULL
          AND ended_at IS NOT NULL
        )
      );
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'nhtsa_current_artifacts'
      AND COLUMN_NAME = 'published_run_id'
  ) THEN
    ALTER TABLE nhtsa_current_artifacts
      ADD COLUMN published_run_id BIGINT UNSIGNED NULL AFTER artifact_id;
  END IF;
  IF EXISTS (
    SELECT 1 FROM nhtsa_current_artifacts WHERE published_run_id IS NULL
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 024: current NHTSA artifacts lack run provenance';
  END IF;
  IF EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'nhtsa_current_artifacts'
      AND COLUMN_NAME = 'published_run_id'
      AND (COLUMN_TYPE <> 'bigint unsigned' OR IS_NULLABLE <> 'NO')
  ) THEN
    ALTER TABLE nhtsa_current_artifacts
      MODIFY COLUMN published_run_id BIGINT UNSIGNED NOT NULL AFTER artifact_id;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'nhtsa_current_artifacts'
      AND INDEX_NAME = 'idx_nhtsa_current_published_run'
  ) THEN
    ALTER TABLE nhtsa_current_artifacts
      ADD KEY idx_nhtsa_current_published_run (published_run_id);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.REFERENTIAL_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'nhtsa_current_artifacts'
      AND CONSTRAINT_NAME = 'fk_nhtsa_current_published_run'
  ) THEN
    ALTER TABLE nhtsa_current_artifacts
      ADD CONSTRAINT fk_nhtsa_current_published_run
      FOREIGN KEY (published_run_id) REFERENCES nhtsa_sync_runs(id);
  END IF;

  INSERT IGNORE INTO nhtsa_schema_migrations(version, applied_at)
  VALUES (2, UTC_TIMESTAMP(6));
END//
DELIMITER ;
CALL upgrade_partsouq_024_nhtsa_run_leases();
DROP PROCEDURE upgrade_partsouq_024_nhtsa_run_leases;

DROP PROCEDURE IF EXISTS assert_partsouq_024_output;
DELIMITER //
CREATE PROCEDURE assert_partsouq_024_output()
BEGIN
  IF (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'nhtsa_sync_runs'
      AND COLUMN_NAME IN (
        'scheduled_job_run_id', 'lease_slot', 'lease_token',
        'heartbeat_at', 'lease_expires_at'
      )
  ) <> 5 OR NOT EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'nhtsa_current_artifacts'
      AND COLUMN_NAME = 'published_run_id' AND IS_NULLABLE = 'NO'
  ) OR (
    SELECT COUNT(*) FROM information_schema.REFERENTIAL_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE()
      AND CONSTRAINT_NAME IN (
        'fk_scheduled_job_parent', 'fk_nhtsa_sync_scheduled_job',
        'fk_nhtsa_current_published_run'
      )
  ) <> 3 OR NOT EXISTS (
    SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'nhtsa_sync_runs'
      AND CONSTRAINT_NAME = 'chk_nhtsa_sync_status_lease'
      AND CONSTRAINT_TYPE = 'CHECK' AND ENFORCED = 'YES'
  ) OR NOT EXISTS (
    SELECT 1 FROM nhtsa_schema_migrations WHERE version = 2
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 024: output contract mismatch';
  END IF;
END//
DELIMITER ;
CALL assert_partsouq_024_output();
DROP PROCEDURE assert_partsouq_024_output;
