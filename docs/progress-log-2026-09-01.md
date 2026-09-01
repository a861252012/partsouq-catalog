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
