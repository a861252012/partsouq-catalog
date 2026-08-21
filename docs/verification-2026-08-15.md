# 爬蟲、mapping 與站方後台驗證紀錄（更新至 2026-08-21）

## 結論

- NHTSA：合成 fixture 已驗證逐欄解析、共用 MySQL 寫入與讀回；沒有使用者授權 VIN，因此不能宣稱 live 成功解碼。
- Mapping：以 fixture 走過 PartSouq parser、repository、publish、VIN 車款確認與後台查詢，端到端成功。
- PartSouq live：host 上的 CloakBrowser session 曾完成兩次 60 筆 sample；
  這不是 10,000 筆 bounded publish。2026-08-21 查核時，Compose 內的正式
  scheduler image 仍是舊版透明 HTTP 路徑並持續 403；正式 server-like
  排程尚未驗證成功，不能把 host sample 當成部署完成。

## 全新 MySQL

使用 MySQL 8.4 從空 volume 依序載入：

1. `db/catalog.sql`
2. `db/nhtsa.sql`
3. `db/admin.sql`
4. `db/station_admin.sql`
5. `migrations/catalog/009_bounded_production_dataset.sql`
6. `migrations/catalog/010_group_uid_identity.sql`

結果：container healthy；共用 DB 同時包含型錄、NHTSA、mapping、站方後台
overlay／audit tables 與 10 個 compatibility views。`nhtsa_vin_decodes`、
`v_vin_part_fitments` 與全部 `station_admin_*` views 均可查詢。

`migrations/catalog/007_unified_vin_mapping.sql` 與 `008_admin_source_ids.sql`
已在既有／fresh schema 驗證，008 也已連續重跑。唯一可證明關聯的舊
snapshot 才會回填；無法唯一判定者保留但不進 mapping view。

## 自動測試

2026-08-21 review 重跑時，在名稱以 `_test` 結尾的獨立資料庫啟用所有
MySQL 測試：

```text
320 passed; 0 skipped; MySQL and browser E2E gates enabled; pytest exit code 0
ruff check: passed
ruff format --check: 130 files already formatted
mypy src/partsouq_catalog/scheduler.py: passed
```

JUnit 結果：`/private/tmp/partsouq-403-review-all-green.xml`。此輪以
`partsouq_review_20260821_test` 執行 shared MySQL gates；Browser E2E 另建立
隨機命名的 `_test` 資料庫，結束後刪除。

catalog 舊模組目前不是全專案 strict-mypy baseline；直接檢查本次涉及的六個
catalog 模組仍會讀出 236 個既有 typing errors，因此不能宣稱全專案 mypy
通過。本輪維持最小修改，以 runtime、MySQL E2E、Ruff 與 scheduler scoped
mypy 作為 gate。

端到端案例驗證：

- PartSouq 只有結束年月的生產區間 `- 12.2020` 正規化為開放起點至 `2020-12`。
- 零件適用區間 `01.2018 - 12.2019` 正規化為 `2018-01` 至 `2019-12`。
- VIN 的年份為 2018 時，可查回該零件。
- 適用區間在 2021–2022 的另一個零件不會被錯配。
- 無法解析的零件區間不會被當成無限期適用。
- 未確認 mapping 前 VIN 零件查詢為空；未帶管理 token 會回 401。
- `TEST-PART-001` 與 `TESTPART001` 可查到同一個 raw 料號。
- 相同 VIN 與 PartSouq vehicle mapping 重複建立會回 HTTP 409。
- 候選查詢會並列 NHTSA 引擎、排氣量、Trim 與 PartSouq 引擎、Grade／Trim，供人工確認。
- 端到端案例先把 VIN 確認到車款 A 並查回 A 的料號，再更正到另一個已發布車款 B，確認結果只剩 B 的料號。
- 已離開 current published snapshot 的 mapping 會在後台列表標為 `stale`，可沿用相同候選規則更正，不需直接修改資料庫。
- 同一 VIN 的 NHTSA 品牌、型號或年式若在後續 decode 改變，舊 mapping 會標為 `stale` 且零件查詢為空；重新人工確認後才恢復。
- 人工 mapping 的 engine／Trim 分隔字元不會造成 generated key 誤碰撞。
- DB 會拒絕超出 1886–2100 的 normalized 年月；fresh schema 與 migration 契約一致。
- 即使 PartSouq 與 NHTSA 舊表 collation 不同，正規化品牌／型號比較仍可執行。
- 1001 筆真實 HTML 格式 fixture 只寫入 1000 筆；被截斷的 Group 不會清除舊 membership、不會標為完整，也不會發布 current snapshot。
- `part_id`、`model_id`、`vehicle_id`、`category_id`、`group_id` 與 PartSouq `vid`／`cid`／`uid`／零件 Code 已從 normalized tables 寫入 snapshot、`v_parts` 與 VIN mapping view，再由 MySQL 實際讀回。
- 後台 `/api/database-summary` 會分開顯示 normalized、published、NHTSA、mapping、sample 與資料品質計數；空資料庫不會被判成通過。
- 站方後台 E2E 每次建立獨立隨機 `_test` MySQL、套用正式四份 schema、灌入
  1,000 筆 fixture，並啟動真實 HTTP server 與 Chrome。瀏覽器實際選擇
  預設 pageSize 30，再選擇 200、前往第 5 頁、以無連字號料號搜尋、修改零件、
  清除 Boolean 覆寫、停用與恢復；MySQL 反查確認 revision
  為 1／2／3／4／5、audit action 為 update／update／update／retire／restore，而且原始 `parts.name`
  與 `published_parts` 未被修改。修改、停用、恢復也逐次反查 sample、published、
  料號 fitment 與 VIN parts API。測試另刻意讓 normalized 與 published snapshot
  值不同，確認未發布的新值不會洩漏；舊編輯表單遇來源更新會回 409，重新載入後
  才能 rebase。測試結束後暫存 DB 已刪除，主 DB 的 1,000 筆
  sample 與 0 筆 overlay event 均未改變。

