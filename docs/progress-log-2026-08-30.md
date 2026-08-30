# 2026-08-30 正式 bounded 資料契約與修正紀錄

> 本文件描述本次程式碼的發布契約與驗證範圍。它不替代正式資料庫的即時
> 狀態；主爬蟲與正式資料庫是否符合契約，必須在每次實際排程完成後重新讀取。

## 正式資料邊界

PartSouq 正式讀取只使用 `v_current_catalog_parts`。此 view 只會回傳同時滿足
以下條件的 bounded snapshot：

- crawl run 是 daemon 觸發、`bounded_success`、目標與實際零件數都是 10,000。
- snapshot 的品牌、型號與年份下限符合資料庫內目前啟用的 bounded scope。
- live HTTP artifact、已接受的 part record、dataset digest 與 scheduler 完成狀態
  都能對上。
- 每一筆 `bounded_parts` 都綁定自己的 evidence record digest；後續 raw crawl
  改寫 `parts` 不會改變已發布 snapshot 的證據。

未完成、缺證據、scope 不一致，或只存在於 raw/full candidate 的資料都不會進入
正式後台或 VIN fitment view。這是 fail-closed 行為，不是資料遺失的修復機制。

## 本輪修正

- 爬蟲與 recover 共用資料庫 runtime lock。不同工作目錄或 container 不能只靠
  本機檔案鎖並行改寫同一份型錄。
- CloakBrowser page-fetch 子程序先原子發布完整 HTML 或錯頁 marker，再關閉 Chromium。瀏覽器
  關閉卡住時，父程序仍可讀取已完成的輸出並回收子程序。
- marker 已提交後若 admission lock 釋放失敗，crawl run 會收尾為 `error`，不會
  留下無法判讀的 `running` marker。
- bounded snapshot 發布前後都重算證據。資料列、scope、artifact 與 accepted
  record 任一項不一致時，舊 snapshot 會保留。
- station-admin 的正式資料來源改為 current view。raw source ID 不可透過後台
  編修或當成已發布資料。

## 舊 snapshot 升級

migration 032 會為既有 `bounded_parts` 嘗試回填 evidence record digest。只有在
同一 crawl run 的 part、artifact 與 snapshot 欄位都能唯一且完整對上時才回填。
migration 035 另外剔除已綁 evidence digest、但 normalized 料號無法由 snapshot
原始料號重算的 legacy 列。無法證明來源的列不會進入正式 view，等待新的可驗證
bounded run 重新發布。
migration 不會猜測或補造證據。

migration 029 前的 bounded run 沒有 scope，而 030／031 要求精確比對目前 singleton
scope。因此舊 10,000 筆 snapshot 套用後會被刻意隱藏；這不是原始資料遺失，必須由
新的 scoped 10,000 筆 daemon run 重新發布。

## 未完成的需求邊界

- NHTSA 的資料欄位以官方解碼回應為準。缺少的 Model、Engine、Displacement 或
  Trim 不會由 PartSouq 猜填。
- VIN 與 PartSouq 車款 mapping、VIN 零件 fitment 只有在來源欄位與人工確認條件
  都符合時才會出現。不能把品牌相同當成已確認 mapping。
- raw full crawl 不具備本次 bounded evidence 契約，不能宣稱為正式發布資料。

## 本次驗證

以下為 migration 035 與所有歷史回放預期同步後的最終 snapshot 驗證。驗證只使用
暫時建立、名稱以 `_test` 結尾的隔離 MySQL；沒有寫入 `partsouq_catalog`
主資料庫，也沒有中斷既有 daemon。

| Gate | 結果 |
| --- | --- |
| 完整 pytest（MySQL gate＋真實 Chrome E2E） | 1,281 passed、0 failed、0 skipped、0 errors |
| station-admin 真實 Chrome＋MySQL E2E | 9 passed |
| 一個 VIN 對 verified 10,000 筆 snapshot 的 mapping integration | 1 passed |
| migration recovery target | 4 passed |
| bounded scope／發布／evidence target | 10 passed |
| station-admin transaction target | 8 passed |
| Ruff check／format（全專案） | 通過，170 files already formatted |
| strict mypy（全 production source） | 通過，104 source files、0 issues |
| `git diff --check` | 通過 |

browser E2E fixture 也改為建立正式路徑所需的 verified 10,000 筆 bounded snapshot，
而非把 raw 1,000 筆資料當成正式資料。它驗證 scope、evidence、quarantine、VIN、
station overlay 與 snapshot isolation；所有資料在測試結束後隨暫時資料庫刪除。

這些結果只證明程式契約與隔離環境行為。它不代表 full crawl、live NHTSA 欄位完整度，
或 confirmed VIN mapping／VIN fitment 已在正式資料庫完成。

## 08-30 晚間獨立驗證與正式機收斂

本節由另一次工作階段補記。三個 subAgent 分頭複核 057acb5：程式碼審查
（admission、crawler、repositories、scheduler、cloak 與 migration 028–035）
沒有找到支援路徑上的具體缺陷；契約同步四項——CI skip 272/56、MANIFEST
35 筆 SHA256、敘述句總和 675、ledger 刪除範圍連續涵蓋到 035——全部相符。
ruff 與 strict mypy 全綠。

