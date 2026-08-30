# PartSouq Catalog

將下列兩個 repository 整併為一個專案：

- `a861252012/partsouq-catalog-crawler`：正式 PartSouq 型錄資料。
- `a861252012/partsouq-crawler`：NHTSA 官方資料同步，以及 PartSouq 歷史證據工具。

原 repository 不會被這個專案修改。

## 一個資料庫

所有正式資料都在同一個 MySQL database（預設 `partsouq_catalog`）：

| 範圍 | 主要資料表 |
| --- | --- |
| PartSouq 型錄 | 原始正規化資料：`brands`、`models`、`vehicles`、`categories`、`groups_t`、`parts`；正式讀取：`bounded_parts`、`v_current_catalog_parts` |
| NHTSA | `nhtsa_*`、`nhtsa_current_records`、`nhtsa_vin_decodes` |
| 站方後台 | `admin_*`、`scheduled_job_runs` |

PartSouq 的舊 SQLite 工具保留在 `partsouq_crawler` 套件中，僅作為歷史封存／原始證據處理；正式排程不以它作為資料來源。因此排程與後台不會再分散到 SQLite 與 MySQL。

## 後台

本機啟動後有兩個角色不同的後台：

- [http://admin.partsouq.localhost:8086/](http://admin.partsouq.localhost:8086/) 是站方後台。支援 10 類資料的瀏覽、搜尋與明細；可寫類型以 overlay 新增、修改、停用與復原，絕不改寫爬蟲原始資料。VIN 車款對照是唯讀衍生資料，解碼與人工確認請使用 8000 的專用流程。列表預設每頁 30 筆，可選 10／25／30／50／100／200 筆。每次 overlay 寫入都保留 actor、reason、revision 與只能追加的 audit event。
- [http://partsouq.localhost:8000/admin](http://partsouq.localhost:8000/admin) 是共用 DB 的資料品質與 API mapping dashboard，不是上述 10 類資料的完整 CRUD 後台。

`.localhost` 是保留給本機 loopback 的 domain，已實測兩個 health endpoint，無須修改 `/etc/hosts` 或取得 root 權限。

8086 站方後台可瀏覽的 10 類資料為：車款配置、零件分類、圖表／Group、零件號碼、零件出現位置、適用車款、中英文零件對照、VIN 車款對照、VIN 零件適用性與對帳案件。它透過正式 view 讀取共用 catalog，不會建第二份空的 catalog，也不會直接曝露未驗證的 raw 列。

8000 資料品質後台可管理：

- 車輛 WMI/VDS 前綴與品牌、型號、年份、引擎、Trim 的對照。
- 對使用者提供的 17 碼 VIN 執行 NHTSA 官方解碼。
- 將解碼後的 VIN 人工確認到明確的 PartSouq `vehicle_id`。
- 零件英文、中文與常用中文名稱。
- 零件號碼與適用車型的人工補充關係。
- 顯示共用 DB 各層資料總筆數、環境變數指定的 sample／bounded 目標進度與資料品質阻擋原因。
- 零件列表採 DB 分頁，可選每頁 10／25／30／50／100／200 筆並直接輸入頁碼。
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

全新 MySQL volume：

```bash
cp .env.example .env
chmod 600 .env
# 先把 .env 中所有 password、token 與 session secret 改成非預設值
docker compose up -d mysql
docker compose --profile migration run --rm --build schema-migrate
docker compose up -d --build admin station-admin
```

MySQL 第一次初始化時會依序載入 `db/catalog.sql`、`db/nhtsa.sql`、
`db/admin.sql` 與 `db/station_admin.sql`。接著必須執行一次明確的
`schema-migrate`，建立 migration ledger、驗證固定 checksum，並安全重播可重複
執行的 migration。Compose 後台與三個 container scheduler 每次啟動前只會執行
read-only 的 migration ledger／checksum gate，不會自行套 DDL。host catalog
LaunchAgent 是例外：catalog daemon 會持續持有本機 daemon lock，並在寫入 ready
marker 前由 migration runner 依序驗證既有 ledger、回收可證明屬於本機舊程序的
逾時 marker、套用尚未執行的版本，最後才啟動正式爬蟲。
現有 volume 不會自動重跑初始化 SQL；開發環境若要重建資料庫，
先確認無需保留資料後再使用 `docker compose down -v`。
Compose 只把各服務需要的變數傳入 container：root 密碼只給 MySQL，8000 token
只給 admin，8086 的 session／登入密碼只給 station-admin。三個 scheduler、兩套後台、
NHTSA 與 PartSouq 仍共用同一個 `partsouq_catalog`。

`schema-migrate` 仍只使用 app 帳號。因 migration 033 會建立 deterministic trigger，
Compose 的 MySQL 已明確設定 `log_bin_trust_function_creators=1`；這不會把 root 密碼
交給 migration、scheduler 或後台。若使用自行管理的 MySQL，DB 管理者必須以等效的
server 啟動設定或全域設定啟用此項，再執行 migration；否則 MySQL 開啟 binary log 時
會以 errno 1419 安全拒絕建立 trigger。

既有 MySQL volume 不要先啟動後台；先只啟動資料庫，再完成下列升級與 health
check：

```bash
docker compose up -d mysql
```

runner 會以固定 manifest 從 001 開始檢查並重播尚未記錄的 active migration
（001–012、015–035），不靠人工猜測既有 volume 的版本。013／014 已被 015
取代，不會在新升級執行。
migration 019 會讓正式 bounded view fail closed：除了精確 10,000 筆與成功的
daemon provenance，還必須有已 seal 的 live HTTP evidence、六種頁面類型與逐筆
accepted part coverage；resume 的每個 scheduler attempt 也必須符合各自的 daemon
狀態與擷取時間窗。沒有 evidence 或只有 fixture evidence 的資料仍可供原始
診斷查詢，但不會出現在正式後台 view 或 VIN mapping。
這個 evidence gate 目前只涵蓋 bounded 10,000；既有 full snapshot 分支仍只有
scheduler provenance，尚未保存同級 live HTTP evidence，不在本次 10,000 筆正式
驗收範圍，也不能據此宣稱 full crawl 已完成 live evidence 驗證。
migration 020 會把 sanitizer 版本記到每筆 HTTP artifact；不相容的舊證據會
自動改為 rejected，排程也會建立新的 bounded run，不把不同 sanitizer 版本
混成同一份正式驗收 manifest。
migration 021 會用大小寫與尾端空白都精確的版本比對，並禁止非目前 sanitizer
版本的 artifact 保持 verified，讓 resume、正式 view 與 Python verifier 一致 fail closed。
migration 022 會為 group terminal receipt 的 run key 建立索引，避免 resume 相容性
檢查隨型錄資料量成長而退化成全表掃描；舊版留下的非終態或非 canonical URL
receipt 也會讓所屬未完成 bounded run 永久標為 rejected，不會被後續 URL 更新洗白。
同一 migration 會把 run 與 artifact 的 evidence 狀態欄位收斂成 byte-exact 契約；
大小寫變體、pending／rejected artifact 或不相容 sanitizer 都不能被當成可續跑證據。
migration 023 將真正 HTTP 404 與 HTTP 200／零解析的已清洗 HTML
存在獨立 diagnostics table，不納入正式 evidence；同 run／group／reason
只保留最新一筆，且持續排除 SSD、cookie 與 headers。只有 transport
HTTP 404 可寫 `not_found` receipt；HTTP 200 錯誤頁或零解析仍 fail closed。
migration 028 只更新 `v_vin_part_fitments`。既有 volume 套用後，人工確認的
partial NHTSA decode 才能產生 VIN 零件適配；年份或車款快照不一致時仍不回傳零件。
migration 029 讓正式 bounded run 保存單一品牌、型號與執行當下凍結的年份下限；
舊 run 三欄皆為 `NULL` 時維持既有未限定 scope 的 evidence hash 語意。
migration 030 將目前啟用的 bounded scope 存成資料庫 singleton，讓 scheduler、
正式 view 與兩個後台讀取同一份 scope；尚未設定 scope 時正式 bounded 資料為空。
migration 031 只讓具備已驗證 evidence、精確 scope 與精確 10,000 筆的 bounded
snapshot 進入 `v_current_catalog_parts`；raw／full candidate 不會被當成正式資料。
migration 029 前建立的 bounded snapshot 沒有 scope；套用 030／031 後會刻意從
正式 view 隱藏，不是資料遺失。必須由新的 scoped 10,000 筆 daemon run 重新發布。
migration 032 將每筆 bounded snapshot 列與接受的 evidence record digest 綁定。
migration 033 禁止直接更新 `bounded_parts`，避免已發布 snapshot 被事後改寫；新版
發布仍使用 transaction 內的 delete＋insert。
migration 034 將 VIN fitment 的一般候選數計算改為 CTE，一次計算 mapping 候選，
避免對每筆零件重複計數；manual override 的既有語意不變。
migration 035 只會移除已綁 evidence digest、但 normalized 料號無法由 snapshot
原始料號重算的 legacy 列；因此受影響的舊 10,000 筆 snapshot 也會 fail closed，
直到新的 verified run 發布。
migration 005 若判定舊 vehicle tree 必須重建，仍會在刪除前 fail closed，且只接受
備份後由操作者明確授權。

升級前先記錄目前 running services，停止後台、排程與 crawler：

```bash
docker compose ps --services --filter status=running
docker compose stop admin station-admin scheduler nhtsa-scheduler queue-scheduler
```

確認沒有額外執行中的 crawler／one-off writer，並完成資料庫備份。若 015
需要修復大型既有索引，還要先確認 MySQL 資料卷與
temporary directory 有足夠空間。先查實際路徑與資料量，再對查到的路徑
執行 `df -h`；online index repair 可先以該表資料與索引總和作為估算
基準，並另外保留餘裕：

```bash
docker compose exec -T mysql sh -c 'mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" -e "SELECT @@tmpdir AS tmpdir, @@innodb_tmpdir AS innodb_tmpdir; SELECT COUNT(*) AS part_quarantine_exists, MAX(DATA_LENGTH) AS data_bytes, MAX(INDEX_LENGTH) AS index_bytes, MAX(DATA_LENGTH + INDEX_LENGTH) AS estimated_bytes FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = '\''part_quarantine'\'';" "$MYSQL_DATABASE"'
docker compose exec mysql df -h /tmp /var/lib/mysql
```

若 SQL 顯示的 temporary directory 不是 `/tmp`，第二行要改查該實際路徑。
第一行若顯示 `part_quarantine_exists=0`，代表尚未執行 011，可略過下列
FULLTEXT preflight；若為 1，則執行：

```bash
docker compose exec -T mysql sh -c 'mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" -e "SHOW EXTENDED INDEX FROM part_quarantine WHERE Key_name = '\''FTS_DOC_ID_INDEX'\'' AND Column_name = '\''FTS_DOC_ID'\''; SET @hidden_fts = FOUND_ROWS(); SET @owned_fulltext = (SELECT COUNT(DISTINCT INDEX_NAME) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = '\''part_quarantine'\'' AND INDEX_TYPE = '\''FULLTEXT'\'' AND INDEX_NAME IN ('\''idx_quarantine_list'\'', '\''idx_quarantine_run_key_resolved_updated'\'', '\''idx_quarantine_run_key_updated'\'', '\''idx_quarantine_resolved'\'', '\''idx_quarantine_group'\'')); SET @visible_fulltext = (SELECT COUNT(DISTINCT INDEX_NAME) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = '\''part_quarantine'\'' AND INDEX_TYPE = '\''FULLTEXT'\''); SELECT @owned_fulltext AS owned_fulltext, @visible_fulltext AS visible_fulltext, @hidden_fts AS hidden_fts, IF(@owned_fulltext > 0 OR (@hidden_fts > 0 AND @visible_fulltext = 0), '\''BLOCK_REBUILD_REQUIRED'\'', '\''OK'\'') AS preflight_status;" "$MYSQL_DATABASE"'
```

最後的 `preflight_status` 必須是 `OK` 才可繼續。若為
`BLOCK_REBUILD_REQUIRED`，先停止升級並另排維護時段重建該表。不可先執行
013／014，否則表面索引可能已修正，但 hidden FTS artifacts 仍會留下。
preflight 通過後執行唯一的 migration 入口：

```bash
docker compose --profile migration run --rm --build schema-migrate
```

runner 會先在記憶體讀完所有 SQL、驗證固定 raw-byte SHA-256 與 statement
邊界，接著取得 database-specific advisory lock。未知 ledger 版本、檔案缺漏、
checksum 漂移或前一次 `applying`／`failed` 都會在下一個 migration DDL 前停止。
新版 scheduler、直接 PartSouq／NHTSA 入口與 legacy supervisor 在建立或修復
`running` marker 前，會短暫取得同一把 admission lock；marker commit 後立即釋放，
不會把整趟爬蟲序列化。第一次升級仍必須先停止所有舊版 writer，因為舊 binary
不認得這把鎖。
MySQL DDL 會 implicit commit，因此失敗後不可手動把 ledger 改成成功；先修正根因，
再精確指定 dirty 版本，從該檔第一句完整重播：

若舊 `crawl_runs` 仍是 `running`，但它連結的 catalog scheduler 已明確是
`failed`，且有 `finished_at`、非零 `exit_code`，並且該 scheduler 只連到一筆
catalog run，runner 才會在 ledger preflight 通過後以 CAS 自動改成
`interrupted`，保留原錯誤並附加修復原因。缺關聯、重複關聯、scheduler
仍在執行、已完成卻留下 running crawl，或其他不一致狀態都會 fail closed，不會
自動改資料。

```bash
docker compose --profile migration run --rm --build schema-migrate \
  partsouq-catalog-migrate apply --retry 15
```

若 migration 005 明確要求重建，完成備份與維護確認後才可執行：

```bash
docker compose --profile migration run --rm --build schema-migrate \
  partsouq-catalog-migrate apply --retry 5 --allow-v5-rebuild
```

`db/station_admin.sql` 有獨立 checksum state。只有 manifest 內已知的舊 checksum
可以升級；未知 checksum 一律視為 drift。檔案更新時 runner 會在 catalog migration
全部完成後重套；若中斷，同樣必須明確重試：

```bash
docker compose --profile migration run --rm --build schema-migrate \
  partsouq-catalog-migrate apply --retry-station-asset
docker compose --profile migration run --rm --build schema-migrate \
  partsouq-catalog-migrate check
```

升級完成後，只恢復升級
前原本 running 的服務；不要因本段文件而啟動原先未執行的 scheduler。
若升級前兩套後台都有執行，可使用
`docker compose up -d --build admin station-admin`。恢復後要核對原 running
services；以下 health check 只執行原本已啟動的後台：

```bash
docker compose ps --services --filter status=running
curl --fail --silent --show-error --retry 10 --retry-connrefused --retry-delay 1 http://partsouq.localhost:8000/api/health
curl --fail --silent --show-error --retry 10 --retry-connrefused --retry-delay 1 http://admin.partsouq.localhost:8086/health
```

啟動前的 gate 驗證 immutable migration 檔、ledger 與 station schema asset checksum，
不宣稱能偵測任意人工 schema drift。兩套後台的必要 table／view／index readiness 由
各自 `/health` 驗證；隔離 MySQL migration gate則負責實際重播與資料保留。

8086 首頁會分開顯示 PartSouq normalized sample、published snapshot、NHTSA current reference records 與逐 VIN decode。Sample 筆數不等於已發布筆數；NHTSA reference records 已同步也不代表已有使用者提供的 VIN decode。

PartSouq 目前可證明的型錄階層是大分類與 Group／diagram 中分類；沒有足夠
資料證明另有獨立的小分類來源，因此後台只允許人工補充，不會用其他欄位
冒充。`part_id`、`model_id`、`vehicle_id`、`category_id`、`group_id` 是共用
DB 內部 ID；`vehicle_vid`、`category_cid`、`group_uid` 是 PartSouq URL
參數；`part_code` 是站方零件表的 Code，不是車型 ID。

## 統一排程入口

三個排程共用同一個 MySQL，但分開執行，避免數小時的 PartSouq 爬取阻塞
NHTSA 與後台佇列。Compose 內的排程都屬 opt-in `scheduler` profile；一般
`docker compose up -d` 不會啟動爬蟲：

```bash
docker compose up -d --build nhtsa-scheduler queue-scheduler
```

Compose image 已包含 CloakBrowser runtime、Chromium、Xvfb 與顯示環境
（Linux arm64/x64 版本），scheduler 在容器內以
`xvfb-run -a --server-args='-screen 0 1366x900x24'` 啟動 CloakBrowser；
`PSQ_CLOAK_PYTHON` 指向容器內 `/usr/local/bin/python`，不使用 macOS
host 路徑。browser readiness marker 只代表瀏覽器已啟動，不代表 Cloudflare
challenge 已通過；只有實際型錄頁出現完整品牌連結後才接受並保存 session cookie。
2026-08-22 實測同一網路下，Linux/Xvfb image 仍停在 challenge（0 個品牌連結），
macOS host 則取得 18 個品牌連結，且 cookie 交給 HTTP client 後可讀回完整型錄頁。
因此目前本機正式 PartSouq 排程應使用 Aqua LaunchAgent 在 host 執行；Compose
`scheduler` 只保留給未來已通過相同 smoke 的 Linux 環境，不得只以 browser ready
或 cookie 檔存在就啟動 10,000 筆驗收。

host 安裝入口是 `deploy/install-macos-catalog-scheduler.zsh`。installer 只在互動
安裝時以隔離子程序讀取 repository 的 `.env`，只輸出固定 DB／crawler allowlist，
不會讓 `.env` 改寫 installer 控制變數；同時拒絕含未提交 tracked 變更的 source tree。
接著從目前 commit 封裝 `pyproject.toml`、`uv.lock`、`README.md`、`src/`、`db/`、
`migrations/` 與 `deploy/` 到 owner-private 的
`~/Library/Application Support/partsouq-catalog/releases/`。release 不含 `.git`、
`.env`、tests 或 GitHub 認證資料，依賴分別以 `uv sync --locked` 與
`pip --require-hashes` 安裝在 release 的最終路徑，避免移動 venv 後 shebang 失效。

installer 只將 DB 與 crawler allowlist 寫入 owner-only
`scheduler.env`；MySQL root、8000 token、8086 認證與 GitHub 資訊都不會進入
runtime。CloakBrowser 使用獨立的 free-only cache；一般 host CLI 也不讀取預設
`~/.cloakbrowser` 的 license，且不把 proxy 或自訂 TLS 設定傳給 browser child。
付費 key、Pro cache artifact、
token、外部自訂 binary／下載網址、proxy 與自訂 TLS 路徑都會被拒絕。installer
建立新 release 時先用空 cache 呼叫 hash-locked CloakBrowser 的官方簽章下載流程，
既有同版本目錄與 `latest_version*` marker 會先移到 owner-only quarantine；只有帶有
trusted marker、且 manifest 完整相符的同 commit release 才能重用。installer 驗證
free browser checksum 後，runner 才以內部固定 binary path 啟動；這不是開放使用者
覆寫；binary resolve 後必須位於 dedicated cache 的 `chromium-*` 非 Pro 版本目錄。
checksum 不符的目標 free version 會移到 owner-only quarantine，再由
hash-locked CloakBrowser 重新下載及複驗，其他 cache 不會被刪除。正式排程硬性要求既有
`partsouq_catalog`，並覆寫為
`PSQ_LIMIT_PARTS=0` 與 `PSQ_BOUNDED_PARTS=10000`，不能被舊 sample 設定或
`_test` DB 降級。cookie／refresh lock 與 scheduler lock 固定共用上述
Application Support 目錄，避免不同 checkout 同時啟動瀏覽器或覆寫 cookie。

headed Chromium 必須從 macOS Aqua session 啟動；LaunchAgent 除了
`LimitLoadToSessionType=Aqua`，也會傳入 host runner marker。plist 與 runner
只讀取 Application Support 內的 release，不再讀取 Desktop／Documents 內的
repository，因此不需要替 LaunchAgent 手動授予 Full Disk Access。runner 仍會
拒絕未帶 marker、SSH、CI 與 Codex sandbox 直接執行，避免 AppKit 以 `SIGABRT`
結束 Chromium 並跳出「未預期的結束」提示。
正式 runner 只支援此本機 LaunchAgent 作為 catalog daemon owner；Compose catalog
scheduler 不得同時啟動。它會同時持有 daemon/job flock，並在 migration checksum
與既有 ledger 完整通過後，才回收唯一一筆逾時的 daemon marker。近期、複數、無法
唯一連結的 marker，或任何 NHTSA／後台 writer 都會 fail closed，不會被誤改。

```bash
# 建立 hash-locked release、render／lint 並安裝 plist，不啟動爬蟲：
deploy/install-macos-catalog-scheduler.zsh --no-start

# 建立或重用完整 release，並開始正式 10,000 筆排程：
deploy/install-macos-catalog-scheduler.zsh

# 查看目前 LaunchAgent 狀態：
launchctl print "gui/$(id -u)/com.partsouq.catalog-scheduler"

# 停用排程；保留 plist、cookie、state 與 logs，之後可再執行 installer 啟用：
deploy/disable-macos-catalog-scheduler.zsh
```

installer 以 host-wide lock 串行切換，並用 `plutil` 填入 release 與 log 絕對路徑；
render 後檢查 placeholder、plist 格式、owner-only 權限、source、`scheduler.env`、
project／CloakBrowser venv 的所有 regular file、bytecode、symlink 字串及其 resolved
regular target 內容、root／目前使用者 ownership，以及目前帳號不可透過 group／other
寫入的權限，
以及 free Chromium checksum。build 產生的正常 bytecode 會在
manifest 建立前清掉；runtime 又固定 `PYTHONDONTWRITEBYTECODE=1`，因此後續新增的
`.pyc`、`.pyo` 或 `__pycache__` 會 fail closed。只有依賴
安裝、CloakBrowser import 與完整性檢查都成功，release 才會寫入
`.install-complete`。Python scheduler 取得 daemon lock 後才寫含 PID 的 readiness
marker；installer 每輪重新讀取 `launchctl`，要求 program、running state、live PID
與 marker PID 完全一致，連續穩定三輪才切換成功。立即退出、bootstrap 失敗、timeout、
signal 或切換中的其他錯誤都會回復實際已載入的先前 plist。完整的舊 release 不會
自動刪除，便於查核與回復；只有
本次安裝失敗且尚未完成的新 release 會清除。stdout／stderr、crawler runtime log
與可變 state 都位於 `~/Library/Application Support/partsouq-catalog/` 的 release
外部，不會改動 immutable app manifest，也不是共用 `/tmp` 檔案。macOS-only 的 56 個
展開測試會在 Linux CI 以精確訊息與筆數跳過；不新增需付費的 macOS GitHub runner。
同一帳號若在檢查與啟動間惡意改寫 cache，仍屬本機同使用者 TOCTOU 殘餘風險；正式
runner 會在啟動前後重驗固定 binary SHA，但不以額外 daemon 或付費服務過度設計。

預設排程如下；都可用同名環境變數調整，不需要人工逐次觸發：

- host catalog scheduler：啟動後自動執行正式 10,000 筆 bounded PartSouq crawl，之後每 30 天執行。
- `nhtsa-scheduler`：依序同步 NHTSA bulk 與 allowlist API，完成後每 24 小時執行。
- `queue-scheduler`：每 30 秒消費 8086 建立的要求；VIN 只處理使用者提供或獲授權的 17 碼值，不枚舉 VIN。

PartSouq catalog 只由專用 `scheduler` 執行；queue 會拒絕舊的 catalog 要求，
避免長時間型錄工作堵住後續 VIN。lock busy 或 scheduler 中斷的工作會保留為
pending 並自動重試；無效 VIN 等一般非零結果會結束為 failed，避免永久重試。

`SCHEDULER_CATALOG_INTERVAL_SECONDS`、`SCHEDULER_NHTSA_INTERVAL_SECONDS`、
`SCHEDULER_PENDING_INTERVAL_SECONDS` 控制頻率；失敗會從 60 秒開始指數退避，
最多等 3,600 秒。Compose 服務共用 `./logs` lock；macOS host 排程則共用前述
owner-private scheduler state lock，同一台主機的不同 checkout 不會重複執行。
排程狀態暫時讀不到時只會退避後重讀，不會直接啟動爬蟲；NHTSA bulk 已完成、
API 失敗時，下一輪會從 API stage 續跑，不會重抓同一批 bulk sources。
每次子程序會即時輸出 Docker log，並把最後 60,000 字、結束碼、執行時間與
`manual`／`daemon`／`queue` 來源寫入 `scheduled_job_runs`。PartSouq
在 Cloudflare refresh backoff 期間每 60 秒輸出一次 heartbeat；合法的最長
20 分鐘冷卻不會被 600 秒 silent-stall watchdog 誤判成卡死。
`crawl_runs.scheduled_job_run_id` 會保存實際 scheduler run；正式資料只接受
`daemon`，不能用手動或 sample run 冒充。若 bounded 資料已完整 commit、但
scheduler 在完成紀錄前中斷，即使 parent 已被記成 `failed`／非零 exit，下一個
daemon 仍會先重播 live evidence、核對精確筆數與關聯，再以 CAS 補記完成；
任一驗證不符就拒絕修復，不會把失敗資料冒充成功，也不會重爬已發布的 10,000 筆。

檔案 lock 是本機的第一層快速防線；crawler 同時會用獨立 MySQL 連線持有
database-scoped named lock，並在每次 commit 前確認 ownership。不同 checkout、
容器或主機只要共用同一個 MySQL DB，就不能同時改寫 catalog。多主機部署仍要
另外處理單一排程 owner、macOS Aqua browser 與故障切換，不能只靠檔案 lock。

host catalog scheduler 明確使用 `PSQ_BOUNDED_PARTS=10000` 並覆寫
`PSQ_LIMIT_PARTS=0`；Compose service 也維持相同資料契約。
正式 catalog scheduler 必須設定非空白的 `PSQ_BOUNDED_BRAND`、
`PSQ_BOUNDED_MODEL` 與正整數 `PSQ_VEHICLE_YEAR_WINDOW`；host installer、runner、
scheduler child 都會在 DB 或瀏覽器啟動前拒絕缺值。Compose 正式預設為
`TOYOTA`／`TACOMA`／`20`。首頁與 locate 頁仍完整解析及保存 evidence，
只在其後挑出唯一目標；找不到或重名會停止。續跑與每月 interval 會比對凍結後的
年份下限，避免跨年沿用不同 scope。`production_to=NULL` 視為仍在產或未封閉，允許；
只有已知的結束年早於下限才排除。發布前仍會再核對全部 10,000 筆均屬該品牌與型號。
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

GitHub Actions 對此 private repository 僅保留手動 `workflow_dispatch`，push 與 PR
不會自動使用 hosted runner 或 artifact storage。預設一律執行下列本機 gate；若要
手動啟動 hosted workflow，必須先自行確認帳號額度與可能費用。

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

目前的契約、驗證範圍與未完成邊界見 [docs/progress-log-2026-08-30.md](docs/progress-log-2026-08-30.md)。歷史測試紀錄見 [docs/verification-2026-08-15.md](docs/verification-2026-08-15.md)。
簡報逐項需求邊界見
[docs/pptx-requirements-audit-2026-08-20.md](docs/pptx-requirements-audit-2026-08-20.md)。

## 安全與資料邊界

- PartSouq catalog 請求前先以可識別 crawler UA 檢查 robots.txt 與 origin；robots
  無法確認允許、origin 不符或 redirect 一律停止（fail-closed），不跟隨 redirect。
- Cloudflare challenge 的處理方式：`cloak.py` 啟動 CloakBrowser，等待並驗證實際
  型錄頁。逾時或仍停在 challenge 頁時不匯出 cookie，並以失敗／退避收尾；不能
  只因出現 `cf_clearance` 就宣告成功。cookie 只存本機 `data/cookies.json`，不會提交。
- challenge 未成功刷新時是 `blocked`／失敗證據，不可報為完成；challenge 一律
  以刷新 cookie 後的 follow-up 請求重試，不輪替 proxy。
- NHTSA bulk／collection API 與單筆 `DecodeVinValues` 分流；bulk 資料不能冒充完整 VIN 車輛名冊。
- VIN source key 使用 SHA-256，排程完成後也會遮罩 request scope 與輸出；業務表與受控 raw evidence 才保留解碼所需完整 VIN。
- `output/`、`logs/`、資料庫 volume、`.env` 與管理 token 都不提交。

來源與整併範圍見 [docs/sources.md](docs/sources.md)。
