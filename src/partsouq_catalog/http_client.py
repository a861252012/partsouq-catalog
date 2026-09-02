"""HTTP 傳輸層（基礎設施）：高速爬蟲 + Cloudflare challenge fail-closed。

使用 CloakBrowser 取得的 Cookie 進行請求。若請求碰到 Cloudflare
驗證頁（例如 cf_clearance 過期），會透過 CloakBrowser 刷新一次 session
並送出 follow-up。follow-up 仍被拒時立即停止，不把重複刷新誤報為成功。

本層只負責「把 HTML 拿回來」；解析與資料寫入分別屬於解析器層
與 Repository 層。

正式 catalog 請求（partsouq.com /en/catalog）在送出前會先以**雙重身分**
檢查 robots.txt 與 origin/path 合法性：我們對站方揭露 crawler 身分
（CATALOG_USER_AGENT）取得 robots.txt，但實際請求以 CloakBrowser 的
browser UA 送出（為了維持 cf_clearance session）；因此 robots 規則對
**這兩個身分都必須允許**才放行 —— 不能只查 crawler 身分、卻用 browser
身分送出請求（production 合規語意）。robots 無法確認允許、origin 不符
或回應為 redirect 時一律停止（fail-closed）。

穩定性與限流的兩個關鍵設計：
1. 連線池刻意只開 2 條（pool_connections / pool_maxsize）：多 worker
   同時撥號時，避免 macOS ephemeral port 耗盡（OSError 49「無法指定
   要求的位址」）。伺服器關閉的閒置 socket（CLOSE_WAIT）會以
   ConnectionError 呈現，由本層的迴圈重建連線池並重試。
2. 每次請求前先 ensure_fresh() 主動確認 cookie 新鮮度；真正碰上
   驗證時才執行完整的刷新流程。全域限速（F5）：每次 wire request
   前呼叫 governor.acquire()，重試也受控 —— adapter 層不做重試
   （max_retries=0），所有重試都回到 get() 的迴圈，每次都會重新
   取得全域時槽（SOL P1）。
"""

import hashlib
import logging
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Protocol
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter

from partsouq_crawler.crawl.robots import (
    RobotsRules,
    has_applicable_access_rules,
    parse_robots,
)

from .cloak import (
    REFRESH_RETRY_BACKOFF,
    NonUnitPageError,
    fetch_page,
    force_refresh_session,
    get_session,
    reject_session,
    session_backoff_remaining,
)
from .config import CLOAK, CRAWL, SITE, Cookies
from .evidence import CatalogHttpResponse, public_source_url

log = logging.getLogger("http")

CATALOG_USER_AGENT = "partsouq-catalog-crawler/0.1 (+https://github.com/a861252012)"
CATALOG_PRODUCT_TOKEN = "partsouq-catalog-crawler"
CATALOG_HOSTS = frozenset({"partsouq.com", "www.partsouq.com"})
BACKOFF_HEARTBEAT_SECONDS = 60.0
ROBOTS_CACHE_TTL_SECONDS = 24 * 60 * 60
ROBOTS_BODY_MAX_BYTES = 500 * 1024


class RequestGovernor(Protocol):
    def acquire(self) -> None: ...

    def throttle(self, seconds: float) -> None: ...


def _cf_value(cookies: Cookies | None) -> str:
    """從 cookie 列表取出 cf_clearance 的值（無則回傳空字串）。

    作為 cookie 版本的訊號：cf_clearance 每次刷新必然改變。
    """
    for c in cookies or []:
        if c.get("name") == "cf_clearance":
            return c.get("value", "")
    return ""


# Cloudflare 驗證頁的特徵片段（出現在頁面前 8000 字元內即視為驗證）
CHALLENGE_MARKERS = (
    "Just a moment",
    "Please wait",
    "請稍候",
    "sec-cpt-",
    "cf-chl",
    "Attention Required",
    "__cf_chl_rt_tk",
    # 新版 Cloudflare 反爬（Turnstile / Managed Challenge）特徵：
    # 舊標記抓不到時，這些頁面會以 HTTP 200 形式混進來（實測約 141KB），
    # 內容沒有零件表格但沒有「Just a moment」—— 不補偵測的話會被當成
    # 合法空頁面，污染 crawl_state（group 4103 事件）。
    "challenge-platform",
    "Turnstile",
    "Verifying you are human",
    "Checking your browser",
    "Managed Challenge",
    "cf-captcha",
)