## 本機後台

本機 Compose 的 MySQL、資料品質後台與完整站方後台已實際啟動：

- `http://partsouq.localhost:8000/admin`：資料品質、sample／published、NHTSA 統計與
  分頁預覽。
- `http://admin.partsouq.localhost:8086/`：由原專案未合併的
  `agent/mysql-admin-archive-import` 分支移植的 10 類 entity 後台；支援搜尋、
  詳情、人工覆寫、停用／恢復與 append-only audit。人工修改不改動爬蟲來源列。

兩個 `.localhost` domain 的 health 均回 HTTP 200，並讀取同一個 MySQL database；
`.localhost` 為標準 loopback domain，不需修改 `/etc/hosts`。
目前畫面顯示 sample `1000/1000`、923 個不重複料號、3 個大分類與 47 個
Group 中分類；預設每頁 30 筆，支援 10／25／30／50／100／200 筆、頁碼輸入與
首頁／前後頁／末頁。sample 尚未發布，NHTSA VIN 與已確認 mapping 仍為 0，因此
正式 production gate 尚未通過；NHTSA 官方 reference sync 則已完成
137,140 筆、377 個 current artifacts、0 rejected。

8086 的 `part_numbers` 是現有 normalized `parts` 的 compatibility adapter，
1000 列代表 1000 個料件出現／適用列，其中只有 923 個不重複料號，不能把
1000 說成 1000 個唯一料號。`source_part_code` 是 PartSouq 表格 Code／圖號
呼叫碼，不是車型或料件 model ID。來源追溯目前只有共用 DB 的 source URL 與
時間；沒有保存可重算的 raw HTTP body／hash。

中分類來自 PartSouq Group／diagram，目前 sample 已有 47 個 Group。現有
型錄路徑沒有可證明的第三層小分類來源，所以後台明確標示為 unavailable，
只能人工補充，不能用其他欄位冒充爬回的小分類。

## NHTSA live 邊界

官方 vPIC endpoint 的 HTTP 可連線性已確認。NHTSA 官方語言範例中的完整示例碼會回傳欄位資料，但同時回 `ErrorCode=1`（檢查碼不正確）；專案實跑結果為 run `failed`、artifact `quarantined`、`nhtsa_vin_decodes=0`、current artifact `=0`，沒有拿它冒充成功 VIN。source key 使用 SHA-256，不寫入完整 VIN。

先前曾以一組公開可見 VIN 做 live 測試，但沒有足夠資料證明已獲車主或資料持有人授權。該筆本機資料、原始回應與文件識別值均已刪除，結果不列入成功證據。

因此目前已確認的是：合成 fixture 可完整保存 VIN、Make、Model、ModelYear、EngineConfiguration、EngineModel、DisplacementL 與 Trim；live 成功案例仍需由使用者提供合法持有或獲授權的 VIN 才能驗收。NHTSA vPIC 只能解碼呼叫者已知的 VIN，不能列舉真實存在的完整 VIN。此專案不猜測或掃描 VIN。

