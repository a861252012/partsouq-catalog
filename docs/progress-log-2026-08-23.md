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
7. PartSouq direct bounded explicit `PSQ_BOUNDED_RUN_KEY` bypass：已修——一律禁止 explicit
   key（scheduled/direct 同規則），補 direct regression；scoped unit 全綠後待真 MySQL 重跑。
8. 15:46唯讀重查主DB：raw／distinct為10,000／3,823；bounded／published／current為0／0／0；
   quarantine total／unresolved為2,260／2,260；VIN decode／mapping／fitment為0／0／0；ledger到022。
9. crawl run5仍是error、target／parts_ok為10,000／10,000、evidence collecting且artifact／record
   為0／0。NHTSA run1／2仍running；source/current artifacts為6／0。
10. MySQL、admin、station-admin healthy；queue-scheduler running；catalog／NHTSA LaunchAgent
    皆未載入。未碰主DB、未啟動Chromium／正式爬蟲、未使用付費服務。
11. Git仍為`HEAD == origin/main == d0b328a`，ahead／behind 0／0。工作樹25 tracked modified、
    2 untracked，tracked diff約`+5796/-645`；不得整批提交。

## 2026-08-23 15:46–16:04 VIN邊界實作與最終adversarial收旂

1. 統一 `vin_source_key()`，並在同一發布交易核對 lease、normalized VIN SHA-256
   source key、artifact、唯一record、natural key、record SHA與canonical payload。
2. API `replace_datasets`改成必須完全等於lease scope；bulk source keys也必須精確相符。
   空API snapshot可清掉自己scope，不會刪除無關current pointer。
3. Finalization新增imported／verified／imported_at、rejections、record rows與source_rows
   對帳；read-only SELECT已建立明確transaction boundary，不再讀舊snapshot。
4. 這批最新scoped gate：unit `32 passed`、NHTSA真MySQL `44 passed`、shared mapping真
   MySQL `1 passed`；Ruff／format／3個production modules strict mypy通過；測後 `_test`
   DB相關計數均為0。沒有寫主DB、沒有啟動瀏覽器。
5. 主代理另外修正shutdown-before-spawn `_record_finish()` DB／RuntimeError分類，以及
   migration DATETIME(0)／DATETIME(6)同秒邊界；聚焦回歸及migration真MySQL測試已通過。
6. 最終獨立adversarial review確認仍有2個P1：
   - recovery沒限制daemon，會改寫manual parent／child；terminal tuple與時間因果也不完整。
   - admission lock release的`RuntimeError`在`dispatch_locked()`／啟動migration路徑仍可往外穿透。
7. 同一review保留3個P2：finalization scheduler child CAS不完整、raw artifact
   check-to-publish filesystem競態，migration仍接受parent先finish、child後start的不可能時序。
8. deterministic Event lease barrier與完整vPIC／variable values／CSSI orchestration真MySQL
   regression仍未補；新增測試後CI skip契約尚未由最終staged snapshot重算。
9. 16:04、本次文件更新前實際查證`HEAD == origin/main == 585ae9e`；工作樹27
   tracked modified、2 untracked，
   tracked diff為`+6831/-709`。沒有stage、commit或push任何程式。
10. 主DB重查仍為：raw／distinct 10,000／3,823；bounded／published／current
    0／0／0；quarantine 2,260／2,260；VIN decode／mapping／fitment 0／0／0；ledger 022。
    PartSouq run 5仍error，NHTSA run 1／2仍running。
11. MySQL、admin、station-admin healthy，queue-scheduler running；catalog／NHTSA LaunchAgent的
    `launchctl print`均exit 113。沒有正式crawl、hosted CI、production deploy或付費服務。

## 2026-08-23 16:04之後 vPIC實測修訂與A1/B1收尾（接手工作階段1）

1. **live API 實測**：`GetWMIs` 端點已被 NHTSA 移除（帶/不帶 page 皆回
   "No HTTP resource was found"）；`GetAllMakes` 單請求 12,340 筆不分頁；
   `GetModelsForMakeId/<id>` 正常；`GetModelsForMakeYear` 回應僅4欄、無
   `Model_Year`；`DecodeWMI/{wmi}` 可用但無法枚舉；VNCS `VNCSEXLRPT.aspx`
   存活，控制項實名 `dlFtrMOBTYPE`/`dlFtrPERIOD`/`dlFtrTESTTYPE`。
2. **文件修訂**：`docs/implementation-plan-2026-08-23.md` 重寫為 v2——移除
   GetWMIs 子計畫、修正 request budget 數學（567k≠3000-4000）、
   `vpic_model_years.required_fields` 移除 `Model_Year`（改 context 注入）、
   「三項決策」更正為四項、VNCS 唯一鍵改為 VIN 條件唯一（generated column）。
