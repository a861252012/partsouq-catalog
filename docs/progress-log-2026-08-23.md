# 2026-08-23 進度與查證紀錄

## Git 交付

| Commit | 內容 | Remote |
|---|---|---|
| `e687c4b` | diagnostic exact contract、fresh cleanup、真 MySQL 404 交易測試 | 已 push `origin/main` |
| `949d91e` | 最終交接與進度文件 | 已 push |
| `692076e` | bounded 競態、404／空解析 diagnostics、secret-safe HTTP 錯誤 | 已 push `origin/main` |
| `e092b33` | NHTSA 長任務 heartbeat 與重複驗證 | 已 push |
| `cba3685` | LaunchAgent 自動升級與排程回收順序 | 已 push |
| `1108fd5` | 正式爬蟲交易與 evidence 續跑閘門 | 已 push |
| `a999c9a` | evidence 版本與發布閘門 | 已 push |
| `baeb235` | 搬移專案並強化本機排程環境 | 已 push |

目前最新程式 commit 與 `origin/main` 是
`e687c4b8bf8f467a17f46a7fd49e6d9c1c717715`；本文件更新會另建文件 commit。

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
