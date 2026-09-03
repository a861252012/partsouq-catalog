# PartSouq Catalog

爬取 PartSouq 公開零件目錄並正規化落庫的系統：品牌 → 型號 → 車款 → 零件組，
整合 NHTSA 官方 VIN 解碼同步與台灣 MOENV VNCS 車籍主檔，提供兩套後台管理、
排程 daemon、版本化 migration 與可重放的 parser 輸入證據鏈。

## 架構

- `src/partsouq_catalog/`：型錄爬蟲主系統（crawler、scheduler、migration runner、evidence）。
- `src/partsouq_admin/`：資料品質與 VIN mapping dashboard（port 8000）。
- `src/partsouq_station_admin/`：站方資料瀏覽後台（port 8086）。
- `src/partsouq_crawler/`：NHTSA vPIC 同步、VNCS 收割器與歷史證據工具。
- `migrations/catalog/`：版本化 SQL migration（001–041，checksum manifest 釘住）。
- `db/`：各子系統 schema 基準。

## 一個資料庫

所有正式資料都在同一個 MySQL database（預設 `partsouq_catalog`）：

| 範圍 | 主要資料表 |
| --- | --- |
| PartSouq 型錄 | 原始正規化資料：`brands`、`models`、`vehicles`、`categories`、`groups_t`、`parts`；正式讀取：`bounded_parts`、`v_current_catalog_parts` |
| NHTSA | `nhtsa_*`、`nhtsa_current_records`、`nhtsa_vin_decodes` |
| 站方後台 | `admin_*`、`scheduled_job_runs` |

## 後台與排程

- [http://admin.partsouq.localhost:8086/](http://admin.partsouq.localhost:8086/) 是站方後台：10 類資料的瀏覽、搜尋與明細；可寫類型以 overlay 修改，保留 actor、reason、revision 與 audit event，不改寫爬蟲原始資料。
- [http://partsouq.localhost:8000/admin](http://partsouq.localhost:8000/admin) 是資料品質與 VIN mapping dashboard：NHTSA 官方解碼、VIN 對照人工確認、零件中英文對照與各層資料筆數。
- 排程：catalog 每 30 天、NHTSA 每 24 小時、後台佇列每 30 秒。正式資料只接受 daemon 來源；手動或 sample run 不會被當成正式資料。
- VIN 只接受使用者合法持有或獲授權的 17 碼值；不猜測、不掃描、不枚舉。

mapping 語意、本機啟動、migration 升級流程與 LaunchAgent 安裝等操作細節見
[docs/operations.md](docs/operations.md)。

## 驗證

GitHub Actions 僅保留手動 `workflow_dispatch`，push 與 PR 不會自動執行。本機 gate：

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

MySQL 端到端測試需使用名稱以 `_test` 結尾的獨立資料庫，完整流程見
[docs/operations.md](docs/operations.md)。驗證範圍包含 PartSouq parser／publish、
NHTSA artifact／VIN decode、後台 mapping API、年份區間交集、重複資料阻擋，
以及站方後台的瀏覽器到 MySQL 寫入生命週期。

## 安全與資料邊界

- PartSouq catalog 請求前先以可識別 crawler UA 檢查 robots.txt 與 origin；robots
  無法確認允許、origin 不符或 redirect 一律停止（fail-closed），不跟隨 redirect。
- Cloudflare challenge 的處理方式：`cloak.py` 啟動 CloakBrowser，等待並驗證實際
  型錄頁。逾時或仍停在 challenge 頁時不匯出 cookie，並以失敗／退避收尾。cookie
  只存本機 `data/cookies.json`，不會提交。
- NHTSA bulk／collection API 與單筆 `DecodeVinValues` 分流；bulk 資料不能冒充
  完整 VIN 車輛名冊。
- `output/`、`logs/`、資料庫 volume、`.env` 與管理 token 都不提交。

## 資料來源、用途限制與免責聲明

- 零件目錄資料來自 partsouq.com 的公開頁面；VIN 解碼來自美國 NHTSA 官方
  公開 API（vPIC）；台灣車籍資料來自 MOENV VNCS 公開服務。本專案與上述
  來源網站及機構沒有隸屬或合作關係，其內容的權利歸各自權利人所有。
- 本專案僅供個人研究、學習與教育用途，禁止任何營利或商業使用。授權條款
  見 [LICENSE.md](LICENSE.md)（PolyForm Noncommercial 1.0.0）。
- 本專案按現狀提供，不附帶任何擔保。作者不對任何人因安裝、執行或使用
  本專案而產生的損害或法律爭議負責，也不為資料的正確性、完整性或時效性
  背書；引用或使用資料前請自行查證，並遵守所在地法規。
- 爬蟲以可識別的 UA 請求來源網站，請求前檢查 robots.txt，遇挑戰或無法
  確認允許即停止；不枚舉 VIN、不猜測個人資料。若來源網站的權利人對本
  專案的資料蒐集有異議，請以書面通知，相關頁面會停止處理。
