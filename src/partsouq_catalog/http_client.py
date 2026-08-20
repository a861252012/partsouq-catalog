"""HTTP 傳輸層（基礎設施）：低速、合規的 PartSouq HTTP collector。

本模組不取得或注入網站 session cookie，也不使用瀏覽器規避工具。
遇到 Cloudflare challenge 會停止目前請求，交由上層把 run 標示為失敗。

本層只負責「把 HTML 拿回來」；解析與資料寫入分別屬於解析器層
與 Repository 層。

穩定性與限流的兩個關鍵設計：
1. 連線池刻意只開 2 條（pool_connections / pool_maxsize）：多 worker
   同時撥號時，避免 macOS ephemeral port 耗盡（OSError 49「無法指定
   要求的位址」）。伺服器關閉的閒置 socket（CLOSE_WAIT）會以
   ConnectionError 呈現，由本層的迴圈重建連線池並重試。
2. 全域限速（F5）：每次 wire request
   前呼叫 governor.acquire()，重試也受控 —— adapter 層不做重試
   （max_retries=0），所有重試都回到 get() 的迴圈，每次都會重新
   取得全域時槽（SOL P1）。
"""

import logging
import random
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests
from requests.adapters import HTTPAdapter

from partsouq_crawler.crawl.robots import RobotsRules, parse_robots

from .config import CRAWL

log = logging.getLogger("http")

CATALOG_USER_AGENT = "partsouq-catalog-crawler/0.1 (+https://github.com/a861252012)"
CATALOG_PRODUCT_TOKEN = "partsouq-catalog-crawler"
CATALOG_HOSTS = frozenset({"partsouq.com", "www.partsouq.com"})


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
    """代表回應內容是 Cloudflare 驗證頁，必須停止目前請求。"""


class NotFoundError(Exception):
    """代表資源在網站端不存在（HTTP 404）。

    對 unit 頁（零件組）是「此 group 沒有資料」的合法狀態，由
    crawl_group 捕獲並視為該組完成；對 locate/pick/vehicle/category
    頁則代表異常，會讓父層標記失敗。用例外而非空字串當 sentinel，
    避免與「空白 HTTP 200」混淆。
    """


class RobotsPolicyError(Exception):
    """代表 robots.txt 無法確認允許正式 catalog 請求。"""


