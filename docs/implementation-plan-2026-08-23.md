# 實作計畫：NHTSA 全量爬取 + VNCS 爬蟲 + Runtime 收尾（v2 修訂版）

- 日期：2026-08-23（v1 初稿）；2026-08-23 v2 依 live API 實測修訂
- 狀態：執行中（使用者已授權分批 commit/push、migration、LaunchAgent、正式 10k）
- 配套閱讀：`docs/handoff-2026-08-23.md`、`docs/handoff-nhtsa-full-crawl-plan-2026-08-23.md`、`docs/progress-log-2026-08-23.md`

## v2 修訂摘要（與 v1 的差異）

v1 由另一模型（MiniMax M3）起草。本版依 2026-08-23 對 NHTSA vPIC 與 VNCS 的
live request 實測結果修正，**v1 的以下內容已證實錯誤並移除**：

| v1 主張 | 實測結果（2026-08-23） |
|---|---|
| `GetWMIs?page=N` 可分頁抓 >10,000 筆 | **端點已被 NHTSA 移除**。帶/不帶 `page` 參數皆回 `"No HTTP resource was found that matches the request URI .../api/vehicles/GetWMIs"` |
| 全展開 ≈ 3000-4000 requests | 錯誤。12,340 makes × ~46 年 ≈ **567k requests**，超過 budget 5,000 兩個數量級；v1 的驗收目標（`vpic_model_years` >100,000 筆）與自身 budget 自相矛盾 |
| `vpic_model_years.required_fields` 含 `Model_Year` | `GetModelsForMakeYear` 回應僅 4 欄（`Make_ID`/`Make_Name`/`Model_ID`/`Model_Name`），**不含** `Model_Year`；年份須由 `ApiSource.context` 注入 |
| §0 標題「三項使用者決策」 | 實列四項，已更正 |

實測同時確認可用的端點：`GetAllMakes`（單請求 12,340 筆、不分頁）、
`GetModelsForMakeId/<id>`、`DecodeWMI/{wmi}`（需已知 WMI，無法枚舉）、
`GetAllManufacturers?page=N`（100/頁）、VNCS `VNCSEXLRPT.aspx`
（WebForms 欄位實名 `dlFtrMOBTYPE`/`dlFtrPERIOD`/`dlFtrTESTTYPE`）。

---

## 0. 重新對齊後的真實需求

| 來源 | 目標 |
|---|---|
| NHTSA vPIC（`vpic.nhtsa.dot.gov`） | Make、Model 目錄全量；WMI 層改以 `DecodeWMI` 按需查詢（無法全量枚舉） |
| 台灣 MOENV VNCS（`vncs.moenv.gov.tw`） | 汽油車 + 柴油車（排除機車），含真實 17 碼 VIN |
| 兩者交集 | 透過 VNCS 取得的 17 碼 VIN 餵 `DecodeVinValues`，回填 NHTSA 缺的 Engine / Trim / Displacement |

**最終欄位**：Make、Model、Year、Engine 形式、Trim 排氣量、VIN 車身號碼。

**四項使用者決策**（不可違反）：

1. Engine / Trim / Displacement **先抓 NHTSA 能提供的最全**，欄位**先留空**，
   等合法 VIN（VNCS 政府公開資料）後用 `DecodeVinValues` 回填。
2. NHTSA + VNCS **同一輪做**。
3. Commit/migration 需先徵求授權。（2026-08-23 更新：使用者已授權分批進行）
4. **禁止枚舉 VIN**；VNCS 政府公開資料視為明確授權來源。

---

## 1. 已完成（前一階段已交付，工作樹上待分批提交）

| 項目 | 檔案 | 內容 |
|---|---|---|
| **P1-a** scheduler recovery daemon-only | `src/partsouq_catalog/scheduler.py` | 6 個 NHTSA recovery query 全部加 `trigger_mode='daemon'` 過濾 |
| **P1-b** RuntimeError → DB exit code 2 | `src/partsouq_catalog/scheduler.py` | `dispatch_locked()` 與 daemon migration 路徑把 `RuntimeError` 轉成 `SCHEDULER_DB_ERROR_EXIT_CODE` |
| **P2-a** finalization child CAS（部分） | `src/partsouq_crawler/nhtsa/repository.py` | `_assert_active_lease()` 加 `SELECT ... FOR UPDATE` 重鎖並驗證 lineage |
| **P2-c** migration parent 時序 | `src/partsouq_catalog/migrations.py` | parent.status='failed' 時阻止 child 在 parent 之後才開始 |
| **B-1** bounded explicit key bypass | `src/partsouq_catalog/crawler.py` | （2026-08-23 新增）`PSQ_BOUNDED_RUN_KEY` 一律禁止——scheduled 與 direct 同規則；resume 一律經 `resumable_bounded_run_key` 相容性檢查。Regression：`test_scheduled_bounded_run_rejects_operator_supplied_run_key`、`test_direct_bounded_run_rejects_operator_supplied_run_key` |
| **Regression 補強** | `tests/test_scheduler.py` 等 | scheduler 104 passed、migrations 20 passed、bounded unit 全綠 |

