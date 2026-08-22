"""HTML 解析器（轉換層）：把 PartSouq 四層頁面轉成結構化 dict。

locate 頁面 → 品牌與型號清單（手風琴式列表）
pick 頁面   → 車型清單（規格表）
vehicle 頁面 → 分類與零件組（樹狀結構）
unit 頁面    → 零件明細（料號/名稱/代碼/備註/數量/範圍表）

本層是純函式：輸入 HTML 字串、輸出 dict 列表，不碰網路也不碰資料庫。
"""

import logging
import re
from html import unescape
from typing import Any, Literal, overload
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup

from partsouq_crawler.parsers.common import parse_unambiguous_range

log = logging.getLogger("parse")

type ParsedRecord = dict[str, Any]

# 使用 lxml 解析器：比 html5lib 快 3~4 倍，且已驗證四層解析輸出完全一致
PARSER = "lxml"

# 分類編號對應的固定分類名稱（PartSouq 的四個主要分類）
CATEGORY_NAMES = {
    "1": "ENGINE/FUEL/TOOL",
    "2": "POWER TRAIN/CHASSIS",
    "3": "BODY/INTERIOR",
    "4": "ELECTRICAL",
}

# 預先編譯的正規表示式（零件組文字格式：NNNN: NAME）
GROUP_LINK_RE = re.compile(r"^([0-9A-Z]+\s+[0-9]+|[0-9]{3,4}):\s*(.*)$")


def _soup(html: str) -> BeautifulSoup:
    """把 HTML 解析成 BeautifulSoup 物件（lxml 引擎）。

    多個解析函式共用同一份 HTML 時（例如 vehicle 頁面要同時解析
    分類與零件組），請先呼叫本函式一次，再把 soup 傳給各解析函式，
    避免同一份 HTML 被 lxml 重複解析。
    """
    return BeautifulSoup(html, PARSER)


def _abs(href: str) -> str:
    """把網址的 HTML 跳脫字元還原（&amp; → &）。"""
    return unescape(href) if href else ""


def _qs(url: str, key: str) -> str | None:
    """從網址的 query string 取出指定參數（沒有則回傳 None）。"""
    q = parse_qs(urlparse(url).query)
    vals = q.get(key, [])
    return vals[0] if vals else None


def _is_partsouq_endpoint(url: str, path: str) -> bool:
    """只接受站內相對網址或 partsouq.com 的指定 endpoint。"""
    parsed = urlparse(url)
    if parsed.scheme and parsed.scheme not in {"http", "https"}:
        return False
    if parsed.scheme and not parsed.netloc:
        return False
    if parsed.netloc:
        try:
            port = parsed.port
        except ValueError:
            return False
        if parsed.hostname != "partsouq.com" or port not in {None, 80, 443}:
            return False
    return parsed.path.rstrip("/") == path


def _candidate_identity(
    url: str,
    *keys: str,
    required: tuple[str, ...] = (),
) -> tuple[str | None, ...]:
    """以 request context 對 canonical link 去重；缺欄時保留原網址。"""
    params = {key: _qs(url, key) for key in keys}
    values = tuple(params[key] for key in keys)
    if not any(values) or any(params[key] is None for key in required):
        return (*values, url)
    return values


def _context_mismatch(
    url: str,
    key: str,
    expected: str | None,
    *,
    allow_missing: bool = False,
) -> bool:
    """已知 request context 時拒絕外來值；可相容省略 brand 的舊連結。"""
    if expected is None:
        return False
    actual = _qs(url, key)
    if actual is None:
        return not allow_missing
    return actual != str(expected)


# ---------------------------------------------------------------- locate


@overload
def parse_brand_index(
    html: str,
    brand: str,
    soup: Any = None,
    diagnostics: Literal[False] = False,
) -> list[ParsedRecord]: ...


@overload
def parse_brand_index(
    html: str,
    brand: str,
    soup: Any = None,
    *,
    diagnostics: Literal[True],
) -> tuple[list[ParsedRecord], int]: ...


