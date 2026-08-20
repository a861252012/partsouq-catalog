# UI／UX 簡報需求對照（2026-08-20）

依據 `車勢零件供應網UI與UX.pptx` 第 3、4、24、25、27 頁，逐項區分已完成、
已有結構但缺正式資料，以及尚未實作。畫面或 fixture 通過不等於 live 資料完成。

## 已完成並有自動驗證

- 料號搜尋與保存會移除空白、連字號並轉大寫；`32-234`、`32234`、
  `32 234` 會使用同一個正規化值。
- 8086 後台預設每頁 30 筆，可選 10／25／30／50／100／200，並可直接輸入頁碼。
- 爬蟲來源列保持不可變；人工修改另存 overlay、revision、actor、reason 與
  append-only audit event。
- 零件料號或英文名稱的 active overlay 會同步反映到 8000 的 sample、published、
  料號適用關係與 VIN 零件 API；停用後不再供應，恢復後重新供應。
- 真實 Browser E2E 會建立獨立 `_test` MySQL、套四份正式 schema，以 Chrome
  編輯資料，再反查四條 API、overlay event、原始 `parts` 與 immutable
  `published_parts`。測試也驗證未發布 normalized 更新不會污染 published API，
  來源在編輯期間改變時舊表單回 409，重新載入後才可 rebase。
- NHTSA 官方 reference sync 與逐 VIN decode 分開顯示，避免把主檔筆數冒充
  VIN 最終資料。

## 結構已完成，但目前資料不足

- NHTSA 已有 VIN、Make、Model、ModelYear、EngineConfiguration、EngineModel、
  DisplacementL、Trim 的 schema、解析、共用 DB 與 fixture E2E；目前沒有使用者
  提供／授權的 VIN，所以 live decode、VIN 車款 mapping 與 VIN 零件仍為 0。
- PartSouq 已有料號、產品名、品牌、型號、車款生產區間、大分類、Group 中分類與
  關聯 ID；目前 1,000 筆是 browser-assisted sample，不是正式排程發布。
- 這批 sample 的 unit 頁沒有可證明的逐料號日期欄，故 `part_range` 全空；年份只
  能標示車款生產期間，不能宣稱每一料號的精確適用月份已 100% 確認。
- 目前 live sample 只證明兩層分類。沒有足夠來源證據可建立獨立小分類，不能用
  Group、Code 或其他欄位冒充。

## 尚未實作，屬於供應網／商城產品範圍

簡報第 24、25 頁的本站商品編號、產品類別、銷售量、零件等級、零售／團購價、
上下架、保固、價目表匯入匯出、銷量排序與「確認且建立下一筆」不屬於原兩包
crawler/admin 的既有資料模型，目前沒有實作。這些功能需要先決定價格來源、
商品與 OEM 料號關係、權限及匯入格式，不能在沒有產品決策時假裝完成。

## 驗收邊界

目前可以宣稱「crawler、共用 DB、可編輯後台與有效值讀取路徑已整合並通過
fixture／Browser E2E」。不能宣稱「正式 PartSouq 無人值守排程已成功」、「VIN
最終資料已完成」或「簡報中的整套供應網商城已完成」。
