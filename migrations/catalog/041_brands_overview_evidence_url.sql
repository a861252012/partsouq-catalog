-- 041：品牌總覽頁納入證據鏈。
--
-- crawler 的品牌清單改為優先抓 /en/brands-16.html：首頁側欄只列
-- 浮動子集（2026-09 實測 16 個且缺 Toyota/Kia），只依賴首頁會讓
-- full crawl 永遠爬不到完整品牌面。總覽頁的 public_source_url 必須
-- 通過 chk_partsouq_artifact_public_url，本 migration 在原條件之外
-- 精確放行該頁；禁 ssd 參數與禁 fragment 的規則維持不變。
--
-- 降級重套安全：約束已在（含 brands 條款）時整段跳過。

SET SESSION lock_wait_timeout = 30;
SET SESSION innodb_lock_wait_timeout = 30;

DROP PROCEDURE IF EXISTS upgrade_partsouq_041_brands_evidence_url;
DELIMITER //
CREATE PROCEDURE upgrade_partsouq_041_brands_evidence_url()
BEGIN
  IF DATABASE() IS NULL OR (
    SELECT COUNT(*) FROM information_schema.TABLES
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'partsouq_http_artifacts'
      AND TABLE_TYPE = 'BASE TABLE'
  ) <> 1 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 041: select database and apply catalog schema first';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.CHECK_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE()
      AND CONSTRAINT_NAME = 'chk_partsouq_artifact_public_url'
      AND CHECK_CLAUSE LIKE '%en/brands-16.html%'
  ) THEN
    ALTER TABLE partsouq_http_artifacts
      DROP CHECK chk_partsouq_artifact_public_url;
    ALTER TABLE partsouq_http_artifacts
      ADD CONSTRAINT chk_partsouq_artifact_public_url CHECK (
        (
          public_source_url = 'https://partsouq.com/en/catalog/genuine'
          OR public_source_url LIKE 'https://partsouq.com/en/catalog/genuine/%'
          OR public_source_url = 'https://partsouq.com/en/brands-16.html'
        )
        AND LOWER(public_source_url) NOT REGEXP '(^|[?&])ssd='
        AND public_source_url NOT LIKE '%#%'
      );
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.CHECK_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE()
      AND CONSTRAINT_NAME = 'chk_partsouq_artifact_public_url'
      AND CHECK_CLAUSE LIKE '%en/brands-16.html%'
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'migration 041: brands overview URL check verification failed';
  END IF;
END//
DELIMITER ;
CALL upgrade_partsouq_041_brands_evidence_url();
DROP PROCEDURE upgrade_partsouq_041_brands_evidence_url;
