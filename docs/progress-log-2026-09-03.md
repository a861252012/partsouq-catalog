# 進度紀錄 2026-09-03

承接 09-02 的交接（VIN 路徑被 reCAPTCHA 擋死、run 44 中斷、scheduler idle）。
本日盤點出覆蓋率的真正缺口，修好三個卡住 full run 的問題，排程切回全量。

## 盤點：覆蓋率缺口在哪

交接文件說「全量資料已在庫」，實查不是這樣。parts／groups 三品牌分布：

| 品牌 | vehicles | groups | parts |
| --- | --- | --- | --- |
| Lexus | 2,177 | 115,402 | 2,331,060 |
| Nissan | 363 | 65,542 | 359,755 |
| Toyota | 1,191 | 77,780 | 1,199,089 |

DB 只有 Lexus、Nissan、Toyota 三個品牌。其餘廠牌（Honda、Mazda、Subaru、
Hyundai、Kia、Suzuki、Mitsubishi、Infiniti、Isuzu、Renault、Volvo、Chrysler、
Jeep、Dodge、Ram）從未爬過。

原因是品牌來源：crawler 的 `_brands()` 只解析首頁 `/en/catalog/genuine` 側欄，
而側欄從 8/22 起就一直只列 16 到 18 個品牌（run 44 全程的 genuine 證據都是
16 筆解析結果，Toyota、Kia 不在其中）。站方真正的完整清單在
`/en/brands-16.html`，用 CloakBrowser 實抓驗證：18 個品牌、
`li a[href]` 結構跟首頁一樣，現有的 `parse_brands` 直接就能解析出 18 筆、
0 malformed。robots.txt 只禁 `/cdn-cgi/`，這頁合法可爬。

所以「全廠牌覆蓋率」的天花板就是這 18 個品牌。VIN 補爬仍被 reCAPTCHA
擋著，維持現狀；先把瀏覽樹能爬的爬滿。

## 修一：品牌來源改抓總覽頁

`crawler._brands()` 改成先抓 `SITE["brands"]`（/en/brands-16.html），
解析成功就用；404 或挑戰頁時退回首頁側欄，兩邊都失敗才報錯
（fail-closed 不變）。證據契約沿用 `("genuine", "parse_brands")`，
不用動 page_type 白名單。

牽動的閘一共四處：

- `evidence.public_source_url`：canonical path 放行 `/en/brands-16.html`。
- `http_client._ensure_catalog_allowed`：brands 頁納入 robots 檢查範圍。
- `http_client._is_catalog_url`：brands 頁被挑戰時也走瀏覽器後備。
- `cloak.fetch_page` 的 IDENTITY_KEYS：加 `/en/brands-16.html`，落點
  校驗才認得了這頁。

## 修二：recover_null_groups 的證據守門

run 44 收尾時 62,736 組 NULL 全部恢復失敗，錯誤是
`formal part evidence requires its vehicle context`。原因：
`recover_null_groups` 呼叫 `crawl_group` 時沒傳 `evidence_vehicle_key`，
evidence_mode 下必炸；手動 `--recover-only` 不記證據所以以前沒事，
full run 內嵌的 recover pass 一跑就爆。

修法：`list_null_groups` 的 SQL JOIN 出 brand/model/vehicle 識別欄位
（含 model_name、brand_name），recover 迴圈用 DB 身分組
`vehicle_record_natural_key` 傳給 `crawl_group`。順帶把
`_brand_from_url(url) or "Toyota"` 的硬編碼後備降級成缺欄位時的最後手段。

## 修三：排程支援 full 模式

`deploy/run-macos-catalog-scheduler.zsh` 硬編碼
`PSQ_BOUNDED_PARTS=10000`， Toyota/TACOMA 的 bounded scope 已收斂，
排程每次派發都失敗（`found 0`），run 51 之後 scheduler 進入 30 天 idle。

run 腳本與 installer 改成從 scheduler.env 讀
`PSQ_LIMIT_PARTS`／`PSQ_BOUNDED_PARTS`：兩者皆 0 走 full（model scope
必須為空，殘留即拒絕），否則維持 bounded 校驗。`.env` 已切 full
（備份在 `.env.bounded-toyota.bak`），爬取參數調回全量設定
（8 workers、4 req/s、burst 8）。

## 契約同步：040 留下的三個尾巴

前個 session 加 migration 040 時漏了幾處同步，全套測試才現形：

1. e2e 降級輔助函式的 ledger 刪除清單停在 39：重套時 gap 檢查報
   `ledger skips an active migration before 040`。17 處清單補 40。
2. 版本序列斷言（單行與多行兩種格式）硬編碼到 39，共 16 處補 40。
3. `040_vin_resolved_uids.sql` 用裸 `CREATE TABLE`，降級測試重套時
   報 errno 1050。改成 `CREATE TABLE IF NOT EXISTS`（023 本來就是這樣寫），
   CATALOG_MANIFEST 的 sha256 同步更新。正式庫已套用不受影響。

