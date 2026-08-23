-- 026_vin_decode_nullable_model.sql
--
-- nhtsa_vin_decodes.model_name 改為可 NULL：vPIC 對部分車型（尤其歐系）
-- 不回 Model 欄位；使用者決策明確「部分解碼是預期行為，缺的欄位留空」，
-- 不可因 Model 缺席而拒絕整筆解碼。

SET SESSION lock_wait_timeout = 30;
SET SESSION innodb_lock_wait_timeout = 30;

ALTER TABLE nhtsa_vin_decodes
  MODIFY model_name VARCHAR(512) NULL;