class ChallengeError(Exception):
    """代表回應內容是 Cloudflare 驗證頁（需要刷新 cookie 後重試）。"""


class NotFoundError(Exception):
    """代表資源在網站端不存在（HTTP 404）。

    對 unit 頁（零件組）是「此 group 沒有資料」的合法狀態，由
    crawl_group 捕獲並視為該組完成；對 locate/pick/vehicle/category
    頁則代表異常，會讓父層標記失敗。用例外而非空字串當 sentinel，
    避免與「空白 HTTP 200」混淆。
    """

    def __init__(self, message: str, response: CatalogHttpResponse | None = None) -> None:
        super().__init__(message)
        self.response = response


class RobotsPolicyError(Exception):
    """代表 robots.txt 無法確認允許正式 catalog 請求。"""


@dataclass
class VinUnit:
    """VIN 解碼結果中的一個 unit（車輛設定變體）連結。"""

    uid: str
    url: str
    cid: str
    vid: str
    ssd: str
    brand: str


@dataclass
class VinDecodeResult:
    """VIN 解碼結果：可能直接列出多個 unit，或列出 vehicle 頁待續爬。"""

    units: list[VinUnit]
    vehicle_links: list[str]
    raw_html_len: int


_UNIT_LINK_RE = re.compile(r"/en/catalog/genuine/unit\?([^\"'>\s]+)", re.IGNORECASE)
_VEHICLE_LINK_RE = re.compile(r"/en/catalog/genuine/vehicle\?([^\"'>\s]+)", re.IGNORECASE)


def _parse_vin_decode_result(html: str, brand: str) -> VinDecodeResult:
    """從 /locate VIN 解碼結果頁解析 unit / vehicle 連結。

    站方登入後的 VIN 搜尋結果可能是：(a) 直接跳到 unit 頁；(b) 列出多個
    unit 的結果頁；(c) 列出 vehicle（車型）頁要再點進分類。本函式不假設
    頁面型態，直接抽取所有 catalog 連結並解析查詢參數，最大程度相容站方
    改版。純函式，可用 fixture 單測。
    """
    units: list[VinUnit] = []
    vehicle_links: list[str] = []
    html = unescape(html)
    for raw_qs in _UNIT_LINK_RE.findall(html):
        qs = parse_qs(raw_qs)
        uid_vals = qs.get("uid")
        if not uid_vals:
            continue
        units.append(
            VinUnit(
                uid=uid_vals[0],
                url=f"{SITE['base']}/en/catalog/genuine/unit?{raw_qs}",
                cid=(qs.get("cid") or [""])[0],
                vid=(qs.get("vid") or [""])[0],
                ssd=(qs.get("ssd") or [""])[0],
                brand=brand,
            )
        )
    for raw_qs in _VEHICLE_LINK_RE.findall(html):
        vehicle_links.append(f"{SITE['base']}/en/catalog/genuine/vehicle?{raw_qs}")
    return VinDecodeResult(units=units, vehicle_links=vehicle_links, raw_html_len=len(html))


def _response_envelope(
    response: requests.Response,
    requested_url: str,
    text: str,
    attempt: int,
) -> CatalogHttpResponse:
    final_url = response.url if isinstance(response.url, str) else requested_url
    raw_content = response.content
    raw_body = raw_content if isinstance(raw_content, bytes) else text.encode("utf-8")
    elapsed_seconds = response.elapsed.total_seconds()
    elapsed_ms = (
        max(0, int(elapsed_seconds * 1000)) if isinstance(elapsed_seconds, (int, float)) else 0
    )
    return CatalogHttpResponse(
        final_url=final_url,
        status_code=response.status_code,
        content_type=response.headers.get("content-type", "").split(";", 1)[0].strip().lower(),
        raw_body_sha256=hashlib.sha256(raw_body).hexdigest(),
        text=text,
        fetched_at=datetime.now(UTC).replace(tzinfo=None),
        elapsed_ms=elapsed_ms,
        attempt=attempt,
    )


def _browser_response(url: str, text: str, attempt: int) -> CatalogHttpResponse:
    """用瀏覽器抓回的 HTML 組出與 _response_envelope 同形的成功回應。

    status_code 固定 200（瀏覽器已確認通過挑戰）；text 即真實頁面內容，
    後續 parser / evidence 與 requests 路徑完全一致。"""
    raw_body = text.encode("utf-8")
    return CatalogHttpResponse(
        final_url=url,
        status_code=200,
        content_type="text/html",
        raw_body_sha256=hashlib.sha256(raw_body).hexdigest(),
        text=text,
        fetched_at=datetime.now(UTC).replace(tzinfo=None),
        elapsed_ms=0,
        attempt=attempt,
    )


