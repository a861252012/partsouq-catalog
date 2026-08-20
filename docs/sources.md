# 來源與整併範圍

本 repository 是新建的 monorepo，不會回寫或改名以下來源：

| 來源 | 匯入內容 | 新專案位置 |
| --- | --- | --- |
| `a861252012/partsouq-catalog-crawler` | `20e80bcea4e8b0caea34bd8c5dfd6becfc64c91b` 的 MySQL PartSouq 型錄 crawler、parser、repository、supervisor | `src/partsouq_catalog/` |
| `a861252012/partsouq-crawler` | `5de066c25cfc19c0be84719732ccce677b4923ce` 的 NHTSA MySQL sync、官方資料 parser、歷史 PartSouq SQLite 工具 | `src/partsouq_crawler/` |
| `a861252012/partsouq-crawler` 未合併分支 `agent/mysql-admin-archive-import` | `f3c0cc2dfae7191b5f9b7e75c9a14a504439774f` 的 Flask/Jinja 站方 CRUD、人工覆寫、audit、來源追溯與監控介面 | `src/partsouq_station_admin/`（改接本專案共用 MySQL schema） |

新加入內容：

- `db/admin.sql`：站方後台的人工對照、分類、對帳、排程紀錄資料表。
- `src/partsouq_admin/`：FastAPI 後台與最小可用操作頁面。
- `src/partsouq_station_admin/`：原分支的完整站方資料編輯後台；人工修改寫入 overlay 與 append-only audit，不直接改爬蟲來源列。
- `src/partsouq_catalog/scheduler.py`：固定工作名稱的單一排程入口。
- `compose.yml`：單一 MySQL、後台與可按需執行的 scheduler service。
- `nhtsa_vin_decodes` 與 `v_vin_part_fitments`：保存已知 VIN 的官方解碼結果，並以人工確認的 PartSouq `vehicle_id` 建立零件適用關係。

後續以 CloakBrowser（`src/partsouq_catalog/cloak.py`）正當放行 Cloudflare
challenge：自動通過 Turnstile 後匯出 session cookie（`cf_clearance` +
`PHPSESSID`，25 分鐘 TTL 自動刷新，只存本機 `data/cookies.json`），HTTP
client 一律附上 cookie 請求；challenge 未成功刷新時仍算 `blocked`，不會被
重新解讀成成功。不使用 proxy 輪替或 browser fingerprint 規避。

NHTSA vPIC 不提供可列舉的完整 VIN 名冊。完整 VIN 僅能由合法持有者提供後送交官方 `DecodeVinValues` 解碼；bulk complaints 內的 11 碼 VIN 欄位不會被當成完整 VIN。

官方依據：

- [NHTSA vPIC Vehicle API](https://vpic.nhtsa.dot.gov/api/)
- [NHTSA vPIC API 語言範例](https://vpic.nhtsa.dot.gov/api/Home/Index/LanguageExamples)