def parse_brand_index(
    html: str,
    brand: str,
    soup: Any = None,
    diagnostics: bool = False,
) -> list[ParsedRecord] | tuple[list[ParsedRecord], int]:
    """解析 locate 頁面 → 型號清單（含 pick 網址）。

    每個型號是手風琴 <h4> 標題，內含連結到
    /en/catalog/genuine/pick?c={brand}&model={name}&ssd={token}
    """
    soup = soup if soup is not None else _soup(html)
    models = []
    candidates = set()
    valid_candidates = set()
    for a in soup.select("a[href]"):
        href = _abs(a.get("href", ""))
        if not _is_partsouq_endpoint(href, "/en/catalog/genuine/pick"):
            continue
        # 其他品牌的交叉導覽連結是合法導覽，不是版型異常。
        if _context_mismatch(href, "c", brand, allow_missing=True):
            continue
        candidate = _candidate_identity(href, "c", "model", "ssd", required=("model",))
        candidates.add(candidate)
        name = a.get_text(strip=True)
        if not name or not href:
            continue
        valid_candidates.add(candidate)
        params = parse_qs(urlparse(href).query)
        models.append(
            {
                "name": name,
                "ssd": params.get("ssd", [None])[0],
                "url": href,
            }
        )
    if diagnostics:
        return models, len(candidates - valid_candidates)
    return models


@overload
def parse_brands(
    html: str,
    soup: Any = None,
    diagnostics: Literal[False] = False,
) -> list[ParsedRecord]: ...


@overload
def parse_brands(
    html: str,
    soup: Any = None,
    *,
    diagnostics: Literal[True],
) -> tuple[list[ParsedRecord], int]: ...


def parse_brands(
    html: str,
    soup: Any = None,
    diagnostics: bool = False,
) -> list[ParsedRecord] | tuple[list[ParsedRecord], int]:
    """解析原廠目錄首頁 → 品牌清單（含代碼）。

    品牌位於側邊欄：<a href="/en/catalog/genuine/locate?c=NAME">
    只採計側邊欄的連結（指向帶純品牌名的 locate 頁面）；
    依名稱去重，避免把表格列誤判為品牌連結。
    """
    soup = soup if soup is not None else _soup(html)
    brands = []
    seen = set()
    candidates = set()
    valid_candidates = set()
    for a in soup.select("li a[href]"):
        href = _abs(a.get("href", ""))
        if not _is_partsouq_endpoint(href, "/en/catalog/genuine/locate"):
            continue
        candidate = _candidate_identity(href, "c", required=("c",))
        candidates.add(candidate)
        code = _qs(href, "c")
        name = a.get_text(strip=True)
        if not code or not name:
            continue
        valid_candidates.add(candidate)
        if name in seen:
            continue
        seen.add(name)
        brands.append({"name": name, "url": href})
    if diagnostics:
        return brands, len(candidates - valid_candidates)
    return brands


# ----------------------------------------------------------------- pick


def _vehicle_fields(th_classes: Any, th_text: str) -> str | None:
    """把 pick 頁面的欄位標題對應到車型欄位名稱。

    各品牌的欄位配置不盡相同（Toyota: Name|Description|Model|Options|Prod
    Period；Nissan: Name|Grade|Market|Model|Year From|Options|Gearbox；
    部分表格還多了 Engine/Body Style 欄）。網站會在每個 <th> 上標記
    class 特徵（如 __model/__options/__prodPeriod/__modelyearfrom），
    因此我們以特徵為鍵，而不是靠欄位位置。
    """
    classes = " ".join(th_classes or [])
    classes_lower = classes.lower()
    text = th_text.strip()
    text_lower = text.lower()
    if "n_name" in classes or text == "Name":
        return "name"
    if "__description" in classes or text == "Description":
        return "description"
    if "__grade" in classes:
        return "grade"
    if "__market" in classes:
        return "market"
    if "__modelyearfrom" in classes:
        return "year_from"
    if "__modelyearto" in classes:
        return "year_to"
    if "__model" in classes or text == "Model":
        return "model_code"
    if "__engine" in classes_lower or text_lower == "engine":
        return "engine"
    if "__prodPeriod" in classes or "prod" in classes.lower():
        return "prod_period"
    if "__options" in classes or text == "Options":
        return "options"
    if (
        "__transmission" in classes_lower
        or "__gearbox" in classes_lower
        or text_lower in {"transmission", "gearbox"}
    ):
        return "transmission"
    if "__bodystyle" in classes_lower or text_lower == "body style":
        return "body_style"
    return None


