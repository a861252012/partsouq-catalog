-- 039：正式 current snapshot 切換全量。
--
-- bounded 10k 仍是正式快照，直到「合格的全量 archive」出現為止：
-- published_parts 全部來自同一個 full crawl run，該 run 必須
-- success、證據已封存（evidence verified＋manifest/dataset 雜湊在位）、
-- 且排程為 catalog daemon completed exit=0、單一 linked crawl。任一
-- 條件不成立（含 archive 寫到一半的 running run）都維持 bounded
-- 輸出，讀者不會看到半成品或未驗收的全量資料。切換在同一查詢內
-- 完成：full_ready = 1 時只輸出全量，0 時只輸出 bounded——不會
-- 兩份同時出現。
--
-- bounded 分支沿用 036 的既有語意（evidence base + 逐組收據閘），
-- 本 migration 不改動 v_current_catalog_parts_evidence_base。
-- MySQL 的 CTE body 不能再巢狀 WITH，故全部 CTE 攤平在頂層。

CREATE OR REPLACE VIEW v_current_catalog_parts AS
WITH
full_ready AS (
    SELECT EXISTS(
        SELECT 1
        FROM published_parts AS published
        JOIN crawl_runs AS full_run
          ON full_run.id = published.crawl_run_id
         AND full_run.dataset_kind = 'full'
         AND full_run.target_parts IS NULL
         AND full_run.status = 'success'
         AND full_run.finished_at IS NOT NULL
         AND full_run.error_msg IS NULL
         AND full_run.evidence_status = 'verified'
         AND full_run.evidence_verified_at IS NOT NULL
         AND full_run.evidence_manifest_sha256 REGEXP '^[0-9a-f]{64}$'
         AND full_run.evidence_dataset_sha256 REGEXP '^[0-9a-f]{64}$'
        JOIN scheduled_job_runs AS full_job
          ON full_job.id = full_run.scheduled_job_run_id
         AND full_job.job_name = 'catalog'
         AND full_job.trigger_mode = 'daemon'
         AND full_job.status = 'completed'
         AND full_job.finished_at IS NOT NULL
         AND full_job.exit_code = 0
        WHERE published.crawl_run_id = (SELECT MIN(crawl_run_id) FROM published_parts)
          AND published.crawl_run_id = (SELECT MAX(crawl_run_id) FROM published_parts)
          AND (SELECT COUNT(*) FROM crawl_runs AS linked
               WHERE linked.scheduled_job_run_id = full_job.id) = 1
    ) AS ready
),
snapshot_groups AS (
    SELECT
        bounded_part.crawl_run_id,
        bounded_part.group_id,
        COUNT(*) AS snapshot_part_count
    FROM bounded_parts AS bounded_part
    GROUP BY bounded_part.crawl_run_id, bounded_part.group_id
),
receipt_artifact_counts AS (
    SELECT
        snapshot_group.crawl_run_id,
        snapshot_group.group_id,
        receipt.source_artifact_id,
        SUM(artifact_record.record_type = 'part') AS parsed_part_count,
        SUM(
            artifact_record.record_type = 'part'
            AND artifact_record.accepted = 1
        ) AS accepted_part_count,
        COUNT(DISTINCT CASE
            WHEN artifact_record.record_type = 'part' AND artifact_record.accepted = 1
            THEN artifact_record.part_id
        END) AS accepted_part_id_count,
        SUM(artifact_record.record_type = 'quarantine_part') AS skipped_record_count
    FROM snapshot_groups AS snapshot_group
    STRAIGHT_JOIN bounded_group_receipts AS receipt
      ON receipt.crawl_run_id = snapshot_group.crawl_run_id
     AND receipt.group_id = snapshot_group.group_id
    STRAIGHT_JOIN partsouq_artifact_records AS artifact_record FORCE INDEX (PRIMARY)
      ON artifact_record.artifact_id = receipt.source_artifact_id
     AND artifact_record.crawl_run_id = receipt.crawl_run_id
    GROUP BY
        snapshot_group.crawl_run_id,
        snapshot_group.group_id,
        receipt.source_artifact_id
),
receipt_snapshot_members AS (
    SELECT
        snapshot_group.crawl_run_id,
        snapshot_group.group_id,
        COUNT(DISTINCT bounded_part.part_id) AS accepted_snapshot_part_count
    FROM snapshot_groups AS snapshot_group
    STRAIGHT_JOIN bounded_group_receipts AS receipt
      ON receipt.crawl_run_id = snapshot_group.crawl_run_id
     AND receipt.group_id = snapshot_group.group_id
    STRAIGHT_JOIN partsouq_artifact_records AS artifact_record FORCE INDEX (PRIMARY)
      ON artifact_record.artifact_id = receipt.source_artifact_id
     AND artifact_record.crawl_run_id = receipt.crawl_run_id
     AND artifact_record.record_type = 'part'
     AND artifact_record.accepted = 1
    STRAIGHT_JOIN bounded_parts AS bounded_part
      ON bounded_part.crawl_run_id = snapshot_group.crawl_run_id
     AND bounded_part.group_id = snapshot_group.group_id
     AND bounded_part.part_id = artifact_record.part_id
     AND bounded_part.evidence_record_sha256 = artifact_record.record_sha256
    GROUP BY snapshot_group.crawl_run_id, snapshot_group.group_id
),
receipt_integrity AS (
    SELECT
        snapshot_group.crawl_run_id,
        snapshot_group.group_id,
        snapshot_group.snapshot_part_count,
        CASE
            WHEN receipt.source_artifact_id IS NOT NULL
             AND artifact.crawl_run_id = snapshot_group.crawl_run_id
             AND artifact.capture_kind = 'live_http'
             AND artifact.page_type = 'unit'
             AND artifact.parser_name = 'parse_parts'
             AND artifact.verification_status = 'verified'
             AND artifact.http_status = 200
             AND artifact.challenge_detected = 0
             AND LOWER(artifact.content_type) LIKE 'text/html%'
             AND artifact.malformed_row_count = 0
             AND artifact.verified_at IS NOT NULL
             AND receipt_artifact_counts.parsed_part_count = receipt.parsed_part_count
             AND receipt_artifact_counts.accepted_part_count = receipt.accepted_part_count
             AND receipt_artifact_counts.accepted_part_id_count = receipt.accepted_part_count
             AND receipt_artifact_counts.skipped_record_count = receipt.skipped_record_count
             AND artifact.parsed_record_count = (
                 receipt.parsed_part_count + receipt.skipped_record_count
             )
             AND artifact.accepted_record_count = receipt.accepted_part_count
             AND artifact.skipped_record_count = receipt.skipped_record_count
             AND receipt_snapshot_members.accepted_snapshot_part_count
                 = receipt.accepted_part_count
             AND snapshot_group.snapshot_part_count = receipt.accepted_part_count
             AND (
                 (receipt.status = 'done'
                  AND receipt.accepted_part_count = receipt.parsed_part_count)
                 OR (receipt.status = 'partial'
                     AND receipt.accepted_part_count > 0
                     AND receipt.accepted_part_count < receipt.parsed_part_count)
             )
            THEN 1 ELSE 0
        END AS is_verified
    FROM snapshot_groups AS snapshot_group
    LEFT JOIN bounded_group_receipts AS receipt
      ON receipt.crawl_run_id = snapshot_group.crawl_run_id
     AND receipt.group_id = snapshot_group.group_id
    LEFT JOIN partsouq_http_artifacts AS artifact
      ON artifact.id = receipt.source_artifact_id
     AND artifact.crawl_run_id = receipt.crawl_run_id
    LEFT JOIN receipt_artifact_counts
      ON receipt_artifact_counts.crawl_run_id = snapshot_group.crawl_run_id
     AND receipt_artifact_counts.group_id = snapshot_group.group_id
     AND receipt_artifact_counts.source_artifact_id = receipt.source_artifact_id
    LEFT JOIN receipt_snapshot_members
      ON receipt_snapshot_members.crawl_run_id = snapshot_group.crawl_run_id
     AND receipt_snapshot_members.group_id = snapshot_group.group_id
),
verified_bounded_group_receipts AS (
    SELECT
        receipt_integrity.crawl_run_id,
        COUNT(*) AS snapshot_group_count,
        SUM(receipt_integrity.snapshot_part_count) AS snapshot_part_count,
        SUM(receipt_integrity.is_verified) AS verified_group_count,
        SUM(
            CASE WHEN receipt_integrity.is_verified = 1
            THEN receipt_integrity.snapshot_part_count ELSE 0 END
        ) AS verified_part_count
    FROM receipt_integrity
    GROUP BY receipt_integrity.crawl_run_id
),
full_snapshot AS (
    SELECT
        'full' AS dataset_scope,
        published_parts.crawl_run_id AS source_crawl_run_id,
        published_parts.part_id, published_parts.vehicle_id, published_parts.model_id,
        published_parts.vehicle_vid, published_parts.brand, published_parts.model,
        published_parts.vehicle_name, published_parts.vehicle_code,
        published_parts.prod_period, published_parts.production_from,
        published_parts.production_to, published_parts.engine, published_parts.trim_name,
        published_parts.part_name, published_parts.part_number,
        published_parts.part_number_normalized, published_parts.category_id,
        published_parts.category_cid, published_parts.category_main,
        published_parts.category_group, published_parts.group_id,
        published_parts.group_code, published_parts.group_uid, published_parts.part_range,
        published_parts.part_from, published_parts.part_to, published_parts.source_url,
        published_parts.note, published_parts.quantity, published_parts.code,
        published_parts.snapshot_at
    FROM published_parts
    JOIN crawl_runs AS full_run
      ON full_run.id = published_parts.crawl_run_id
     AND full_run.dataset_kind = 'full'
     AND full_run.target_parts IS NULL
     AND full_run.status = 'success'
     AND full_run.finished_at IS NOT NULL
     AND full_run.error_msg IS NULL
     AND full_run.evidence_status = 'verified'
     AND full_run.evidence_verified_at IS NOT NULL
     AND full_run.evidence_manifest_sha256 REGEXP '^[0-9a-f]{64}$'
     AND full_run.evidence_dataset_sha256 REGEXP '^[0-9a-f]{64}$'
    JOIN scheduled_job_runs AS full_job
      ON full_job.id = full_run.scheduled_job_run_id
     AND full_job.job_name = 'catalog'
     AND full_job.trigger_mode = 'daemon'
     AND full_job.status = 'completed'
     AND full_job.finished_at IS NOT NULL
     AND full_job.exit_code = 0
    WHERE (SELECT COUNT(*) FROM crawl_runs AS linked
           WHERE linked.scheduled_job_run_id = full_job.id) = 1
),
chosen AS (
    SELECT
        evidence_base.dataset_scope, evidence_base.source_crawl_run_id,
        evidence_base.part_id, evidence_base.vehicle_id, evidence_base.model_id,
        evidence_base.vehicle_vid, evidence_base.brand, evidence_base.model,
        evidence_base.vehicle_name, evidence_base.vehicle_code,
        evidence_base.prod_period, evidence_base.production_from,
        evidence_base.production_to, evidence_base.engine, evidence_base.trim_name,
        evidence_base.part_name, evidence_base.part_number,
        evidence_base.part_number_normalized, evidence_base.category_id,
        evidence_base.category_cid, evidence_base.category_main,
        evidence_base.category_group, evidence_base.group_id, evidence_base.group_code,
        evidence_base.group_uid, evidence_base.part_range, evidence_base.part_from,
        evidence_base.part_to, evidence_base.source_url, evidence_base.note,
        evidence_base.quantity, evidence_base.code, evidence_base.snapshot_at
    FROM v_current_catalog_parts_evidence_base AS evidence_base
    JOIN verified_bounded_group_receipts AS receipt_gate
      ON receipt_gate.crawl_run_id = evidence_base.source_crawl_run_id
     AND receipt_gate.snapshot_part_count = 10000
     AND receipt_gate.verified_group_count = receipt_gate.snapshot_group_count
     AND receipt_gate.verified_part_count = receipt_gate.snapshot_part_count
    WHERE (SELECT ready FROM full_ready) = 0
    UNION ALL
    SELECT
        full_snapshot.dataset_scope, full_snapshot.source_crawl_run_id,
        full_snapshot.part_id, full_snapshot.vehicle_id, full_snapshot.model_id,
        full_snapshot.vehicle_vid, full_snapshot.brand, full_snapshot.model,
        full_snapshot.vehicle_name, full_snapshot.vehicle_code,
        full_snapshot.prod_period, full_snapshot.production_from,
        full_snapshot.production_to, full_snapshot.engine, full_snapshot.trim_name,
        full_snapshot.part_name, full_snapshot.part_number,
        full_snapshot.part_number_normalized, full_snapshot.category_id,
        full_snapshot.category_cid, full_snapshot.category_main,
        full_snapshot.category_group, full_snapshot.group_id, full_snapshot.group_code,
        full_snapshot.group_uid, full_snapshot.part_range, full_snapshot.part_from,
        full_snapshot.part_to, full_snapshot.source_url, full_snapshot.note,
        full_snapshot.quantity, full_snapshot.code, full_snapshot.snapshot_at
    FROM full_snapshot
    WHERE (SELECT ready FROM full_ready) = 1
)
SELECT * FROM chosen;
