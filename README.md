# PartSouq Catalog

將下列兩個 private repository 整併為一個專案：

- `a861252012/partsouq-catalog-crawler`：正式 PartSouq 型錄資料。
- `a861252012/partsouq-crawler`：NHTSA 官方資料同步，以及 PartSouq 歷史證據工具。

原 repository 不會被這個專案修改。

## 一個資料庫

所有正式資料都在同一個 MySQL database（預設 `partsouq_catalog`）：

| 範圍 | 主要資料表 |
| --- | --- |
| PartSouq 型錄 | `brands`、`models`、`vehicles`、`categories`、`groups_t`、`parts`、`published_parts` |
| NHTSA | `nhtsa_*`、`nhtsa_current_records`、`nhtsa_vin_decodes` |
| 站方後台 | `admin_*`、`scheduled_job_runs` |

PartSouq 的舊 SQLite 工具保留在 `partsouq_crawler` 套件中，僅作為歷史封存／原始證據處理；正式排程不以它作為資料來源。因此排程與後台不會再分散到 SQLite 與 MySQL。

## 後台

本機啟動後有兩個角色不同的後台：

- [http://admin.partsouq.localhost:8086/](http://admin.partsouq.localhost:8086/) 是完整站方後台。支援 10 類資料瀏覽、搜尋、明細、新增、修改、停用與復原；預設每頁 30 筆，可選 10／25／30／50／100／200 筆。修改以 overlay 生效，不改寫爬蟲原始資料；每次寫入都保留 actor、reason、revision 與只能追加的 audit event。零件料號／英文名稱的 active overlay 會由 8000 API 與 VIN 零件查詢讀取；也可查看監控與排入已獲授權 VIN 的解碼要求。
- [http://partsouq.localhost:8000/admin](http://partsouq.localhost:8000/admin) 是共用 DB 的資料品質與 API mapping dashboard，不是上述 10 類資料的完整 CRUD 後台。

`.localhost` 是保留給本機 loopback 的 domain，已實測兩個 health endpoint，無須修改 `/etc/hosts` 或取得 root 權限。

8086 站方後台可管理的 10 類資料為：車款配置、零件分類、圖表／Group、零件號碼、零件出現位置、適用車款、中英文零件對照、VIN 車款對照、VIN 零件適用性與對帳案件。它透過 view 讀取現有的共用 catalog，不會建第二份空的 catalog。

8000 資料品質後台可管理：

- 車輛 WMI/VDS 前綴與品牌、型號、年份、引擎、Trim 的對照。
- 對使用者提供的 17 碼 VIN 執行 NHTSA 官方解碼。
- 將解碼後的 VIN 人工確認到明確的 PartSouq `vehicle_id`。
- 零件英文、中文與常用中文名稱。
- 零件號碼與適用車型的人工補充關係。
- 顯示共用 DB 各層資料總筆數、1000 筆 sample 進度與資料品質阻擋原因。
- 零件列表採 DB 分頁，可選每頁 10／25／50／100／200 筆並直接輸入頁碼。
- 零件大分類、Group 中分類，以及僅由人工維護的小分類中文標籤。
- 對帳頻道與排程要求。

NHTSA vPIC 是「輸入已知 VIN 後解碼」，不是完整 VIN 名冊。專案只接受使用者合法持有或獲授權的 17 碼 VIN；不猜測、不掃描、不枚舉 VIN。VIN 相關讀寫 API 都需要 `X-Admin-Token`。

解碼後分開保存 `Make`、`Model`、`ModelYear`、`EngineConfiguration`、`EngineModel`、`DisplacementL` 與 `Trim`。`Trim` 是車型等級，不等於排氣量。

## 車輛與零件 mapping

PartSouq 會保存：

- `part_number`、`part_name`
- 品牌、型號、具體車款與車型代碼
- 車款生產月份區間、零件適用月份區間
- PartSouq 引擎與 Grade／Trim

系統先把品牌、型號做英數正規化，再以 NHTSA 年式落在 PartSouq 已發布生產區間內的資料列為「候選」。候選不會自動當成正確 mapping；管理者可確認 VIN 對應的 PartSouq `vehicle_id`。若兩個來源使用不同名稱，也可明確勾選人工 override，但必須留下確認依據，且目標車款仍須存在於目前已發布型錄及年份區間內。`GET /api/vins/{vin}/parts` 會分開回傳 `vehicle_mapping_status=confirmed` 與 `fitment_status=compatible_by_model_year`。後者只是年式相容判斷；沒有實際生產月份時，不宣稱零件月份已完全確認。有效期間是車款生產區間與零件適用區間的交集。

PartSouq 成功發布後，後台的 VIN mapping 列表會把已不在 current snapshot 的 `vehicle_id` 標為 `stale`；NHTSA 後續若修正同一 VIN 的品牌、型號或年式，原確認也會標為 `stale`，且不再回傳舊零件。管理者應重新查候選並以既有 mapping ID 更正；更正仍會重新驗證目標車款與年份，不能直接指定未發布車款。

## 本機啟動

```bash
cp .env.example .env
# 先把 .env 中的 password 與 PARTSOUQ_ADMIN_TOKEN 改成非預設值
docker compose up -d --build
```

MySQL 第一次初始化時會依序載入 `db/catalog.sql`、`db/nhtsa.sql`、`db/admin.sql` 與 `db/station_admin.sql`。現有 volume 不會自動重跑初始化 SQL；開發環境若要重建資料庫，先確認無需保留資料後再使用 `docker compose down -v`。

既有 volume 升級前，先停止後台、排程與 crawler，並完成資料庫備份，再執行：

```bash
docker compose exec -T mysql sh -c 'mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE"' < migrations/catalog/007_unified_vin_mapping.sql
docker compose exec -T mysql sh -c 'mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE"' < migrations/catalog/008_admin_source_ids.sql
docker compose exec -T mysql sh -c 'mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE"' < migrations/catalog/009_bounded_production_dataset.sql
docker compose exec -T mysql sh -c 'mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE"' < db/station_admin.sql
```

migration 可重複執行。它只會為能唯一證明關聯的舊 snapshot 回填
`vehicle_id` 與來源 ID；無法唯一確認的列會保留但不進 VIN mapping，須等
下一次完整成功的 PartSouq publish 更新。

`db/station_admin.sql` 可重複執行，用來建立 8086 後台的 compatibility views、overlay heads 與 append-only audit events。現有 volume 升級完後，以 `docker compose up -d --build station-admin` 啟動站方後台。

8086 首頁會分開顯示 PartSouq normalized sample、published snapshot、NHTSA current reference records 與逐 VIN decode。Sample 筆數不等於已發布筆數；NHTSA reference records 已同步也不代表已有使用者提供的 VIN decode。

PartSouq 目前可證明的型錄階層是大分類與 Group／diagram 中分類；沒有足夠
資料證明另有獨立的小分類來源，因此後台只允許人工補充，不會用其他欄位
冒充。`part_id`、`model_id`、`vehicle_id`、`category_id`、`group_id` 是共用
DB 內部 ID；`vehicle_vid`、`category_cid`、`group_uid` 是 PartSouq URL
參數；`part_code` 是站方零件表的 Code，不是車型 ID。

## 統一排程入口

本機 Compose 直接用三個常駐服務模擬 server 排程。它們共用同一個 MySQL，
但分開執行，避免數小時的 PartSouq 爬取阻塞 NHTSA 與後台佇列：

```bash
docker compose up -d --build scheduler nhtsa-scheduler queue-scheduler
```

預設排程如下；都可用同名環境變數調整，不需要人工逐次觸發：

- `scheduler`：啟動後自動執行正式 10,000 筆 bounded PartSouq crawl，之後每 30 天執行。
- `nhtsa-scheduler`：依序同步 NHTSA bulk 與 allowlist API，完成後每 24 小時執行。
- `queue-scheduler`：每 30 秒消費 8086 建立的要求；VIN 只處理使用者提供或獲授權的 17 碼值，不枚舉 VIN。

PartSouq catalog 只由專用 `scheduler` 執行；queue 會拒絕舊的 catalog 要求，
避免長時間型錄工作堵住後續 VIN。lock busy 或 scheduler 中斷的工作會保留為
pending 並自動重試；無效 VIN 等一般非零結果會結束為 failed，避免永久重試。

`SCHEDULER_CATALOG_INTERVAL_SECONDS`、`SCHEDULER_NHTSA_INTERVAL_SECONDS`、
`SCHEDULER_PENDING_INTERVAL_SECONDS` 控制頻率；失敗會從 60 秒開始指數退避，
最多等 3,600 秒。daemon 與實際 job 都使用共享 lock，同一工作不會重複執行。
排程狀態暫時讀不到時只會退避後重讀，不會直接啟動爬蟲；NHTSA bulk 已完成、
API 失敗時，下一輪會從 API stage 續跑，不會重抓同一批 bulk sources。
每次子程序會即時輸出 Docker log，並把最後 60,000 字、結束碼、執行時間與
`manual`／`daemon`／`queue` 來源寫入 `scheduled_job_runs`。PartSouq
`crawl_runs.scheduled_job_run_id` 會保存實際 scheduler run；正式資料只接受
`daemon`，不能用手動或 sample run 冒充。若 bounded 資料已完整 commit、但
scheduler 在完成紀錄前中斷，下一個 daemon 會先核對筆數與關聯並補記完成，
不會重爬同一批 10,000 筆。

檔案 lock 適用本機單主機，三個 Compose 服務必須共用 `./logs` volume。若改為
多主機部署，需另外使用跨主機的 DB lease，不能把這套 flock 當成分散式鎖。

Compose 明確使用 `PSQ_BOUNDED_PARTS=10000` 並覆寫 `PSQ_LIMIT_PARTS=0`。
bounded dataset 必須精確 10,000 筆且通過來源／欄位／關聯品質關卡才會
`bounded_success` 與 exit `0`；它是可驗證的正式限量資料，不冒充全站完整 snapshot。

`PSQ_LIMIT_PARTS` 只保留給隔離測試。設為正整數時，爬蟲以獨立的
`sample` run 保存最多指定筆數的 normalized `parts` 關聯資料；即使上限
落在單一零件組中間，也不會清除該組舊 membership 或把該組標成完成。
Sample 不會更新 `published_parts`／`v_parts`，DB 狀態為 `sample`，CLI
exit code 為 `3`；真正錯誤仍為 `1`，完整成功才是 `0`。

來源專案有 PartSouq 每月排程，沒有指定 NHTSA 頻率。本機 Compose 明確採用
「PartSouq 每 30 天、NHTSA 每 24 小時、後台佇列每 30 秒」作為可重現的部署值；
正式環境可用前述環境變數調整。這是本專案的部署選擇，不宣稱是來源網站規定。

## 驗證

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

MySQL 端到端測試需使用名稱以 `_test` 結尾的獨立資料庫。以下流程會刪除並重建明確命名的 `partsouq_catalog_test`，不可改成正式資料庫名稱：

```bash
set -a
. ./.env
set +a

docker compose exec -T mysql sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -e "DROP DATABASE IF EXISTS partsouq_catalog_test; CREATE DATABASE partsouq_catalog_test CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; GRANT ALL PRIVILEGES ON partsouq_catalog_test.* TO \`$MYSQL_USER\`@\`%\`;"'
docker compose exec -T mysql sh -c 'mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" partsouq_catalog_test' < db/catalog.sql
docker compose exec -T mysql sh -c 'mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" partsouq_catalog_test' < db/nhtsa.sql
docker compose exec -T mysql sh -c 'mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" partsouq_catalog_test' < db/admin.sql
docker compose exec -T mysql sh -c 'mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" partsouq_catalog_test' < db/station_admin.sql

PARTSOUQ_DB_NAME=partsouq_catalog_test \
NHTSA_TEST_MYSQL=1 \
UNIFIED_TEST_MYSQL=1 \
STATION_ADMIN_E2E=1 \
STATION_ADMIN_E2E_BROWSER_CHANNEL=chrome \
uv run pytest
```

`STATION_ADMIN_E2E=1` 會另外建立一個隨機命名且以 `_test` 結尾的暫存
MySQL database，啟動真實 HTTP server，再用本機 Chrome 操作 8086 站方後台。
案例會檢查 1,000 筆分頁、編輯覆寫、來源版本衝突、停用、恢復、revision／audit event，並
直接查 DB 確認爬蟲來源列未被修改；結束後會刪除該暫存 database。Chrome
無法啟動時會直接失敗，不會把 E2E 偷偷略過。

驗證範圍包含 PartSouq parser／publish、NHTSA artifact／VIN decode、後台 mapping
API、年份區間交集、重複資料阻擋，以及站方後台的瀏覽器到 MySQL 寫入生命週期。

本次實測結果與限制見 [docs/verification-2026-08-15.md](docs/verification-2026-08-15.md)。
簡報逐項需求邊界見
[docs/pptx-requirements-audit-2026-08-20.md](docs/pptx-requirements-audit-2026-08-20.md)。

## 安全與資料邊界

- 不輪替 proxy、不使用 browser fingerprint 規避；Cloudflare challenge 一律以
  CloakBrowser（`cloak.py`）產生的 session cookie（`cf_clearance` + `PHPSESSID`）
  正當放行，cookie 自動刷新且只存在本機 `data/cookies.json`，不會提交。
- Cloudflare challenge 未成功刷新時是 `blocked`／失敗證據，不可報為完成。
- NHTSA bulk／collection API 與單筆 `DecodeVinValues` 分流；bulk 資料不能冒充完整 VIN 車輛名冊。
- VIN source key 使用 SHA-256，排程完成後也會遮罩 request scope 與輸出；業務表與受控 raw evidence 才保留解碼所需完整 VIN。
- `output/`、`logs/`、資料庫 volume、`.env` 與管理 token 都不提交。

來源與整併範圍見 [docs/sources.md](docs/sources.md)。