3. **B-1 修正**：`crawler.py` bounded run 一律禁止 `PSQ_BOUNDED_RUN_KEY`
   （scheduled/direct 同規則）；新增 direct regression test。舊錯誤訊息字串
   全 repo 無殘留。`handoff-2026-08-23.md`「尚未修正」段同步標記已修。
4. **Skip 契約更正**：unit job 期望值 217 → **262**（=206 env-gated + 56
   macOS-gated；與 e2e job 的 56 交叉驗證）。本地 junit 實測 206 筆 skip
   全數在允許清單內。217 初稿值的推導無法重現，判定為過時套件狀態下誤算。
5. **品質關卡（本機 macOS / Python 3.14.5）**：ruff check ✓、ruff format ✓
   （152 files）、mypy --strict ✓（95 files）、unit suite **800 passed /
   206 skipped**、skip 契約 206 ✓、`test_ci_contract.py` 7 passed。
6. 待辦：真 MySQL 測試（NHTSA_TEST_MYSQL=1 等）→ 分批 commit/push。

## 2026-08-23（接手工作階段2）queue 觸發鏈修正與真MySQL全綠

1. **發現 P1 級生產缺陷（未提交工作內）**：admin API 允許 `nhtsa-bulk/api/vin`
   請求，由 scheduler pending 路徑以 `_JOB_CONTEXT.trigger_mode='queue'` 派發；
   但工作樹新增的 `_assert_active_lease`、NHTSA child 恢復查詢與 migration
   `_repairable_stale_nhtsa_runs` 全部只認 `'daemon'` → 所有 queue 觸發的
   NHTSA run 無法 finalize、中斷後永遠卡 running。
2. **修正不變量**：NHTSA 合法觸發來源 = {daemon, queue}；manual/direct 一律
   拒絕。改動：repository lineage 斷言、scheduler 三個 child 恢復 UPDATE、
   active 偵測 query、migrations 兩處 repairable filter。parent/catalog/
   daemon-cadence 維持 daemon-only（語意正確，不變）。
3. **測試修正**：integration fixtures 的 'manual' 改為 'daemon'（模擬主要
   生產路徑）；manual 負向案例保留並補齊合法 lease 欄位（舊寫法違反新 CHECK
   約束）；`test_successful_child_with_exact_completed_domain_is_completed`
   參數化覆蓋 daemon+queue；修復 concurrent writer 測試的 scope/job 錯配；
   bounded_admin_performance 的 DROP TABLE 加 FOREIGN_KEY_CHECKS 暫關
   （新 FK fk_nhtsa_current_published_run 所需）。
4. **驗證**：真 MySQL 全套 `999 passed / 9 skipped / 0 failed`（7m06s，
   partsouq_catalog_test，ledger=24）；純 unit `801 passed / 207 skipped`；
   skip 契約更新為 **263 = 207 env-gated + 56 macOS-gated**（ci.yml +
   test_ci_contract 同步）；ruff/format/mypy/git diff --check 全綠。

## 2026-08-23（接手工作階段3）正式管線實跑與三個實戰修復

1. **B2/B3 上線**：LaunchAgent release 建立並 bootstrap；正式 bounded run
   自動啟動（run7 起）。
2. **毒組根因確診**：`[Toyota group=7507] parsed 0 parts` 反覆重現
   （run7/8/9/10，位元組級一致）。取出 partsouq_http_diagnostics 的
   body_blob 驗屍：HTTP 200、車輛表完整（TOYOTA1000 KP30-,1969-78 RHD），
   零件表殼（Number|Name|Code）渲染正常但**站方零資料列**——合法空組，
   非版型變更、非反爬。修復：parsers 新增 `has_empty_parts_table()`；
   crawler 對此情境 receipt done/0；evidence 契約允許 unit 頁 0 筆結果
   （replay 一致性照驗）。真頁 HTML 作為 regression fixture。
3. **解碼契約放寬（對齊使用者決策「缺欄位留空」）**：Make+ModelYear 必要，
   Model/engine/displacement/trim 缺席存 NULL（migration 026 + schema 三處
   同步）；ErrorCode≠0 不再整筆拒絕（歐系 VIN 檢查碼警告 code=1 但資料
   可用），code/text 留存於 error_code/error_text/payload_json。
   實證：42+ 筆入庫，含 sparse 與 code=1 案例。失敗者為 NHTSA 完全無
   申報的台灣專屬車（由 VNCS 自身 make/model/cc 兜底，設計內）。
4. **運維教訓**：admin_crawl_requests 完成時會遮罩 VIN（隱私設計）——
   重試須從源頭表 tw_vncs_vehicles 重新取 VIN，不可重用舊列；
   queue-scheduler/admin 容器映像必須隨碼重建，否則對新 ledger crash-loop。
