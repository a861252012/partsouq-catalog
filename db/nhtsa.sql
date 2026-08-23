CREATE TABLE IF NOT EXISTS nhtsa_schema_migrations (
    version INT UNSIGNED PRIMARY KEY,
    applied_at DATETIME(6) NOT NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS nhtsa_sync_runs (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    scheduled_job_run_id BIGINT UNSIGNED NULL,
    run_key VARCHAR(191) NOT NULL,
    scope_name VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    source_keys_json JSON NOT NULL,
    lease_slot VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NULL,
    lease_token CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
    started_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    heartbeat_at DATETIME(6) NULL,
    lease_expires_at DATETIME(6) NULL,
    ended_at DATETIME(6) NULL,
    artifacts_downloaded INT UNSIGNED NOT NULL DEFAULT 0,
    artifacts_reused INT UNSIGNED NOT NULL DEFAULT 0,
    source_rows BIGINT UNSIGNED NOT NULL DEFAULT 0,
    new_versions BIGINT UNSIGNED NOT NULL DEFAULT 0,
    rejected_rows BIGINT UNSIGNED NOT NULL DEFAULT 0,
    error_message TEXT NULL,
    INDEX idx_nhtsa_sync_runs_key (run_key, id),
    INDEX idx_nhtsa_sync_lease_expiry (status, lease_expires_at, id),
    UNIQUE KEY uq_nhtsa_sync_scheduled_job (scheduled_job_run_id),
    UNIQUE KEY uq_nhtsa_sync_lease_slot (lease_slot),
    CONSTRAINT fk_nhtsa_sync_scheduled_job
        FOREIGN KEY (scheduled_job_run_id) REFERENCES scheduled_job_runs(id),
    CONSTRAINT chk_nhtsa_sync_status_lease CHECK (
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
    )
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS nhtsa_source_artifacts (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    dataset_name VARCHAR(64) NOT NULL,
    source_key VARCHAR(128) NOT NULL,
    source_url TEXT NOT NULL,
    http_status SMALLINT UNSIGNED NOT NULL,
    response_headers_json JSON NOT NULL,
    etag VARCHAR(255) NULL,
    last_modified VARCHAR(255) NULL,
    content_type VARCHAR(255) NULL,
    content_length BIGINT UNSIGNED NULL,
    sha256 CHAR(64) NOT NULL,
    stored_path TEXT NOT NULL,
    byte_count BIGINT UNSIGNED NOT NULL,
    parser_name VARCHAR(128) NOT NULL,
    parser_version VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    downloaded_at DATETIME(6) NOT NULL,
    verified_at DATETIME(6) NULL,
    imported_at DATETIME(6) NULL,
    source_rows BIGINT UNSIGNED NOT NULL DEFAULT 0,
    new_versions BIGINT UNSIGNED NOT NULL DEFAULT 0,
    rejected_rows BIGINT UNSIGNED NOT NULL DEFAULT 0,
    error_message TEXT NULL,
    UNIQUE KEY uq_nhtsa_artifact (dataset_name, source_key, sha256, parser_version),
    INDEX idx_nhtsa_artifacts_source (dataset_name, source_key, id)
) ENGINE=InnoDB;

-- vPIC does not publish a complete VIN inventory. This table stores only
-- 17-character VINs supplied by an authorized user and decoded by the
-- official DecodeVinValues endpoint.
CREATE TABLE IF NOT EXISTS nhtsa_vin_decodes (
    vin CHAR(17) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    make_name VARCHAR(255) NOT NULL,
    # vPIC 對部分車型不回 Model（歐系常見）；部分解碼是預期行為。
    model_name VARCHAR(512) NULL,
    model_year SMALLINT UNSIGNED NOT NULL,
    engine_configuration VARCHAR(255) NULL,
    engine_model VARCHAR(255) NULL,
    displacement_l DECIMAL(14,9) NULL,
    trim_name VARCHAR(255) NULL,
    series_name VARCHAR(255) NULL,
    error_code VARCHAR(64) NOT NULL,
    error_text TEXT NULL,
    payload_json JSON NOT NULL,
    source_url TEXT NOT NULL,
    source_artifact_id BIGINT UNSIGNED NOT NULL,
    decoded_at DATETIME(6) NOT NULL,
    INDEX idx_nhtsa_vin_vehicle (make_name, model_name(191), model_year),
    CONSTRAINT fk_nhtsa_vin_artifact
        FOREIGN KEY (source_artifact_id) REFERENCES nhtsa_source_artifacts(id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS nhtsa_artifact_members (
    artifact_id BIGINT UNSIGNED NOT NULL,
    member_name VARCHAR(512) NOT NULL,
    uncompressed_bytes BIGINT UNSIGNED NOT NULL,
    compressed_bytes BIGINT UNSIGNED NOT NULL,
    crc32 BIGINT UNSIGNED NULL,
    field_names_json JSON NOT NULL,
    schema_sha256 CHAR(64) NOT NULL,
    PRIMARY KEY (artifact_id, member_name),
    CONSTRAINT fk_nhtsa_member_artifact
        FOREIGN KEY (artifact_id) REFERENCES nhtsa_source_artifacts(id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS nhtsa_record_versions (
    dataset_name VARCHAR(64) NOT NULL,
    natural_key_sha256 CHAR(64) NOT NULL,
    record_sha256 CHAR(64) NOT NULL,
    natural_key_text TEXT NOT NULL,
    external_id VARCHAR(255) NULL,
    make_name VARCHAR(255) NULL,
    model_name VARCHAR(512) NULL,
    model_year SMALLINT UNSIGNED NULL,
    campaign_number VARCHAR(64) NULL,
    component_name VARCHAR(512) NULL,
    summary_text LONGTEXT NULL,
    payload_json JSON NOT NULL,
    first_observed_at DATETIME(6) NOT NULL,
    PRIMARY KEY (dataset_name, natural_key_sha256, record_sha256),
    INDEX idx_nhtsa_record_external (dataset_name, external_id),
    INDEX idx_nhtsa_record_vehicle (dataset_name, make_name, model_name(191), model_year),
    INDEX idx_nhtsa_record_campaign (dataset_name, campaign_number)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS nhtsa_artifact_records (
    artifact_id BIGINT UNSIGNED NOT NULL,
    dataset_name VARCHAR(64) NOT NULL,
    natural_key_sha256 CHAR(64) NOT NULL,
    record_sha256 CHAR(64) NOT NULL,
    member_name VARCHAR(512) NOT NULL,
    source_line BIGINT UNSIGNED NOT NULL,
    PRIMARY KEY (artifact_id, member_name, source_line),
    INDEX idx_nhtsa_artifact_natural_key (
        artifact_id, dataset_name, natural_key_sha256, record_sha256
    ),
    CONSTRAINT fk_nhtsa_artifact_record_artifact
        FOREIGN KEY (artifact_id) REFERENCES nhtsa_source_artifacts(id) ON DELETE CASCADE,
    CONSTRAINT fk_nhtsa_artifact_record_version
        FOREIGN KEY (dataset_name, natural_key_sha256, record_sha256)
        REFERENCES nhtsa_record_versions(dataset_name, natural_key_sha256, record_sha256)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS nhtsa_rejected_rows (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    artifact_id BIGINT UNSIGNED NOT NULL,
    member_name VARCHAR(512) NOT NULL,
    source_line BIGINT UNSIGNED NOT NULL,
    raw_sha256 CHAR(64) NOT NULL,
    error_type VARCHAR(128) NOT NULL,
    error_message TEXT NOT NULL,
    raw_text LONGTEXT NOT NULL,
    rejected_at DATETIME(6) NOT NULL,
    UNIQUE KEY uq_nhtsa_rejected_line (artifact_id, member_name, source_line),
    CONSTRAINT fk_nhtsa_rejected_artifact
        FOREIGN KEY (artifact_id) REFERENCES nhtsa_source_artifacts(id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS nhtsa_current_artifacts (
    dataset_name VARCHAR(64) NOT NULL,
    source_key VARCHAR(128) NOT NULL,
    artifact_id BIGINT UNSIGNED NOT NULL,
    published_run_id BIGINT UNSIGNED NOT NULL,
    published_at DATETIME(6) NOT NULL,
    PRIMARY KEY (dataset_name, source_key),
    INDEX idx_nhtsa_current_published_run (published_run_id),
    CONSTRAINT fk_nhtsa_current_artifact
        FOREIGN KEY (artifact_id) REFERENCES nhtsa_source_artifacts(id),
    CONSTRAINT fk_nhtsa_current_published_run
        FOREIGN KEY (published_run_id) REFERENCES nhtsa_sync_runs(id)
) ENGINE=InnoDB;

CREATE OR REPLACE VIEW nhtsa_current_records AS
SELECT
    v.dataset_name,
    v.natural_key_sha256,
    v.record_sha256,
    v.natural_key_text,
    v.external_id,
    v.make_name,
    v.model_name,
    v.model_year,
    v.campaign_number,
    v.component_name,
    v.summary_text,
    v.payload_json,
    a.id AS source_artifact_id,
    a.source_key,
    a.source_url,
    a.sha256 AS source_artifact_sha256,
    r.member_name AS source_member,
    r.source_line,
    a.downloaded_at AS source_downloaded_at,
    a.parser_version
FROM nhtsa_current_artifacts AS c
JOIN nhtsa_source_artifacts AS a ON a.id = c.artifact_id
JOIN nhtsa_artifact_records AS r ON r.artifact_id = a.id
JOIN nhtsa_record_versions AS v
  ON v.dataset_name = r.dataset_name
 AND v.natural_key_sha256 = r.natural_key_sha256
 AND v.record_sha256 = r.record_sha256;

INSERT IGNORE INTO nhtsa_schema_migrations(version, applied_at)
VALUES (1, UTC_TIMESTAMP(6)), (2, UTC_TIMESTAMP(6));
