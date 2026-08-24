"""PartSouq 每月爬蟲的設定（集中管理，可用環境變數覆寫）。

這裡是整個專案的「單一事實來源」：資料庫連線、網站網址、CloakBrowser
整合、爬取參數，全部集中在這個模組，其他模組一律從這裡讀取。
"""

import json
import os
import shlex
import tempfile
from pathlib import Path
from typing import TypedDict

from .state_files import ensure_private_state_directory

type Cookie = dict[str, str]
type Cookies = list[Cookie]


class SiteConfig(TypedDict):
    base: str
    genuine: str
    locate: str
    pick: str
    vehicle: str
    unit: str


class CloakConfig(TypedDict):
    state_dir: Path
    venv_python: str
    cookie_export_file: Path
    cookie_file: Path
    lock_file: Path
    error_log_file: Path
    launcher: list[str]
    user_agent: str


class CrawlConfig(TypedDict):
    min_delay: float
    max_delay: float
    http_timeout: int
    max_retries: int
    challenge_retries: int
    retry_after_cap: int
    start_brand: str
    limit_brands: int
    limit_models: int
    limit_vehicles: int
    workers: int
    request_rate: float
    request_burst: int
    max_run_days: float
    min_brands: int
    limit_groups: int
    limit_parts: int
    vehicle_year_window: int
    bounded_parts: int
    bounded_run_key: str
    scheduled_job_run_id: int
    evidence_max_body_bytes: int
    evidence_max_run_bytes: int
    evidence_max_artifacts: int
    row_count_shrink_ratio: float
    block_breather: int


# 專案根目錄。安裝成套件後仍可用 PARTSOUQ_HOME 指向實際資料目錄。
BASE_DIR = (
    Path(os.environ.get("PARTSOUQ_HOME", str(Path(__file__).resolve().parents[2])))
    .expanduser()
    .resolve()
)
# host runner 會把不同 checkout 指向同一個使用者私有 state dir，確保
# cookie 與 refresh lock 跨 worktree 協調；未設定時維持專案內隔離。
CLOAK_STATE_DIR = (
    Path(os.environ.get("PSQ_CLOAK_STATE_DIR", BASE_DIR / "data")).expanduser().absolute()
)
COOKIE_FILE = CLOAK_STATE_DIR / "cookies.json"

# MySQL 連線設定。PartSouq 型錄、NHTSA 與後台只使用同一組 PARTSOUQ_DB_*。
DB_CONFIG = {
    "host": os.environ.get("PARTSOUQ_DB_HOST", "127.0.0.1"),
    "port": int(os.environ.get("PARTSOUQ_DB_PORT", "3308")),
    "user": os.environ.get("PARTSOUQ_DB_USER", "partsouq"),
    "password": os.environ.get("PARTSOUQ_DB_PASSWORD", "partsouq-local"),
    "database": os.environ.get("PARTSOUQ_DB_NAME", "partsouq_catalog"),
}

# PartSouq 的各層頁面網址模板
SITE: SiteConfig = {
    "base": "https://partsouq.com",
    "genuine": "https://partsouq.com/en/catalog/genuine",
    "locate": "https://partsouq.com/en/catalog/genuine/locate?c={brand}",
    "pick": "https://partsouq.com/en/catalog/genuine/pick?c={brand}&model={model}&ssd={ssd}",
    "vehicle": "https://partsouq.com/en/catalog/genuine/vehicle?c={brand}&ssd={ssd}&vid={vid}&q=",
    "unit": "https://partsouq.com/en/catalog/genuine/unit?c={brand}&ssd={ssd}&vid={vid}&cid={cid}&uid={uid}&q=",
}