@overload
def parse_vehicles(
    html: str,
    brand: str,
    soup: Any = None,
    diagnostics: Literal[False] = False,
) -> list[ParsedRecord]: ...


@overload
def parse_vehicles(
    html: str,
    brand: str,
    soup: Any = None,
    *,
    diagnostics: Literal[True],
) -> tuple[list[ParsedRecord], int]: ...


def parse_vehicles(
    html: str,
    brand: str,
    soup: Any = None,
    diagnostics: bool = False,
) -> list[ParsedRecord] | tuple[list[ParsedRecord], int]:
    """解析 pick 頁面的規格表 → 車型清單。

    欄位隨品牌與表格而異（部分品牌多了 Engine / Body Style / Grade /
    Market 等欄）。我們以 th 的 class 特徵對應欄位，而且只採計
    帶 /vehicle? 連結的列 —— 那些才是真正的車型。
    """
    soup = soup if soup is not None else _soup(html)
    vehicles = []
    malformed = 0
    candidates = set()
    valid_candidates = set()
    candidate_specs: dict[tuple[str | None, ...], tuple[str, ...]] = {}
    for a in soup.select("a[href]"):
        href = _abs(a.get("href", ""))
        # 只把本品牌車型的連結視為 candidate；其他品牌的交叉導覽
        # 連結是合法導覽，不是版型異常。
        if _is_partsouq_endpoint(href, "/en/catalog/genuine/vehicle") and not _context_mismatch(
            href, "c", brand, allow_missing=True
        ):
            candidates.add(_candidate_identity(href, "c", "ssd", "vid"))

    for table in soup.select("table"):
        rows = table.select("tr")
        if not rows:
            continue
        ths = rows[0].select("th")
        # 跳過沒有特徵標記的品牌/標題表格（Brand|Name|Code）
        col_map = {}
        has_marker = False
        for idx, th in enumerate(ths):
            field = _vehicle_fields(th.get("class"), th.get_text())
            if field == "name":
                has_marker = True
            if field:
                col_map[idx] = field
        if not has_marker:
            continue
        for tr in rows[1:]:
            tds = tr.find_all("td")
            if len(tds) <= 1:
                continue
            links = [
                a
                for a in tr.select("a[href]")
                if _is_partsouq_endpoint(_abs(a.get("href", "")), "/en/catalog/genuine/vehicle")
            ]
            matching_links = [
                a
                for a in links
                if not _context_mismatch(_abs(a.get("href", "")), "c", brand, allow_missing=True)
            ]
            if not matching_links:
                continue
            url = _abs(matching_links[0].get("href"))
            rec: ParsedRecord = {
                "name": "",
                "description": "",
                "model_code": "",
                "options": "",
                "prod_period": "",
            }
            for idx, field in col_map.items():
                if idx < len(tds):
                    rec[field] = tds[idx].get_text(strip=True)
            # 沒有明確的 Prod Period 欄時，用 year_from + year_to 兜出期間
            if not rec["prod_period"] and (rec.get("year_from") or rec.get("year_to")):
                yf, yt = rec.get("year_from") or "", rec.get("year_to") or ""
                if yf and yt:
                    rec["prod_period"] = f"{yf} - {yt}"
                elif yf:
                    rec["prod_period"] = f"{yf} -"
                else:
                    rec["prod_period"] = f"- {yt}"
            production_range = parse_unambiguous_range(rec["prod_period"])
            rec["production_from"] = production_range.start
            rec["production_to"] = production_range.end
            rec["ssd"] = _qs(url, "ssd")
            rec["vid"] = _qs(url, "vid")
            rec["url"] = url
            key = tuple(
                str(rec.get(field) or "")
                for field in (
                    "model_code",
                    "name",
                    "description",
                    "options",
                    "prod_period",
                    "grade",
                    "market",
                    "engine",
                    "transmission",
                    "body_style",
                )
            )
            if not any(key):
                continue
            row_candidates = {
                _candidate_identity(_abs(a.get("href", "")), "c", "ssd", "vid")
                for a in matching_links
            }
            for candidate in row_candidates:
                if candidate in candidate_specs and candidate_specs[candidate] != key:
                    malformed += 1
                    continue
                candidate_specs[candidate] = key
                valid_candidates.add(candidate)
            vehicles.append((rec, key))
    # ssd / vid / url 是請求用 token，不是車型身分。若依 ssd 去重，
    # 同 token 的不同規格會靜默消失；改以 parser 已辨識的穩定規格去重。
    seen: set[tuple[str, ...]] = set()
    out: list[ParsedRecord] = []
    for vehicle, key in vehicles:
        if key in seen:
            continue
        seen.add(key)
        out.append(vehicle)
    malformed += len(candidates - valid_candidates)
    if diagnostics:
        return out, malformed
    return out


