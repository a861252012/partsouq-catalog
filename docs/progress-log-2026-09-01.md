# 進度紀錄 2026-09-01

承接 08-31 的交接（bounded 10k 重建、LaunchAgent 重啟、VIN 解碼鏈路驗證）。
本日完成 sparse VIN 橋接、VNCS 冪等、8086 候選確認流程，並重新盤點 CI skip 契約。

## migration 037：sparse override 橋接快照未發布欄位

- 問題：`v_vin_part_fitments` 的 sparse 分支要求「decode 欄位非空時快照必須相符」，
  但正式快照的 engine／trim 本來就沒有發布（NULL）。候選查詢與 mapping 建立都接受
  這種組合，view 卻拒絕，mapping id=1 建立後 fitment 一直是 0。
- 修法：037 重建 view，sparse 分支三個欄位改為「快照 NULL 且 mapping 攜帶 decode
  值時橋接；快照有值但不符仍拒絕」。套用正式庫後 fitment 0 → 512 筆（BODY/INTERIOR
  220、POWER TRAIN/CHASSIS 213、ENGINE/FUEL/TOOL 79）。
- 踩坑：output assert 一開始用未限定的欄位片段做 LOCATE，MySQL 會把 view 定義
  改寫成含 schema 限定的反引號形式，比對不到。改以別名開頭、不含 schema 名的
  片段（`` `catalog_part`.`engine`),''') is null) and ... ``）才命中。
- 回歸測試釘住三種情境：橋接成立、快照有值不符仍拒絕、mapping 未攜帶 decode 值
  不橋接。

## migration 038：VNCS 同步內容指紋冪等

- 問題：`upsert_vehicles` 對非 VIN 列只 INSERT（一碼多車不參與唯一），排程重跑
  會整批追加重複列。資料盤點顯示現有 8,650 列指紋全部互異（先前的「重複」統計
  是漏了 vehicle_kind 與內容比對的誤判），真正的風險在未來的排程重跑。
- 修法：038 以 `JSON_ARRAY` 封裝全部語意欄位取 SHA2，建立 STORED generated
  column 與 `uq_vncs_source_identity` 唯一鍵。同內容合併為更新，一碼多車
  （model_raw／approval_date 不同）指紋互異照樣保留；既有
  `ON DUPLICATE KEY UPDATE` 不用改。
- 踩坑三件：
  1. output assert 用了 `IS_GENERATED` 欄位——那是 MariaDB 的，MySQL 8 沒有，
     CALL 直接 1054。改用 `EXTRA LIKE '%stored%generated%'`。
  2. dirty retry 時 preflight 拒絕已存在的欄位，卡死重試。MySQL 8 DDL 原子性
     保證欄位與索引同生同滅，preflight 改為「欄位已存在即視為已套用」。
  3. 動態 DDL 用 `||` 串字串——MySQL 預設把它當邏輯 OR，PREPARE 收到 `'0'`。
     改 `CONCAT()`。
- 測試期望反轉：原本測試釘住「非 VIN 列重跑會追加」（`codes.count == 2`），
  這正是要修的行為，改為釘「同內容合併、不同內容並存」。

## 8086 站方後台：VIN 車款候選確認流程

- 新增 `GET /station/vins/candidates`（解碼摘要＋候選表）與
  `POST /station/vins/confirm`（建立 mapping）。驗證規則與 8000 API 一致：
  sparse 解碼必須勾人工確認並填依據、`source_name` 三態由伺服端裁決、
  `vin_prefix` 取 VIN 前 11 碼、重複建立轉資料錯誤。
- `vin_vehicle_mappings` 的 generic mutation 維持唯讀（既有測試釘住），新流程
  走專用路由；寫入前 `FOR SHARE` 鎖解碼列。
- 瀏覽器 E2E 補兩段：已對應 VIN 顯示既有對應、無確認表單；未對應 VIN 走
  exact 候選 → 建立對應 → 列表出現 → 8000 API 立即可查 fitment。
- 測試庫 schema 落後造成的 3 個紅測（station_admin view 是舊版 AND 判斷），
  以測試庫重套 migration 解決，不是程式回歸。

## CI skip 契約重新盤點

- 契約自 057acb5 起落後：`runtime-lock tests` 訊息不在白名單、總數 272/56 過時。
- 以 linux 容器重現 CI 兩個 job 的條件實測：unit job（無 MySQL）skip 311、
  e2e job（MySQL＋瀏覽器）skip 72，其中 macOS 侷限測試 72。兩處數字與白名單
  已同步（`ci.yml`、`test_ci_contract.py`）。
- 教訓：批次改寫測試檔的腳本兩次把 `DELETE FROM ... WHERE` 前綴吃掉，靠
  e2e 紅測才抓到。批次改寫後必須立刻 grep 驗證完整語句。

## 正式庫狀態

- migration ledger：001-038 全部 applied、asset `station-admin` 最新、`check` 綠。
- 測試庫同步到 038。
- 全套關卡：ruff／mypy strict／pytest（含真 MySQL 與瀏覽器 E2E）1319 passed。

## NHTSA vPIC 目錄全量與 VIN 缺口收斂

- `nhtsa-api` 加入 daemon 白名單（預設 7 天）：composite `nhtsa` 會連帶抓需求敘述外的
  bulk 資料集，vPIC 全量另走獨立排程。manual 派發會被 lineage 閘控擋下
  （`NhtsaLeaseLostError`），屬設計行為。
- vPIC 全量實測：12,727 artifacts、137,422 列全部 import、0 拒收，約 82 分鐘。
  目錄現況：makes 12,351、models 31,979、manufacturers 22,979、variables 144、
  variable_values 69,969。增量重跑多數請求會命中 ETag／304 快走。
- VNCS VIN 缺口 1,016 碼重新 enqueue（pending 佇列逐筆消化，約 40 分鐘收斂）。
  結果：11 碼新增解碼（2,153 → 2,164），1,005 碼由 vPIC 判定無可用解碼、以
  undecodable 終局。抽樣其 WMI（TMB Škoda、WMA MAN、JM7 Mazda、JTH Lexus、
  RH9 Skyline 等）皆非美規——vPIC 是美國市場資料庫，這些車本來就查不到，
  fail-closed 分類正確。所謂「1,000 碼缺口」的天花板本來就不是 100%。

## 全量模式導入正式證據驗收（commit 1666cc7）

- 正式 full run（daemon 派發）與 bounded 同規格記錄 parser 輸入證據；
  手動 one-shot 維持不記證據、不得發布。
- 既有 `verify_run_evidence` 會把全部 artifact／body 載進記憶體且寫死 target=10,000，
  百萬頁規模不可行。新增串流版：artifact×record 以單一 LEFT JOIN 串流讀取、
  body 走獨立連線主鍵查找、part 對帳用第二條串流（筆數相等＋逐列比對），
  manifest 與 dataset 雜湊增量計算，格式與 bounded 版完全一致。
- 測試的關鍵一筆：同一份資料，串流版與 bounded 版必須算出相同的 manifest／dataset
  雜湊。踩坑兩件：brand record 沒有 parent 是合法的（漏了 bounded 版的特例）；
  `closing(...)` 包住串流產生器，避免中途失敗時 unbuffered cursor 殘列污染連線。
- `archive_full_candidate_parts` 從「僅供診斷」轉正：先封存證據、發布交易內二次
  整算比對，任一步失敗整筆 rollback，線上仍讀上一份 snapshot。
- scheduler：catalog 閘控放行 full 排程（0/0、無 model scope，殘留 scope 即拒絕）；
  run_crawl 子程序的同名閘同步更新；daemon 迴圈僅在 bounded 模式同步 desired scope。
- 全套關卡 1,367 passed（含真 MySQL 與瀏覽器 E2E）。

## 全量爬取啟動

- crawl_run 44、run_key `2026-09`、dataset_kind full、daemon lineage。
  證據預算放大（100GiB／300 萬 artifacts），速率 4 req/s token bucket、8 workers、
  1-2s 隨機延遲。啟動 10 分鐘：1,037 artifacts 全 200、零 challenge。
  觀察點：block 偵測（0 groups 大頁呼吸退避）偶發屬正常防禦，若密集出現需降速。

## 全量爬取首日：receipt bug 與站方 /locate 語意（commits 5dd2ae3、6c568bf）

- **receipt bug（自傷）**：group completion 會寫 `bounded_group_receipts`，其驗證
  只接受 active formal bounded run——full run 每台車完成時都爆 RuntimeError，
  20 分鐘累積 884 台車 error。修正：收據只在 bounded 模式寫入，full 的覆蓋性
  由 `verify_run_evidence_full` 的 part 對帳保證。受害者 724 台，重訪自癒中。
- **站方 /locate 語意（既有長尾，非回歸）**：部分 vehicle/category 頁被站方
  302 回 `locate?c=Toyota&psq=lb`（錯誤訊息補上 Location 後才看清）。8 月
  run 18 就有 211 台同類失敗，一直沒有收斂機制。新政策：轉址目標為 /locate
  時改拋 NotFoundError，既有零件組依 404 組語意收斂
  （`mark_vehicle_groups_not_found`：清 membership＋標 not_found），車輛以
  空資料或其餘分類完成。其餘轉址維持 fail-closed。
- **vid=0 是常態**：全站車輛 vid 都是 0（bounded 10k 也是），車輛身分靠
  name＋model_code，不是 vid。
- **待查**：23 台車出現「fetched page does not identify expected unit uid」
  ——站端 uid 與 8 月記錄不同，身分守衛拒收。量小，先觀察。
- 重啟語意實測有效：同月 run_key 重用 run id、done 車輛跳過、receipt 已標
  的組跳過。車輛 done 標記在品牌邊界才 commit（外部查詢看不到未提交列，
  既有設計，非資料遺失）。
- 測試隔離教訓：daemon 退避測試沒 mock job lock，本機正式 daemon 一跑就撞
  真實 flock。fixture 統一 mock。
- 現況：crawl_run 44 走到 Lexus，15,931 組 done、256,476 parts current、
  18,644 artifacts、零 challenge。

## migration 039：正式 snapshot 切換全量（commit ae02182）

- 新增切換閘 `full_ready`：published_parts 全部來自同一個 full run（success、
  證據已封存、catalog daemon completed exit=0、單一 linked crawl）時 current
  view 改讀全量；任一條件不成立維持 bounded 10k。兩分支互斥不並存。
- 踩坑三件：CTE body 不能巢狀 WITH（全部攤平頂層）；CTE 引用須先定義後使用
  （full_snapshot 移到 chosen 前）；full_snapshot 的 ON 殘留 full_ready 的
  `published` 別名（1054 一小時才抓到）。
- 契約同步：CATALOG_MANIFEST sha、runner ledger 清單與 apply 期望延伸到 39、
  敘述句 1 筆（總和 726）、fresh schema 視圖測試改釘切換語意。
- 正式庫已手動套用（view 已切換語意、bounded 10k 照常輸出）；ledger 補記
  需等 crawl_run 44 結束（runner 拒絕 running writer），屆時 apply 為冪等。
- 測試庫 ledger 已到 39。