# CloakBrowser（隱匿瀏覽器）的整合設定
CLOAK: CloakConfig = {
    "state_dir": CLOAK_STATE_DIR,
    "venv_python": os.environ.get(
        "PSQ_CLOAK_PYTHON",
        str(Path(os.environ.get("CLOAK_VENV", "~/.venvs/partsouq-cloak/bin/python")).expanduser()),
    ),
    "cookie_export_file": Path(
        os.environ.get("PSQ_COOKIE_EXPORT_FILE", CLOAK_STATE_DIR / ".cloak-export.json")
    ).expanduser(),
    "cookie_file": COOKIE_FILE,  # 持久化 session cookie（程序啟動時沿用，見 cloak.get_session）
    "lock_file": COOKIE_FILE.parent / ".cloak-refresh.lock",
    "error_log_file": Path(
        os.environ.get(
            "PSQ_CLOAK_ERROR_LOG_FILE",
            CLOAK_STATE_DIR / "cloak-launch.err.log",
        )
    ).expanduser(),
    # 啟動瀏覽器子程序時附加的前綴命令。headless=False 在無顯示環境
    # （如 Compose 的 Linux container）需要虛擬顯示，容器內設
    # PSQ_CLOAK_LAUNCHER="xvfb-run -a"；macOS host 留空即可。
    "launcher": shlex.split(os.environ.get("PSQ_CLOAK_LAUNCHER", "")),
    # HTTP 請求必須使用與瀏覽器一致的身分 UA，cf_clearance 才會成立。
    # macOS host 瀏覽器是 Chrome/145 Mac；Linux container 的 CloakBrowser
    # 以 Windows fingerprint 呈現 Chrome/146，需用 PSQ_CLOAK_USER_AGENT
    # 覆寫成對應的 Windows UA。
    "user_agent": os.environ.get(
        "PSQ_CLOAK_USER_AGENT",
        (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
        ),
    ),
}

# 爬取行為參數（可用環境變數覆寫；0 代表不設上限）
CRAWL: CrawlConfig = {
    # 節奏：每個 worker 每個請求之間隨機休息 2~5 秒。
    # 全站總請求速率維持在低檔，避免被 Cloudflare 盯上。
    # 慢一點更安全；爬蟲本來就設計成可以連續跑好幾天。
    "min_delay": float(os.environ.get("PSQ_MIN_DELAY", "2.0")),  # 每請求最小間隔（秒）
    "max_delay": float(os.environ.get("PSQ_MAX_DELAY", "5.0")),  # 每請求最大間隔（秒）
    "http_timeout": 20,  # 單次 HTTP 請求逾時（秒）
    "max_retries": 5,  # 失敗重試次數
    "challenge_retries": 3,  # 碰到驗證時重新取得 cookie 的次數
    "retry_after_cap": 300,  # 429 Retry-After 等待上限（秒）（F4：防伺服器給巨額值）
    "start_brand": os.environ.get("PSQ_START_BRAND", ""),  # 只爬指定品牌（空=全部）
    "limit_brands": int(os.environ.get("PSQ_LIMIT_BRANDS", "0")),  # 品牌數上限
    "limit_models": int(os.environ.get("PSQ_LIMIT_MODELS", "0")),  # 每品牌型號數上限（測試用）
    "limit_vehicles": int(os.environ.get("PSQ_LIMIT_VEHICLES", "0")),  # 每型號車型數上限
    "workers": int(os.environ.get("PSQ_WORKERS", "4")),  # 並行 worker 數（in-flight 數，非總速率）
    # F5 全域 request governor：總請求率與 worker 數脫鉤。
    # 預設 0.5 token/s（每 2 秒一個請求）＋ burst 4：2 個 worker 時
    # 速率約等於原本的 2~5s/worker，但 4 個 worker 不會線性翻倍，
    # 也不會在等待結束時形成突發流量。
    "request_rate": float(os.environ.get("PSQ_REQUEST_RATE", "0.5")),  # 全域每秒請求數
    "request_burst": int(os.environ.get("PSQ_REQUEST_BURST", "4")),  # token bucket burst 上限
    # supervisor 的單趟執行期限。Crawler 也使用同一個值記錄最低 request
    # budget；若已知剩餘 group 明顯超出樂觀容量，會在發送網路請求前
    # 警告。此期限在 supervisor 重啟後重算，不能當成跨程序硬 SLA。
    "max_run_days": float(os.environ.get("PSQ_MAX_RUN_DAYS", "25")),
    # 首頁品牌清單的最低品牌數（SOL P1）：首次爬取（空 DB）時閉合檢查
    # 拿「本次解析結果」對「DB 已知」沒有意義（兩者同源），縮水解析
    # 會被誤判成完整。低於此門檻視為網站縮水/反爬頁，run 直接 error。
    # 目前站上有 18 個品牌；若站方增減請用環境變數調整。
    "min_brands": int(os.environ.get("PSQ_MIN_BRANDS", "18")),
    "limit_groups": int(os.environ.get("PSQ_LIMIT_GROUPS", "0")),  # 全站零件組數上限（測試用）
    # 舊有的未發布 sample 上限；只供測試，不可與 bounded 模式併用。
    "limit_parts": int(os.environ.get("PSQ_LIMIT_PARTS", "0")),
    # 只收錄「生產期間與最近 N 個日曆年重疊」的車款（含第 N 年）：
    # 界線在每次執行時以當天日期動態計算（2026 跑 = 2006 年起，
    # 2028 跑 = 2008 年起），不寫死年份。0 = 不過濾（全量）。
    # 生產結束年不明的車款一律照爬，不因資料缺失而漏抓。
    "vehicle_year_window": int(os.environ.get("PSQ_VEHICLE_YEAR_WINDOW", "0")),
    # 正式的有界資料集上限。只有精確達標、無爬取錯誤且通過
    # 品質關卡才會原子發布到 bounded_parts；不會改寫全站 snapshot。
    "bounded_parts": int(os.environ.get("PSQ_BOUNDED_PARTS", "0")),
    # 排程重試必須沿用同一個 logical run 才能續爬。空值時由
    # Crawler 先找同 target/provenance 的未完成 run，沒有才建新 key。
    "bounded_run_key": os.environ.get("PSQ_BOUNDED_RUN_KEY", "").strip(),
    # 由 partsouq-scheduler 建立子程式時注入。直接 CLI 執行為 0，
    # 不得通過正式 bounded 發布 gate。
    "scheduled_job_run_id": int(os.environ.get("SCHEDULED_JOB_RUN_ID", "0")),
    # Formal evidence stores only compressed, secret-sanitized parser input.
    # These are hard fail-closed budgets, not targets; operators may lower them
    # after the first verified 10k run establishes an observed distribution.
    "evidence_max_body_bytes": int(
        os.environ.get("PSQ_EVIDENCE_MAX_BODY_BYTES", str(8 * 1024 * 1024))
    ),
    "evidence_max_run_bytes": int(
        os.environ.get("PSQ_EVIDENCE_MAX_RUN_BYTES", str(1024 * 1024 * 1024))
    ),
    "evidence_max_artifacts": int(os.environ.get("PSQ_EVIDENCE_MAX_ARTIFACTS", "50000")),
    # 零件數縮水門檻（SOL review P1）：本次解析到的零件數 < 前次
    # receipt 的 row_count × 此比例時視為「格式完整但內容縮水」
    # （反爬變體/版型異常），拒絕寫 terminal receipt。前次 < 3 筆的
    # 小組不做檢查（零件可能被站方合法下架一兩筆）。0 停用。
    "row_count_shrink_ratio": float(os.environ.get("PSQ_ROW_COUNT_SHRINK_RATIO", "0.5")),
    # 疑似反爬頁（大頁面解析 0 零件/0 組）觸發時的強制停頓秒數：
    # Cloudflare 的限速器在 4 worker 連發時會給反爬頁，停頓讓它喘息、
    # 避免 crawler 在封鎖期間用最大速率重錘同一批頁面。
    "block_breather": int(os.environ.get("PSQ_BLOCK_BREATHER", "45")),
}

