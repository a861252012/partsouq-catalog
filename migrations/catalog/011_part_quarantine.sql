-- 011_part_quarantine.sql
--
-- 站方合法存在、但無法發布的零件列 quarantine（SOL review P1）。
--
-- 背景：unit 頁的「純料號列」（有料號/Code/Quantity/Range 但完全沒有
-- 可驗證的產品名稱文字）不是版型異常，不能算 malformed；但發布資料
-- 必須能把料號對到產品名稱，因此不落 parts 表。若整組直接標 done，
-- 下次排程不再重抓，這些料號就永久漏掉、也無法保證「每個料號都能
-- mapping 到名稱」。
--
-- 本 migration：
--   1. 建立 part_quarantine 表，記錄被跳過的料號原始欄位，供追蹤/後續
--      手動處置（UNIQUE(group_id, part_number, range_str, reason) 冪等）。
--   2. groups_t.fetched_status 為 VARCHAR(16)，'partial' 曾用於「有
--      quarantine 的組」；policy 已改為「忽略 + 紀錄」（組照常標
--      done、quarantine 表為完整紀錄，見 012），partial 不再產生。
--   3. 基線：把既有 done/not_found 之外的重抓組（例如舊 run 留下的
--      fetched_status 異常值）維持原樣，不自動改寫任何既有 receipt。

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
  created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_quarantine (group_id, part_number, range_str, reason),
  KEY idx_quarantine_group (group_id),
  CONSTRAINT fk_quarantine_group FOREIGN KEY (group_id)
    REFERENCES groups_t(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
