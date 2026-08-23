-- 027_partsouq_empty_unit_evidence.sql
--
-- chk_partsouq_artifact_counts 放寬 parsed_record_count 允許 0：unit 頁
-- 存在合法「空零件組」（表殼渲染完整但站方零資料列；實證 TOYOTA1000
-- KP30 BODY STRIPE，三輪 run 位元組級重現）。程式端已改為 receipt
-- done/0，evidence replay 一致性照驗（repositories.py unit 頁例外）。
-- accepted_record_count <= parsed_record_count 不變。

SET SESSION lock_wait_timeout = 30;
SET SESSION innodb_lock_wait_timeout = 30;

ALTER TABLE partsouq_http_artifacts
  DROP CHECK chk_partsouq_artifact_counts,
  ADD CONSTRAINT chk_partsouq_artifact_counts CHECK (
    parsed_record_count >= 0
    AND accepted_record_count <= parsed_record_count
  );