# 正式 host runtime 會把可變日誌放在 release 外；其他入口維持既有預設。
LOG_DIR = Path(os.environ.get("PSQ_RUNTIME_LOG_DIR", BASE_DIR / "logs")).expanduser().absolute()


def load_cookies(path: Path = COOKIE_FILE) -> Cookies | None:
    """從磁碟載入已存 cookie。回傳 dict 列表或 None（不存在/解析失敗）。"""
    if not path.exists():
        return None
    try:
        raw: object = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, list):
        return None
    cookies: Cookies = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        name = item.get("name")
        value = item.get("value")
        domain = item.get("domain", "partsouq.com")
        cookie_path = item.get("path", "/")
        if (
            not isinstance(name, str)
            or not isinstance(value, str)
            or not isinstance(domain, str)
            or not isinstance(cookie_path, str)
        ):
            return None
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": domain,
                "path": cookie_path,
            }
        )
    return cookies


def save_cookies(cookies: Cookies) -> None:
    """以 owner-only 暫存檔原子更新 cookie，避免半份 JSON 或權限窗口。"""
    ensure_private_state_directory(COOKIE_FILE.parent)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=COOKIE_FILE.parent,
            prefix=f".{COOKIE_FILE.name}.",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            os.fchmod(temporary.fileno(), 0o600)
            json.dump(cookies, temporary, indent=1)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, COOKIE_FILE)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