## PartSouq live

正式 `partsouq-catalog-crawl` 對 `https://partsouq.com/en/catalog/genuine` 的結果：

```text
HTTP 403
ChallengeError
exit code 1
crawl_runs.status = error
new crawl parts_ok = 0 rows
published_parts = 0 rows
```

2026-08-20 已用本機 `scheduler` daemon 實際執行正式 bounded 10,000 筆工作；
run `bounded-10000-s260820111142950` 同樣在 genuine catalog 得到 HTTP 403，
`crawl_runs.status=error`、`parts_ok=0`，linked `scheduled_job_runs` 為
`catalog/daemon/failed/exit=1`，然後進入 60 秒退避。DB 仍保留較早的
browser-assisted 1,000 筆歷史 sample；它不是這次排程的輸出，也不進
`v_current_catalog_parts`。

2026-08-19 再次以一般低速 HTTP 重跑，結果仍相同。Docker runtime 時鐘已
與主機同步；最新 DB run 為 `sample-20260819T120727998635`，MySQL 以 UTC
保存 `2026-08-19 04:06:42`，狀態仍為 `error`、0 筆。

一般 HTTP 於 2026-08-19 22:04（台北）再次確認仍為 HTTP 403、
`cf-mitigated: challenge`。

**2026-08-20 已切換為 CloakBrowser cookie 機制**：`cloak.py` 以指紋修補版
Chromium（CloakBrowser）讓 Turnstile 人機驗證在無人操作下自動通過，匯出
session cookie（`cf_clearance` + `PHPSESSID`，TTL 25 分鐘自動刷新，存本機
`data/cookies.json`），HTTP client 一律附上 cookie 請求；此機制本質是瀏覽器
指紋規避與驗證自動通過，非站方授權。challenge 未成功刷新時仍算 `blocked`，
不報為完成。不輪替 proxy。

同日以 sample E2E（Toyota model、`PSQ_LIMIT_PARTS=60`）與
`partsouq-scheduler --job catalog --daemon` 無人值守執行驗證：爬蟲跑完
60 筆 fitment 列，`crawl_runs.status=sample`（`sample-20260820T221248030118`，
`target=60`、`parts_ok=60`）、對應 `scheduled_job_runs` 為
`catalog/daemon/completed`。注意事項：

- 該次完成的排程 run 是 **exit_code=3**（樣本達上限的預期停止；scheduler 以
  `success_codes=(0,3)` 記為 completed）。若樣本未達上限就結束，crawler 改記
  `error`，不偽造 sample 完成。
- 60 筆全部是**更新既有樣本列**（`parts_new=0`），不是新增；`parts` 總數為
  1060（含較早的 1000 筆 browser-assisted sample）。未發布
  （`published_parts=0`、`v_current_catalog_parts=0`）。
- 同一日稍早的另一段 daemon 嘗試（scheduled ids 12–16，11:19→13:15 UTC，
  退避間隔 486→966→1928→3606 秒 ≈ 480→960→1920→3600）**先於**這次
  成功的 run（id 20）發生，且全數 exit=1。成功 run（id 20）之後，daemon
  再跑 id 21、22 仍失敗（exit=1，間隔回到 3600 秒）—— 403 迴圈在單次
  成功後依然重演。cookie 機制只讓單次 run 通過，尚未證明可持續無人值守
  完成 10,000 筆 bounded 目標。

**2026-08-21 針對上述 403 迴圈修正並重新實測**：根因不是爬取頻率
（crawl 維持 0.5 req/s），而是**每次新 process 都重啟 CloakBrowser 重解一次
Turnstile**（`get_session()` 只吃記憶體快取，不讀磁碟上的 `data/cookies.json`）；
短時間連續重解被 Cloudflare 標記，refresh 失敗後 cloak 退避（60→1200s）、
daemon 又以 480→960→1920→3600 無限重試。修正：

- `get_session()` 啟動時以 `data/cookies.json` 的 mtime 判斷新鮮度，TTL 內的
  話直接沿用（`_seed_from_disk`），不再每個 run 重啟瀏覽器。
- daemon 連續失敗達 `MAX_CONSECUTIVE_FAILURES=5` 後停止指數重試，等下一次
  interval 再檢查，不再每小時重啟瀏覽器錘站。
- **被拒 cookie 不再復活**：`force_refresh_session` 記下被伺服器拒絕的
  `cf_clearance` 版本，`_seed_from_disk` 跳過同一份（否則清了快取又從磁碟
  撈回，403 迴圈重演）。
