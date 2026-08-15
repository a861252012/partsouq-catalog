# 來源與整併範圍

本 repository 是新建的 monorepo，不會回寫或改名以下來源：

| 來源 | 匯入內容 | 新專案位置 |
| --- | --- | --- |
| `a861252012/partsouq-catalog-crawler` | `20e80bcea4e8b0caea34bd8c5dfd6becfc64c91b` 的 MySQL PartSouq 型錄 crawler、parser、repository、supervisor | `src/partsouq_catalog/` |
| `a861252012/partsouq-crawler` | `5de066c25cfc19c0be84719732ccce677b4923ce` 的 NHTSA MySQL sync、官方資料 parser、歷史 PartSouq SQLite 工具 | `src/partsouq_crawler/` |

新加入內容：

- `db/admin.sql`：站方後台的人工對照、分類、對帳、排程紀錄資料表。
- `src/partsouq_admin/`：FastAPI 後台與最小可用操作頁面。
- `src/partsouq_catalog/scheduler.py`：固定工作名稱的單一排程入口。
- `compose.yml`：單一 MySQL、後台與可按需執行的 scheduler service。
- `nhtsa_vin_decodes` 與 `v_vin_part_fitments`：保存已知 VIN 的官方解碼結果，並以人工確認的 PartSouq `vehicle_id` 建立零件適用關係。

整併時刻意未帶入自動 Cloudflare cookie 刷新、CloakBrowser 或其他規避功能。PartSouq 新排程採用一般 HTTP 與低速限制；challenge 會停止，不會被重新解讀成成功。

NHTSA vPIC 不提供可列舉的完整 VIN 名冊。完整 VIN 僅能由合法持有者提供後送交官方 `DecodeVinValues` 解碼；bulk complaints 內的 11 碼 VIN 欄位不會被當成完整 VIN。

官方依據：

- [NHTSA vPIC Vehicle API](https://vpic.nhtsa.dot.gov/api/)
- [NHTSA vPIC API 語言範例](https://vpic.nhtsa.dot.gov/api/Home/Index/LanguageExamples)
