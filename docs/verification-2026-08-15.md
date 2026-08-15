# 爬蟲與 mapping 驗證紀錄（2026-08-15）

## 結論

- NHTSA：合成 fixture 已驗證逐欄解析、共用 MySQL 寫入與讀回；沒有使用者授權 VIN，因此不能宣稱 live 成功解碼。
- Mapping：以 fixture 走過 PartSouq parser、repository、publish、VIN 車款確認與後台查詢，端到端成功。
- PartSouq live：正式 crawler 目前被 Cloudflare challenge 擋下，沒有取得任何線上型錄資料；狀態正確記為 `error`，不可宣稱 live 成功。

## 全新 MySQL

使用 MySQL 8.4 從空 volume 依序載入：

1. `db/catalog.sql`
2. `db/nhtsa.sql`
3. `db/admin.sql`

結果：container healthy，共 28 個資料表／檢視。`nhtsa_vin_decodes` 與 `v_vin_part_fitments` 均可查詢。

`migrations/catalog/007_unified_vin_mapping.sql` 與 `008_admin_source_ids.sql`
已在既有／fresh schema 驗證，008 也已連續重跑。唯一可證明關聯的舊
snapshot 才會回填；無法唯一判定者保留但不進 mapping view。

## 自動測試

2026-08-19 最終重跑時，在名稱以 `_test` 結尾的獨立資料庫啟用所有
MySQL 測試：

```text
117 passed, 1 warning
ruff check: passed
ruff format --check: 104 files already formatted
```

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

## 本機後台

本機 Compose 的 MySQL 與 admin 已實際啟動；`GET /api/health` 回
`{"status":"ok"}`，`GET /api/database-summary` 可回讀共用 MySQL。
目前畫面正確顯示 sample `0/1000`、最近爬取 `error`、PartSouq 與 NHTSA
皆為 0 筆，且 `requirements_met=false`。

中分類來自 PartSouq Group／diagram；目前為空是因 live crawl 在首頁即
被擋，沒有任何 Group 落庫。現有型錄路徑沒有可證明的第三層小分類來源，
所以後台明確標示為 unavailable，只能人工補充，不能算爬取成功資料。

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
parts = 0 rows
published_parts = 0 rows
```

2026-08-19 再次以一般低速 HTTP 重跑，結果仍相同。Docker runtime 時鐘已
與主機同步；最新 DB run 為 `sample-20260819T120727998635`，MySQL 以 UTC
保存 `2026-08-19 04:06:42`，狀態仍為 `error`、0 筆。

因此目前只能確認 parser／DB／mapping 的 fixture 路徑；沒有足夠證據宣稱 PartSouq 線上爬取成功。專案不解 CAPTCHA、不注入 `cf_clearance`，也不使用代理或瀏覽器指紋規避。