- **上限只計站台失敗**：鎖衝突（75）、scheduler DB 紀錄失敗（改以
  `SCHEDULER_DB_ERROR_EXIT_CODE=2` 區分）、子程序無法啟動（127）都不計入
  `MAX_CONSECUTIVE_FAILURES` —— 這些原因會自癒，計入只會讓 catalog 在
  無辜的狀況下靜默整個 interval（預設 30 天）。

重新實測：先跑一趟完整 sample E2E（`PSQ_LIMIT_PARTS=60`，23:54:51 起、
3 分 12 秒、瀏覽器重啟 + 解驗證），完成後緊接跑第二趟（23:59:34 起）：
日誌顯示 `reusing persisted session cookies (cf_clearance, 127s old)`，
**未重啟瀏覽器**，38 秒完成 60/60。兩趟 `scheduled_job_runs` 皆
`completed`（exit_code=3）、`crawl_runs` 皆 `sample` 60/60（id 25、26）。
注意：其後一次 daemon run（id 25，00:15 CST）仍失敗（exit=1），來源程序
不明，cookie 復用只證明「單次連續執行」而非完整無人值守；尚未證明可持續
完成 10,000 筆 bounded 目標。

**2026-08-21 review 修正**：上述 `exit_code=3` 記為 completed 是歷史行為，
會讓測試 sample 冒充正式排程成功。現行 scheduler 只接受 exit `0`；sample
exit `3` 會保留為 failed。另解析器雖已保留同 code、不同 `uid` 的變體，舊 DB
唯一鍵與 receipt 曾只使用 `(category, code)`，會覆蓋／誤跳過第二個變體；
migration 010 與 crawler/repository 已統一改用 `(category, code, uid)`。

同日以已自然通過站方驗證的 Codex 可見瀏覽器，沿頁面真實連結唯讀走訪
Suzuki／Chevrolet Cruize/MW（HR52S-2，2003-11 至 2005-03），擷取 47 個
unit 頁的 1000 筆 DB natural-key 唯一關聯列。來源證據包為
`outputs/partsouq-live-sample-20260819-final.json`；內含 source URL、時間、
HTML byte count 與每頁 SHA-256。逐列比較證據包與 MySQL 的料號、名稱、
Code、range 與 cid／group code／uid，差異為 0；必要欄位、ID 與孤兒關聯
缺漏皆為 0。

這批資料只標為 `sample_not_published`：`parts=1000`、不重複料號 923、
`published_parts=0`。站方目前 unit 表沒有可證明的逐料號日期欄，所以
`part_range` 1000 筆皆空；畫面年份只來自該車款生產期間，不宣稱是逐料號
精確月份。此瀏覽器協助擷取不是正式排程 transport，不能據此宣稱 requests
crawler 已可無人值守取得資料。

## 排程邊界

`partsouq-scheduler --daemon` 已提供常駐週期與自動重試。Compose 分成
PartSouq、NHTSA 與後台佇列三個常駐服務，避免長時間工作互相阻塞；共享 lock
會拒絕同類工作重複執行。正式 PartSouq 服務固定覆寫舊 sample 設定，改用
`PSQ_BOUNDED_PARTS=10000`。每個 catalog child 另保存 `SCHEDULED_JOB_RUN_ID`，
讓 bounded crawl 可精確連回 `scheduled_job_runs`，並以 `trigger_mode=daemon`
排除手動執行，不是只用時間推測來源。若資料 publish 已 commit、但 parent 在
完成排程紀錄前中斷，下一輪會先核對 exact target 與 bounded snapshot 後對帳，
不會再重爬同一批 10,000 筆。兩筆交易之間 current view 會維持 fail-closed（0 筆），
直到 scheduler 完成紀錄或重啟對帳成功才公開資料，不會預先偽造 exit `0`。
若完成寫入回應不明確，daemon 也會先重查 cadence，再決定是否重跑。
NHTSA 中斷留下的 scheduler-owned running row
也會在取得 family lock 後自動標記為 interrupted；bulk 已完成而 API 失敗時，
24 小時內 retry 會從 API stage 續跑；超過 24 小時會重新同步 bulk。
排程狀態查詢失敗只會 backoff 後重讀，不會把
「無法判斷是否到期」誤當成「立即到期」。

本段只描述已實作並通過隔離測試的排程行為；正式 10,000 筆是否成功，仍須以
本機 `bounded_success`、精確筆數、scheduler exit `0` 與資料品質讀回為準。

## 效能