pytest 起初跑出一堆失敗，逐輪定位後確認全是環境落差，不是程式問題：

- 先 source .env 再跑測試，`PSQ_BOUNDED_*` 會觸發新版 scope 驗證，擋掉
  37 個測試的 Crawler 建構。
- 改用乾淨環境，這次缺 DB 憑證。compose 的 MySQL 密碼只從 .env 來，
  config.py 預設密碼對不上，157 個 Access denied。
- 測試資料庫停在 migration 29，ledger 記的 029 checksum 又與現行檔案
  不符；舊 mysql 容器沒有 `--log-bin-trust-function-creators=1`，033 的
  trigger 裝不上。

處理：重建 mysql 容器；砍掉重建 `partsouq_catalog_test`（先載 `db/catalog.sql`
與 `db/nhtsa.sql` 基線，再 apply 到 035）；順手刪掉三個殘留沙箱
（`partsouq_gate_20260830_test` 和兩個 `partsouq_migration_*_test`）。
之後全套 1,281 案 exit 0，0 失敗。

這裡學到一件事：AGENTS.md 的標準關卡指令在這台機器跑不動，要先載 .env
的 DB 憑證、再 unset 全部 `PSQ_*`。文件還沒改，先記在這。

命名違規修正（已改、尚未提交）：`publish_bounded_parts` 的 scoped_source
計數查詢與 source SELECT、`discard_invalid_bounded_membership` 的 UPDATE
JOIN 與 scope_clause，單字母別名改成 part／source_group／category／
vehicle／model／brand；`test_partsouq_bounded_limit.py` 與
`test_partsouq_live_evidence.py` 的 SQL 斷言與 mock 跟著同步。第 656 行
附近的 source_valid 子查詢也是單字母別名，但那是 1eb653d7 的既有碼，
依審查規則不夾帶。

正式機重啟：LaunchAgent 以 057acb5 release 啟動（排程間隔 2,592,000 秒）。
migration 028–035 已套到 `partsouq_catalog`，desired scope singleton 為
toyota/tacoma/2006。bounded run 43 於同日 19:31 以 `bounded_success` 收尾：
2 車款、427 組、精確 10,000 筆全新 parts，過程零 ERROR／限流／逾時；
`v_current_catalog_parts` 原子發布 10,000 筆，啟動時的 fail-closed 空窗
就此關閉，正式讀取恢復。啟動初期 stderr 出現的「running jobs exist」
升級失敗，是多個啟動 child 競爭的暫態——一個 child 先建了 running run，
另一個的 030 preflight 被擋；migration 套完就不再發生，沒有資料影響。

## 08-31 receipt 契約與後台健康檢查

本輪新增 migration 036。它把正式 bounded snapshot 的群組來源固定成
`bounded_group_receipts`，並讓 current view 同時核對 receipt、artifact、
accepted part 與 snapshot 成員。`done` 必須全數收錄；`partial` 必須是有收錄、
但未全數收錄。已發布 run 的 receipt 不可更新或刪除。

crawler 也補了兩個漏記路徑：同一頁同時有可收錄與品質閘門排除的具名稱料號，
以及續爬時已看過可收錄料號但仍有被排除的具名稱料號。兩種情況都會寫入
`part_quarantine`，不會讓未發布列消失。bounded 續爬的 SQL 少一個括號，會讓
MySQL 8.4 解析失敗；已修正並加入回歸測試。

兩個後台的 health/readiness 先驗證 migration 036 的 schema、view、trigger 與
索引契約，再查資料。資料庫仍停在舊 schema、或 quarantine 索引缺失時，現在會
受控回傳 503，不會洩漏原始 MySQL 500。fresh `admin.sql` 也同步使用與 migration
相同的 join 順序與索引，避免歷史 artifact 很多時掃描整張 artifact record 表。

NHTSA ErrorCode 改為辨識逗號分隔的多個 code；station-admin 顯示
`decode_completeness`，不會把缺 Model、Engine 或 Trim 的解碼資料誤當成可做嚴格
fitment 的完整資料。

本輪第一次完整 gate 的兩項 failure 是 E2E fixture 只載入 `catalog.sql`，卻用
migration 036 的後台 health 契約驗證索引缺失。fixture 改為載入完整 fresh schema
後，測試真正驗到缺索引時的 503 行為。

驗證使用暫時建立的 `partsouq_fullgate_20260831_test`：

| Gate | 結果 |
| --- | --- |
| migration 001–036 replay 與 `check` | 通過 |
| 完整 pytest | 1,293 passed、9 skipped、0 failed（共 1,302 項） |
| Ruff check／format | 通過，170 files already formatted |
| strict mypy（本輪 6 個 production module） | 通過，0 issues |
| `git diff --check` | 通過 |

本輪沒有手動寫入正式 `partsouq_catalog`，也沒有停止或重啟正式 daemon。測試結束後已
刪除上述暫時資料庫。正式 ledger 目前只到 migration 035；唯讀 `check` 會先因
`asset:station-admin` 的既有 checksum drift 停止，因此 migration 036 尚未套用。正式
資料何時升級，仍必須先依既有流程處理 station-admin asset，再走排程與 migration。VIN 與
PartSouq 的 confirmed mapping、嚴格 VIN fitment，以及真正三層零件分類，仍不能從目前
來源資料自動補造。
