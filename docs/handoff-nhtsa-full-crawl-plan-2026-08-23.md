# 交接：NHTSA 全量公開資料爬取 + 台灣 VNCS 爬蟲（分析與執行計畫）

- 日期：2026-08-23
- 狀態：**尚未開始實作**，本文件是分析成果 + 執行計畫，供新 session 接手
- 前置閱讀：`docs/handoff-2026-08-23.md`、`docs/progress-log-2026-08-23.md`（本文件為其最新補充）

新 session 開場建議指令：

```
請閱讀 <repo>/docs/handoff-nhtsa-full-crawl-plan-2026-08-23.md 與
<repo>/docs/handoff-2026-08-23.md、<repo>/docs/progress-log-2026-08-23.md，
然後依計畫 Phase 1 → 2 → 3 依序實作，Phase 4 驗收。
```

---

## 0. 重要：實際 repo 路徑

**本機實際 repo 在：**

```
/Users/a861252012/Desktop/folder/code/partsouq-catalog
```

**注意陷阱：** opencode 的 workspace root 是
`/Users/a861252012/Documents/Codex/2026-08-15/https-github-com-a861252012-partsouq-catalog`
——**這個目錄是空的**（只是 clone 目錄，內容不存在）。所有工作都要在 Desktop 路徑進行。

---

## 1. 使用者需求（真實需求，非交接文件舊框架）

「把 NHTSA 的全部資料都爬回來」＋台灣國產車／在地車型。

最終欄位：**Make（品牌）、Model（車系型號）、Year（年份）、Engine（引擎形式）、Trim（排氣量）、VIN（車身號碼）**。

- 爬取網站①：**NHTSA vPIC**（`vpic.nhtsa.dot.gov`）——免費公開 API，含全世界絕大多數註冊車廠的 WMI（VIN 前 3 碼）與基本規格庫，無版權爭議。目標：建立基礎大資料庫。
- 爬取網站②：**台灣 MOENV VNCS**（`https://vncs.moenv.gov.tw/VNCSEXLRPT.aspx`）——只爬**汽油車與柴油車**，不爬機車。欄位：「車型名稱」「年份」「車身號碼／引擎號碼」（17 碼即車身號碼）。「車型名稱」把品牌/型號/款式/cc數/車身規則/變速箱/車門寫在同一欄。

### VIN 知識（使用者說明）
- VIN = Vehicle Identification Number，17 位元，一台車身分證，全球唯一。
- 結構：前 3 碼 WMI（全球製造商代碼）、4–9 碼 VDS（車輛特徵碼）、10–17 碼 VIS（車輛識別碼）。
- 「爬前 11 碼即可精準知道品牌、型號、年份、引擎形式」——但注意：**NHTSA 公開 API 不含「每車型 engine 規格」**，engine/trim 只來自 `DecodeVinValues`（需真實 VIN）或自建 VDS 解碼。

---

## 2. 使用者已做的決策（必須遵守）

| 決策點 | 選擇 |
|---|---|
| Engine/排氣量/Trim 來源 | **先抓 NHTSA 能提供的最全**（WMI + Make + Model + Model-Year + Manufacturer + VehicleType），Engine/Trim/Displacement 欄位**先留空**，等合法 VIN 後用 `DecodeVinValues` 回填 |
| 台灣 VNCS 時程 | **NHTSA + VNCS 同一輪做** |
| 提交 | 不擅自 commit；Phase 2 的 DB 套用與 commit 需先徵求使用者授權 |
| VIN 政策 | 禁止自行枚舉 VIN；只能「使用者提供」或「明確授權」來源（VNCS 政府公開資料屬明確授權來源） |

---

## 3. 目前狀態（2026-08-23 實測）

### Git
- HEAD：`b725fa84e4585b9523f407afdeaed95a8988bfbf`（branch `main`）
- 工作樹 **29 個檔案未提交**（含 25 M + 2 ??）：
  - `M`：`.github/workflows/ci.yml`、`db/admin.sql`、`db/catalog.sql`、`db/nhtsa.sql`、`src/partsouq_catalog/migrations.py`、`src/partsouq_catalog/repositories.py`、`src/partsouq_catalog/scheduler.py`、`src/partsouq_crawler/cli.py`、`src/partsouq_crawler/nhtsa/api.py`、`api_client.py`、`api_service.py`、`client.py`、`models.py`、`mysql_schema.sql`、`progress.py`、`repository.py`、`service.py`、多個 tests
  - `??`（未追蹤）：`migrations/catalog/024_nhtsa_run_leases.sql`、`tests/crawler/integration/test_nhtsa_scheduler_mysql.py`