另外 `test_mysql_full_candidate_archive_requires_verified_evidence` 與
`test_full_candidate_archive_preserves_source_ids_without_formal_mapping`
的 view 期望停在 039 之前：daemon exit=0 後 `v_current_catalog_parts`
按 039 切換閘應輸出全量快照，測試卻還期望 0 筆或 None。兩處期望
同步為 039 語意。用 git stash 驗證過這些在 HEAD（65b6a31）上也紅，
不是本次修改造成。

## 測試與關卡

- 新增 `tests/test_brands_index_source.py`：總覽頁解析、證據 URL 放行、
  `_brands()` 優先順序與 fallback、雙來源全失敗 fail-closed。
- `test_group_closure_and_quarantine.py` 加 evidence_mode 的 recover
  測試，釘住「必須組出 vehicle natural key」；fixture 補 DB 身分欄位。
- `test_unified_project.py` 加 full 排程的 runtime 測試（空 scope 傳遞、
  殘留 model scope 拒絕），靜態斷言同步。
- 全套關卡：ruff、mypy strict（六個更動檔）、pytest（真 MySQL）
  1,334 passed、9 skipped。

## 部署：兩次失敗後 full run 恢復運轉

重建發布包沒有一把過，踩到兩個新問題：

1. **正式庫 ledger checksum drift（migration:040）**。前個 session 套用
   040 之後，040 的 SQL 後來改成 `IF NOT EXISTS`、sha256 跟著變了，
   正式庫 ledger 卻還記著舊雜湊。scheduler 啟動時的 apply 直接報
   `ledger checksum drift` 拒絕啟動，installer 的 child readiness
   等不到 marker，回滾又因 launchctl EIO 失敗（job 留在未載入狀態）。
   修法：UPDATE ledger 的 040 sha256 到新值，`migrations check` 綠後
   重裝成功。
2. **evidence 預算還停在 bounded 預設**。run 44 resume 後立刻撞
   fail-closed 儲存預算（scheduler.env 沒設 PSQ_EVIDENCE_MAX_*，
   預設 1GiB／50,000 artifacts，對百萬頁 full 遠遠不夠）。依 09-01
   全量設定補進 `.env`：body 8MiB、run 100GiB、artifacts 300 萬。

## migration 041：品牌總覽頁納入證據 URL 約束

預算放行後 full run 還是斷在
`Check constraint 'chk_partsouq_artifact_public_url' is violated`——
017 的 URL 約束只認 /en/catalog/genuine 前綴，brands-16.html 的
輸入證據寫不進去。程式端放行了、資料庫端沒跟上。

041 以冪等 procedure 重建約束：精確放行
`https://partsouq.com/en/brands-16.html`，禁 ssd 與禁 fragment
不變。CATALOG_MANIFEST（041、sha256）、敘述句清單（+6、總和 733）、
e2e 降級清單與版本序列斷言（40 → 41）全部同步。正式庫已套用，
check 綠。

## 現況

- 發布包 895c552（含全部修復），launchd scheduler 運轉中。
- crawl_run 44 恢復 running（daemon lineage 6887）：done 車輛跳過、
  1,991 個 error/pending 重試、15 個新品牌從頭爬、62,736 組 NULL
  由修好的 recover pass 收斂。
- 啟動觀察：最近 2 分鐘 13 個 artifact、全 200、零 challenge。

## 下一步

監督 full run 收斂：爬完 18 個品牌後 run 44 應以 success 收尾，
屆時 039 的 full_ready 閘放行全量 snapshot。VIN 補爬維持擱置，
等有 reCAPTCHA 解法再說。

## 收攤：案子停止，爬取終止

下午把 Toyota 的 redirect 問題查透了：站方把
`locate?c=Toyota` 永久 301 到品牌著陸頁 `/catalog/toyota`，那頁
沒有型號清單（0 個 pick 錨點），品牌層重試會無限失敗。同一份
型號清單在 `pick?c=Toyota` 可取得（275 個型號、0 malformed），
修復已寫好——locate 撞這種 301 時改抓 pick 層，解析契約原樣
複用。

修到這裡，整條 full run 的路徑上已經累積了太多站方行為的
例外處理：首頁品牌清單浮動、brands 總覽頁要另外抓、URL 約束
跟著改、evidence 預算要調、 Toyota 的 locate 又被 301。每一
項都修得起，但站方行為不受我們控制，下次正常化改版又是新一
輪。加上 VIN 補爬被 reCAPTCHA 擋死，專案決定停損：full crawl
不繼續，正式資料維持 Lexus／Nissan／Toyota 三品牌約 388.9 萬
筆 parts。

收攤狀態：

- LaunchAgent 已 bootout，plist 也移除了，重啟電腦不會復活。
- crawl_run 44 停在 running 殘留；沒有排程器在跑，不會有人
  動它。殘留 marker 依 900 秒回收規則留著無妨。
- Toyota pick 後備修復連同測試 commit 備查，未部署。
- 正式庫 migration 041 已套用，`migrations check` 綠。
