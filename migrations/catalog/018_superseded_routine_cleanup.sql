-- 018_superseded_routine_cleanup.sql
--
-- 013/014 已由 015 取代，新的 runner 不會在一般升級重播它們。若舊版
-- mysql client 在 CREATE/CALL 中斷，可能留下 migration-owned procedure。
-- 以 forward-only migration 精確移除已知名稱；不使用模糊 LIKE，也不碰
-- 任何不在 immutable manifest 內的 routine。

DROP PROCEDURE IF EXISTS upgrade_partsouq_013_quarantine_run_key_updated_index;
DROP PROCEDURE IF EXISTS assert_partsouq_013_output;
DROP PROCEDURE IF EXISTS upgrade_partsouq_014_quarantine_run_key_resolved_updated_index;
DROP PROCEDURE IF EXISTS assert_partsouq_014_output;
