-- 台灣 MOENV VNCS 汽油車/柴油車車輛主檔與極簡同步 run ledger。
-- 與 migrations/catalog/025 + 038 保持一致。

-- 唯一鍵為「條件唯一」：只有 17 碼 VIN 參與 uq_vncs_vin（generated column
-- 把非 VIN 列映射成 NULL，MySQL 多 NULL 不衝突）。非 VIN 列由 038 的
-- 完整內容指紋 uq_vncs_source_identity 保障重跑冪等：一碼多車的列在
-- model_raw／approval_date 等欄位上必然不同，指紋互異，絕不靜默丟資料。
CREATE TABLE IF NOT EXISTS tw_vncs_vehicles (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    vehicle_kind ENUM('汽油車', '柴油車') NOT NULL,
    make VARCHAR(128) NOT NULL,
    model_raw VARCHAR(512) NOT NULL,
    displacement_cc SMALLINT UNSIGNED NULL,
    body_rule VARCHAR(64) NULL,
    transmission VARCHAR(32) NULL,
    doors TINYINT UNSIGNED NULL,
    style VARCHAR(128) NULL,
    model_year SMALLINT UNSIGNED NOT NULL,
    model_group_code VARCHAR(64) NOT NULL DEFAULT '',
    body_or_engine_code VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    is_vin BOOLEAN NOT NULL,
    vin_code VARCHAR(32) GENERATED ALWAYS AS (
        CASE WHEN is_vin THEN body_or_engine_code END
    ) STORED,
    period VARCHAR(32) NULL,
    approval_date VARCHAR(32) NULL,
    check_code VARCHAR(64) NULL,
    source_url TEXT NOT NULL,
    payload_json JSON NOT NULL,
    first_seen_at DATETIME(6) NOT NULL,
    last_synced_at DATETIME(6) NOT NULL,
    source_identity_sha256 CHAR(64) GENERATED ALWAYS AS (
        SHA2(CONVERT(JSON_ARRAY(
            vehicle_kind, make, model_raw, displacement_cc, body_rule, transmission,
            doors, style, model_year, model_group_code, body_or_engine_code,
            is_vin, period, approval_date, check_code
        ) USING utf8mb4), 256)
    ) STORED NOT NULL,
    UNIQUE KEY uq_vncs_vin (vin_code),
    UNIQUE KEY uq_vncs_source_identity (source_identity_sha256),
    KEY idx_vncs_code (body_or_engine_code, model_year),
    KEY idx_vncs_vehicle (make, model_raw(191), model_year),
    CONSTRAINT chk_vncs_vehicle_kind CHECK (vehicle_kind IN ('汽油車', '柴油車'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS vncs_sync_runs (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    scheduled_job_run_id BIGINT UNSIGNED NULL,
    run_key VARCHAR(191) NOT NULL,
    status ENUM('running', 'completed', 'failed', 'interrupted') NOT NULL,
    rows_seen BIGINT UNSIGNED NOT NULL DEFAULT 0,
    rows_upserted BIGINT UNSIGNED NOT NULL DEFAULT 0,
    malformed_rows BIGINT UNSIGNED NOT NULL DEFAULT 0,
    started_at DATETIME(6) NOT NULL,
    ended_at DATETIME(6) NULL,
    error_message TEXT NULL,
    INDEX idx_vncs_sync_runs_key (run_key, id),
    CONSTRAINT fk_vncs_sync_scheduled_job
        FOREIGN KEY (scheduled_job_run_id) REFERENCES scheduled_job_runs(id),
    CONSTRAINT chk_vncs_sync_terminal CHECK (
        (status = 'running' AND ended_at IS NULL)
        OR (status IN ('completed', 'failed', 'interrupted') AND ended_at IS NOT NULL)
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
