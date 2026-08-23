# 2026-08-23 進度與查證紀錄

## Git 交付

| Commit | 內容 | Remote |
|---|---|---|
| `b7711d4` | 完成最終交接與現況查證紀錄 | 已 push `origin/main` |
| `f80483e` | 更新最終交接與正式資料缺口 | 已 push `origin/main` |
| `e687c4b` | diagnostic exact contract、fresh cleanup、真 MySQL 404 交易測試 | 已 push `origin/main` |
| `949d91e` | 最終交接與進度文件 | 已 push |
| `692076e` | bounded 競態、404／空解析 diagnostics、secret-safe HTTP 錯誤 | 已 push `origin/main` |
| `e092b33` | NHTSA 長任務 heartbeat 與重複驗證 | 已 push |
| `cba3685` | LaunchAgent 自動升級與排程回收順序 | 已 push |
| `1108fd5` | 正式爬蟲交易與 evidence 續跑閘門 | 已 push |
| `a999c9a` | evidence 版本與發布閘門 | 已 push |
| `baeb235` | 搬移專案並強化本機排程環境 | 已 push |

交接盤點開始時，`HEAD` 與 `origin/main` 都是
`f80483ea0adcad4afdc7879fec65a6e0fd3d58ec`。

## 2026-08-23 12:25–13:00

1. 唯讀確認主 DB：raw `parts=10000`，但 bounded／published／正式 view 均為 0；
   quarantine 2,260；NHTSA VIN decode／mapping／fitment 均為 0。
2. 定位正式 run 5 失敗背景：達到 raw 10,000 後仍有 failed scope，evidence 保持
   collecting，不能發布。
3. 修正 bounded／sample 多 in-flight vehicle 造成的 late-worker 越界。
4. 404 改成必須核對 status 與 unit URL；缺 envelope、URL 不符與 HTTP 200 空解析都
   fail closed。
5. migration 023 新增有界、去敏、inline 的 HTTP failure diagnostic；不共用正式
   evidence CAS，避免診斷資料污染發布證據或無限長大。
6. fresh migration test 發現 `compression/body_blob/original_bytes/stored_bytes` 曾誤加到
   `partsouq_http_artifacts`；已移除並重跑 fresh replay 通過。
7. adversarial review 又找到非官方 URL fallback 可能保留 userinfo，以及 content-type
   可夾帶 secret；均已修正並補回歸測試。
8. 提交並推送 `692076e`。

## 最新 gate

| Gate | 結果 |
|---|---|
| Ruff check／format | 通過 |
| strict mypy（本輪三個 source modules） | 通過 |
| HTTP／bounded／migration focused tests | 通過 |
| migration 023 fresh replay／rerun | 通過 |
| MySQL diagnostic upsert／隔離／解壓去敏 | 通過 |
| `git diff --check` | 通過 |
| 獨立 review | 無 P0／P1；留 2 個 P2 測試強化 |
| 最新 commit 完整 E2E | 未執行，依使用者最後指示略過 |

## 目前 runtime

- `mysql`：healthy，`127.0.0.1:3308`。
- `admin`：healthy，`127.0.0.1:8000`。
- `station-admin`：healthy，`127.0.0.1:8086`。
- `queue-scheduler`：running。
- catalog／NHTSA 正式 scheduler：未執行。
- 主 DB catalog ledger：到 migration 022；migration 023 待部署。
- GitHub Actions：最後一筆 CI 是 `394fda8` failure；後續 commit 以 `[skip ci]`
  避免額外 Actions 使用量。這不是 deploy failure。

## 尚未達成的驗收結論

- 沒有「正式已發布 PartSouq 10,000 筆」。
- 沒有「NHTSA VIN 真實 decode」。
- 沒有「confirmed VIN↔PartSouq vehicle mapping」。
- 沒有「VIN↔part fitment」。
- 沒有以最新 `692076e` 跑完整真實資料後台 E2E。

詳細操作順序、成功條件與禁止事項見
`docs/handoff-2026-08-23.md`。

## 2026-08-23 13:00–13:05

1. 完成 migration 023 runtime exact contract：欄位/default/charset、完整 index metadata、
   同 DB foreign key、完整 CHECK clause 與 `ENFORCED=YES`。
2. 修掉 hard-404 MySQL 測試預植 diagnostic 的假綠；改由實際 Crawler 第一次失敗後
   查證 diagnostic，第二次成功重跑驗同一 id/upsert 單列。
3. 補 weak same-token CHECK、`NOT ENFORCED`、prefix index、fresh CREATE、dirty retry
   mutation tests。
4. Ruff、strict mypy、針對性 migration／MySQL gate 全部 exit 0；獨立 re-review PASS。
5. commit `e687c4b` 已 push `origin/main`。
6. 再次唯讀盤點主 DB：raw parts 10,000；published/current 仍 0；NHTSA VIN/mapping/
   fitment 仍 0；NHTSA run 1/2 仍 stale running。