### 資料（依交接文件，尚未重新驗證）
- PartSouq raw：10,000 筆；正式 bounded／published／current：全部 0 筆（最新 bounded run = `error`）
- Quarantine：2,260 筆
- NHTSA VIN decode：0；VIN 車款 mapping：0；VIN 零件適配：0
- NHTSA run 1、2：仍為 stale `running`（需安全回收，不可人工改成成功）
- Migration ledger：到 022；023 尚未套用（024 也未套用）
- MySQL、admin、station-admin healthy；queue-scheduler running

---

## 4. 本 session 關鍵發現（與交接文件的差異）

1. **NHTSA runtime 其實「已寫好但未 commit」**，不是「沒寫」：
   - `repository.py` 已實作 lease 取得/CAS（`_assert_active_lease`）、續約（`heartbeat`）、釋放（`_finish_run`）、原子發布（`complete_run_and_publish_artifacts` 含 token CAS）、`published_run_id` 寫入（repository.py:669/673/677 與 881/888）。
   - `progress.py:56-111` 已有 heartbeat 背景執行緒。
   - `db/nhtsa.sql` 與 `src/partsouq_crawler/nhtsa/mysql_schema.sql` 已含 024 的全部欄位（lease_slot/lease_token/heartbeat_at/lease_expires_at/published_run_id）。
   - `migrations.py` manifest 已列 023、024（v1–24）。
   - 換言之：交接文件說的「5 個未完成草稿」其實是「已改好但未 commit、未跑測試、未套 DB」的狀態。
2. **VNCS 網站實際可達**，且表格「車身碼或引擎碼」欄**多為真實 17 碼 VIN**（例：`KNAPX81BDV7443274`）。這是**政府公開資料**，可合法餵給 NHTSA `DecodeVinValues` 回填 Engine/Trim——使用者「Engine 先留空」的方案可被此自動補齊。
3. **NHTSA 涵蓋率真正的洞**：
   - `datasets.py:439-455` `VPIC_FIXED_SOURCES` 只有 3 個端點：`GetAllMakes`、`GetModelsForMakeId/0`（**寫死 /0，make 0 不存在，models 實際抓不到**）、`GetVehicleVariableList`。
   - **沒有** `GetWMIs`、沒有 per-Make `GetModelsForMakeId/{id}` 展開、沒有 `GetModelsForMakeYear`、沒有 `GetVehicleTypesForMakeId`。
   - `api.py:17-23` allowlist 也只放行 `GetModelsForMakeId/0` 寫死路徑。
4. **已知唯一 runtime 殘留缺口**：完整 `NhtsaApiSyncService.run` vPIC 路徑有 250ms lease race，缺決定性 `threading.Event` barrier（見 docs/handoff-2026-08-23.md:208）。
5. **PartSouq bounded run = error 是獨立平行線**，與 NHTSA/VIN 無關，本輪不處理。

