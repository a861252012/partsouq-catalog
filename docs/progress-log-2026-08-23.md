# 2026-08-23 進度與查證紀錄

## Git 交付

| Commit | 內容 | Remote |
|---|---|---|
| `d0b328a` | 凍結最終交接與最新阻擋證據 | 已 push `origin/main` |
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

## 2026-08-23 13:45–14:05 最終交接再查證

1. Git 基線更新為 `HEAD == origin/main == a46c3a9`；工作樹現有 18 個 modified、2 個
   untracked，約 `+2106/-450`，全是尚未提交的 NHTSA schema/runtime/tests 整合稿。
2. migration 024 原本的 MySQL 8.4 exact CHECK mismatch 已修正；legacy upgrade、重跑、
   CHECK mutation與 running/NULL lease mutation 真 MySQL gate：`4 passed`。
3. scheduler exact lineage／rollback 真 MySQL gate：`5 passed`。
4. NHTSA exactly-one writer、expired takeover、舊 token失效、bulk/VIN atomic rollback 真
   MySQL gate：`15 passed`。本輪合併重跑兩個 NHTSA integration files：`20 passed`。
5. migration＋scheduler＋progress unit：`121 passed`；15 個異動 production source files 的
   strict mypy 通過；Ruff check、`git diff --check` 通過。
6. Ruff format 尚未全綠：`tests/crawler/integration/test_nhtsa_mysql_sync.py` 需要格式化。
7. room-of-doubt review 找到尚未修正的 P1：atomic publish 後 child 非零退出會把 completed
   scheduler row 改 failed；Bulk/API/VIN heartbeat enter／close／join 邊界；stale worker 中間
   artifact mutation未完全綁 lease；長 finalize transaction 可能跨過 expiry。
8. 因仍有 P1，本批 NHTSA diff 未 commit、未 push、未套主 DB，也未啟動正式排程。
9. 唯讀重查主 DB：raw 10,000、distinct 3,823、bounded/published/current 皆 0、quarantine
   2,260、VIN decode/mapping/fitment 皆 0、catalog ledger 022；NHTSA run 1/2 仍 running。
10. MySQL、admin、station-admin healthy，queue-scheduler running；catalog/NHTSA LaunchAgent
    未載入。完整現況與接手順序已更新到 `docs/handoff-2026-08-23.md`。

## 2026-08-23 14:05–14:19 最終交接收斂

1. 確認 Git 已推送基線為 `HEAD == origin/main == f634b68`，branch 沒有 ahead／behind。
2. 工作樹目前有 20 個 modified、2 個 untracked，約 `+3420/-587`；包含 NHTSA
   lease／atomic publish 與 PartSouq bounded resume 兩批未提交修正。
3. NHTSA runtime 已補齊 intermediate artifact lease fence、heartbeat stop／bounded join、
   cleanup error、lock 後 fresh DB timestamp 與跨 TTL finalize；scheduler 已修正 lock order 及
   completed tuple authority。
4. 實作 agent scoped gate：NHTSA progress 17 passed、crawler unit 130 passed、NHTSA 真
   MySQL 19 passed、scheduler unit 95 passed、scheduler真 MySQL 9 passed，Ruff／mypy 通過。
5. 主 agent以 `partsouq_catalog_test` 實跑 unified mapping，確認仍失敗：fixture 在取得 lease
   前呼叫 `create_artifact()`，回 `TypeError: missing 1 required positional argument: 'lease'`。
6. PartSouq run 5 的新根因已確認：membership 已達 10,000，但仍有 scope error；舊 resolver
   會續用同 run，而 remaining=0，形成無法修復的永久重試。
7. bounded resume 草稿已落在 `repositories.py` 與 `test_partsouq_bounded_limit.py`：達配額且
   有 error 時拒絕舊 run、保留 raw/artifact/lineage，讓 scheduler 建新 logical run。
8. 新增 bounded resume unit 聚焦測試 `4 passed`、Ruff check 通過；Ruff format仍失敗，真
   MySQL 參數化測試尚未執行，尚未獨立 review，因此不得啟動正式 10,000。
9. 唯讀重查主 DB：raw 10,000、distinct 3,823、bounded/published/current 皆 0、quarantine
   2,260、VIN decode/mapping/fitment 皆 0、NHTSA artifacts 6/0、ledger 022。
10. MySQL、admin、station-admin healthy，queue-scheduler running；catalog／NHTSA LaunchAgent
    均未載入（`launchctl print` exit 113）。沒有寫主 DB、沒有啟動爬蟲、沒有跑完整 E2E。

