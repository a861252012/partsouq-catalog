# 2026-08-29 爬蟲一致性修正與驗證紀錄

> 歷史紀錄。此文件只描述 2026-08-29 的快照，不代表目前程式或正式資料庫狀態。
> 現行契約與驗證邊界請看 `docs/progress-log-2026-08-30.md`。

## 修正範圍

本輪以 `299e73a` 為基準，只處理已重現的資料一致性與瀏覽器生命週期問題。

1. unit 頁身分驗證改為完整比對 `uid`。請求中若已有 `c`、`vid`、`cid`，
   也必須與瀏覽器落點及頁面 canonical／hidden 欄位一致。只接受
   `https://partsouq.com` 的標準連線。
2. 當日版本的 CloakBrowser 子程序先正常關閉瀏覽器，再原子發布 HTML 或錯頁 marker。
   此順序已在後續版本調整；歷史結論不得視為現行 Chromium 生命週期契約。
3. `--recover-only` 與 full crawl 共用 `crawler.lock`。部分失敗會回傳 exit 1，
   獨立 recover run 會標成 `error`，不再留下假的成功紀錄。
4. full run 收尾會補抓所有非本 run 的 group receipt。正式發布前再檢查：
   - 所有 group 都屬於本 run，且狀態只能是大小寫完全相同的 `done`／`not_found`。
   - `done` 的 `fetched_row_count` 必須等於本 run 的實際零件列數。
   - `not_found` 的 receipt 與本 run 零件列數都必須是 0。
   - `recover-*` 維護 run 不得進入正式發布。
5. 續跑非 terminal crawl run 時會清掉上一輪的 `finished_at` 與 `error_msg`。

## 驗證結果

| Gate | 結果 |
|---|---|
| Ruff check／format（全專案） | 通過 |
| strict mypy（本輪 5 個 production modules） | 通過，0 錯誤 |
| 完整 pytest＋MySQL gate | 1,147 tests：1,138 passed、9 skipped、0 failed |
| station-admin 不啟動真 Chromium 的 E2E／preflight | 7 passed |
| 測試 DB 清理 | 6 個主要表均為 0 列 |
| `git diff --check` | 通過 |
| 獨立 adversarial review | 發布 gate、Chromium、UID、recover 均無剩餘 finding |

完整 gate 的 9 個 skip 都來自 `STATION_ADMIN_E2E=1` 選配測試。為避免再次觸發
macOS 的「Chromium 未預期結束」警示，本輪只執行其中 7 個不啟動真瀏覽器的案例；
2 個會啟動真 Chromium 的 UI 案例沒有執行，不能寫成已通過。

## 資料與執行邊界

- 本輪 MySQL 測試只使用 `partsouq_catalog_test`，結束後已確認 fixture 清空。
- 正式 DB 只做唯讀查詢。重啟前 full run 18 仍為 `running`，有 434 個 error、
  437 個 pending，且有 4,820 個 group 尚未持有 `2026-08` 的有效 receipt。
- 22:07 在確認沒有 Chromium／CloakBrowser 子程序後，正常中斷仍載入舊碼的
  crawl `PID 7574`。scheduler 記錄 `exit=-15`，依既有機制在 60 秒後自動重試。
  22:08 新 crawl `PID 52939` 啟動，重用既有 cookie 並從 Toyota 續爬；沒有清 DB。
- 因此本輪只證明修正版會阻擋不完整發布，不能宣稱 full catalog 已正式發布。
- 本輪沒有重跑 10,000 筆正式爬取，也沒有改寫 NHTSA 或 VIN mapping 資料。
- 本輪尚未 commit／push；需由使用者明確要求後才執行。

## 仍待後續驗收

1. 在非 Codex sandbox、且不會彈出 macOS crash alert 的 runner，執行 2 個真瀏覽器
   station-admin E2E。
2. 讓目前 full crawl 以新程式續跑並收斂 error／pending／receipt，再觀察發布 gate。
3. full snapshot、NHTSA 欄位完整度、confirmed VIN↔PartSouq mapping 與 VIN fitment
   仍須依正式資料另行驗收，不能由本輪單元與整合測試代替。