### 已實作程式碼位置速查（新 session 必讀）
- source 定義：`src/partsouq_crawler/nhtsa/datasets.py`（`VPIC_FIXED_SOURCES` :439、`CSSI_SOURCES` :516）
- allowlist + parser：`src/partsouq_crawler/nhtsa/api.py`（`VPIC_PATHS` :17、`NhtsaApiPolicy` :40、`NhtsaApiParser` :73）
- API client：`src/partsouq_crawler/nhtsa/api_client.py`
- API sync 服務（動態展開範例在 manufacturer/variable）：`src/partsouq_crawler/nhtsa/api_service.py:101-152`
- repo/lease/publish：`src/partsouq_crawler/nhtsa/repository.py`（`start_run` :104、`heartbeat` :200、`_assert_active_lease` :1071、`complete_run_and_publish_artifacts` :582、`_finish_run` :1100）
- heartbeat 執行緒：`src/partsouq_crawler/nhtsa/progress.py:56-111`
- CLI：`src/partsouq_crawler/cli.py:302-358`（`_dispatch_nhtsa`、`_scheduled_job_run_id` 讀 `SCHEDULED_JOB_RUN_ID` env）
- migration runner：`src/partsouq_catalog/migrations.py`（manifest :30-55；入口 `partsouq-catalog-migrate`）
- 測試：`tests/crawler/unit/test_nhtsa_api.py`、`test_nhtsa_parser.py`、`test_nhtsa_progress.py`；`tests/crawler/integration/test_nhtsa_mysql_sync.py`、`test_nhtsa_scheduler_mysql.py`（未追蹤）；`tests/e2e/test_catalog_migration_runner.py`
- 工具鏈：pytest（`testpaths=["tests"]`）、ruff（line-length 100）、mypy（py312）

---

## 5. 執行計畫

### Phase 1 — 擴充 NHTSA vPIC 涵蓋率（真正缺的爬取邏輯）

檔案：`src/partsouq_crawler/nhtsa/api.py`、`datasets.py`、`api_service.py`、`tests/`

1. **allowlist 擴充（`api.py:17-23`）**
   - 新增 `GetWMIs`（含 `page` 參數，分頁）
   - `GetModelsForMakeId/[0-9]+`（把寫死的 `/0` 改動態）
   - `GetModelsForMakeYear/make/{make}/modelyear/{year}`（年式維度）
   - 視需要 `GetVehicleTypesForMakeId/[0-9]+`
2. **新 DatasetSpec（`datasets.py`）**
   - `vpic_wmis`：WMI / Name / Country / Type（欄位名稱實作時用一次 sample request 確認）
   - `vpic_model_years`：Make / Model / Year（取自 `GetModelsForMakeYear`）
   - `vpic_models` 改由 per-Make 展開餵入（不再是 `/0`）
3. **動態 source 展開（`api_service.py`，仿現有 manufacturer/variable 展開 :101-152）**
   - `GetAllMakes` 完成後，對每個 `Make_ID` 生成 `GetModelsForMakeId/{id}` 子 source
   - 對 (make, year) 組合生成 `GetModelsForMakeYear` 子 source（year 範圍先取常見 1981–當年度）
4. **parser** 沿用 `NhtsaApiParser`（通用），只需新 spec 的 `required_fields` 正確。
5. **repository** 通用 persist 路徑直接吃新 dataset（`nhtsa_record_versions` 已支援 make/model/year）。
6. **測試**：`test_nhtsa_api.py` 加新 allowlist 端點單元測試；`test_nhtsa_mysql_sync.py` 加 model 展開整合測試。

### Phase 2 — 驗證並收尾 NHTSA runtime（多半已存在，只差收尾）

檔案：`repository.py`、`progress.py`、`migrations/catalog/024_*`、`tests/`

1. 跑測試套件確認現有 runtime 通過：
   - `pytest tests/crawler/unit/test_nhtsa_api.py tests/crawler/unit/test_nhtsa_parser.py tests/crawler/unit/test_nhtsa_progress.py`
   - `pytest tests/crawler/integration/test_nhtsa_mysql_sync.py tests/crawler/integration/test_nhtsa_scheduler_mysql.py`
   - `pytest tests/e2e/test_catalog_migration_runner.py tests/test_catalog_migrations.py tests/test_ci_contract.py tests/test_unified_project.py`
2. 補齊 250ms lease race：在 `NhtsaApiSyncService.run` vPIC 路徑加決定性 `threading.Event` barrier。
3. 一致性檢查：`db/nhtsa.sql` ↔ `mysql_schema.sql` ↔ migration 023/024 ↔ `tests/test_ci_contract.py`、`test_catalog_migrations.py`（manifest hash 不能破）。
4. 本地用 `compose.yml` MySQL（`docker compose up -d mysql`，需 `.env` 有 `PARTSOUQ_DB_PASSWORD`、`PARTSOUQ_MYSQL_ROOT_PASSWORD`）+ `partsouq-catalog-migrate apply` 實際套用 023/024 驗證（**系統操作，需先徵求使用者授權**）。
5. 全部通過後才 commit（依使用者指示）。

