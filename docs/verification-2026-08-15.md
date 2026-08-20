# 爬蟲、mapping 與站方後台驗證紀錄（更新至 2026-08-20）

## 結論

- NHTSA：合成 fixture 已驗證逐欄解析、共用 MySQL 寫入與讀回；沒有使用者授權 VIN，因此不能宣稱 live 成功解碼。
- Mapping：以 fixture 走過 PartSouq parser、repository、publish、VIN 車款確認與後台查詢，端到端成功。
- PartSouq live：一般 requests 已確認被 Cloudflare challenge 擋下（403）；
  2026-08-20 改以 CloakBrowser 產生的 session cookie（`cf_clearance` +
  `PHPSESSID`，自動刷新）附於 HTTP client 請求後，bounded E2E 與
  `partsouq-scheduler --job catalog --daemon` 無人值守執行均完成並寫入
  MySQL（`crawl_runs.status=sample`、`scheduled_job_runs=completed`）。

## 全新 MySQL

使用 MySQL 8.4 從空 volume 依序載入：

1. `db/catalog.sql`
2. `db/nhtsa.sql`
3. `db/admin.sql`
4. `db/station_admin.sql`
5. `migrations/catalog/009_bounded_production_dataset.sql`

結果：container healthy；共用 DB 同時包含型錄、NHTSA、mapping、站方後台
overlay／audit tables 與 10 個 compatibility views。`nhtsa_vin_decodes`、
`v_vin_part_fitments` 與全部 `station_admin_*` views 均可查詢。

`migrations/catalog/007_unified_vin_mapping.sql` 與 `008_admin_source_ids.sql`
已在既有／fresh schema 驗證，008 也已連續重跑。唯一可證明關聯的舊
snapshot 才會回填；無法唯一判定者保留但不進 mapping view。

## 自動測試

2026-08-20 最終重跑時，在名稱以 `_test` 結尾的獨立資料庫啟用所有
MySQL 測試：

```text
263 passed; 0 skipped; MySQL and browser E2E gates enabled; pytest exit code 0
ruff check: passed
ruff format --check: 126 files already formatted
mypy src/partsouq_station_admin: passed
```

JUnit 結果：`/private/tmp/partsouq-final-20260820.xml`。此輪以
`partsouq_final_20260820_test` 執行 shared MySQL gates；Browser E2E 另建立
隨機命名的 `_test` 資料庫，結束後刪除。

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

**2026-08-20 已切換為 CloakBrowser cookie 機制**：`cloak.py` 以真實瀏覽器
正當通過 Turnstile 後匯出 session cookie（`cf_clearance` + `PHPSESSID`，
TTL 25 分鐘自動刷新，存本機 `data/cookies.json`），HTTP client 一律附上
cookie 請求；challenge 未成功刷新時仍算 `blocked`，不報為完成。不使用
proxy 輪替或瀏覽器指紋規避。同日以 bounded E2E（Toyota model 1000）與
`partsouq-scheduler --job catalog --daemon` 無人值守執行驗證：爬蟲完成
60 筆零件寫入 MySQL，`crawl_runs.status=sample`、`scheduled_job_runs`
`catalog/daemon/completed`，daemon 依 interval 等待而非無限重試。

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