# -------------------------------------------------------------- vehicle


@overload
def parse_category_links(
    html: str,
    brand: str,
    soup: Any = None,
    diagnostics: Literal[False] = False,
    expected_ssd: str | None = None,
    expected_vid: str | None = None,
) -> list[ParsedRecord]: ...


@overload
def parse_category_links(
    html: str,
    brand: str,
    soup: Any = None,
    *,
    diagnostics: Literal[True],
    expected_ssd: str | None = None,
    expected_vid: str | None = None,
) -> tuple[list[ParsedRecord], int, int]: ...


def parse_category_links(
    html: str,
    brand: str,
    soup: Any = None,
    diagnostics: bool = False,
    expected_ssd: str | None = None,
    expected_vid: str | None = None,
) -> list[ParsedRecord] | tuple[list[ParsedRecord], int, int]:
    """解析 vehicle 頁面 → 分類導覽連結。

    每個主要分類（Engine、Power Train、Body、Electrical）都是一個
    vehicle 頁面的變體連結，帶 cid + cname 參數。注意：站方對每一頁
    簽發獨立的 ssd token，因此分類連結的 ssd 與母頁不同是常態；
    expected_ssd 保留給舊簽名相容，不作為鑑別條件。
    """
    soup = soup if soup is not None else _soup(html)
    cats = []
    malformed = 0
    skipped_context = 0
    candidates = set()
    valid_candidates = set()
    seen: dict[str, str] = {}
    for a in soup.select("a[href]"):
        href = _abs(a.get("href", ""))
        if not _is_partsouq_endpoint(href, "/en/catalog/genuine/vehicle"):
            continue
        # 外來 context（其他車型的交叉導覽連結）與「Categories」這類
        # 無 cid 的標籤連結是合法導覽，不是版型異常 —— 在進入 candidate
        # 對帳前先排除，避免整台車被誤判 malformed。注意：站方對每一頁
        # 簽發獨立的 ssd token（分類連結帶自己的 ssd），所以 ssd 不能
        # 當鑑別條件；品牌與 vid 才是車輛身分。被排除的連結仍計入
        # skipped_context 供呼叫端診斷（首次爬取沒有 DB 歷史可對照時，
        # 靜默排除可能藏住漏抓的分類）。
        if _context_mismatch(href, "c", brand, allow_missing=True) or _context_mismatch(
            href, "vid", expected_vid
        ):
            skipped_context += 1
            continue
        cid = _qs(href, "cid")
        if not cid:
            skipped_context += 1
            continue
        candidate = _candidate_identity(
            href,
            "c",
            "ssd",
            "vid",
            "cid",
            required=("cid",),
        )
        candidates.add(candidate)
        text = a.get_text(strip=True)
        if not text:
            # 同一 cid 可能同時有圖片與文字 anchor；最後以 cid
            # 對帳，只有完全沒有文字 peer 才算 malformed。
            continue
        cname = _qs(href, "cname")
        name = unquote(cname) if cname else text
        if cid in seen:
            if seen[cid] != name:
                malformed += 1
            else:
                valid_candidates.add(candidate)
            continue
        seen[cid] = name
        valid_candidates.add(candidate)
        cats.append(
            {
                "category_name": name,
                "cid": cid,
                "ssd": _qs(href, "ssd"),
                "vid": _qs(href, "vid"),
                "url": href,
            }
        )
    malformed += len(candidates - valid_candidates)
    if diagnostics:
        return cats, malformed, skipped_context
    return cats


