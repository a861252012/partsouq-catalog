-- 040_vin_resolved_uids.sql
-- VIN 補爬：把站方 VIN 解碼結果（VIN -> 站方 unit uid）落庫，做冪等對帳。
-- 用途：瀏覽樹（brand->model->vehicle->category）可能漏掉某些僅能經 VIN
-- 解碼抵達的車輛設定（uid）。本表記錄「已知 VIN -> 解出的 uid」，補爬時
-- 只抓 groups_t 尚缺的 (vehicle, cid, uid)，避免重複抓已覆蓋的組。

CREATE TABLE vin_resolved_uids (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    vin             CHAR(17)        NOT NULL,
    brand           VARCHAR(64)     NOT NULL,
    resolved_uid    VARCHAR(32)     NOT NULL,
    resolved_url    VARCHAR(1024)   NOT NULL,
    category_cid    VARCHAR(16)     NULL,
    vehicle_id      BIGINT UNSIGNED NULL,
    source_run_key  VARCHAR(32)     NOT NULL,
    status          VARCHAR(16)     NOT NULL DEFAULT 'pending'
                    COMMENT 'pending=待補爬, crawled=已補抓, not_found=站方無此頁',
    resolved_at     DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    crawled_at      DATETIME(6)     NULL,
    UNIQUE KEY uq_vin_uid (vin, resolved_uid),
    KEY idx_vin_pending (status, brand),
    KEY idx_vin_vehicle (vehicle_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