2026-08-20 以同一部本機、重建後服務、每個 endpoint 連續 25 次量測：

- `GET /api/database-summary`：p50 `36.55 ms`、p95 `49.27 ms`。
- 8086 首頁：p50 `7.15 ms`、p95 `8.02 ms`。
- 8086 零件第 5 頁、pageSize 200：p50 `13.80 ms`、p95 `17.90 ms`。

修正前兩個首頁會展開 `nhtsa_current_records` 的多表 view 掃描 137,140 列，
p50 分別約 `776.56 ms` 與 `629.55 ms`。修正後改以 current artifact metadata
的 `source_rows` 聚合；主 DB 逐 dataset 與總數讀回皆與原 view 相同。

## 2026-08-21 穩定度修正與實機 E2E（最終 code）

### 根因（live E2E 暴露）

站方合法存在兩種「看似異常」的資料，先前 parser 把它們計為 malformed、
crawler 因此失敗整台車（raise RuntimeError → vehicle 不標 done → 該車型
永遠爬不完、sample 永遠到不了 60 筆）：

1. **空名稱零件列**：部分 unit 頁固定含有只有料號+code、完全沒有文字名稱
   的列（連圖片 alt 都沒有）。實測 Toyota group 1104 每頁 3 列
   （9161140612／9111140608／9328016008）。
2. **圖片-only 組連結**：部分車型（實測 Mitsubishi L300 整頁、Toyota
   部分車型 44 組）的組清單是 diagram 縮圖連結，無任何文字 anchor。

### 修正（已提交 `7a50020`，14 檔 +715/-49）

- `parsers.py parse_parts`：新增 `diagnostics` 回傳
  `(parts, malformed, skipped_nameless, skipped_rows)`。空名稱列若其餘欄位只含
  料號/code/數量/日期/note → `skipped_nameless`（不落庫、不算 malformed）；
  含其他文字或缺 code → 仍 `malformed`。
- `parsers.py parse_groups`：新增 `image_only`。圖片-only 連結（有 uid）
  接受為空 code/name 的 group，以 `(cid, uid)` 去重；同 uid 的文字連結
  就地升級；同 uid 不同非空 code 的文字列是變體專屬資料，兩者都保留。
- `crawler.py crawl_group`：`skipped_nameless`／`image_only` 只警告；
  只有真 malformed 才失敗整台車。整頁皆空名稱時標 done(0 列)（縮水
  檢查仍在前面，已命名組縮成 0 仍會 raise）。
- `crawler.py` closure 對帳改比 **uid**（不比 code）：圖片↔文字呈現格式
  跨月轉換不再誤判成「group 消失」而永久 brick 車型。
- `repositories.py upsert_group`：圖片月（code 空）→ 文字月（code 有值）
  轉換時，只對「既有空 code 列」就地升級；目標鍵已被變體列佔用時退回
  標準 upsert（不觸發唯一鍵衝突 —— `db._execute` 對 IntegrityError 會
  回滾整個 transaction）。文字列不被圖片月覆寫成空名稱；uid 空字串的
  legacy 列不參與對帳。
- `http_client.py`：robots 雙身分 AND 檢查 —— 揭露的 crawler UA 與實際
  請求的 browser UA **都必須**允許才放行（只查 crawler 身分而用 browser
  身分送出等於繞過站方對一般流量的規則）。
- `Dockerfile`／`compose.yml`：CloakBrowser 正式化（Linux arm64/x64 binary
  預下載、Chromium 系統依賴、Xvfb + xauth、`PSQ_CLOAK_*` env、scheduler
  data volume）。


### 自動測試

`330 passed`（`PARTSOUQ_DB_NAME=partsouq_catalog_test` +
`UNIFIED_TEST_MYSQL=1 NHTSA_TEST_MYSQL=1 STATION_ADMIN_E2E=1
STATION_ADMIN_E2E_BROWSER_CHANNEL=chrome`）；ruff check/format 全過。
新增/更新：`test_parsers_semantics.py`（空名稱列三態、圖片-only 組、變體
同 uid 不同 code、升級/去重）、`test_group_uid_identity.py`（MySQL 整合：
code 轉換對帳、變體不 merge、文字不被圖片月覆寫、1062 路徑完全不觸發
例外、legacy uid 不參與）、`test_unified_project.py`、`test_partsouq_bounded_limit.py`、
`test_catalog_http_compliance.py`（robots 雙身分）。

三輪 subAgent 懷疑性審查 + 一輪實機證據稽核；審查發現的真 bug 均已修正
並以測試 pin（跨月 closure brick、同 uid 變體被合併、text→image 名稱
銷毀、IntegrityError 吞掉已回滾 transaction）。

