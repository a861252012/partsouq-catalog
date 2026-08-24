# AGENTS.md — 貢獻者指南

本指南協助協作者（人類或 AI 代理）在本儲存庫正確工作：涵蓋強制規定、
程式碼審查標準、專案結構與操作流程。

**位置**：儲存庫根目錄 `AGENTS.md`。

## 目錄

1. [政策與強制規定](#政策與強制規定)
2. [程式碼審查規則](#程式碼審查規則)
3. [專案結構指南](#專案結構指南)
4. [操作指南](#操作指南)

## 政策與強制規定

### 命名與別名（硬性規定）

- SQL 不得使用語意模糊的表別名。`nhtsa_sync_runs AS runs`、
  `scheduled_job_runs AS jobs` 這類需要回查才能理解的縮寫一律禁止。
  表名過長時，別名必須保留完整語意（例：
  `nhtsa_sync_runs AS sync_run`），或直接使用完整表名。
- 變數命名同上：`r`、`tmp2`、`d` 這類需回推才懂的名字禁止出現。
- class 命名必須直接表達職責，禁止抽象代稱。
- 新增程式碼若無法在不回查的情況下讀懂，視為未完成。

### 語言規範

- 註解、docstring、文件、commit message 以台澎金馬地區正體中文書寫。
- 識別字（變數／函式／class／模組）使用英文。
- 術語採台灣慣用用法：資料庫、網路、程式、預設、支援、記錄（非紀錄
  作動詞用）、設定。

### 文案潤飾（硬性規定）

備註、說明、文件與 git commit 敘述在定稿前，必須以 `humanizer-zh-tw`
skill 潤飾（位於
`~/.config/opencode/skills/humanizer-zh-tw/SKILL.md`），去除 AI 寫作
痕跡，確保語意通順好理解。適用範圍：

- 程式內的中文註解與 docstring。
- `docs/` 下所有說明文件的新增或改寫段落。
- 每一筆 commit message 的中文敘述。
- 對使用者的工作回報與決策說明。

純機械性內容（log 訊息、錯誤字串、測試參數名）不在潤飾範圍。

### 變更驗證關卡（提交前必跑）

凡是更動執行期程式碼、測試、migration 或建置／測試設定，提交前必須
依序跑完全套關卡：

```zsh
uv run --locked ruff check && uv run --locked ruff format --check
uv run --locked mypy --strict <更動的檔案>
PARTSOUQ_DB_NAME=partsouq_catalog_test NHTSA_TEST_MYSQL=1 UNIFIED_TEST_MYSQL=1 \
  uv run --locked pytest -W error -q --strict-config --strict-markers
```

- **每一筆會影響行為的 commit 之前，全套 pytest 必須重新跑過**。
  只跑相關子集不算通過——2026-08-24 的 ledger 斷層回歸
  （commit 44be8b2 之後僅跑子集就推送，三個 migration 測試靜默轉紅）
  即是教訓。
- 純文件變更（`docs/`、`*.md`）可免跑全套，但內容宣稱的事實必須與
  程式碼現況一致；不得留下會誤導審查者的過時描述。

### 契約同步義務

新增 migration 或環境閘控測試時，以下項目必須同步更新並核對：

- `.github/workflows/ci.yml` 與 `tests/test_ci_contract.py` 的 skip 總數
  （目前 268＝212 環境閘控＋56 macOS 閘控）。新增一個 env-gated 測試
  就要 +1。
- `tests/e2e/test_catalog_migration_runner.py` 內各降級輔助函式的
  ledger 刪除清單：刪除範圍必須「從目標版本一路涵蓋到最新版」，
  只刪中間會觸發 gap 檢查（`ledger skips an active migration`），
  留下殘列則讓第二次 apply 誤判已套用。
- `tests/test_catalog_migrations.py` 的敘述句數量清單與總和斷言。
- `src/partsouq_catalog/migrations.py` 的 `CATALOG_MANIFEST` 常數：
  發布新 migration 時登記檔名與 SHA256。

### 工作狀態回報

- 最終回覆只有在「工作完成且適用的驗證關卡全數通過」時才能宣告完成；
  仍在進行中不得以完成收尾。
- 需要使用者具體決策時，說明決策點與選項，不以「請繼續」含糊帶過。

### Git 安全

- 預設在使用者目前的 checkout 與分支上工作。建立分支、切換分支、
  建立 worktree 需經使用者明確同意。
- 只有使用者明確要求時才 commit / push。commit message 精簡、祈使句、
  中文描述，敘述需先經 humanizer-zh-tw 潤飾（見「文案潤飾」）；
  本機已跑過全套關卡者以 `[skip ci]` 標記。
- 不修改 git config、不 force-push、不做互動式 rebase。

## 程式碼審查規則

### 通報門檻

- 只在「支援路徑上會造成具體錯誤行為」時通報缺陷，並說明觸發情境與
  呼叫端可見的後果。無法建立具體後果就不通報。
- 不因「另一種寫法比較乾淨／對稱」通報缺陷；僅在與既有支援路徑產生
  具體不一致時提出。
- 新增的抽象、狀態欄位、相容性分支必須對應到明確需求或已驗證風險；
  對不上具體需求的機制應指出並建議最小移除。

### 測試證據

- 測試要在最高且穩定的呼叫端邊界驗證行為，期望值來自需求或獨立依據，
  不接受只用實作自身邏輯反推的斷言。
- 新行為需要代表性回歸測試；刻意不支援的類別也要有測試釘住。
  不要求窮舉所有可建構排列。

### 審查範圍

- 審查完整 diff（相對於合併基準），不只看最後一筆增量。
- 既有問題僅在「本次修補使其進入支援路徑」時納入範圍；順手清理
  另開任務，不夾帶。

## 專案結構指南

### 概觀

PartSouq 零件目錄系統：爬蟲（HTTP＋隱匿瀏覽器）、排程器、NHTSA VIN
解碼同步、台灣 MOENV VNCS 車籍主檔、後台管理（8000／8086）、
migration 與證據鏈。Python 3.14、uv 鎖定相依、MySQL 8（本機 docker，
port 3308）。

### 重要檔案與目錄

- `src/partsouq_catalog/`：型錄爬蟲主系統。
  - `crawler.py`：核心爬取流程（品牌→型號→車款→零件組）、bounded
    資料集、車款年份視窗政策（`vehicle_year_window`）。
  - `repositories.py`：資料存取、證據寫入（`record_http_evidence`
    強制 `trigger_mode='daemon'` 的排程來源）。
  - `scheduler.py`：統一排程入口（catalog/nhtsa/pending/vncs job）。
  - `migrations.py`：ledger 型 migration runner。
  - `config.py`：全部以環境變數覆寫的行為參數。
- `src/partsouq_crawler/`：獨立爬蟲模組。
  - `nhtsa/`：vPIC API 與 bulk 下載、VIN 解碼部分欄位契約。
  - `vncs/`：台灣 MOENV VNCS（Playwright 收割器、TWCA TLS 錨定）。
- `migrations/catalog/`：版本化 SQL migration；清單登記於
  `src/partsouq_catalog/migrations.py` 的 `CATALOG_MANIFEST` 常數。
- `db/station_admin.sql`：站方後台 schema 基準。
- `tests/`：單元／整合／e2e；env-gated 測試需對應環境變數才執行。
- `docs/progress-log-YYYY-MM-DD.md`：每個里程碑的工作歷程（必須如實
  更新，包含失敗與回歸）。
- `deploy/`：macOS LaunchAgent 安裝／解除腳本。

### 子系統注意事項

- **證據鏈**：正式資料的每一頁都要有可重放的 parser 輸入證據。
  手動 one-shot 觸發的爬取不符合 provenance 要求，會在第一筆證據
  寫入時失敗——必須走 scheduler daemon。
- **bounded 與全量互斥**：`PSQ_BOUNDED_PARTS` 非 0 時為 bounded 模式
  （精確達標才原子發布）；全量模式必須明確設 `PSQ_BOUNDED_PARTS=0`。
- **續爬語意**：receipt 已完成的型號／車款自動跳過；殘留 running
  marker 需滿 900 秒才可被新 run 回收。行程被殺後直接重啟即可續爬。
- **VNCS**：翻頁依賴站方 Infragistics paging JS API（Playwright），
  TLS 需 repo 內 TWCA 中繼憑證。非 VIN 引擎碼列無唯一約束，
  重跑前需清理同名條件的重複列。

## 操作指南

### 環境需求

- macOS 或 Linux；Python 3.14+；`uv`；`make` 非必要（無 Makefile）。
- 本機服務以 docker compose 管理：mysql(3308)、admin(8000)、
  station-admin(8086)、queue-scheduler。容器映像需隨程式碼重建，
  舊映像對新 schema 會 crash-loop。
- 機密只放 `.env`（不入庫）；測試用資料庫 `partsouq_catalog_test`。

### 開發流程

1. 在使用者目前的 checkout／分支上工作。
2. 更動前先理解周遭程式碼的框架與慣例；沿用現有工具，不自行引入
   新相依套件。
3. 實作連同測試一起交付；行為變更必附代表性回歸測試。
4. 提交前跑完「變更驗證關卡」全套，並核對契約同步義務清單。
5. 每個里程碑更新 `docs/progress-log-*.md`：做了什麼、為什麼、
   失敗了什麼、怎麼修的。文件落後於事實視為缺陷。

### 常用指令

```zsh
# 格式與靜態檢查
uv run --locked ruff check && uv run --locked ruff format --check
uv run --locked mypy --strict src/partsouq_catalog/crawler.py

# 單一測試
uv run --locked pytest tests/test_partsouq_vehicle_year_window.py -W error -q

# 全套（含真 MySQL 的 e2e）
PARTSOUQ_DB_NAME=partsouq_catalog_test NHTSA_TEST_MYSQL=1 UNIFIED_TEST_MYSQL=1 \
  uv run --locked pytest -W error -q --strict-config --strict-markers

# Migration 套用／檢查（正式資料庫）
uv run --locked python -m partsouq_catalog.migrations apply --retry 0
```

### 營運備忘

- 全量 crawl 一律以 daemon 模式啟動：
  ```zsh
  PSQ_BOUNDED_PARTS=0 PSQ_VEHICLE_YEAR_WINDOW=20 \
    caffeinate -is uv run --locked python -m partsouq_catalog.scheduler \
      --job catalog --daemon --interval-seconds 3600
  ```
- LaunchAgent（`com.partsouq.catalog-scheduler`）與手動 daemon 不可
  並存（job lock 互斥）；切換前先 `launchctl bootout`，恢復用
  `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.partsouq.catalog-scheduler.plist`。
- 排程間隔判定看「最後一筆 daemon 模式的 catalog 記錄」；manual 模式
  的殘留記錄不影響判定，但會留在 `scheduled_job_runs` 供稽核。
- 完整歷程見 `docs/progress-log-2026-08-23.md`（含毒組驗屍、
  解碼契約放寬、ledger 回歸事件）。
