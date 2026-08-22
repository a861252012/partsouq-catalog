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

後續加入 CloakBrowser 機制（`src/partsouq_catalog/cloak.py`）處理 Cloudflare
challenge：啟動 Chromium 並等待實際 PartSouq 型錄頁；只有頁面 URL 與完整
品牌連結同時通過驗證後才匯出 session cookie（25 分鐘 TTL，自動刷新，只存
本機 `data/cookies.json`）。仍停在 challenge 頁、逾時，或刷新後的 HTTP
follow-up 仍被拒時，都會 fail-closed，不把 `cf_clearance` 存在誤報為成功，
也不在同一請求反覆重啟瀏覽器。不輪替 proxy。正式
catalog 請求仍先檢查 robots.txt 與 origin（fail-closed），不跟隨 redirect。
目前 Compose image 已安裝 CloakBrowser runtime、Chromium 與 Xvfb 顯示
環境，容器內以 `xvfb-run` 啟動。browser readiness marker 只證明瀏覽器已啟動，不能證明
型錄頁或 cookie 可被 HTTP transport 接受；原生 host 的 Python venv 路徑不能在 container 中使用。
目前本機 PartSouq 正式排程走受限於 Aqua session 的 host LaunchAgent；Linux
Compose scheduler 保留 fail-closed，需先通過相同 live smoke 才能取代 host 路徑。

NHTSA vPIC 不提供可列舉的完整 VIN 名冊。完整 VIN 僅能由合法持有者提供後送交官方 `DecodeVinValues` 解碼；bulk complaints 內的 11 碼 VIN 欄位不會被當成完整 VIN。

官方依據：

- [NHTSA vPIC Vehicle API](https://vpic.nhtsa.dot.gov/api/)
- [NHTSA vPIC API 語言範例](https://vpic.nhtsa.dot.gov/api/Home/Index/LanguageExamples)