### 實機 E2E（2026-08-21 02:00–02:45 CST，主 DB）

最終 code 兩趟連續 sample（Toyota、`PSQ_LIMIT_PARTS=60`、單 worker）：

| 項目 | Run 1（全刷新） | Run 2（cookie 復用） |
|---|---|---|
| 起始 | 02:40:59 | 02:44:18 |
| 瀏覽器 | CloakBrowser launching | 無（reusing persisted session cookies, 43s old） |
| 結果 | DONE parts=60/60, parts_new=0 | DONE parts=60/60, parts_new=0 |
| 耗時 | ~3m08s | ~36s |
| ERROR/Traceback | 0 | 0 |

兩趟 log 均有 `44 image-only group link(s) accepted` 與
`3 part row(s) without product name skipped` 警告，不再失敗車型；
`crawl_runs.status=sample`、`parts_ok=60`；`scheduled_job_runs` exit=3
（樣本達上限的預期停止；依新語意 `success_codes=(0,)` 記為 failed ——
這是設計，不是失敗）；DB 無空名稱零件列。migration 010 已套用主 DB、
空名稱垃圾列已刪除。

### 誠實邊界（不變）

- 正式 Compose scheduler 仍是**舊 image**（透明 HTTP 路徑、無
  CloakBrowser）並持續 403 —— 正式 10,000 筆 bounded publish 仍未在
  正式環境驗證成功；host 上的 sample 是驗證 crawler 機制的證據，不是
  部署完成證明。
- robots 雙身分 AND 檢查：站方 robots.txt 目前只有 `User-agent: *`
  `Disallow: /cdn-cgi/`，兩身分皆通過；實測無阻。
- sample exit=3 在 `scheduled_job_runs` 記 `failed` 是 `dbd9f53` 後的新
  語意（正式 daemon 無 limit 時 exit=0 才算 completed）。

### SOL review 修正（2026-08-21，`7a50020` 之後）

GPT5.6 SOL MAX 對 `7a50020` 的 review 指出 1 個 P0 + 2 個 P1 資料完整性
風險 + 3 個 P2；已全數處理並提交 `6023ad4`（15 檔 +779/-54，已 push）：

1. **P0 Xvfb server-args 拆錯**（`compose.yml`）：`--server-args=-screen 0 1366x900x24`
   經 `shlex.split` 後 `0` 會被 `xvfb-run` 當成要執行的 command。改為
   `--server-args='-screen 0 1366x900x24'`（單一 argv 元素），並新增
   `test_cloak.py::test_launch_cloak_keeps_server_args_as_single_argv`
   直接驗證最終 `Popen` argv。
2. **P1 無名稱料號不可標 done**：`parse_parts` diagnostics 多回傳
   `skipped_rows`；新增 `part_quarantine` 表（migration 011 + `db/catalog.sql`）
   與 `PartRepository.quarantine_parts`；`crawl_group` 在出現無名稱列時把
   該組標 `fetched_status='partial'`（非 terminal done），下次排程重抓，
   料號不再永久漏掉。新增
   `tests/crawler/unit/test_group_closure_and_quarantine.py`（quarantine +
   partial 三種情境）。
3. **P1 group closure 改 UID→code 集合**：`list_group_identities_for_category`
   回傳 `dict[uid, set[code]]`；`crawler._group_closure_mismatches` 偵測
   「同 uid 的 code 變體消失」（review 範例 0902/0903），文字→圖片-only
   呈現降級則告警不失敗。既有 MySQL 整合測試的斷言同步更新。
4. **P2 group upsert round trip**：新增 `GroupIdentity` + `preload_group_identity`，
   `upsert_group(identity=...)` 以記憶體快取取代每組一次 image-row SELECT；
   快取隨升級/插入就地更新，正確性由 MySQL 整合測試（text→image→text、
   同 uid 變體、legacy uid）覆蓋。
5. **P2 動態 tuple 型別**：`parse_parts` diagnostics 固定為 4-tuple（不再
   依 flag 變動形狀）；`parse_groups` 維持既有 4-tuple。完全 typed result
   的大規模重構留待後續（專案 gate 為 ruff + pytest，非 mypy）。
6. **P2 Docker/docs**：Dockerfile/compose 註解改為「Linux arm64/x64」、
   scheduler 正式 image 維持共用 Dockerfile（多 stage 獨立 target 留待
   後續）；README 補 migration 011；本文件補齊提交狀態。

