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

- [http://localhost:8086/](http://localhost:8086/) 是完整站方後台。支援 10 類資料瀏覽、搜尋、明細、新增、修改、停用與復原；可選每頁 10／25／50／100／200 筆。修改以 overlay 生效，不改寫爬蟲原始資料；每次寫入都保留 actor、reason、revision 與只能追加的 audit event。也可查看監控與排入已獲授權 VIN 的解碼要求。
- [http://localhost:8000/admin](http://localhost:8000/admin) 是共用 DB 的資料品質與 API mapping dashboard，不是上述 10 類資料的完整 CRUD 後台。

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

排程只經由 `partsouq-scheduler` 執行，並把 stdout、結束碼與執行時間寫入 `scheduled_job_runs`。

```bash
# PartSouq 型錄：一般 HTTP、低速；遇到 challenge 立即停止，不會規避
docker compose run --rm scheduler partsouq-scheduler --job catalog

# 最多抓 1000 筆 PartSouq fitment sample；exit code 3 代表預期 sample 完成
docker compose run --rm -e PSQ_LIMIT_PARTS=1000 scheduler \
  partsouq-catalog-crawl --workers 1 --fresh

# NHTSA：bulk 與 allowlist API 依序執行
docker compose run --rm scheduler partsouq-scheduler --job nhtsa

# 也可個別執行
docker compose run --rm scheduler partsouq-scheduler --job nhtsa-bulk --scope all
docker compose run --rm scheduler partsouq-scheduler --job nhtsa-api --scope all

# 僅解碼一組使用者提供／獲授權的完整 VIN
docker compose run --rm scheduler partsouq-scheduler --job nhtsa-vin --scope '<使用者提供的17碼VIN>'

# 消費 8086 後台建立的 VIN／爬取要求
docker compose run --rm scheduler partsouq-scheduler --job pending
```

`PSQ_LIMIT_PARTS=0` 才是完整型錄模式。設為正整數時，爬蟲以獨立的
`sample` run 保存最多指定筆數的 normalized `parts` 關聯資料；即使上限
落在單一零件組中間，也不會清除該組舊 membership 或把該組標成完成。
Sample 不會更新 `published_parts`／`v_parts`，DB 狀態為 `sample`，CLI
exit code 為 `3`；真正錯誤仍為 `1`，完整成功才是 `0`。

來源專案已明確提供 PartSouq 每月排程，但沒有提供 NHTSA 的既定頻率。因此 repository 不會擅自啟用 cron；請依資料新鮮度與資源預算，在部署端以同一個 `partsouq-scheduler` 入口設定實際時間。8086 後台只會把要求寫入持久佇列；未另設 timer／cron 前，需執行上面的 `--job pending` 才會開始處理。

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
案例會檢查 1,000 筆分頁、編輯覆寫、停用、恢復、revision／audit event，並
直接查 DB 確認爬蟲來源列未被修改；結束後會刪除該暫存 database。Chrome
無法啟動時會直接失敗，不會把 E2E 偷偷略過。

驗證範圍包含 PartSouq parser／publish、NHTSA artifact／VIN decode、後台 mapping
API、年份區間交集、重複資料阻擋，以及站方後台的瀏覽器到 MySQL 寫入生命週期。

本次實測結果與限制見 [docs/verification-2026-08-15.md](docs/verification-2026-08-15.md)。

## 安全與資料邊界

- 不解 CAPTCHA、不注入 `cf_clearance`、不輪替 proxy、不使用 browser fingerprint 規避。
- Cloudflare challenge 是 `blocked`／失敗證據，不可報為完成。
- NHTSA bulk／collection API 與單筆 `DecodeVinValues` 分流；bulk 資料不能冒充完整 VIN 車輛名冊。
- VIN source key 使用 SHA-256，排程完成後也會遮罩 request scope 與輸出；業務表與受控 raw evidence 才保留解碼所需完整 VIN。
- `output/`、`logs/`、資料庫 volume、`.env` 與管理 token 都不提交。

來源與整併範圍見 [docs/sources.md](docs/sources.md)。