@overload
def parse_groups(
    html: str,
    brand: str,
    default_cid: str = "1",
    soup: Any = None,
    diagnostics: Literal[False] = False,
    expected_ssd: str | None = None,
    expected_vid: str | None = None,
    expected_cid: str | None = None,
) -> list[ParsedRecord]: ...


@overload
def parse_groups(
    html: str,
    brand: str,
    default_cid: str = "1",
    soup: Any = None,
    *,
    diagnostics: Literal[True],
    expected_ssd: str | None = None,
    expected_vid: str | None = None,
    expected_cid: str | None = None,
) -> tuple[list[ParsedRecord], int, int, int]: ...


def parse_groups(
    html: str,
    brand: str,
    default_cid: str = "1",
    soup: Any = None,
    diagnostics: bool = False,
    expected_ssd: str | None = None,
    expected_vid: str | None = None,
    expected_cid: str | None = None,
) -> list[ParsedRecord] | tuple[list[ParsedRecord], int, int, int]:
    """解析 vehicle 頁面 → 零件組連結（NNNN: NAME → /unit?...）。

    零件組位於目前啟用的分類區塊；每個都連結到
    /en/catalog/genuine/unit?c=..&ssd=..&vid=..&cid=N&uid=M&q=

    diagnostics=True 時回傳 (groups, malformed, skipped_context, image_only)。
    image_only = 圖片-only 連結（diagram 縮圖、無任何文字 anchor）的組數：
    站方合法版型（實測 Mitsubishi 部分車型的整頁都是此類連結），依
    cid+uid 仍可爬取，接受為空 code/name 的 group，不是版型異常。
    """
    soup = soup if soup is not None else _soup(html)
    groups = []
    malformed = 0
    skipped_context = 0
    image_only = 0
    seen: dict[tuple[str, str, str], str] = {}
    seen_uids: set[tuple[str, str]] = set()
    candidates = set()
    valid_candidates = set()
    candidate_specs: dict[tuple[str | None, ...], tuple[str, str]] = {}
    for a in soup.select("a[href]"):
        href = _abs(a.get("href", ""))
        # 只接受真正的 unit endpoint。redirect?next=/unit?... 或其他
        # query 內含 /unit? 的連結不是 group candidate。
        if not _is_partsouq_endpoint(href, "/en/catalog/genuine/unit"):
            continue
        # 外來 context（其他車型的交叉導覽連結）是合法導覽，不是版型
        # 異常 —— 在 candidate 對帳前先排除。站方每頁簽發獨立 ssd
        # token，因此 ssd 不能當鑑別條件；品牌與 vid 才是車輛身分。
        # 被排除的連結仍計入 skipped_context 供呼叫端診斷。
        if _context_mismatch(href, "c", brand, allow_missing=True) or _context_mismatch(
            href, "vid", expected_vid
        ):
            skipped_context += 1
            continue
        cid = _qs(href, "cid") or default_cid
        uid = _qs(href, "uid")
        candidate = _candidate_identity(
            href,
            "c",
            "ssd",
            "vid",
            "cid",
            "uid",
            required=("uid",),
        )
        candidates.add(candidate)
        if expected_cid is not None and cid != str(expected_cid):
            continue
        if not uid:
            continue
        text = a.get_text(strip=True)
        if not text:
            # 圖片-only 連結（diagram 縮圖、無任何文字 anchor）：站方
            # 合法版型（實測 Mitsubishi 部分車型的整頁都是此類連結）。
            # 沒有可解析的 group code/name，但仍可依 cid+uid 爬取
            # unit 頁 —— 接受為空 code/name 的 group，不計 malformed。
            # 圖片與文字可能同時連到同一組；以 (cid, uid) 去重，文字
            # anchor 已收錄時不重複 append。
            valid_candidates.add(candidate)
            if (cid, uid) in seen_uids:
                continue
            seen_uids.add((cid, uid))
            image_only += 1
            groups.append(
                {
                    "group_code": "",
                    "group_name": "",
                    "category_name": CATEGORY_NAMES.get(cid, f"CATEGORY {cid}"),
                    "cid": cid,
                    "uid": uid,
                    "ssd": _qs(href, "ssd"),
                    "vid": _qs(href, "vid"),
                    "url": href,
                }
            )
            continue
        m = GROUP_LINK_RE.match(text)
        if not m:
            continue
        group_name = m.group(2).strip()
        if not group_name:
            continue
        candidate_spec = (m.group(1), group_name)
        if candidate in candidate_specs and candidate_specs[candidate] != candidate_spec:
            malformed += 1
            continue
        candidate_specs[candidate] = candidate_spec
        valid_candidates.add(candidate)
        # 身分包含 uid：同一 (cid, group_code) 在不同車型變體區會以
        # 不同 uid 出現，內容可能不同（變體專屬零件）；只保留第一筆
        # 會漏抓。名稱衝突只在「完全相同身分」時才計 malformed。
        identity = (cid, m.group(1), uid)
        if identity in seen:
            if seen[identity] != group_name:
                malformed += 1
            continue
        seen[identity] = group_name
        uid_key = (cid, uid)
        if uid_key in seen_uids:
            # 同一組先前已以圖片-only 收錄（空 code/name）：就地升級，
            # 不重複 append。注意：只升級「仍是圖片-only」的 group ——
            # 若同 uid 已有不同 code 的文字 group（變體專屬資料），
            # 照舊 append（HEAD 語意，不能靜默丟掉）。
            upgraded = False
            for g in groups:
                if g["cid"] == cid and g["uid"] == uid and not g["group_code"]:
                    g["group_code"] = m.group(1)
                    g["group_name"] = group_name
                    upgraded = True
                    break
            if upgraded:
                continue
        seen_uids.add(uid_key)
        groups.append(
            {
                "group_code": m.group(1),
                "group_name": group_name,
                "category_name": CATEGORY_NAMES.get(cid, f"CATEGORY {cid}"),
                "cid": cid,
                "uid": uid,
                "ssd": _qs(href, "ssd"),
                "vid": _qs(href, "vid"),
                "url": href,
            }
        )
    malformed += len(candidates - valid_candidates)
    if diagnostics:
        return groups, malformed, skipped_context, image_only
    return groups