仍在工作樹未動：P2-b（raw artifact check-to-publish race）、250ms lease barrier。

---

## 2. Phase 1 — NHTSA vPIC 涵蓋率擴充（修正版）

### 2.1 目標與範圍

```
GetAllMakes                    →  vpic_makes            固定，單請求 12,340 筆（已驗證不分頁）
  ↓ 對每個 Make_ID 展開
GetModelsForMakeId/<id>        →  vpic_models          動態 ≈ 12.3k requests（可控）

GetAllManufacturers?page=N     →  vpic_manufacturers   已實作
GetVehicleVariableList         →  vpic_variables       已實作
GetVehicleVariableValuesList   →  vpic_variable_values 已實作
```

**明確不做**：
- ~~`GetWMIs`~~：端點已移除（見修訂摘要）。WMI→品牌對照改由 `DecodeWMI/{wmi}`
  按需查詢；對最終欄位需求無影響（VIN→規格靠 `DecodeVinValues`）。
- `GetModelsForMakeYear` 全量展開：567k requests 不可行。降級為「抽樣決策」：
  先對 10 個代表 make 各跑一年份，實測延遲/空回率後再決定小範圍採用或放棄；
  年份維度主要由 VNCS VIN → `DecodeVinValues` 回填承擔。

### 2.2 檔案：`src/partsouq_crawler/nhtsa/api.py`

allowlist 新增兩條 regex（移除 v1 的 `GetWMIs` 條目）：

```python
VPIC_PATHS = (
    re.compile(r"/api/vehicles/GetAllMakes(?:$|\?)"),
    re.compile(r"/api/vehicles/GetModelsForMakeId/[1-9][0-9]*"),
    re.compile(
        r"/api/vehicles/GetModelsForMakeYear"
        r"/make/[A-Za-z0-9%]+/modelyear/[0-9]{4}"
    ),
    re.compile(r"/api/vehicles/GetAllManufacturers"),
    re.compile(r"/api/vehicles/GetVehicleVariableList"),
    re.compile(r"/api/vehicles/GetVehicleVariableValuesList/[0-9]+"),
)
```

### 2.3 檔案：`src/partsouq_crawler/nhtsa/datasets.py`

`vpic_models` 改為動態來源 spec（identity 含 make），新增抽樣用
`vpic_model_years` spec。注意 `required_fields` **不得含 `Model_Year`**
（回應無此欄位），年份由 context 注入後併入 payload（既有機制見
`api.py` 的 `payload.update(dict(source.context))`）：

```python
"vpic_model_years": DatasetSpec(
    name="vpic_model_years",
    delimiter=",",
    has_header=False,
    field_names=(),
    required_fields=("Make_ID", "Make_Name", "Model_ID", "Model_Name"),
    identity_fields=("Model_ID",),
    external_id_field="Model_ID",
    make_field="Make_Name",
    model_field="Model_Name",
),
```

需補一個 unit test 保護：「context 合併發生在 `required_fields` 驗證之前」，
防止未來有人把驗證提前造成誤拒。

### 2.4 檔案：`src/partsouq_crawler/nhtsa/api_service.py`

`run()` 的 vPIC scope 改為：固定來源同步後，讀取 `vpic_makes` 文件，
對每筆 `Make_ID` 建立 `vpic_models_for_make_<id>` source 依序同步。
每完成一批（如 500 個 make）記錄 progress checkpoint。
request budget 維持 fail-closed 斷言；預期總量 ≈ 12.3k + 既有數千。

### 2.5 測試

- allowlist regex 單元測試（含惡意 path 拒絕）
- mock `_sync_source` 驗證動態展開順序、次數與 budget 斷言
- context-before-validation 測試（見 2.3）