class SessionManager:
    """控制請求節奏的 HTTP 工作階段。每個 worker 各自持有一個實例。"""

    def __init__(self, gov=None):
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.proxies.clear()
        self._robots: RobotsRules | None = None
        # F5：全域 request governor（可選）。提供時，429 的 Retry-After
        # 會同時暫停「所有」worker —— 限流是全域的，單一 worker 的
        # 退避不該讓其他 worker 繼續撞牆。
        self.gov = gov
        self.session.headers.update(
            {
                "User-Agent": CATALOG_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Connection": "keep-alive",
            }
        )
        # 連線池與重試設定：見模組文件說明。SOL P1：adapter 層
        # max_retries=0 —— 重試統一由 get() 的迴圈控制，每次迭代都會
        # 重新 acquire 全域時槽，否則 urllib3 層的重試會繞過限流。
        self._mount_adapter()

    def _mount_adapter(self):
        """掛上連線池 adapter（2 條連線，adapter 層不做重試）。"""
        adapter = HTTPAdapter(
            pool_connections=2,
            pool_maxsize=2,
            max_retries=0,
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _wire_get(self, url: str) -> requests.Response:
        """送出不跟隨 redirect、也不保存 cookie 的單一 GET。"""
        self.session.cookies.clear()
        try:
            return self.session.get(
                url,
                timeout=float(str(CRAWL["http_timeout"])),
                allow_redirects=False,
            )
        finally:
            self.session.cookies.clear()

    def get(self, url: str) -> str:
        """GET 請求：正式 catalog 先檢查 robots，再依設定重試。

        404 有特殊語意：代表該資源在網站端不存在（例如某車型的某個
        group 頁）。以 NotFoundError 拋出、由呼叫端決定如何處理
        （unit 頁視為「此 group 無資料」，其他頁視為失敗），不與
        「空白 HTTP 200」混淆。

        驗證回應會立即停止，不重試、不刷新 cookie、不嘗試規避。
        """
        self._ensure_catalog_allowed(url)

        last_err = None
        attempt = 0
        while attempt < CRAWL["max_retries"]:
            attempt += 1
            try:
                # SOL P1：每次 wire request 前取得全域時槽，重試也受控。
                if self.gov is not None:
                    self.gov.acquire()
                r = self._wire_get(url)
                text = r.text or ""
                # 驗證偵測優先：403 或任何帶驗證特徵的回應均立即停止。
                if r.status_code in (403,) or self._is_challenge(r, text):
                    raise ChallengeError(f"http {r.status_code} challenge at {url[:100]}")
                if r.status_code == 429:
                    # 429 是限流，不是驗證被拒。依 Retry-After（或固定
                    # 下限）休眠後重試；不處理 challenge 或 cookie。
                    last_err = requests.RequestException(f"http 429 rate-limited at {url[:100]}")
                    log.warning(
                        "rate-limited (429) at %s (attempt %d/%d); backing off",
                        url[:100],
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
                    raise NotFoundError(f"http 404 at {url[:100]}")
                if 300 <= r.status_code < 400:
                    raise RobotsPolicyError(f"catalog redirect refused at {url[:100]}")
                if not (200 <= r.status_code < 300):
                    # 其他非 2xx（500/502...）不該被當成成功頁面，重試
                    raise requests.RequestException(f"http {r.status_code} at {url[:100]}")
                return text
            except ChallengeError as e:
                last_err = e
                log.error("challenge detected; stopping request for %s", url[:100])
                break
            except requests.exceptions.ConnectionError as e:
                # F5：只有「連線層」失敗（伺服器關閉的過期 socket /
                # CLOSE_WAIT / 連線被拒）才需要重建連線池 —— 500 等
                # 有正常 response 的錯誤 keep-alive 仍健康，舊碼一律
                # 丟棄池化 socket，白費重新撥號。
                last_err = e
                log.warning(
                    "connection error (attempt %d/%d): %s", attempt, CRAWL["max_retries"], e
                )
                self._reset_connections()
                time.sleep(2 + random.random() * 2)
            except requests.exceptions.Timeout as e:
                last_err = e
                log.warning("request timeout (attempt %d/%d): %s", attempt, CRAWL["max_retries"], e)
                self._reset_connections()
                time.sleep(2 + random.random() * 2)
            except requests.RequestException as e:
                # 其他（500/502 等）：有正常 response，連線池保持健康
                last_err = e
                log.warning("request error (attempt %d/%d): %s", attempt, CRAWL["max_retries"], e)
                time.sleep(2 + random.random() * 2)
        raise last_err or RuntimeError(f"get failed: {url[:100]}")

    def _ensure_catalog_allowed(self, url: str) -> None:
        """正式 PartSouq catalog 首次請求前取得 robots，無法確認即停止。"""
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
        is_catalog_path = parts.path == "/en/catalog" or parts.path.startswith("/en/catalog/")
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
        if self._robots is None:
            if self.gov is not None:
                self.gov.acquire()
            response = self._wire_get(robots_url)
            text = response.text or ""
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
            self._robots = parse_robots(robots_url, text.encode(), "utf-8")

        if not self._robots.allows(CATALOG_USER_AGENT, url):
            raise RobotsPolicyError(f"robots disallows catalog URL: {url[:100]}")

    @staticmethod
    def _has_applicable_robots_rules(text: str) -> bool:
        """確認本 crawler 或萬用 UA group 具有 Allow／Disallow 指令。"""
        parser = RobotFileParser()
        parser.parse(text.splitlines())
        for entry in parser.entries:
            if entry.applies_to(CATALOG_PRODUCT_TOKEN):
                return bool(entry.rulelines)
        for entry in parser.entries:
            if any(agent.strip() == "*" for agent in entry.useragents):
                return bool(entry.rulelines)
        default_entry = getattr(parser, "default_entry", None)
        return bool(default_entry and default_entry.rulelines)

    @staticmethod
    def _retry_after_seconds(r) -> float:
        """從 429 回應的 Retry-After 標頭取得建議等待秒數。

        F4 修復：
        - 支援 HTTP-date 格式（Retry-After: Wed, 21 Oct 2015 ...）——
          舊碼 float() 對日期拋錯，固定退回 65 秒。
        - 設上限 retry_after_cap：伺服器的 Retry-After 可能錯得離譜
          （實測 Retry-After: 999999 ≈ 11 天），無上限會讓 worker
          睡到地老天荒。
        """
        ra = r.headers.get("retry-after")
        secs = 20.0
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
    def _is_challenge(r, text: str) -> bool:
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

    def _reset_connections(self):
        """丟棄所有池化的 keep-alive socket（例如 CLOSE_WAIT 卡住後）。"""
        try:
            self.session.close()
        except Exception:
            pass
        self._mount_adapter()

    def sleep(self):
        """依設定延遲隨機休息（2~5 秒），降低站方請求負載。"""
        time.sleep(random.uniform(CRAWL["min_delay"], CRAWL["max_delay"]))