自動測試（本輪）：`PARTSOUQ_DB_NAME=partsouq_catalog_test +
UNIFIED_TEST_MYSQL=1 NHTSA_TEST_MYSQL=1` → **341 passed, 1 skipped**；
加 `STATION_ADMIN_E2E=1`（真實 Chrome）→ **341 passed**；ruff check/format
全過。本機 `partsouq_catalog`（主 DB）與 `partsouq_catalog_test` 均已套用
migration 011。

### SOL review 第二輪（2026-08-21，`6023ad4` 之後）

GPT5.6 SOL MAX 覆核 `6023ad4`：確認 commit/push/P0/closure/341 tests 均
正確，但指出 `partial` 語意仍有缺口（P1）+ 數個 P2，已全數處理如下
（本輪修正尚未 commit）：

1. **P1 partial 是真正 non-terminal receipt**（語意統一 + 發布 gate）：
   - `fetched_group_map()` / `is_group_fetched()` 排除 `partial`（重試/
     續爬會重抓 partial 組，不再當成本 run 已抓完）；`crawl_group` 的
     記憶體 fetched map 也不放入 partial。
   - bounded 早收（達 target）與收尾 gate、完整 run 的 success gate
     都新增檢查：`count_partial_groups(run_key)` / `count_quarantined(run_key)`
     （未處置列）/ `remaining_group_count(run_key)`（完整 run）任一 > 0
     即標 error、不 publish —— 不會再出現
     `part_quarantine > 0 + groups_t.partial + bounded_success` 的矛盾。
   - 新增 mock 層 E2E：`test_bounded_run_with_partial_group_never_publishes`、
     `test_bounded_run_with_unresolved_quarantine_never_publishes`、
     `test_full_run_requires_group_closure_for_success`；MySQL 整合測試
     `test_mysql_partial_and_quarantine_counts_gate_publish` 驗證 SQL 層
     （partial 計數、resolved 後不再計入、fetched map 排除 partial）。
   - `part_quarantine` 新增人工處置欄位 `resolved_at` / `resolution`
     （migration 012 + `db/catalog.sql`），處置流程寫入文件。
2. **P2 Xvfb 測試綁定 compose.yml**：`test_psq_cloak_launcher_env_keeps_server_args_single_argv`
   改為直接讀取 `compose.yml` 的 `PSQ_CLOAK_LAUNCHER` 值做 `shlex.split`
   斷言（不再硬編碼字串）；compose 回退舊寫法時測試會失敗。
3. **P2 多 worker 快取清除**：`crawl_vehicle` 不再清空全域
   `_group_identities`，改為只移除**本車**各 category 的快取（並行
   worker 的快取不受影響，不會反覆 preload）。
4. **P2 型別**：`parse_parts` 4-tuple 已固定（上一輪）；strict mypy 的
   249 筆診斷多為既有問題，完全 typed result 重構仍留待後續（文件化）。
5. **P2 共用 Dockerfile**：維持共用（含 Chromium），多 stage 獨立 target
   留待後續（文件化）。
6. **P2 image 實測**：`docker compose build scheduler` 成功；以
   compose 相同的 `xvfb-run -a --server-args='-screen 0 1366x900x24'`
   在容器內啟動 CloakBrowser → **CDP_READY Chrome/146.0.7680.177**、
   正常關閉（容器內 CloakBrowser 真能啟動）。
7. **P1 截斷路徑 quarantine**（第三輪 subagent 覆核發現）：quota 截斷
   （`complete_group=False`）時頁面上的無名稱列原本被靜默丟棄且 run
   仍可 bounded_success —— 已補上 quarantine（不標 partial，組未
   receipt、resume 重抓）；新增
   `test_truncated_group_still_quarantines_nameless_rows`。另把 partial
   重抓時機（同 run 失敗車重試 / 下一個 run_key）與人工處置流程
   （resolved_at + 組 receipt 需轉 done/not_found 兩步驟）寫進
   repositories docstring。

自動測試（本輪）：`PARTSOUQ_DB_NAME=partsouq_catalog_test +
UNIFIED_TEST_MYSQL=1 NHTSA_TEST_MYSQL=1` → **343 passed, 1 skipped**；
ruff check/format 全過。主 DB 與 `partsouq_catalog_test` 均已套用
migration 012。

### SOL review 第四輪（2026-08-21，`3cf22d0` 之後）

GPT5.6 SOL MAX 覆核後指出 2 個 P1 + 數個 P2；已全數處理如下（尚未
commit）：