---

## 3. Phase 2 — Runtime 驗證與收尾

與 v1 相同，不變：

1. 跑全套既有測試（unit + 真 MySQL + migration runner e2e）。
2. 補 250ms lease race deterministic barrier（`progress.py` heartbeat Event）。
3. Schema 三處一致性檢查（`db/nhtsa.sql` ↔ `mysql_schema.sql` ↔ migration 024）。
4. 套 migration 023/024（writer 全停後執行；使用者已原則性授權，執行前逐項確認）。

---

## 4. Phase 3 — 台灣 VNCS 爬蟲（新模組）

模組結構、parser 啟發式、CLI/scheduler 整合與 v1 相同，修正兩點：

### 4.1 表單控制項實名（live 驗證）

頁面下拉選單實名為 `dlFtrMOBTYPE`（汽油車/柴油車/機車）、`dlFtrPERIOD`
（第一期~第六期）、`dlFtrTESTTYPE`（新車型/逐車/沿用）。client 以動態解析
hidden fields 為主、這些實名只當斷言，避免改版即壞。

### 4.2 唯一鍵修正（v1 錯誤）

v1 的 `uq_vncs_body_engine(body_or_engine_code, model_year)` 在非 VIN 時
（引擎號碼）可能一碼多車，會靜默丟資料。改為條件唯一——只有 VIN 參與唯一約束
（MySQL 多 NULL 不衝突）：

```sql
vin_code VARCHAR(32) GENERATED ALWAYS AS (
    CASE WHEN is_vin = 1 THEN body_or_engine_code END
) STORED,
UNIQUE KEY uq_vncs_vin (vin_code),
KEY idx_vncs_code (body_or_engine_code, model_year)
```

---

## 5. 驗收指標（修正版）

| 指標 | 預期 | 變更理由 |
|---|---|---|
| `nhtsa_sync_runs` ≥1 筆 completed | ✓ | 不變 |
| `vpic_makes` | = 12,340（實測值） | 收緊為精確數 |
| `vpic_models`（per-make） | > 30,000 | 不變 |
| ~~`vpic_wmis` > 10,000~~ | 移除 | 端點已死 |
| `vpic_model_years` | 抽樣決策後另定 | budget 不可行 |
| `tw_vncs_vehicles` > 1,000；is_vin > 100 | ✓ | 不變 |
| `nhtsa_vin_decodes` > 100（VNCS 回填） | ✓ | 不變 |

---

## 6. 開放問題狀態更新

| # | 問題 | 狀態 |
|---|---|---|
| Q1 | GetAllMakes 分頁？ | **已答**：不分頁，單請求 12,340 筆 |
| Q2 | GetWMIs 欄位？ | **已答**：端點已移除，子計畫取消 |
| Q3-Q8 | VNCS parser 精度／migration 時機／commit 分批／daemon vs CLI／VIN 回填批次／10k 啟動 | 使用者已給原則授權；Q7 維持小批次 ≤100 起步 |

---

## 7. Commit 策略（已授權，依 handoff 批次）

```
批次 A（NHTSA）：scheduler/nhtsa/migrations/tests/ci/docs —— 數個 scoped commits
批次 B（PartSouq bounded）：repositories.py + test_partsouq_bounded_limit.py —— 單獨 commit
之後：migration 023/024 → Phase 1a → Phase 2 → Phase 3（VNCS）→ B3 正式 10k
```

維持 repo 慣例 `[skip ci]`；本地品質關卡（ruff/format/mypy/unit/skip 契約）
提交前必跑，證據記入 progress-log。

**Skip 契約更正（2026-08-23）**：unit job 期望值由工作樹初稿的 217 更正為
**268 = 212（env-gated，平台無關；含 VNCS integration 新案例）+ 56
（`MACOS_LAUNCH_AGENT_ONLY`，ubuntu 上 skip；與 e2e job 的
`--expected-count 56` 交叉驗證一致）**。本機為 macOS，驗證時以 212 對帳。
（初稿 217 無法重現，判定為過時套件狀態下誤算。）

---

## 8. 文件同步紀律（2026-08-23 使用者指示）

- 提交前：文件敘述必須與程式行為一致；錯誤或過時敘述（如本版修訂摘要所列）
  必須先改正才可 commit。
- 程式碼 docstring/comments 與行為逐檔核對。
- 各里程碑連同實測證據記入 `docs/progress-log-2026-08-23.md`。
