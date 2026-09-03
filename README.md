# PartSouq Catalog

爬取 PartSouq 公開零件目錄並正規化寫入資料庫的系統（階層：品牌 → 型號 → 車款 → 零件組），整合 NHTSA VIN 解碼與台灣環境部 VNCS 車籍主檔。系統提供兩套後台管理介面、排程 daemon、版本化 migration 機制與可重放的原始證據鏈。

## 專案結構

- `src/partsouq_catalog/`：型錄爬蟲核心、排程 daemon、migration runner 與證據保存。
- `src/partsouq_admin/`：資料品質與 VIN mapping dashboard（port 8000）。
- `src/partsouq_station_admin/`：站方資料瀏覽與稽核後台（port 8086）。
- `src/partsouq_crawler/`：NHTSA vPIC 同步、VNCS 車籍擷取與輔助工具。
- `migrations/catalog/`：版本化 SQL migration（001–041）。
- `db/`：各子系統 schema 定義基準。

## 後台服務

本機啟動後提供兩個職責分工的後台：

- **站方後台**（[http://admin.partsouq.localhost:8086/](http://admin.partsouq.localhost:8086/)）：提供型錄資料瀏覽、搜尋與明細檢視。所有修改均採 overlay 機制記錄稽核軌跡，不覆寫爬蟲原始資料。
- **資料品質後台**（[http://partsouq.localhost:8000/admin](http://partsouq.localhost:8000/admin)）：處理 NHTSA 官方解碼、人工確認 VIN 與車款對照，以及維護零件中英文名稱。

詳細業務 mapping 規則、排程設定與維運流程請見 [docs/operations.md](docs/operations.md)。

## 快速開始

複製設定檔並透過 Docker 啟動服務：

```bash
cp .env.example .env
chmod 600 .env
# 請先更新 .env 中的密碼、管理 token 與金鑰

# 啟動資料庫並執行 migration
docker compose up -d mysql
docker compose --profile migration run --rm --build schema-migrate

# 啟動後台服務
docker compose up -d --build admin station-admin
```

## 驗證與測試

本機執行程式碼檢查與測試：

```bash
uv sync --extra dev
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

若需執行包含 MySQL 的端到端整合測試，請參考 [docs/operations.md](docs/operations.md) 設定測試資料庫環境變數。

## 合規邊界與免責聲明

- **資料來源**：零件目錄來自 partsouq.com 公開頁面，VIN 解碼取自美國 NHTSA vPIC 官方 API，車籍資料來自台灣環境部 VNCS。本專案與上述機構無任何隸屬或合作關係。
- **爬取邊界**：爬蟲嚴格遵守來源網站 robots.txt 與 fail-closed 原則，遇存取挑戰立即中斷。系統僅接受使用者合法持有或獲授權之 17 碼 VIN，嚴禁猜測或枚舉。
- **授權限制**：僅供個人研究與學習用途，禁止任何商業使用。採用 [PolyForm Noncommercial 1.0.0](LICENSE.md) 授權釋出。
- **免責宣告**：本專案依現狀提供，不提供任何明示或暗示保證。作者不對因使用本專案所生之損害或法律責任負責。來源權利人若對資料蒐集有異議，請以書面通知，將立即停止處理相關內容。