### Phase 3 — 台灣 VNCS 爬蟲（ASP.NET 網頁）

新模組：`src/partsouq_crawler/vncs/`（client / parser / models / datasets / service / cli）

1. **client**：GET `VNCSEXLRPT.aspx` 取 `__VIEWSTATE`/`__EVENTVALIDATION` → POST 表單（`車輛種類=汽油車/柴油車`、`車型名稱`、`車型年份`、`車身或引擎碼`、`期別`、`廠牌`、`檢測類別`、`車型組`）；用「全列」或分頁抓全部；只取汽油車+柴油車，排除機車。確認 HTML 解析庫可用性（beautifulsoup4/lxml，缺則加依賴並更新 pyproject）。
2. **parser**：解析表格列（車輛種類、車型名稱、車型年份、車型組代號、車身碼或引擎碼、期別、核准日期、查核碼）。`車型名稱` 內含 品牌/型號/樣式/cc數/車身規則/變速箱/車門 → 寫啟發式拆分器拆成結構欄位。
3. **schema**：新表 `tw_vncs_vehicles`（vehicle_kind、make、model、displacement_cc、body_rule、transmission、doors、model_year、body_or_engine_code、period、approval_date、check_code、source_url、payload_json）。17 碼 `車身碼或引擎碼` 標記為 `is_vin`。
4. **CLI + scheduler**：新增 `vncs-sync` job kind（仿 `nhtsa-sync-api` 子程序模式，scheduler.py:1400-1437 附近）。
5. **測試**：HTML fixture 單元測試 + mock HTTP 整合測試。
6. **（可選延伸）** 把 VNCS 中 17 碼 VIN 作為政府公開授權來源，餵 `DecodeVinValues` 回填 Engine/Trim，自動補齊 Phase 1 留空的欄位。

### Phase 4 — 驗收

- NHTSA：`vpic_makes` / `vpic_models`(per-make) / `vpic_wmis` / `vpic_model_years` 筆數 > 0，且 `nhtsa_sync_runs` 狀態為 `completed`、lease 正常釋放。
- VNCS：台灣汽油/柴油車款入庫，17 碼 VIN 正確標記。
- 更新 `docs/handoff-2026-08-23.md` / `progress-log`：標註 runtime 已收尾並 commit、VIN 政策改由 VNCS 公開資料滿足。

---

## 6. 驗證指令速查

```bash
cd /Users/a861252012/Desktop/folder/code/partsouq-catalog

# 測試（先跑 unit，再跑需要 MySQL 的 integration）
pytest tests/crawler/unit/test_nhtsa_api.py tests/crawler/unit/test_nhtsa_parser.py tests/crawler/unit/test_nhtsa_progress.py
pytest tests/crawler/integration/test_nhtsa_mysql_sync.py tests/crawler/integration/test_nhtsa_scheduler_mysql.py
pytest tests/e2e/test_catalog_migration_runner.py tests/test_catalog_migrations.py tests/test_ci_contract.py

# lint / typecheck
ruff check src tests
mypy src

# MySQL（需 .env 設定 PARTSOUQ_DB_PASSWORD、PARTSOUQ_MYSQL_ROOT_PASSWORD）
docker compose up -d mysql
partsouq-catalog-migrate apply   # 套用 023/024
```

---

## 7. 注意事項與約束

1. **不擅自 commit**；Phase 2 的 DB 套用與任何 commit 都先問使用者。
2. **VIN 政策**：禁止枚舉 VIN；VNCS 政府公開資料視為明確授權來源，可用於 `DecodeVinValues`。
3. **PartSouq bounded run error 是平行線**，本輪不做；不要被它拖住。
4. NHTSA 公開 API 沒有每車型 engine 規格；Engine/Trim 欄位以「留空 + 之後 VIN 回填」為準，不要自建 VDS 解碼（使用者已排除）。
5. 交接文件 `docs/handoff-2026-08-23.md` 中「runtime 未完成」的描述已過時，以本文件 §4 為準。
6. `024_nhtsa_run_leases.sql` 與 `test_nhtsa_scheduler_mysql.py` 是未追蹤檔，記得在 commit 時一起納入。