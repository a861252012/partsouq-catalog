-- 012_part_quarantine_resolution.sql
--
-- SOL review P1 收尾：part_quarantine 的人工處置記錄欄位。
--
-- 背景：quarantine 列（無名稱料號列）由爬蟲自動寫入、組標 partial；
-- 處置方式有兩種：
--   1. 站方補上名稱 → 下次排程/重試重抓該組，料號正常落庫後，同一
--      (group_id, part_number, range_str) 的 quarantine 列應由管理員
--      確認並填 resolved_at（或由日後的自動化解決流程處理）。
--   2. 管理員核對後確認該列永遠無法發布（例如站方移除該料號）→
--      填 resolved_at + resolution 說明。
-- 未處置（resolved_at IS NULL）的列會讓 count_quarantined() > 0，
-- 進而阻擋 bounded_success / success 的發布 gate。

ALTER TABLE part_quarantine
  ADD COLUMN resolved_at DATETIME NULL AFTER run_key,
  ADD COLUMN resolution VARCHAR(255) NULL AFTER resolved_at,
  ADD KEY idx_quarantine_resolved (run_key, resolved_at);