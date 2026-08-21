-- 012_part_quarantine_resolution.sql
--
-- part_quarantine 的運維處置記錄欄位。
--
-- 政策（使用者決定）：無名稱料號列是「忽略 + 紀錄」—— 爬蟲自動寫入
-- quarantine 表作為完整紀錄，組照常標 done、發布照常進行，不阻擋任何
-- gate。resolved_at / resolution 供運維核對後標記處置狀態（純審計紀錄）：
--   1. 站方補上名稱 → 之後的 run 重新爬取，料號正常落庫；
--   2. 管理員核對後確認該列永遠無法發布 → 填 resolved_at + resolution。
-- count_quarantined()（未處置列數）可供運維查詢，不影響流程。

ALTER TABLE part_quarantine
  ADD COLUMN resolved_at DATETIME NULL AFTER run_key,
  ADD COLUMN resolution VARCHAR(255) NULL AFTER resolved_at,
  ADD KEY idx_quarantine_resolved (run_key, resolved_at);