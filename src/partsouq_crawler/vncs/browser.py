"""VNCS 瀏覽器路線：Playwright 驅動真實 Chromium，呼叫頁面自身的
Infragistics WebDataGrid JS API 翻頁。

背景（2026-08-23 實測）：目標站 https://vncs.moenv.gov.tw/VNCSEXLRPT.aspx 是
ASP.NET WebForms + Infragistics WebDataGrid（id=wdgMain）。篩選可用傳統
form POST 觸發；但翻頁走 Infragistics 私有 AJAX 協定（__IGCallback_wdgMain），
純 HTTP 逆向失敗，因此改由真實瀏覽器執行頁面自己的翻頁 API。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Any, Self

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from partsouq_crawler.vncs.config import VncsConfig
from partsouq_crawler.vncs.parser import VncsParserError, parse_grid_records, parse_vehicles

GRID_ID = "wdgMain"
FILTER_SELECT_SELECTOR = "#dlFtrMOBTYPE"
# dlFtrMOBTYPE 選項值：''（全部）/ G 汽油車 / D 柴油車 / M 機車；政策只抓 G、D。
KIND_OPTION_VALUES = frozenset({"G", "D"})

# 官方頁 inline script 的 paging 狀態 JSON（2026-08-23 實測：G → pc=686）：
# ["Paging"],{"c":{"ps":10,"pa":1,"pc":686,"pi":0}
PAGING_JSON_PATTERN = r'\["Paging"\],\{"c":\{"ps":(\d+),"pa":(\d+),"pc":(\d+),"pi":(\d+)'
_PAGING_JSON_RE = re.compile(PAGING_JSON_PATTERN)

# 2026-08-23 於真站驗證可用的 API 路徑（兩個入口都指到同一顆控制項）：
#   window.ig_controls['wdgMain'] === $find('wdgMain')
#   .get_behaviors().get_paging().set_pageIndex(<0-based>) / get_pageIndex() /
#   get_pageCount()
# 安裝階段把「找到 paging behavior」封裝成 window.__vncsFindPaging：優先
# get_behaviors().get_paging()，取不到時再防禦式列舉 behaviors collection，
# 找第一個有 set_pageIndex 的行為物件（不同 Infragistics 版本 API 名可能不同）。
_INSTALL_PAGING_HELPER_SCRIPT = r"""
() => {
  const findGrid = () => {
    try {
      const controls = window.ig_controls || {};
      if (controls['wdgMain']) return controls['wdgMain'];
    } catch (e) {}
    try {
      if (window.$find) {
        const found = window.$find('wdgMain');
        if (found) return found;
      }
    } catch (e) {}
    try {
      if (window.Sys && window.Sys.Application) {
        const component = window.Sys.Application.findComponent('wdgMain');
        if (component) return component;
      }
    } catch (e) {}
    return null;
  };
  const findPaging = () => {
    const grid = findGrid();
    if (!grid) return null;
    let behaviors = null;
    try { behaviors = grid.get_behaviors(); } catch (e) { behaviors = null; }
    if (!behaviors) return null;
    try {
      const paging = behaviors.get_paging();
      if (paging && typeof paging.set_pageIndex === 'function') return paging;
    } catch (e) {}
    let length = -1;
    try {
      length = typeof behaviors.get_length === 'function'
        ? behaviors.get_length()
        : (typeof behaviors.get_count === 'function' ? behaviors.get_count() : -1);
    } catch (e) { length = -1; }
    for (let i = 0; length >= 0 && i < length; i += 1) {
      let behavior = null;
      try {
        if (typeof behaviors.getBehavior === 'function') behavior = behaviors.getBehavior(i);
        else if (typeof behaviors.getItem === 'function') behavior = behaviors.getItem(i);
      } catch (e) { behavior = null; }
      if (behavior && typeof behavior.set_pageIndex === 'function') return behavior;
    }
    return null;
  };
  window.__vncsFindPaging = findPaging;
  // 表格內容簽章：偵測 AJAX 翻頁後 DOM 是否真的換了內容。
  window.__vncsGridSignature = () => {
    const cells = document.querySelectorAll('#wdgMain td');
    const parts = [];
    for (let i = 0; i < cells.length; i += 1) parts.push(cells[i].textContent);
    return parts.join('|');
  };
  // 資料列提取。2026-08-23 實測 DOM 結構（Infragistics WebDataGrid）：
  // 每筆邏輯記錄是一個外層 <tr>（TH 列號 + TD 車輛種類/車型名稱/車型年份 +
  // 一個 colspan=12 的包裝 TD；包裝 TD 內有巢狀 table，其單一 tr 的 TD 就是
  // 受測轉速…原地檢測模式 等 12 欄的值）。欄名 th 位於另一個「表頭外層 tr」
  // 的巢狀 table 內，因此用跨列狀態機先記住欄名再對應資料值。
  // 車型名稱被伺服器截斷顯示（…），完整名稱放在 span title，故以 title 覆寫。
  window.__vncsGridRows = () => {
    const gridEl = document.getElementById('wdgMain');
    if (!gridEl) return [];
    const cellText = (el) => {
      const titled = el.querySelector ? el.querySelector('span[title]') : null;
      return ((titled && titled.getAttribute('title')) || el.textContent || '').trim();
    };
    const LEFT_KEYS = ['車輛種類', '車型名稱', '車型年份'];
    const rowsOut = [];
    let groupNames = null;
    for (const table of Array.from(gridEl.querySelectorAll('table'))) {
      for (const tr of Array.from(table.rows)) {
        const cells = Array.from(tr.cells);
        const wrapper = cells.find(
          (c) => c.colSpan >= 2 && c.querySelector && c.querySelector('table')
        );
        if (!wrapper) continue;
        const innerTable = wrapper.querySelector('table');
        if (!innerTable || !innerTable.rows.length) continue;
        const innerRow = innerTable.rows[0];
        const innerCells = Array.from(innerRow.cells);
        const left = cells
          .filter((c) => c.tagName === 'TD' && c !== wrapper)
          .filter((c) => !(c.querySelector && c.querySelector('table')))
          .map(cellText);
        if (left.length === LEFT_KEYS.length && !innerCells.some((c) => c.tagName === 'TH')) {
          if (!groupNames) continue;
          const record = {};
          for (let j = 0; j < LEFT_KEYS.length; j += 1) record[LEFT_KEYS[j]] = left[j];
          const values = innerCells.map(cellText);
          for (let j = 0; j < groupNames.length && j < values.length; j += 1) {
            const name = groupNames[j];
            if (!name || /^\d+$/.test(name)) continue; // 跳過空欄名與分頁數字欄
            record[name] = values[j];
          }
          const kind = record['車輛種類'] || '';
          if (kind !== '汽油車' && kind !== '柴油車' && kind !== '機車') continue;
          rowsOut.push(record);
        } else if (innerCells.some((c) => c.tagName === 'TH')) {
          groupNames = innerCells.map((c) => (c.textContent || '').trim());
        }
      }
    }
    return rowsOut;
  };
  return true;
}
"""

_PAGING_STATE_SCRIPT = """
() => {
  const paging = window.__vncsFindPaging ? window.__vncsFindPaging() : null;
  if (!paging) return null;
  const state = { pageIndex: null, pageCount: null };
  try { state.pageIndex = paging.get_pageIndex(); } catch (e) {}
  try { state.pageCount = paging.get_pageCount(); } catch (e) {}
  return state;
}
"""

_SET_PAGE_INDEX_SCRIPT = """
(pageIndexZeroBased) => {
  const paging = window.__vncsFindPaging ? window.__vncsFindPaging() : null;
  if (!paging || typeof paging.set_pageIndex !== 'function') {
    throw new Error('VNCS grid paging behavior not found');
  }
  paging.set_pageIndex(pageIndexZeroBased);
  return true;
}
"""

_GRID_SIGNATURE_SCRIPT = "() => (window.__vncsGridSignature ? window.__vncsGridSignature() : '')"

_GRID_ROWS_SCRIPT = "() => (window.__vncsGridRows ? window.__vncsGridRows() : [])"

_WAIT_FOR_PAGE_SCRIPT = """
([targetPageIndex, previousSignature]) => {
  const paging = window.__vncsFindPaging ? window.__vncsFindPaging() : null;
  if (!paging) return false;
  let pageIndex = null;
  try { pageIndex = paging.get_pageIndex(); } catch (e) {}
  if (pageIndex !== targetPageIndex) return false;
  if (!window.__vncsGridSignature) return true;
  return window.__vncsGridSignature() !== previousSignature;
}
"""

_CLICK_FILTER_BUTTON_SCRIPT = """
() => {
  const button = document.getElementById('ubnDoFilter')
    || document.querySelector('input[name="ubnDoFilter"]');
  if (!button) return false;
  button.click();
  return true;
}
"""

_ROWS_CALLBACK = Callable[[list[dict[str, object]]], None]


class VncsBrowserError(RuntimeError):
    """瀏覽器路線的 fail-closed 錯誤（找不到 grid/按鈕或翻頁逾時）。"""


class VncsBrowserHarvester:
    """單一車輛種類一個瀏覽器 session；harvest() 內逐頁解析並回呼 on_rows。"""

    def __init__(self, config: VncsConfig) -> None:
        self.config = config
        # 測試可覆寫為 0 以加速；正式值跟隨 rate_limit_seconds。
        self.inter_page_delay = config.rate_limit_seconds
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None

    async def __aenter__(self) -> Self:
        manager = async_playwright()
        self.playwright = await manager.start()
        self.browser = await self.playwright.chromium.launch(
            headless=self.config.browser_headless,
            args=["--disable-blink-features=AutomationControlated"],
        )
        self.context = await self.browser.new_context()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> None:
        cleanup_error: BaseException | None = None
        if self.context is not None:
            context, self.context = self.context, None
            try:
                await context.close()
            except BaseException as error:
                cleanup_error = error
        if self.browser is not None:
            browser, self.browser = self.browser, None
            try:
                await browser.close()
            except BaseException as error:
                cleanup_error = cleanup_error or error
        if self.playwright is not None:
            playwright, self.playwright = self.playwright, None
            try:
                await playwright.stop()
            except BaseException as error:
                cleanup_error = cleanup_error or error
        if cleanup_error is not None:
            raise cleanup_error

    async def harvest(
        self,
        kind: str,
        *,
        start_page: int = 1,
        max_pages: int | None,
        page_timeout_s: float,
        on_rows: _ROWS_CALLBACK,
    ) -> dict[str, Any]:
        """抓單一車輛種類：篩選 → 逐頁 set_pageIndex → 解析 → on_rows。

        回傳 report{kind, pages_done, rows_seen, malformed_rows, total_pages,
        last_page, start_page}。斷點續傳由 service 層以 start_page 控制。
        """
        if kind not in KIND_OPTION_VALUES:
            raise ValueError(f"unsupported VNCS vehicle kind option: {kind!r}")
        if self.context is None:
            raise RuntimeError("VncsBrowserHarvester must be used as an async context manager")
        page = await self.context.new_page()
        page.set_default_timeout(page_timeout_s * 1000)
        try:
            return await self._harvest_on_page(
                page, kind, start_page=start_page, max_pages=max_pages, on_rows=on_rows
            )
        except PlaywrightError as error:
            raise VncsBrowserError(f"{type(error).__name__}: {error}") from error
        finally:
            await page.close()

    async def _harvest_on_page(
        self,
        page: Page,
        kind: str,
        *,
        start_page: int,
        max_pages: int | None,
        on_rows: _ROWS_CALLBACK,
    ) -> dict[str, Any]:
        await page.goto(self.config.base_url, wait_until="load")
        await page.select_option(FILTER_SELECT_SELECTOR, kind)
        installed = await page.evaluate(_INSTALL_PAGING_HELPER_SCRIPT)
        if not installed:
            raise VncsBrowserError("could not install the VNCS paging helper")
        clicked = False
        async with page.expect_navigation(wait_until="load"):
            clicked_result = await page.evaluate(_CLICK_FILTER_BUTTON_SCRIPT)
            clicked = bool(clicked_result)
        if not clicked:
            raise VncsBrowserError("VNCS filter submit button was not found")
        # POST 導航後文件重載，helper 必須重新安裝。
        await page.evaluate(_INSTALL_PAGING_HELPER_SCRIPT)

        current_page_1based = max(1, start_page)
        total_pages = await self._resolve_total_pages(page)
        if current_page_1based > 1:
            await self._goto_page(page, current_page_1based - 1, total_pages)
            state = await self._paging_state(page)
            total_pages = self._merged_total_pages(total_pages, state)

        pages_done = 0
        rows_seen = 0
        malformed_rows = 0
        while True:
            records, malformed = await self._extract_records(page)
            malformed_rows += malformed
            rows_seen += len(records)
            on_rows(records)
            pages_done += 1
            if max_pages is not None and pages_done >= max_pages:
                break
            if current_page_1based >= total_pages:
                break
            signature = await page.evaluate(_GRID_SIGNATURE_SCRIPT)
            # set_pageIndex 是 0-based：1-based 第 current_page+1 頁 → index=current_page。
            await page.evaluate(_SET_PAGE_INDEX_SCRIPT, current_page_1based)
            await self._wait_for_grid_page(page, current_page_1based, str(signature))
            current_page_1based += 1
            state = await self._paging_state(page)
            total_pages = self._merged_total_pages(total_pages, state)
            await asyncio.sleep(self.inter_page_delay)
        return {
            "kind": kind,
            "pages_done": pages_done,
            "rows_seen": rows_seen,
            "malformed_rows": malformed_rows,
            "total_pages": total_pages,
            "last_page": current_page_1based,
            "start_page": max(1, start_page),
        }

    async def _goto_page(self, page: Page, target_index_zero_based: int, total_pages: int) -> None:
        if target_index_zero_based >= total_pages:
            raise VncsBrowserError(
                f"start page beyond the last page ({target_index_zero_based + 1} > {total_pages})"
            )
        signature = await page.evaluate(_GRID_SIGNATURE_SCRIPT)
        await page.evaluate(_SET_PAGE_INDEX_SCRIPT, target_index_zero_based)
        await self._wait_for_grid_page(page, target_index_zero_based, str(signature))

    @staticmethod
    async def _extract_records(page: Page) -> tuple[list[dict[str, object]], int]:
        """優先以 DOM 提取的格線列解析；空頁時允許 0 列，結構消失則 fail-closed。"""
        rows = await page.evaluate(_GRID_ROWS_SCRIPT)
        if rows:
            return parse_grid_records([dict(row) for row in rows])
        # AJAX 換頁瞬間 DOM 列可能短暫為空：先等一格再取一次，避免把
        # 過渡狀態誤判成最終空頁。
        await page.wait_for_timeout(500)
        rows = await page.evaluate(_GRID_ROWS_SCRIPT)
        if rows:
            return parse_grid_records([dict(row) for row in rows])
        content = await page.content()
        try:
            # 相容路徑：伺服器若回傳傳統靜態表格，沿用既有 parser。
            return parse_vehicles(content.encode("utf-8"))
        except VncsParserError as error:
            if "wdgMain" in content:
                # 格線存在但此頁沒有資料列（如篩選結果為空）：合法空頁。
                # parse_vehicles 對現代格線頁會報「缺欄位」，同屬此類。
                _ = error
                return [], 0
            raise

    @staticmethod
    async def _wait_for_grid_page(
        page: Page, target_index_zero_based: int, previous_signature: str
    ) -> None:
        try:
            await page.wait_for_function(
                _WAIT_FOR_PAGE_SCRIPT,
                arg=[target_index_zero_based, previous_signature],
            )
        except PlaywrightTimeoutError:
            # 簽章未變（相鄰頁完全同容）時不硬失敗：upsert 冪等，重複列無害。
            pass

    @staticmethod
    async def _paging_state(page: Page) -> dict[str, int | None]:
        state = await page.evaluate(_PAGING_STATE_SCRIPT)
        if not isinstance(state, dict):
            return {"pageIndex": None, "pageCount": None}
        return {
            "pageIndex": VncsBrowserHarvester._optional_int(state.get("pageIndex")),
            "pageCount": VncsBrowserHarvester._optional_int(state.get("pageCount")),
        }

    async def _resolve_total_pages(self, page: Page) -> int:
        state = await self._paging_state(page)
        count = state["pageCount"]
        if count is not None and count >= 1:
            return count
        match = _PAGING_JSON_RE.search(await page.content())
        if match is None:
            raise VncsBrowserError("VNCS paging metadata was not found after filtering")
        return int(match.group(3))

    @staticmethod
    def _merged_total_pages(current: int, state: dict[str, int | None]) -> int:
        count = state["pageCount"]
        return count if count is not None and count >= 1 else current

    @staticmethod
    def _optional_int(value: object) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return None