1. **P1 驗收標準重新定義（文件化）**：原始「PartSouq 發現的每個料號
   都能 mapping 到名稱」的 100% 嚴格標準，因站方資料本身不提供名稱
   而無法達成；使用者已明示「忽略 + 紀錄」政策。現行驗收標準 =
   (a) 已發布的每一列都有名稱；(b) 發現但無法發布的料號列**全部**
   記錄在 `part_quarantine`（可查、可處置）；(c) 每次發布在 log 留下
   未處置 quarantine 數量（bounded 早收/收尾、full success 三處）。
2. **P1 本機執行環境套用最新程式**：`docker compose build scheduler`
   重建 image，並以 grep 驗證 image 內 == HEAD（無
   `count_partial_groups`、含 `resolved_at = NULL` 重開邏輯、含 admin
   quarantine endpoint）；容器內 CloakBrowser 再測 CDP ready；
   `admin`（:8000）與 `station-admin`（:8086）已啟動並回應 health。
   scheduler 未啟動（10,000 正式爬取待使用者 OK）。
3. **P1 quarantine 重現時重開處置狀態**：`quarantine_parts` 的
   ON DUPLICATE 更新增加 `resolved_at = NULL, resolution = NULL` ——
   同一料號在後續 run 再次出現時重新計入 `count_quarantined`，不會
   藏在舊的「已處置」紀錄下。MySQL 測試覆蓋（resolve → 新 run 重現
   → 重開 + 計入）。
4. **P2 快取清理**：`crawl_vehicle` 以 try/finally 在本車爬完（成功或
   失敗）後清除本車各 category 的 GroupIdentity 快取，完整全站爬取
   記憶體不再隨所有 category 持續成長。
5. **P2 migration 012 可重複執行**：改為 009/010 的條件式 procedure
   模式（information_schema 檢查 + postflight assert），已在主 DB 與
   `_test` DB 各重跑兩次驗證（rc=0）。
6. **P2 後台處置入口**：admin 新增 `GET /api/quarantine`（state/
   run_key 篩選、分頁）、`POST /api/quarantine/{id}/resolve`、
   `database-summary` 新增 `quarantine.total / unresolved` 計數；
   `admin.html` 新增 Quarantine 紀錄區塊（列表 + 「標記處置」按鈕，
   需 admin token）。
7. **P2 文件修正**：README 移除「image 不含 CloakBrowser」的過時說法
   （改為已內建並實測）；`db/catalog.sql` 的 `fetched_status` 註解
   移除舊 partial 語意（改為「partial 為歷史值，不再產生」）。

自動測試（本輪）：`tests/test_admin_quarantine.py`（5 個新測試）+ MySQL
quarantine 重開測試 + 既有 bounded/closure 測試 → **49 passed**；完整
suite 待最後一輪確認。migration 012 重跑 rc=0。

### 政策決定：無名稱列改「忽略 + 紀錄」（2026-08-21，使用者決定）

上述「partial 非 terminal + 發布 gate 阻擋」的嚴格設計，使用者覆核後
決定改為：**不完整的無名稱料號列 = 忽略 + 紀錄即可，不該讓整批
（bounded/full run）標成失敗。**

- `crawl_group` 三個無名稱路徑（整頁無名稱 / bounded resume / 完整組）
  與截斷路徑：無名稱列一律寫進 `part_quarantine` 記錄 + log warning，
  組照常標 `done`（進 fetched map、同 run 不重抓、正常發布）。
- 移除 `count_partial_groups` 與三個發布 gate（bounded 早收、bounded
  收尾、full success 的 partial/quarantine/remaining_group_count 檢查）；
  `count_quarantined` 保留為運維查詢（`resolved_at` 填上後不再計入）。
- `fetched_group_map()` / `is_group_fetched()` 恢復單純以
  `fetched_run_key` 判斷（不再排除 partial，partial 不再產生）。
- migration 011/012 的註解同步更新（partial 為歷史設計；resolved_at /
  resolution 保留為純審計紀錄）。
- 測試更新：`test_bounded_run_publishes_despite_quarantined_rows`
  （紀錄存在仍照常發布，固定此政策）、
  `test_mysql_quarantine_records_nameless_rows_without_blocking`、
  兩個 unit 測試改斷言 done + fetched map 含該組；移除三個 gate 測試。
- 影響：10,000 bounded 正式驗證不會再因無名稱列而 error；有無名稱列
  的組會以 warning 記錄在 log，料號列在 `part_quarantine` 可查詢。