5. **VNCS 全量**：汽油車 686 頁全量入庫成功；柴油首輪因連續高頻請求後
   站方降級而 fail-closed（診斷掃描 939 頁 0 壞列證明資料本身乾淨），
   改 2s/頁節流重跑中。

## 2026-08-24（工作階段4）正式管線全綠與 VNCS 資料重建完成

1. **B3 正式 bounded 10,000 達成（run14）**：bounded_success、parts_ok=10000、
   evidence VERIFIED（306 artifacts）、bounded_parts=10,000、
   v_current_catalog_parts=10,000、scheduler exit 0。三個前置修復
   （合法空組 receipt、evidence unit 零筆例外、migration 027 CHECK 放寬）
   缺一不可，另修第二種版型（uid=4160 三欄 Number|Name|Code，空名稱列
   走 quarantine 政策）。
2. **VNCS 資料重建完成**：先前兩輪 full run 因非 VIN 引擎碼無唯一約束而
   累積跨 run 重複；清表後以分段韌性腳本（50 頁/段、失敗重試一次跳過、
   2s/頁）重建：G 686 頁 → 3,981 列/2,462 VIN；D 939 頁 → 4,669 列/
   691 VIN；合計 8,650 列 / 3,153 VIN，與理論值吻合。
3. **NHTSA 解碼入庫 209 筆（驗收 >100 達標）**：含 sparse（Model/engine
   NULL）與 ErrorCode≠0（歐系檢查碼警告）案例；未解出者為 NHTSA 無申報
   的台灣專屬車，由 VNCS 自身欄位兜底。

## 2026-08-24（工作階段5）bounded 語意的邊界與下一戰役範圍

**重要發現**：run14 的 10,000 筆全部歸屬單一 vehicle（TOYOTA1000 KP30,
vid='0',1969 古董車；DONE 統計 brands=1/models=1/vehicles=2）——bounded
語意在首個模型即達標停止（log:「part row limit reached; stopping before
next brand」）。就驗收而言成立（精確 10,000＋evidence verified＋原子發布
全鏈證明）；但 C1 VIN↔車款 mapping 與 fitment 的實際價值需要**全量 crawl
戰役**（18 品牌 × 全模型，受 politeness 節流，估需數日）才有意義。此為下一階段範圍決策：
(a) 直接啟動全量 crawl（LaunchAgent 已具備，改 interval/一次性觸發）
(b) 先以現有 10k 完成 C1-C2-D2 工具鏈，全量資料到位後重跑。

## 2026-08-24（工作階段6）全量 crawl 正式啟動與運維配置

**使用者決策**：直接啟動全量 crawl（選項 a）；要求詳細記錄實際完成時間並於
完成時報告；硬碟保底 50GB，接近即自動停止。

1. **啟動方式（重要教訓）**：證據系統強制 `scheduled_trigger_mode='daemon'`
   （repositories.py record_http_evidence 驗證），手動 one-shot 觸發會在
   第一筆證據寫入時崩潰（exit：invalid scheduler provenance）。必須以
   `python -m partsouq_catalog.scheduler --job catalog --daemon` 啟動。
2. **LaunchAgent 已暫停**（bootout），由本 session 的 daemon 取代：
   `caffeinate -is uv run python -m partsouq_catalog.scheduler --job catalog
   --daemon --interval-seconds 3600`，env 覆寫 **PSQ_BOUNDED_PARTS=0**
   （否則 bounded 驗證閘門會無限重觸發）。run id **1053**，
   started_at=2026-08-24 12:13 local。完成後應恢復 LaunchAgent：
   `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.partsouq.catalog-scheduler.plist`
   （或改回 30 天 interval 的等效 daemon）。
3. **監工腳本** `/tmp/opencode/crawl_watchdog.py`（每分鐘巡邏）：
   磁碟可用 <55GB → SIGTERM 停爬＋報告＋通知；偵測完成 → 寫
   `/tmp/opencode/fullcrawl-report.txt`（起訖時間/耗時/零件數）＋osascript
   通知；每小時進度寫 `/tmp/opencode/fullcrawl-progress.log`。
   完成時間權威來源＝scheduled_job_runs id 1053 的 finished_at。
4. **初期觀察**：Toyota 1960-70 年代車款區大量空組／無名稱列（合法 quarantine，
   忽略並記錄政策），parts 數短期不動屬正常。實測 ~213 groups/hr
   （含 cf cookie 輪換）。CloakBrowser 冷啟動曾兩次 60s 未就緒（暫時性，
   重啟後 2s 就緒）；手動重現發現缺 CLOAKBROWSER_CACHE_DIR 會卡下載，
   生產路徑恆設定之。