## 2026-08-23 14:19–14:39 最終交接校正

1. 確認 Git 基線為 `HEAD == origin/main == 09f0daf`；程式 working tree 仍有 20 個 modified、
   2 個 untracked，tracked diff 約 `+3943/-598`。沒有提交任何程式草稿。
2. 唯讀重查主 DB：raw parts 10,000、distinct part numbers 3,823、bounded／published／current
   皆 0、quarantine 2,260、VIN decode／mapping／fitment皆 0；crawl run 5 仍為 error。
3. NHTSA run 1／2 仍為 running；source artifacts 6（5 imported、1 importing）、current 0；
   catalog ledger 仍只到 022。主 DB沒有套 023／024，也沒有人工改狀態。
4. unified MySQL mapping 的 lease fixture blocker 已修正，先前真 MySQL重跑為 `1 passed`。
5. NHTSA runtime 最新 scoped gate：focused unit 23 passed、crawler unit 136 passed、真 MySQL
   integration 22 passed；Ruff、format、strict mypy、`git diff --check` 通過。
6. 主 agent精準重跑 `tests/test_unified_project.py` 4 個 scheduler案例，結果 `4 failed`；原因是
   測試 fake 尚未接受新的 parent／`parent_scheduled_job_run_id` 介面。
7. 獨立 review 另確認 3 個 NHTSA P2：exit 0 但非 exact tuple 時 linked domain lease 未立即
   中斷；bulk／API artifact identity gate 不完整；parent recovery 未要求 child `finished_at`。
8. PartSouq bounded resume 已有 unit 6 passed、真 MySQL 3 passed、單檔 bounded suite exit 0、
   Ruff／mypy／diff clean；獨立 review 仍找到 direct explicit bounded key bypass 這 1 個 P2。
9. Docker MySQL／admin／station-admin仍 healthy，queue-scheduler running；catalog／NHTSA
   LaunchAgent 均未載入。沒有正式 crawl、沒有完整 E2E、沒有使用付費服務。
10. 已依最新證據更新最終交接文件；下一位 agent 應先修上述小範圍 gate／P2，再拆成
    NHTSA 與 PartSouq 兩個獨立 commit，禁止整批直接提交。

## 2026-08-23 14:39–15:00 最終交接收尾

1. 修正 `tests/test_unified_project.py` 4 個舊 scheduler fake signature；精準重跑 `4 passed`。
2. 修正 NHTSA 三個 review P2：exit-0／non-exact domain lease、bulk／API artifact identity、
   parent child `finished_at` recovery，並補 unit與真 MySQL regression。
3. 真 MySQL測試曾實際抓到 MySQL 8.4 self-update error 1093；stale parent recovery 改成
   `LEFT JOIN` active child 後，NHTSA兩個 integration files重跑 `35 passed`。
4. 完整無 DB／browser suite 最終為 `786 passed, 164 skipped, 0 failed`；JUnit 在
   `/private/tmp/nhtsa-current-full-unit.xml`，tests=950、failures=0、errors=0、skips=164。
5. 獨立 gate audit確認新增 4 個 MySQL cases 後，Ubuntu CI unit預期 skip 應為 220：本機
   gated skips 164，加唯一 macOS-only marker展開 56。已將 workflow與 contract從 216改為
   220；這是 marker模擬，尚未宣稱 Ubuntu hosted runner實跑。
6. CI contract `7 passed`；CI YAML parse `yaml-ok`；17 個 scoped Python files的 Ruff check／
   format通過；strict mypy先前已通過；`git diff --check`通過。
7. `partsouq_catalog_test` 清理讀回：NHTSA sync runs、source artifacts、current artifacts、
   scheduled NHTSA jobs均為 0。測試沒有寫主 DB。
8. 唯讀重查主 DB：raw 10,000、distinct part numbers 3,823、bounded／published／current
   仍為 0、quarantine 2,260、VIN decode／mapping／fitment均為 0、ledger仍到 022。
9. PartSouq run 5仍是 error，evidence collecting、artifact／record count為 0；NHTSA run 1／2
   仍是 running，source/current artifacts為 6／0。沒有人工改狀態或刪資料。
10. MySQL、admin、station-admin仍 healthy，queue-scheduler running；catalog／NHTSA
    LaunchAgent未載入。沒有正式 crawl、production deploy、hosted CI或付費服務。
11. 工作樹程式現為 23 tracked modified、2 untracked，tracked diff約 `+4078/-607`；NHTSA
    scoped gate雖綠，仍須完成整體 diff review後單獨 commit；PartSouq direct explicit bounded
    key bypass仍是待修 P2。