class SessionManager:
    """持有 Cookie、按需刷新、並控制請求節奏的 HTTP 工作階段。

    每個 worker 執行緒一個實例，共用同一份 cookie 來源。
    """

    def __init__(
        self,
        cookies: Cookies | None = None,
        no_browser: bool = False,
        gov: RequestGovernor | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.session = requests.Session()
        # 傳輸層不得沿用環境代理（避免流量被本機 proxy 攔截/改寫）。
        self.session.trust_env = False
        self.session.proxies.clear()
        self._robots: RobotsRules | None = None
        self._robots_fetched_at: float | None = None
        self._monotonic = monotonic
        # F5：全域 request governor（可選）。提供時，429 的 Retry-After
        # 會同時暫停「所有」worker —— 限流是全域的，單一 worker 的
        # 退避不該讓其他 worker 繼續撞牆。
        self.gov = gov
        self.session.headers.update(
            {
                "User-Agent": CLOAK["user_agent"],
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "en-US,en;q=0.9",
                "Connection": "keep-alive",
            }
        )
        # no_browser 模式（除錯用）：只用已存 cookie，絕不啟動瀏覽器
        # 刷新（見 ensure_fresh / get 的處理）。
        self.no_browser = no_browser
        # 連線池與重試設定：見模組文件說明。SOL P1：adapter 層
        # max_retries=0 —— 重試統一由 get() 的迴圈控制，每次迭代都會
        # 重新 acquire 全域時槽，否則 urllib3 層的重試會繞過限流。
        self._mount_adapter()
        self.cookies: Cookies | None = cookies
        if cookies:
            self._apply_cookies()

    def _mount_adapter(self) -> None:
        """掛上連線池 adapter（2 條連線，adapter 層不做重試）。"""
        adapter = HTTPAdapter(
            pool_connections=2,
            pool_maxsize=2,
            max_retries=0,
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _apply_cookies(self) -> None:
        """把 cookie 列表**整份**套進 requests 的 cookie jar。

        SOL review P2：先清空 jar 再套用新快照 —— 舊碼用
        session.cookies.update() 只覆寫新快照中存在的鍵，刷新結果
        缺少舊 PHPSESSID 等 cookie 時舊值會殘留在 jar 裡，請求仍
        帶上已失效的舊 session。
        """
        jar = requests.cookies.RequestsCookieJar()
        for c in self.cookies or []:
            jar.set(
                c["name"],
                c["value"],
                domain=c.get("domain", "partsouq.com"),
                path=c.get("path", "/"),
            )
        self.session.cookies.clear()
        self.session.cookies.update(jar)

    def _wire_get(self, url: str, headers: dict[str, str] | None = None) -> requests.Response:
        """送出「不跟隨 redirect」的單一 GET；cookie jar 會暫時清空。

        robots.txt 與其他不應帶 session cookie 的請求走這條路；以
        stream 回傳，讓呼叫端限制讀取量並負責 close。請求完成後 jar
        還原，不影響後續 catalog 請求的 cookie。
        """
        saved = requests.cookies.RequestsCookieJar()
        saved.update(self.session.cookies)
        self.session.cookies.clear()
        try:
            return self.session.get(
                url,
                timeout=float(str(CRAWL["http_timeout"])),
                allow_redirects=False,
                headers=headers,
                stream=True,
            )
        finally:
            self.session.cookies.clear()
            self.session.cookies.update(saved)

    def refresh(self) -> bool:
        """透過 single-flight 的 session 管理員重新取得 cookie。

        成功回傳 True。所有 worker 共用同一條刷新路徑，
        因此永遠只會啟動一個瀏覽器。

        no_browser 模式下直接回傳 False（P2 修復）：refresh 是唯一
        沒檢查 no_browser 的入口，直接呼叫時會啟動瀏覽器。
        """
        if self.no_browser:
            return False
        cookies = get_session()
        if not cookies:
            return False
        self.cookies = cookies
        self._apply_cookies()
        return True

    def ensure_fresh(self) -> None:
        """在 cookie 快到期前主動刷新（每次請求前呼叫）。

        get_session() 是 single-flight 且 TTL 感知的，所以成本極低：
        cookie 仍新鮮時只做一次時間檢查。刷新時只有一個 worker 真正
        啟動瀏覽器，其餘短暫等待後直接沿用結果。

        cookie 物件會被整份替換，所以 identity 判斷可能誤判（refresh
        後 worker 的 self.cookies 可能仍與 state 指向同一份舊 list，
        導致新 cookie 沒被套上，繼續用舊 cookie 打請求 —— 實際發生：
        刷新後仍拿 403/反爬頁）。改用「cf_clearance 值」比較：它每次
        刷新必然改變，是可靠的版本訊號。
        """
        if self.no_browser:
            return
        cookies = get_session()
        if cookies is None:
            return
        if cookies is not self.cookies and _cf_value(cookies) != _cf_value(self.cookies):
            self.cookies = cookies
            self._apply_cookies()

    def get(self, url: str) -> str:
        """GET 請求：含重試 + 驗證自動刷新。回傳 HTML 文字。"""

        return self.get_response(url).text

    def get_response(self, url: str) -> CatalogHttpResponse:
        """GET 請求並回傳不含 cookie／任意 headers 的 evidence envelope。

        404 有特殊語意：代表該資源在網站端不存在（例如某車型的某個
        group 頁）。以 NotFoundError 拋出、由呼叫端決定如何處理
        （unit 頁視為「此 group 無資料」，其他頁視為失敗），不與
        「空白 HTTP 200」混淆。

        連續碰到驗證且刷新失敗超過 challenge_retries 次時直接放棄
        該請求（讓監督迴圈/續爬機制接手），避免在 Cloudflare 封鎖
        期間反覆啟動瀏覽器造成「刷新失敗風暴」。

        F4 修復：
        - 重試預算與刷新預算分開：刷新成功**不消耗** HTTP attempt，
          保證刷新後必有 follow-up 請求 —— 舊碼最後一次 attempt 刷新
          成功後迴圈已耗盡，新 cookie 從未被使用就直接拋舊錯誤。若
          follow-up 仍是 challenge，立即停止；同一請求不得反覆重啟
          瀏覽器取得無法被 HTTP transport 接受的 cookie。
        - 驗證偵測優先於 429：429 + cf-mitigated challenge 標頭先當
          驗證處理（刷新 cookie），不被當一般限流 —— 舊碼 429 檢查
          在前，5 次請求 0 次刷新，永遠過不了。
        """
        last_err: Exception | None = None
        refresh_failures = 0
        refresh_successes = 0
        attempt = 0
        # 正式 catalog 請求先過 robots/origin/path 檢查；robots 無法確認
        # 允許時立即停止（不發 catalog 請求）。
        self._ensure_catalog_allowed(url)
        url_parts = urlsplit(url)
        normalized_path = url_parts.path.rstrip("/") or "/"
        safe_url = (
            public_source_url(url)
            if (url_parts.hostname or "").lower() in CATALOG_HOSTS
            and (
                normalized_path == "/en/catalog/genuine"
                or normalized_path == "/en/brands-16.html"
                or normalized_path.startswith("/en/catalog/genuine/")
            )
            else urlunsplit((url_parts.scheme, url_parts.hostname or "", url_parts.path, "", ""))
        )[:100]
        while attempt < CRAWL["max_retries"]:
            attempt += 1
            try:
                # SOL P1：每次 wire request 前取得全域時槽（重試、刷新
                # 後的 follow-up 也都受控）—— 拿一次 token 打 5 次請求
                # 等於沒有限流。throttle 設定的全域暫停也在這裡生效。
                if self.gov is not None:
                    self.gov.acquire()
                r = self.session.get(url, timeout=CRAWL["http_timeout"], allow_redirects=False)
                encoding = r.encoding or r.apparent_encoding or "utf-8"
                r.encoding = encoding
                text = r.text or ""
                # 驗證偵測優先（F4）：403 或任何帶驗證特徵的回應
                # （含 429 + cf-mitigated: challenge）一律進驗證分支。
                if r.status_code in (403,) or self._is_challenge(r, text):
                    raise ChallengeError(f"http {r.status_code} challenge at {safe_url}")
                if r.status_code == 429:
                    # P2 修復：429 是「限流」不是「驗證被拒」—— 舊碼把
                    # 429 併入 challenge 分支，每次都會殺掉健康的瀏覽器、
                    # 冷啟動重解驗證（~3-4 分鐘），且刷新成功後 failures
                    # 歸零，等於無視退避連續燒瀏覽器。限流應尊重伺服器
                    # 節奏：依 retry-after（或固定下限）休眠後重試，不動
                    # 瀏覽器、不刷新 cookie。
                    last_err = requests.RequestException(f"http 429 rate-limited at {safe_url}")
                    log.warning(
                        "rate-limited (429) at %s (attempt %d/%d); backing off",
                        safe_url,
                        attempt,
                        CRAWL["max_retries"],
                    )
                    retry_after = self._retry_after_seconds(r)
                    # F5：限流是全域的 —— 讓其他 worker 也一起暫停，
                    # 避免它們在 Retry-After 期間繼續撞牆。
                    if self.gov is not None:
                        self.gov.throttle(retry_after)
                    time.sleep(retry_after)
                    continue
                if r.status_code == 404:
                    raise NotFoundError(
                        f"http 404 at {safe_url}",
                        _response_envelope(r, url, text, attempt),
                    )
                if 300 <= r.status_code < 400:
                    location = r.headers.get("Location", "") or ""
                    # Cloudflare 挑戰轉址：視同 403 challenge —— ChallengeError
                    # 才會被迴圈 except 捕捉，觸發 cookie 刷新與瀏覽器後備重試。
                    if (
                        self._is_challenge(r, text)
                        or "challenge" in location.lower()
                        or "cdn-cgi" in location.lower()
                    ):
                        raise ChallengeError(f"http 3xx challenge at {safe_url}")
                    # unit 頁被轉址離開原 unit（uid 過期被導向 /locate、首頁
                    # 或別的 unit）：該組在站端已不存在 → NotFoundError，由
                    # crawl_group 標成 not_found（terminal），絕不靜默 receipt
                    # 成 done/0。同 unit（同 uid）的站內正規化轉址不在此限，
                    # 維持 fail-closed 交由瀏覽器後備跟隨。
                    if self._is_unit_request(url) and not self._redirect_keeps_unit(url, location):
                        raise NotFoundError(
                            f"catalog unit redirected away at {safe_url} -> {location}",
                            _response_envelope(r, url, text, attempt),
                        )
                    if self._redirects_to_brand_locate(url, location):
                        # 站方對「不存在的 vehicle/category 組合」會 302 回
                        # 品牌索引（實證 run 44：vehicle?cid=2 ->
                        # locate?c=Toyota&psq=lb），與 unit 過期同語意：
                        # terminal not_found，由呼叫端收斂，不重試。
                        raise NotFoundError(
                            f"catalog page redirected to brand locate at {safe_url} -> {location}",
                            _response_envelope(r, url, text, attempt),
                        )
                    # 其餘 catalog 頁面轉址維持 fail-closed：不跟隨、不猜
                    # 測語意。錯誤訊息附上 Location 供運維判讀站方正規化
                    # 行為。
                    raise RobotsPolicyError(f"catalog redirect refused at {safe_url} -> {location}")
                if not (200 <= r.status_code < 300):
                    # 其他非 2xx（500/502...）不該被當成成功頁面，重試
                    last_err = requests.RequestException(f"http {r.status_code} at {safe_url}")
                    raise last_err
                return _response_envelope(r, url, text, attempt)
            except ChallengeError as e:
                last_err = e
                if self.no_browser:
                    # no_browser 模式：不允許啟動瀏覽器刷新，直接放棄
                    log.error("challenge while no-browser mode; giving up on %s", safe_url)
                    break
                if refresh_successes:
                    reject_session(_cf_value(self.cookies))
                    last_err = ChallengeError(
                        f"fresh browser session still challenged at {safe_url}"
                    )
                    log.error(
                        "fresh browser session still challenged; refusing another "
                        "browser refresh for %s",
                        safe_url,
                    )
                    break
                if refresh_failures >= CRAWL["challenge_retries"]:
                    log.error(
                        "too many failed refreshes (%d); giving up on %s",
                        refresh_failures,
                        safe_url,
                    )
                    break
                log.warning("challenge hit (attempt %d/%d)", attempt, CRAWL["max_retries"])
                # 收到 challenge = 快取 cookie 已被伺服器拒絕，強制失效並重新刷新。
                # SOL review P2：帶上被拒的 cf_clearance 版本 —— 若其他
                # worker 已把全域 session 刷新成更新版本（延遲返回的舊
                # challenge），直接沿用新 cookie，不再清掉重刷、再啟動
                # 一次瀏覽器。
                cookies = force_refresh_session(_cf_value(self.cookies))
                if not cookies:
                    refresh_failures += 1
                    log.error("cookie refresh failed (%d consecutive)", refresh_failures)
                    self._sleep_with_backoff(attempt)
                    continue
                self.cookies = cookies
                self._apply_cookies()
                # P2 修復：刷新成功後歸零失敗計數 —— 舊碼不歸零，
                # 「fail, fail, success, fail」序列在第 4 次就達
                # challenge_retries 而提前放棄，即使刷新已恢復。
                refresh_failures = 0
                refresh_successes += 1
                # F4 修復：刷新成功不消耗 attempt 預算 —— 保證下一輪
                # 迭代用新 cookie 發 follow-up 請求（舊碼最後一次
                # attempt 刷新成功後沒有第 6 次請求，直接拋舊錯誤）。
                attempt -= 1
                time.sleep(2 + random.random() * 3)
            except requests.exceptions.ConnectionError as e:
                # F5：只有「連線層」失敗（伺服器關閉的過期 socket /
                # CLOSE_WAIT / 連線被拒）才需要重建連線池 —— 500 等
                # 有正常 response 的錯誤 keep-alive 仍健康，舊碼一律
                # 丟棄池化 socket，白費重新撥號。
                last_err = requests.RequestException(f"connection failed at {safe_url}")
                log.warning(
                    "connection error at %s (attempt %d/%d; %s)",
                    safe_url,
                    attempt,
                    CRAWL["max_retries"],
                    type(e).__name__,
                )
                self._reset_connections()
                time.sleep(2 + random.random() * 2)
            except requests.exceptions.Timeout as e:
                last_err = requests.RequestException(f"request timed out at {safe_url}")
                log.warning(
                    "request timeout at %s (attempt %d/%d; %s)",
                    safe_url,
                    attempt,
                    CRAWL["max_retries"],
                    type(e).__name__,
                )
                self._reset_connections()
                time.sleep(2 + random.random() * 2)
            except requests.RequestException as e:
                # 其他（500/502 等）：有正常 response，連線池保持健康
                if e is not last_err:
                    last_err = requests.RequestException(f"request failed at {safe_url}")
                log.warning(
                    "request error at %s (attempt %d/%d; %s)",
                    safe_url,
                    attempt,
                    CRAWL["max_retries"],
                    type(e).__name__,
                )
                time.sleep(2 + random.random() * 2)
        # requests 傳輸被 Cloudflare 以 TLS fingerprint 擋下（managed
        # challenge / 轉址挑戰）時，退而求其次用真實瀏覽器抓取 —— 瀏覽器
        # 具備真實 fingerprint，載入同一份 cookie 即可通過。這是 fail-closed
        # 之外的最後手段：fetch_page 只在拿到「非挑戰」頁面時回傳 HTML，
        # 否則回傳 None，我們仍照原邏輯拋錯，絕不把挑戰頁當成資料。
        if (
            not self.no_browser
            and last_err is not None
            and isinstance(last_err, (ChallengeError, RobotsPolicyError))
            and self._is_catalog_url(url)
        ):
            try:
                browser_html = fetch_page(url)
            except NonUnitPageError as off_unit:
                # Browser fallback 沒有可驗證的 HTTP 404 envelope。落到
                # /locate 可能是站方下架，也可能是 challenge、錯品牌或
                # 錯 context；一律 fail-closed，不能寫 terminal not_found。
                raise RobotsPolicyError(
                    f"browser fetch landed outside requested catalog page for "
                    f"{safe_url}: {off_unit}"
                ) from off_unit
            if browser_html is not None:
                log.info("browser-fetch fallback succeeded for %s", safe_url)
                return _browser_response(url, browser_html, attempt)
        raise last_err or RuntimeError(f"get failed: {safe_url}")

    def _ensure_catalog_allowed(self, url: str) -> None:
        """正式 PartSouq catalog 首次請求前取得 robots，無法確認即停止。

        只檢查正式 catalog 主機（partsouq.com）與 /en/catalog 路徑；
        其他主機/路徑（例如測試用的 partsouq.example）不受此閘影響。
        origin（https://partsouq.com）、無 userinfo、無歧義路徑
        （% 編碼、反斜線、./.. 片段）都必須符合才放行。
        """
        parts = urlsplit(url)
        if parts.hostname not in CATALOG_HOSTS:
            return
        path_segments = parts.path.split("/")
        if (
            "%" in parts.path
            or "\\" in parts.path
            or any(segment in {".", ".."} for segment in path_segments)
        ):
            raise RobotsPolicyError(f"ambiguous PartSouq path refused: {parts.path[:100]}")
        is_catalog_path = (
            parts.path == "/en/catalog"
            or parts.path == "/en/brands-16.html"
            or parts.path.startswith("/en/catalog/")
        )
        if not is_catalog_path:
            return
        if (
            parts.scheme.lower() != "https"
            or parts.hostname != "partsouq.com"
            or parts.port not in (None, 443)
            or parts.username is not None
            or parts.password is not None
        ):
            raise RobotsPolicyError(f"unsupported catalog origin: {parts.scheme}://{parts.netloc}")

        robots_url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
        robots_expired = (
            self._robots_fetched_at is None
            or self._monotonic() - self._robots_fetched_at >= ROBOTS_CACHE_TTL_SECONDS
        )
        if self._robots is None or robots_expired:
            if self.gov is not None:
                self.gov.acquire()
            response = self._wire_get(robots_url, headers={"User-Agent": CATALOG_USER_AGENT})
            body_buffer = bytearray()
            try:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    remaining = ROBOTS_BODY_MAX_BYTES + 1 - len(body_buffer)
                    body_buffer.extend(chunk[:remaining])
                    if len(body_buffer) > ROBOTS_BODY_MAX_BYTES:
                        raise RobotsPolicyError(
                            f"robots exceeds {ROBOTS_BODY_MAX_BYTES} byte limit at {robots_url}"
                        )
            finally:
                response.close()
            body = bytes(body_buffer)
            text = body.decode("utf-8", errors="replace")
            if response.status_code == 403 or self._is_challenge(response, text):
                raise ChallengeError(f"http {response.status_code} challenge at {robots_url}")
            if not 200 <= response.status_code < 300:
                raise RobotsPolicyError(
                    f"robots unavailable (http {response.status_code}) at {robots_url}"
                )
            final_url = response.url if isinstance(response.url, str) else robots_url
            final_parts = urlsplit(final_url)
            if (final_parts.scheme.lower(), final_parts.netloc.lower()) != (
                parts.scheme.lower(),
                parts.netloc.lower(),
            ):
                raise RobotsPolicyError(f"robots redirected outside catalog origin: {final_url}")
            if not text.strip() or not self._has_applicable_robots_rules(text):
                raise RobotsPolicyError(f"robots has no explicit applicable rules at {robots_url}")
            self._robots = parse_robots(robots_url, body, "utf-8")
            self._robots_fetched_at = self._monotonic()

        if not self._robots.allows(CATALOG_USER_AGENT, url) or not self._robots.allows(
            CLOAK["user_agent"], url
        ):
            # 兩個身分都必須允許：揭露的 crawler 身分與實際送出的
            # browser UA。只查 crawler 身分而用 browser 身分送出，
            # 等於繞過站方對一般流量的 robots 規則。
            raise RobotsPolicyError(f"robots disallows catalog URL: {public_source_url(url)[:100]}")

    @staticmethod
    def _has_applicable_robots_rules(text: str) -> bool:
        """確認本 crawler 或萬用 UA group 具有 Allow／Disallow 指令。"""
        return has_applicable_access_rules(text, CATALOG_PRODUCT_TOKEN)

    @staticmethod
    def _is_catalog_url(url: str) -> bool:
        """判斷 URL 是否落在正式 catalog 主機的 catalog 路徑下。

        只用於決定是否對被挑戰的請求啟用瀏覽器抓取後備：非 catalog 的
        請求（robots.txt、圖片等）不應走這條路。品牌總覽頁也列入
        （它是 full crawl 的品牌來源，被挑戰時同樣需要瀏覽器後備）。"""
        parts = urlsplit(url)
        return (parts.hostname or "").lower() in CATALOG_HOSTS and (
            parts.path == "/en/catalog"
            or parts.path == "/en/brands-16.html"
            or parts.path.startswith("/en/catalog/")
        )

    @staticmethod
    def _is_unit_request(url: str) -> bool:
        """判斷請求是否為 unit 頁（零件組明細頁）。

        只有 unit 頁的 3xx 才賦予「組已 gone」語意；索引頁等其他
        catalog 頁面的轉址維持 fail-closed（RobotsPolicyError）。"""
        return urlsplit(url).path.rstrip("/") == "/en/catalog/genuine/unit"

    @staticmethod
    def _redirects_to_brand_locate(requested_url: str, location: str) -> bool:
        """判斷轉址目標是否為品牌索引頁（/en/catalog/genuine/locate）。

        站方以 302 回 /locate 代表「該頁在站端不存在」（實證：
        vehicle?cid=2 -> locate?c=Toyota&psq=lb），與 unit 過期同語意，
        屬 terminal not_found 而非可疑轉址。location 允許相對路徑，
        以 urljoin 對齊請求 URL 後比對。"""
        if not location:
            return False
        target = urlsplit(urljoin(requested_url, location))
        return target.path.rstrip("/") == "/en/catalog/genuine/locate"

    @staticmethod
    def _redirect_keeps_unit(requested_url: str, location: str) -> bool:
        """判斷轉址目標是否仍是同一個 unit（同路徑且同 uid）。

        同 unit 的站內正規化轉址（如補上參數）不算離開；uid 變了或
        落到 /unit 以外路徑（/locate、首頁）都視為離開。location 允許
        相對路徑，以 urljoin 對齊請求 URL 後比對。"""
        if not location:
            return False
        target = urlsplit(urljoin(requested_url, location))
        if target.path.rstrip("/") != "/en/catalog/genuine/unit":
            return False
        requested_uid = parse_qs(urlsplit(requested_url).query).get("uid", [None])[0]
        target_uid = parse_qs(target.query).get("uid", [None])[0]
        return requested_uid is not None and requested_uid == target_uid

    @staticmethod
    def _retry_after_seconds(r: requests.Response) -> float:
        """從 429 回應的 Retry-After 標頭取得建議等待秒數。

        F4 修復：
        - 支援 HTTP-date 格式（Retry-After: Wed, 21 Oct 2015 ...）——
          舊碼 float() 對日期拋錯，固定退回 65 秒。
        - 設上限 retry_after_cap：伺服器的 Retry-After 可能錯得離譜
          （實測 Retry-After: 999999 ≈ 11 天），無上限會讓 worker
          睡到地老天荒。
        """
        ra = r.headers.get("retry-after")
        secs = REFRESH_RETRY_BACKOFF + 5
        if ra:
            try:
                secs = float(ra)
            except (TypeError, ValueError):
                try:
                    dt = parsedate_to_datetime(ra)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=UTC)
                    secs = (dt - datetime.now(UTC)).total_seconds()
                except (TypeError, ValueError):
                    pass
        return min(max(15.0, secs), CRAWL["retry_after_cap"])

    @staticmethod
    def _is_challenge(r: requests.Response, text: str) -> bool:
        """判斷回應是否為 Cloudflare 驗證頁。

        除了檢查正文特徵片段，也檢查回應標頭：Cloudflare 的驗證回應
        會帶 cf-mitigated: challenge 標頭，比只比對文字更可靠、
        也更早偵測到（不必等整個正文下載完）。
        """
        headers = r.headers
        if headers.get("cf-mitigated") == "challenge":
            return True
        if headers.get("cf-chl"):
            return True
        return any(m in text[:8000] for m in CHALLENGE_MARKERS)

    def _reset_connections(self) -> None:
        """丟棄所有池化的 keep-alive socket（例如 CLOSE_WAIT 卡住後）。"""
        try:
            self.session.close()
        except Exception:
            pass
        self._mount_adapter()

    def _sleep_with_backoff(self, attempt: int) -> None:
        """刷新失敗後的等待。

        P2 修復：與 cloak 的指數退避對齊 —— cloak 退避窗口
        （60s→120s→…→1200s）尚未走完時，以剩餘時間為準；否則才用
        下限（避免伺服器冷卻期間狂打）。
        """
        remaining = session_backoff_remaining()
        wait_seconds = (
            remaining + 5 if remaining > 0 else max(REFRESH_RETRY_BACKOFF + 5, 15 * (attempt + 1))
        )
        while wait_seconds > 0:
            chunk_seconds = min(BACKOFF_HEARTBEAT_SECONDS, wait_seconds)
            log.warning("cookie refresh backoff; %.0fs remaining", wait_seconds)
            time.sleep(chunk_seconds)
            wait_seconds -= chunk_seconds

    def sleep(self) -> None:
        """依設定延遲隨機休息（2~5 秒），模擬人類瀏覽節奏。"""
        time.sleep(random.uniform(CRAWL["min_delay"], CRAWL["max_delay"]))
