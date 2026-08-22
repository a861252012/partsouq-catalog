-- PartSouq catalog schema. The database is selected by Docker/MySQL connection settings.
-- Monthly full-crawl of https://partsouq.com/en/catalog/genuine

-- 品牌 (Brand)
CREATE TABLE IF NOT EXISTS brands (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  name        VARCHAR(64) NOT NULL,          -- TOYOTA, Lexus, ...
  code        VARCHAR(64) NULL,              -- TOYOTA00
  url         VARCHAR(512) NULL,
  UNIQUE KEY uq_brand_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 車系/型號 (Model, from locate page accordion)
CREATE TABLE IF NOT EXISTS models (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  brand_id    INT NOT NULL,
  name        VARCHAR(128) NOT NULL,         -- 4RUNNER, COROLLA, ...
  ssd         TEXT NULL,                     -- session token for pick page
  url         VARCHAR(1024) NULL,
  fetched_at  DATETIME NULL,
  UNIQUE KEY uq_model (brand_id, name),
  CONSTRAINT fk_model_brand FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 具體車款 (Vehicle, from pick page Specifications table)
CREATE TABLE IF NOT EXISTS vehicles (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  model_id    INT NOT NULL,
  identity_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL, -- v5 穩定規格 SHA256；不含 session token
  name        VARCHAR(256) NOT NULL DEFAULT '',  -- Name: ALPHARD/VELLFIRE/HV
  description VARCHAR(512) NULL,             -- Description: AGH3#,AYH30,GGH3#
  model_code  VARCHAR(128) NOT NULL DEFAULT '',  -- Model: AGH30W-NFXGK
  options     VARCHAR(512) NULL,             -- Options: ATM,MTM: ...
  prod_period VARCHAR(64) NULL,              -- Prod Period: 01.2015 - ...
  production_from CHAR(7) NULL,              -- normalized YYYY-MM, inclusive
  production_to CHAR(7) NULL,                -- normalized YYYY-MM, inclusive; NULL=open
  grade       VARCHAR(256) NULL,
  market      VARCHAR(128) NULL,
  engine      VARCHAR(256) NULL,
  transmission VARCHAR(256) NULL,
  body_style  VARCHAR(256) NULL,
  ssd         TEXT NULL,                     -- vehicle session token
  vid         VARCHAR(32) NULL,              -- vid param
  url         VARCHAR(1024) NULL,
  fetched_at  DATETIME NULL,
  UNIQUE KEY uq_vehicle_identity_v5 (model_id, identity_hash),
  CONSTRAINT fk_vehicle_model FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE,
  CONSTRAINT chk_vehicle_production_from CHECK (
    production_from IS NULL OR (
      production_from REGEXP '^[0-9]{4}-(0[1-9]|1[0-2])$'
      AND CAST(LEFT(production_from, 4) AS UNSIGNED) BETWEEN 1886 AND 2100
    )
  ),
  CONSTRAINT chk_vehicle_production_to CHECK (
    production_to IS NULL OR (
      production_to REGEXP '^[0-9]{4}-(0[1-9]|1[0-2])$'
      AND CAST(LEFT(production_to, 4) AS UNSIGNED) BETWEEN 1886 AND 2100
    )
  ),
  CONSTRAINT chk_vehicle_production_order CHECK (
    production_from IS NULL OR production_to IS NULL OR production_from <= production_to
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 零件大分類 (Main category: ENGINE/FUEL/TOOL, POWER TRAIN/CHASSIS, ...)
CREATE TABLE IF NOT EXISTS categories (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  vehicle_id  INT NOT NULL,
  name        VARCHAR(256) NOT NULL,
  cid         VARCHAR(32) NULL,              -- cid param
  fetched_at  DATETIME NULL,
  KEY idx_cat_name (vehicle_id, name(200)),
  UNIQUE KEY uq_cat_cid (vehicle_id, cid),
  CONSTRAINT fk_cat_vehicle FOREIGN KEY (vehicle_id) REFERENCES vehicles(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 零件組／diagram（中分類；站方目前無可證明的獨立小分類來源）
-- Group examples: 0901 STANDARD TOOL, 1101 PARTIAL ENGINE ASSEMBLY, ...
CREATE TABLE IF NOT EXISTS groups_t (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  category_id INT NOT NULL,
  code        VARCHAR(16) NOT NULL DEFAULT '',  -- 0901, 1101...
  name        VARCHAR(256) NULL,             -- STANDARD TOOL
  uid         VARCHAR(32) NOT NULL DEFAULT '', -- uid param；同 code 的變體身分
  url         VARCHAR(1024) NULL,
  fetched_at  DATETIME NULL,
  fetched_run_key VARCHAR(32) NULL,          -- 最後一次抓取零件的 run_key（group terminal state，F1b）
  fetched_status VARCHAR(16) NULL,           -- done / not_found（F5 receipt；HTTP 200 零解析一律視為異常不寫 receipt；partial 為歷史值，不再產生）
  fetched_row_count INT DEFAULT 0,           -- 本組零件筆數（F5 receipt，content hash 基礎）
  verified_row_count INT NOT NULL DEFAULT 0, -- 歷次 done 的最高筆數；縮水偵測基準，只升不降
  UNIQUE KEY uq_group (category_id, code, uid),
  CONSTRAINT fk_group_cat FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 零件 (Part, from unit page table)
CREATE TABLE IF NOT EXISTS parts (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  group_id    INT NOT NULL,
  part_number VARCHAR(64) NOT NULL,          -- Number: 190000V200
  name        VARCHAR(512) NOT NULL,         -- Name: ENGINE ASSY, PARTIAL
  code        VARCHAR(64) NULL,              -- Code: 11000
  note        TEXT NULL,                     -- Note
  quantity    VARCHAR(16) NULL,              -- Quantity: 01
  range_str   VARCHAR(64) NOT NULL DEFAULT '',  -- Range: 01.2015 - 01.2018
  part_from   CHAR(7) NULL,                  -- normalized YYYY-MM, inclusive
  part_to     CHAR(7) NULL,                  -- normalized YYYY-MM, inclusive; NULL=open
  url         VARCHAR(1024) NULL,
  seen_run_id BIGINT NULL,                    -- 最近一次完整抓到此列的 logical run id
  created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_part (group_id, part_number, range_str),
  KEY idx_part_number (part_number),
  KEY idx_part_name (name(200)),
  KEY idx_part_updated (updated_at),
  KEY idx_part_seen_run (seen_run_id),
  CONSTRAINT fk_part_group FOREIGN KEY (group_id) REFERENCES groups_t(id) ON DELETE CASCADE,
  CONSTRAINT chk_part_from CHECK (
    part_from IS NULL OR (
      part_from REGEXP '^[0-9]{4}-(0[1-9]|1[0-2])$'
      AND CAST(LEFT(part_from, 4) AS UNSIGNED) BETWEEN 1886 AND 2100
    )
  ),
  CONSTRAINT chk_part_to CHECK (
    part_to IS NULL OR (
      part_to REGEXP '^[0-9]{4}-(0[1-9]|1[0-2])$'
      AND CAST(LEFT(part_to, 4) AS UNSIGNED) BETWEEN 1886 AND 2100
    )
  ),
  CONSTRAINT chk_part_range_order CHECK (
    part_from IS NULL OR part_to IS NULL OR part_from <= part_to
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 站方合法存在、但無法發布的零件列（無可驗證產品名稱，SOL review P1）。
-- 不落 parts（發布資料必須能把料號對到名稱），也不能讓該組標 done 後
-- 被永久忽略：寫入此表供追蹤（使用者決定的「忽略＋紀錄」政策，組照常
-- 標 done、發布照常進行）。resolved_at / resolution 供運維標記處置
-- 狀態；同一料號在後續 run 再次出現時會重開處置狀態。
CREATE TABLE IF NOT EXISTS part_quarantine (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  group_id    INT NOT NULL,
  part_number VARCHAR(64) NOT NULL,
  range_str   VARCHAR(64) NOT NULL DEFAULT '',
  reason      VARCHAR(32) NOT NULL,            -- nameless / 其他未來類別
  code        VARCHAR(64) NULL,
  quantity    VARCHAR(16) NULL,
  note        TEXT NULL,
  run_key     VARCHAR(128) NOT NULL,           -- 發現時所在的 logical run
  resolved_at DATETIME NULL,                   -- 人工處置時間（migration 012）
  resolution  VARCHAR(255) NULL,               -- 處置說明（migration 012）
  created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_quarantine (group_id, part_number, range_str, reason),
  KEY idx_quarantine_list (resolved_at, updated_at),
  KEY idx_quarantine_run_key_resolved_updated (run_key, resolved_at, updated_at),
  CONSTRAINT fk_quarantine_group FOREIGN KEY (group_id)
    REFERENCES groups_t(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 最近一次完整 success 的不可變、反正規化 current snapshot。
-- normalized tables 可讓 failed/partial attempt 繼續 upsert；v_parts 只讀
-- 本表，因此不會在未完成 attempt 中途改變。成功收尾時以同一交易
-- upsert 本次列並刪除過期列；失敗 rollback 後仍保留上一版。
CREATE TABLE IF NOT EXISTS published_parts (
  part_id        INT NOT NULL PRIMARY KEY,
  vehicle_id     INT NOT NULL,
  model_id       INT NULL,
  vehicle_vid    VARCHAR(32) NULL,
  brand          VARCHAR(64) NOT NULL,
  model          VARCHAR(128) NOT NULL,
  vehicle_name   VARCHAR(256) NOT NULL,
  vehicle_code   VARCHAR(128) NOT NULL,
  prod_period    VARCHAR(64) NULL,
  production_from CHAR(7) NULL,
  production_to   CHAR(7) NULL,
  engine         VARCHAR(256) NULL,
  trim_name      VARCHAR(256) NULL,
  part_name      VARCHAR(512) NOT NULL,
  part_number    VARCHAR(64) NOT NULL,
  part_number_normalized VARCHAR(64) NOT NULL,
  category_id    INT NULL,
  category_cid   VARCHAR(32) NULL,
  category_main  VARCHAR(256) NOT NULL,
  category_group VARCHAR(256) NULL,
  group_id       INT NULL,
  group_code     VARCHAR(16) NOT NULL,
  group_uid      VARCHAR(32) NULL,
  part_range     VARCHAR(64) NOT NULL,
  part_from      CHAR(7) NULL,
  part_to        CHAR(7) NULL,
  source_url     VARCHAR(1024) NULL,
  note           TEXT NULL,
  quantity       VARCHAR(16) NULL,
  code           VARCHAR(64) NULL,
  snapshot_at    DATETIME NOT NULL,
  KEY idx_published_part_number (part_number),
  KEY idx_published_part_number_normalized (part_number_normalized),
  KEY idx_published_brand_model (brand, model),
  KEY idx_published_snapshot_page (snapshot_at, part_id),
  KEY idx_published_vehicle (vehicle_id),
  KEY idx_published_model (model_id),
  KEY idx_published_category (category_id),
  KEY idx_published_group (group_id),
  CONSTRAINT chk_published_production_from CHECK (
    production_from IS NULL OR (
      production_from REGEXP '^[0-9]{4}-(0[1-9]|1[0-2])$'
      AND CAST(LEFT(production_from, 4) AS UNSIGNED) BETWEEN 1886 AND 2100
    )
  ),
  CONSTRAINT chk_published_production_to CHECK (
    production_to IS NULL OR (
      production_to REGEXP '^[0-9]{4}-(0[1-9]|1[0-2])$'
      AND CAST(LEFT(production_to, 4) AS UNSIGNED) BETWEEN 1886 AND 2100
    )
  ),
  CONSTRAINT chk_published_production_order CHECK (
    production_from IS NULL OR production_to IS NULL OR production_from <= production_to
  ),
  CONSTRAINT chk_published_part_from CHECK (
    part_from IS NULL OR (
      part_from REGEXP '^[0-9]{4}-(0[1-9]|1[0-2])$'
      AND CAST(LEFT(part_from, 4) AS UNSIGNED) BETWEEN 1886 AND 2100
    )
  ),
  CONSTRAINT chk_published_part_to CHECK (
    part_to IS NULL OR (
      part_to REGEXP '^[0-9]{4}-(0[1-9]|1[0-2])$'
      AND CAST(LEFT(part_to, 4) AS UNSIGNED) BETWEEN 1886 AND 2100
    )
  ),
  CONSTRAINT chk_published_part_order CHECK (
    part_from IS NULL OR part_to IS NULL OR part_from <= part_to
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 正式、有界、current-only 的 PartSouq dataset。它明確不代表全站；
-- 只有 exact target 且通過來源/關聯品質關卡的 bounded run 會在
-- 同一交易更換本表。part_id 是穩定分頁鍵，預先正規化的
-- 料號則避免查詢時每列 REGEXP_REPLACE。
CREATE TABLE IF NOT EXISTS bounded_parts (
  part_id        INT NOT NULL PRIMARY KEY,
  crawl_run_id   INT NOT NULL,
  vehicle_id     INT NOT NULL,
  model_id       INT NOT NULL,
  vehicle_vid    VARCHAR(32) NOT NULL,
  brand          VARCHAR(64) NOT NULL,
  model          VARCHAR(128) NOT NULL,
  vehicle_name   VARCHAR(256) NOT NULL,
  vehicle_code   VARCHAR(128) NOT NULL,
  prod_period    VARCHAR(64) NULL,
  production_from CHAR(7) NULL,
  production_to   CHAR(7) NULL,
  engine         VARCHAR(256) NULL,
  trim_name      VARCHAR(256) NULL,
  part_name      VARCHAR(512) NOT NULL,
  part_number    VARCHAR(64) NOT NULL,
  part_number_normalized VARCHAR(64) NOT NULL,
  category_id    INT NOT NULL,
  category_cid   VARCHAR(32) NOT NULL,
  category_main  VARCHAR(256) NOT NULL,
  category_group VARCHAR(256) NOT NULL,
  group_id       INT NOT NULL,
  group_code     VARCHAR(16) NOT NULL,
  group_uid      VARCHAR(32) NOT NULL,
  part_range     VARCHAR(64) NOT NULL,
  part_from      CHAR(7) NULL,
  part_to        CHAR(7) NULL,
  source_url     VARCHAR(1024) NOT NULL,
  note           TEXT NULL,
  quantity       VARCHAR(16) NULL,
  code           VARCHAR(64) NOT NULL,
  snapshot_at    DATETIME NOT NULL,
  KEY idx_bounded_run (crawl_run_id),
  KEY idx_bounded_part_number (part_number),
  KEY idx_bounded_part_number_normalized (part_number_normalized),
  KEY idx_bounded_brand_model (brand, model),
  KEY idx_bounded_snapshot_page (snapshot_at, part_id),
  CONSTRAINT chk_bounded_production_from CHECK (
    production_from IS NULL OR (
      production_from REGEXP '^[0-9]{4}-(0[1-9]|1[0-2])$'
      AND CAST(LEFT(production_from, 4) AS UNSIGNED) BETWEEN 1886 AND 2100
    )
  ),
  CONSTRAINT chk_bounded_production_to CHECK (
    production_to IS NULL OR (
      production_to REGEXP '^[0-9]{4}-(0[1-9]|1[0-2])$'
      AND CAST(LEFT(production_to, 4) AS UNSIGNED) BETWEEN 1886 AND 2100
    )
  ),
  CONSTRAINT chk_bounded_production_order CHECK (
    production_from IS NULL OR production_to IS NULL OR production_from <= production_to
  ),
  CONSTRAINT chk_bounded_part_from CHECK (
    part_from IS NULL OR (
      part_from REGEXP '^[0-9]{4}-(0[1-9]|1[0-2])$'
      AND CAST(LEFT(part_from, 4) AS UNSIGNED) BETWEEN 1886 AND 2100
    )
  ),
  CONSTRAINT chk_bounded_part_to CHECK (
    part_to IS NULL OR (
      part_to REGEXP '^[0-9]{4}-(0[1-9]|1[0-2])$'
      AND CAST(LEFT(part_to, 4) AS UNSIGNED) BETWEEN 1886 AND 2100
    )
  ),
  CONSTRAINT chk_bounded_part_order CHECK (
    part_from IS NULL OR part_to IS NULL OR part_from <= part_to
  ),
  CONSTRAINT chk_bounded_range_overlap_start CHECK (
    part_to IS NULL OR production_from IS NULL OR part_to >= production_from
  ),
  CONSTRAINT chk_bounded_range_overlap_end CHECK (
    production_to IS NULL OR part_from IS NULL OR production_to >= part_from
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 爬蟲狀態 (斷點續爬)
CREATE TABLE IF NOT EXISTS crawl_state (
  id           INT AUTO_INCREMENT PRIMARY KEY,
  run_key      VARCHAR(32) NULL,             -- 當月 run 標記（例如 '2026-08'）；NULL=相容模式
  scope        VARCHAR(32) NOT NULL,         -- brand / model / vehicle / category / group / part
  scope_key    VARCHAR(256) NOT NULL,        -- unique key within scope
  status       VARCHAR(16) NOT NULL,         -- pending / done / error
  error_msg    TEXT NULL,
  updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_state_run (run_key, scope, scope_key),
  KEY idx_state_run (run_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 爬蟲運行記錄
CREATE TABLE IF NOT EXISTS crawl_runs (
  id           INT AUTO_INCREMENT PRIMARY KEY,
  run_key      VARCHAR(32) NULL,             -- 當月 run 標記（例如 '2026-08'）
  started_at   DATETIME NOT NULL,
  finished_at  DATETIME NULL,
  status       VARCHAR(16) NULL,             -- running / success / error
  dataset_kind VARCHAR(16) NOT NULL DEFAULT 'full', -- full / sample / bounded
  target_parts INT NULL,
  scheduled_job_run_id BIGINT UNSIGNED NULL,
  brands_ok    INT DEFAULT 0,
  models_ok    INT DEFAULT 0,
  vehicles_ok  INT DEFAULT 0,
  groups_ok    INT DEFAULT 0,
  parts_ok     INT DEFAULT 0,
  parts_new    INT DEFAULT 0,
  error_msg    TEXT NULL,
  UNIQUE KEY uq_run_key (run_key),
  KEY idx_crawl_run_schedule (scheduled_job_run_id),
  CONSTRAINT chk_crawl_run_target CHECK (target_parts IS NULL OR target_parts > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 「現存」語意：只讀最近一次完整 success 交易建立的 snapshot。
CREATE OR REPLACE VIEW v_parts AS
SELECT
  part_id, vehicle_id, model_id, vehicle_vid,
  brand, model, vehicle_name, vehicle_code, prod_period,
  production_from, production_to, engine, trim_name,
  part_name, part_number,
  category_id, category_cid, category_main, category_group,
  group_id, group_code, group_uid,
  part_range, part_from, part_to, source_url, note, quantity, code
FROM published_parts;