12. 依上述最新證據重寫 `docs/handoff-2026-08-23.md` 與桌面摘要；正式 10,000 publish、
    授權 VIN decode／mapping／fitment及正式資料 real Chrome E2E仍未完成。

## 2026-08-23 15:00–15:15 最終交接凍結

1. 完整無 DB／browser suite第二輪完成：`789 passed, 164 skipped, 0 failed`；JUnit為
   `/private/tmp/nhtsa-current-full-unit-round2.xml`。
2. 本輪真 MySQL整合 gate最終為 `66 passed`；使用 `partsouq_catalog_test`，測後4個NHTSA
   相關計數均讀回0。沒有寫主 DB。
3. scheduler新增3個DB錯誤分類／progress regression後，單檔為 `98 passed`。
4. staging audit確認 NHTSA-only應提交23個路徑，排除`repositories.py`與
   `test_partsouq_bounded_limit.py`；NHTSA-only CI契約為217 skips，混合工作樹為220。
5. 最終 adversarial review發現3個尚未修正P1：304 reuse繞過parser/raw integrity、migration
   legacy recovery依賴秒級時間相等、hard-kill running child＋expired domain無法自動回收。
6. 另記錄P2：`_record_start()` release-lock RuntimeError、VIN artifact/payload綁定、
   `replace_datasets`範圍、migration audit欄位與多個測試假綠缺口。
7. 因仍有P1，NHTSA程式草稿沒有stage／commit／push，migration 024沒有套主 DB，也沒有啟動
   Chromium或正式爬蟲。
8. 唯讀重查主 DB：raw 10,000、distinct 3,823、bounded/published/current 0/0/0、
   quarantine 2,260、VIN decode/mapping/fitment 0/0/0、ledger 022；NHTSA run 1/2仍running。
9. MySQL、admin、station-admin仍healthy；queue-scheduler running；catalog／NHTSA
   LaunchAgent的`launchctl print`皆exit 113。
10. Git在本次文件修改前仍為`HEAD == origin/main == 3f8d4e6`；程式工作樹23 tracked
    modified、2 untracked，tracked diff約`+4135/-617`。

## 2026-08-23 15:15–15:46 P1修正與第二次交接凍結

1. NHTSA migration legacy recovery改為5秒有向因果窗，要求run↔child雙向唯一；補+5秒通過、
   +6秒拒絕、1:N、N:1、recent failed child／parent、wrong trigger與候選後CAS mutation案例。
2. Post-024 hard-kill recovery加入expired lease、stale heartbeat／domain／child、精確parent
   lineage與active sibling拒絕；同交易將running child失敗、domain interrupted並清lease，必要時
   收斂running parent。舊error保留且時間不倒退。
3. migration新增focused真MySQL gate：`10 passed, 38 deselected`；Ruff check／format與
   `git diff --check`通過。依交接優先指示，未跑migration整檔或完整suite。
4. NHTSA 304 raw integrity P1已由subagent落盤：conditional前後驗parser、status、verified、
   rejection、regular file、byte count與SHA-256；200用temporary file＋atomic replace修復tamper。
   scoped unit＋真MySQL為`57 passed`，品質gate通過；尚未做主代理整合review。
5. scheduler `_record_start()` release-lock `RuntimeError`與CI／finalization契約由subagent落盤；
   scoped結果`107 passed`，真MySQL`2 passed`；尚未做主代理整合review。
6. 尚未實作：VIN payload／artifact／source key exact binding、`replace_datasets` lease scope限制、
   deterministic lease barrier與完整API orchestration regression。
7. PartSouq direct bounded explicit `PSQ_BOUNDED_RUN_KEY` bypass仍未修。
8. 15:46唯讀重查主DB：raw／distinct為10,000／3,823；bounded／published／current為0／0／0；
   quarantine total／unresolved為2,260／2,260；VIN decode／mapping／fitment為0／0／0；ledger到022。
9. crawl run5仍是error、target／parts_ok為10,000／10,000、evidence collecting且artifact／record
   為0／0。NHTSA run1／2仍running；source/current artifacts為6／0。
10. MySQL、admin、station-admin healthy；queue-scheduler running；catalog／NHTSA LaunchAgent
    皆未載入。未碰主DB、未啟動Chromium／正式爬蟲、未使用付費服務。
11. Git仍為`HEAD == origin/main == d0b328a`，ahead／behind 0／0。工作樹25 tracked modified、
    2 untracked，tracked diff約`+5796/-645`；不得整批提交。