# ----------------------------------------------------------------- unit


@overload
def parse_parts(
    html: str,
    soup: Any = None,
    diagnostics: Literal[False] = False,
) -> tuple[list[ParsedRecord], int]: ...


@overload
def parse_parts(
    html: str,
    soup: Any = None,
    *,
    diagnostics: Literal[True],
) -> tuple[list[ParsedRecord], int, int, list[ParsedRecord]]: ...


def parse_parts(
    html: str,
    soup: Any = None,
    diagnostics: bool = False,
) -> tuple[list[ParsedRecord], int] | tuple[list[ParsedRecord], int, int, list[ParsedRecord]]:
    """解析 unit 頁面的零件表。

    unit 頁面有兩張表：先是車型資訊的標題表，再來才是零件表
    （class 約為 'glow pop-vin'）。零件欄位以表頭名稱對應；目前站上
    同時存在 6 欄與 7 欄版型。沒有表頭的舊 fixture 則維持
    Number|Name|Code|Note|Quantity|Range 的相容順序。**第一個儲存格**
    仍必須連結到 /search/all?q=。

    嚴謹度（P1 修復）：只接受「搜尋連結出現在第一格」的列 —— 若同頁
    有一筆合法資料讓外層 guard 通過，其他欄位不足或空料號的列必須被
    正確排除，避免靜默漏資料或寫入錯誤料號。

    P2 修復：不依賴 `<tbody>`（部分頁面沒有顯式 tbody，lxml/html.parser
    不會自動補），直接以 table 的直接子 `tr` 為準；td 也只取直接子層，
    避免巢狀 table 的儲存格竄入造成欄位錯位。

    回傳 (parts, malformed)，diagnostics=True 時回傳
    (parts, malformed, skipped_nameless, skipped_rows)：
    - parts：結構完整、Number 與 Name 非空，且能辨認 Code、Quantity 的
      零件列。Unified／Filter Note／Specification 只合併進 note；只有
      明確名為 Range／Prod period 的欄位才會寫入日期範圍。
    - malformed：異常 candidate 列數 —— 表頭缺必要欄位、資料欄數與
      表頭不符、無表頭時不是舊 6 欄版型、搜尋網址非 PartSouq
      `/en/search/all`、q 為空，或顯示料號與 q 不同。這代表頁面版型
      異常（或反爬變體）仍解析出「看似零件」的殘缺列；呼叫端必須拒絕
      寫 terminal receipt。
    - skipped_nameless：站方合法存在、但完全沒有可驗證文字名稱的列
      （純料號列，連圖片 alt 都沒有）。發布資料必須能把料號對到產品
      名稱，因此不落庫；但這**不是**版型異常，呼叫端不得因此失敗整台
      車（實測 Toyota 等車型的部分 unit 頁固定含有此類列）。
    - skipped_rows：上述被跳過列的原始欄位 dict（SOL review P1）。
      呼叫端把它們寫入 quarantine 表，避免「整組標 done、料號永久漏掉」。
    """
    soup = soup if soup is not None else _soup(html)
    parts_by_key: dict[tuple[str, str], ParsedRecord] = {}
    malformed = 0
    skipped_nameless = 0
    skipped_rows: list[ParsedRecord] = []
    for table in soup.find_all("table"):
        # 巢狀 table（包在另一個 table 的 td 裡）不是零件表本身，
        # 必須排除 —— 否則其內層列會被當成獨立的零件列（P2 修復，
        # fresh probe 會同時產生假料號與真料號）。
        if table.find_parent("table"):
            continue
        # 直接子層 tr（無顯式 tbody）與 thead/tbody 內的 tr 都要檢查；
        # 只取其一會漏掉混合結構的另一半。
        direct_rows = table.find_all("tr", recursive=False)
        section_rows = [
            tr
            for section in table.find_all(["thead", "tbody"], recursive=False)
            for tr in section.find_all("tr", recursive=False)
        ]
        all_rows = direct_rows + section_rows
        header_row = next(
            (tr for tr in all_rows if tr.find_all("th", recursive=False)),
            None,
        )
        header_cells = header_row.find_all(["th", "td"], recursive=False) if header_row else []
        header_labels = [cell.get_text(" ", strip=True) for cell in header_cells]
        header_indexes = {}
        note_indexes = []
        for index, label in enumerate(header_labels):
            normalized = re.sub(r"[^a-z0-9]+", " ", label.lower()).strip()
            field = {
                "number": "part_number",
                "part number": "part_number",
                "name": "name",
                "part name": "name",
                "code": "code",
                "quantity": "quantity",
                "qty": "quantity",
                "qty required": "quantity",
                "range": "range_str",
                "part range": "range_str",
                "prod period": "range_str",
                "production period": "range_str",
            }.get(normalized)
            if field:
                header_indexes[field] = index
            elif normalized in {"note", "unified", "filter note", "specification"}:
                note_indexes.append(index)
        required_headers = {"part_number", "name", "code", "quantity"}
        header_is_usable = not header_cells or required_headers <= header_indexes.keys()

        trs = direct_rows + [
            tr
            for tb in table.find_all("tbody", recursive=False)
            for tr in tb.find_all("tr", recursive=False)
        ]
        for tr in trs:
            tds = tr.find_all("td", recursive=False)
            if not tds:
                continue
            # 先以 endpoint path 辨認 candidate，讓同路徑的外站網址也會
            # 被回報 malformed，而不是靜默忽略。
            search_links = []
            for a in tds[0].select("a[href]"):
                href = _abs(a.get("href", ""))
                if urlparse(href).path.rstrip("/") == "/en/search/all":
                    search_links.append(href)
            if not search_links:
                continue
            if header_cells:
                if not header_is_usable or len(tds) != len(header_cells):
                    malformed += 1
                    continue
            elif len(tds) != 6:
                malformed += 1
                continue
            cells = [td.get_text(strip=True) for td in tds]
            if header_cells:
                part_number = cells[header_indexes["part_number"]]
                part_name = cells[header_indexes["name"]]
                code = cells[header_indexes["code"]]
                quantity = cells[header_indexes["quantity"]]
                range_str = (
                    cells[header_indexes["range_str"]] if "range_str" in header_indexes else ""
                )
                note_values = [
                    (header_labels[index], cells[index]) for index in note_indexes if cells[index]
                ]
                if (
                    len(note_indexes) == 1
                    and header_labels[note_indexes[0]].strip().lower() == "note"
                ):
                    note = cells[note_indexes[0]]
                else:
                    note = "; ".join(f"{label}: {value}" for label, value in note_values)
            else:
                part_number, part_name, code, note, quantity, range_str = cells
            queries = set()
            valid_links = True
            for href in search_links:
                if not _is_partsouq_endpoint(href, "/en/search/all"):
                    valid_links = False
                    break
                query = _qs(href, "q")
                if not query:
                    valid_links = False
                    break
                queries.add(query)
            if not valid_links or len(queries) != 1 or part_number not in queries:
                malformed += 1
                continue
            if not part_name:
                # 站方合法存在、但沒有可驗證文字名稱的純料號列
                # （無名稱文字、無圖片 alt）。發布資料必須能把料號對到
                # 產品名稱，因此不落庫；但這不是版型異常，不能算進
                # malformed —— 否則含此類列的 unit 頁整台車永遠失敗。
                # 例外：其餘欄位若含有料號/code/數量/日期/note 以外的
                # 文字，或連 code 都缺，代表欄位錯位/版型異常，仍算
                # malformed。
                allowed_cells = {part_number, code, quantity, range_str, ""}
                for index in note_indexes:
                    allowed_cells.add(cells[index])
                if not code or any(c for c in cells if c not in allowed_cells):
                    malformed += 1
                    continue
                skipped_nameless += 1
                skipped_rows.append(
                    {
                        "part_number": part_number,
                        "code": code,
                        "quantity": quantity,
                        "range_str": range_str,
                        "note": note,
                    }
                )
                continue
            part = {
                "part_number": part_number,
                "name": part_name,
                "code": code,
                "note": note,
                "quantity": quantity,
                "range_str": range_str,
            }
            part_range = parse_unambiguous_range(range_str)
            part["part_from"] = part_range.start
            part["part_to"] = part_range.end
            # receipt/shrink 必須使用 DB natural key 的實際列數；同頁重複
            # DOM row 不得把 fetched_row_count 灌大。後出現的列與 MySQL
            # ON DUPLICATE KEY UPDATE 語意一致，覆蓋同鍵的前一列 payload。
            parts_by_key[(part_number, range_str)] = part
    if diagnostics:
        return list(parts_by_key.values()), malformed, skipped_nameless, skipped_rows
    return list(parts_by_key.values()), malformed


def looks_like_challenge(html: str) -> bool:
    """粗略判斷回應是否為 Cloudflare 驗證頁（供診斷使用）。"""
    return "Just a moment" in html[:8000] or "請稍候" in html[:8000] or "cf_chl" in html[:8000]