7. 更新最終交接文件，將下一步改為先完成 NHTSA lease／原子發布／stale recovery，
   再套 migration 023 與重跑正式 PartSouq 10,000。

## 2026-08-23 13:13–13:18 最終交接盤點

1. 確認唯一正式 checkout 是
   `/Users/a861252012/Desktop/folder/code/partsouq-catalog`；舊 Documents 路徑只剩空目錄。
2. 確認 `HEAD == origin/main == f80483e`，branch `main` 沒有 ahead／behind。
3. 發現工作樹有未追蹤 `migrations/catalog/024_nhtsa_run_leases.sql`。它只是未完成草稿，
   未進 manifest、runtime、測試或 DB，禁止直接執行或提交。
4. 重新唯讀查證主 DB：raw parts 10,000、distinct part numbers 3,823、bounded／published／
   current 皆 0、quarantine 2,260、NHTSA VIN decode／mapping／fitment 皆 0。
5. NHTSA run 1／2 仍為 running；scheduler child 7／9 仍為 failed exit 124／125。
   source artifacts 共 6 筆（5 imported、1 importing），current artifact 0。
6. 主 DB catalog ledger 仍到 022，migration 023 未套，diagnostics table 不存在。
7. Docker MySQL、admin、station-admin healthy，queue-scheduler running；兩個 health endpoint
   均實際回 OK。catalog／NHTSA LaunchAgent 未載入。
8. 沒有執行完整 E2E、沒有套 migration、沒有啟動正式爬蟲，也沒有更動主 DB。

## 2026-08-23 13:20–13:31 最終交付再查證

1. 確認交接文件 commit `b7711d4` 已推到 `origin/main`，branch 沒有 ahead／behind。
2. 重新檢查工作樹：除未追蹤 migration 024 外，已有 4 個 fresh schema 檔案被修改；
   這 5 個檔案都尚未 commit／push。
3. 這批未提交變更只有 NHTSA lease／scheduler link／current provenance 的 schema 草稿；
   migration manifest、runner exact contract、runtime lease／CAS、原子發布與測試仍未完成。
4. migration 024 仍會被主 DB 兩筆 stale `running` NHTSA run 擋住，不能直接套用；
   fresh schema 與既有 runtime 也尚未相容，不能單獨提交 schema。
5. 再次唯讀查證主 DB：raw parts 10,000、distinct part numbers 3,823、bounded／published／
   current 皆 0、quarantine 2,260、VIN decode／mapping／fitment 皆 0、catalog ledger 到 022。
6. NHTSA run 1／2 仍為 `running`；source artifacts 仍為 6 筆（5 imported、1 importing），
   current artifact 0。沒有修改或刪除任何主 DB 資料。
7. Docker MySQL、admin、station-admin 仍 healthy，queue-scheduler 仍 running；
   catalog／NHTSA LaunchAgent 未載入。

## 2026-08-23 13:31–13:45 NHTSA lease 整合與最終交接

1. 兩個實作 agent 完成未提交的 NHTSA runtime lease 與 scheduler lineage 整合稿，停在安全
   落盤點；沒有 commit／push，也沒有碰主 DB。
2. Runtime 已加入單 writer lease、token／expiry CAS、獨立 DB heartbeat，以及 bulk／API／VIN
   current publish、domain completed、scheduler completed 的原子交易。
3. Scheduler 已加入 parent→bulk／API child 直接 link；child 成功核對 exact domain run，失敗只
   中斷 exact linked run；移除 regex、LIKE 與 output marker authority。
4. migration 024 已進 manifest，fresh schema、runner exact contract 與 legacy stale recovery
   都有草稿；目前 migration hash 與 manifest 一致。
5. 聚焦 Python tests exit 0；scheduler 單檔 92 passed；NHTSA 相關 123 passed、14 MySQL gate
   skipped。Ruff check、strict mypy 8 個 source files、`git diff --check` 通過。
6. 真 MySQL fresh migration E2E 仍失敗：`migration:024 failed`，原因是
   `NHTSA run lease schema contract mismatch: checks`。這是目前第一個 blocker。
7. Ruff format check 仍指出 `src/partsouq_catalog/migrations.py` 3 處格式差異；完整 E2E、
   MySQL race／atomic rollback、獨立 adversarial review 尚未執行。
8. Git 基線仍為 `HEAD == origin/main == 3944312`。工作樹有 16 個 modified、1 個 untracked，
   共 `+1703/-435`；未提交整合稿不得直接套主 DB。
9. 再次唯讀查證主 DB：raw parts 10,000、published/current 0、quarantine 2,260、NHTSA
   run 1／2 stale running、VIN decode／mapping／fitment 0、catalog ledger 022。
10. 更新完整交接文件與桌面摘要。接手第一步是修 migration 024 exact CHECK gate，不是啟動
    正式爬蟲。
