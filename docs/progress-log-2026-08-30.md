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
