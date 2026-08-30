"""自癒監督迴圈：讓每月無人值守的爬蟲永遠有人顧。

launchd 每個月觸發一次本程式。它負責「擁有」爬蟲子程序，並執行
一連串健康檢查（loop of checks），確保全程不需要任何人介入：

  1. 爬蟲程序還活著嗎？        -> 崩潰就重啟
  2. 有別的爬蟲在跑嗎？        -> 收養/接管，避免雙寫資料庫
  3. 爬蟲最近有進度嗎？        -> 卡住就重啟（心跳檢查）
  4. 爬蟲記憶體有沒有洩漏？    -> RSS 超過上限就重啟
  5. 磁碟空間還夠嗎？          -> 不足時記錄並提前退場
  6. 資料庫還健康嗎？          -> SELECT 1 失敗時警告
  7. cookie 還新鮮嗎？         -> 過期就記 warning（刷新是 crawler 的職責）
  8. 爬取完成沒？              -> 全部品牌完成就乾淨退出
  9. 總執行時限到了嗎？        -> 超過上限（25 天）強制結束

重啟風暴保護：在時間窗口內重啟超過 RESTART_MAX 次，監督迴圈會
進入長時間冷卻，而不是繼續狂打網站。每趟結束時把重啟次數、原因
與計數寫入 logs/summary.json，一個月後 10 秒內就能判斷這趟是否正常。
"""

import json
import logging
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from types import FrameType
from typing import NotRequired, TypedDict

from .admission import (
    AdmissionLockBusy,
    acquire_catalog_writer_admission,
    release_catalog_writer_admission,
)
from .cloak import COOKIE_TTL
from .config import COOKIE_FILE, CRAWL, LOG_DIR
from .db import Database
from .scheduler import CHILD_TERMINATE_GRACE_SECONDS
from .state_files import (
    PrivateRotatingFileHandler,
    ensure_private_state_directory,
    open_private_state_file,
)

log = logging.getLogger("supervisor")

CHECK_INTERVAL = 60  # 健康檢查間隔（秒）
HANG_TIMEOUT = 20 * 60  # 20 分鐘沒有新零件 => 判定卡死，重啟
PROGRESS_QUERY = "SELECT MAX(updated_at) AS last_write FROM parts"
RESTART_MAX = 3  # 每窗口超過 3 次重啟 => 進入冷卻
# 重啟計數器在此時間窗口內有效。SOL review P1：窗口必須**嚴格大於**
# 卡死週期 × 門檻 —— 若固定每 HANG_TIMEOUT 卡死一次且窗口剛好等於
# 週期 × 門檻，第 4 次重啟會剛好把第 1 次排除（now - t == W 不滿足
# now - t < W），永遠只有 3 筆、冷卻永不觸發。多加 2×CHECK_INTERVAL
# 的餘量吸收 tick 粒度（60s）造成的週期抖動。
RESTART_WINDOW = HANG_TIMEOUT * RESTART_MAX + CHECK_INTERVAL * 2  # 20 分鐘卡死 × 3 次門檻 + 餘量
COOLDOWN = 30 * 60  # 重啟風暴後的冷卻時間（秒）
MEMORY_LIMIT_MB = 2048  # 爬蟲 RSS 超過此值 => 重啟（疑似記憶體洩漏）
DISK_MIN_FREE_MB = 5120  # 磁碟剩餘低於此值（MB）=> 記錄並提前退場
MAX_RUN_SECONDS = int(float(CRAWL.get("max_run_days", 25)) * 24 * 3600)  # 單趟最長執行時限

# crawler 入口的命令列特徵：直譯器大小寫不敏感（macOS 的 Python 安裝在
# /Library/Frameworks/.../MacOS/Python，comm 也可能被截斷）。直譯器 token
# 允許「純 python3」或「絕對路徑結尾是 Python」兩種形式，命令可能是
# 「-m partsouq_catalog.run_crawl」或「/path/to/partsouq_catalog/run_crawl.py」。
CRAWLER_CMDLINE_RE = re.compile(
    r"^(?:\S*[Pp]ython[\d.]*)(?:\s+)(?:-m\s+partsouq_catalog\.run_crawl|"
    r"\S*partsouq_catalog[/\\]run_crawl\.py)(?:\s|$)",
)


class RestartSummary(TypedDict):
    time: str
    reason: str


class SupervisorSummary(TypedDict):
    restarts: list[RestartSummary]
    cooldowns: int
    started: str
    finished: str | None
    status: NotRequired[str]


class Supervisor:
    """監督迴圈：檢查、重啟、冷卻，負責讓爬蟲一路跑到完成。"""

    def __init__(self, workers: int = 4) -> None:
        self.workers = workers
        self.proc: subprocess.Popen[bytes] | None = None
        self.restarts: list[float] = []
        # 單一心跳基準：目前 crawler 子程序的啟動時刻（monotonic）。
        # 卡死判斷統一以它為準，避免「寫入老化 + 寬限」疊加造成
        # 約 40 分鐘才偵測到卡死（P1 修復）。
        self.crawler_started_at = 0.0
        self.db: Database | None = None
        self.cooldown_until = 0.0
        # process 內仍使用 monotonic，避免系統時間校正影響冷卻；磁碟上
        # 改存 wall-clock epoch，Supervisor 重啟後才能還原剩餘時間。
        self.restart_state_path = LOG_DIR / "supervisor_state.json"
        self._restart_state_loaded = False
        self.started_at = time.monotonic()
        # 這趟的統計（結束時寫入 logs/summary.json）
        self.summary: SupervisorSummary = {
            "restarts": [],
            "cooldowns": 0,
            "started": time.strftime("%Y-%m-%d %H:%M:%S"),
            "finished": None,
        }

    # ------------------------------------------------------ restart state

    def _load_restart_state(self) -> None:
        """載入跨程序的重啟窗口與冷卻狀態。

        state file 只存 wall-clock epoch；載入時換回本程序的 monotonic
        基準。檔案遺失是正常首次啟動，內容損毀則清空後繼續，避免監督
        程序因一個小型狀態檔永久無法啟動。
        """
        self._restart_state_loaded = True
        self.restarts = []
        self.cooldown_until = 0.0
        try:
            descriptor = open_private_state_file(self.restart_state_path, os.O_RDONLY)
            try:
                state_file = os.fdopen(descriptor, "r", encoding="utf-8")
            except BaseException:
                os.close(descriptor)
                raise
            with state_file:
                state = json.load(state_file)
            if not isinstance(state, dict) or state.get("version") != 1:
                raise ValueError("unsupported restart state")
            restart_times = state.get("restart_times", [])
            if not isinstance(restart_times, list):
                raise ValueError("restart_times must be a list")
            cooldown_wall = state.get("cooldown_until", 0)
            if isinstance(cooldown_wall, bool) or not isinstance(cooldown_wall, (int, float)):
                raise ValueError("cooldown_until must be a number")
            cooldown_wall = float(cooldown_wall)
            if not math.isfinite(cooldown_wall):
                raise ValueError("cooldown_until must be finite")

            now_wall = time.time()
            now_mono = time.monotonic()
            # 一段 cooldown 正常結束後，造成風暴的舊事件也必須一起清掉；
            # 否則 COOLDOWN(30m) 小於 RESTART_WINDOW(62m)，下一次錯誤會
            # 立刻再次進入冷卻。
            cooldown_expired = 0 < cooldown_wall <= now_wall
            if not cooldown_expired:
                for value in restart_times:
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        raise ValueError("restart timestamp must be a number")
                    value = float(value)
                    if not math.isfinite(value):
                        raise ValueError("restart timestamp must be finite")
                    age = max(0.0, now_wall - value)
                    if age < RESTART_WINDOW:
                        self.restarts.append(now_mono - age)

            if cooldown_wall > now_wall:
                # 壁鐘若被往回校正，磁碟 deadline 可能異常遙遠；最多只
                # 恢復一個完整 COOLDOWN，不能把 crawler 永久鎖住。
                remaining = min(cooldown_wall - now_wall, COOLDOWN)
                self.cooldown_until = now_mono + remaining
        except FileNotFoundError:
            return
        except (OSError, OverflowError, ValueError, TypeError, json.JSONDecodeError) as e:
            log.warning("restart state ignored: %s", e)
            self.restarts = []
            self.cooldown_until = 0.0

        # 每次載入都原子寫回正規化後的內容，同時移除過期資料或修復
        # 損毀檔；寫入失敗只降級為本程序內保護，不中止 supervisor。
        self._persist_restart_state()

    def _persist_restart_state(self) -> None:
        """以原子 replace 保存 restart/cooldown 的 wall-clock 狀態。"""
        if not self._restart_state_loaded:
            return
        now_mono = time.monotonic()
        now_wall = time.time()
        restart_times = [
            now_wall - max(0.0, now_mono - value)
            for value in self.restarts
            if isinstance(value, (int, float)) and math.isfinite(value)
        ]
        cooldown_wall = (
            now_wall + max(0.0, self.cooldown_until - now_mono)
            if self.cooldown_until > now_mono
            else 0
        )
        state = {
            "version": 1,
            "restart_times": restart_times,
            "cooldown_until": cooldown_wall,
        }
        temporary_path: Path | None = None
        try:
            ensure_private_state_directory(self.restart_state_path.parent)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.restart_state_path.name}.",
                dir=self.restart_state_path.parent,
            )
            temporary_path = Path(temporary_name)
            try:
                temporary_file = os.fdopen(descriptor, "w", encoding="utf-8")
            except BaseException:
                os.close(descriptor)
                raise
            with temporary_file:
                os.fchmod(temporary_file.fileno(), 0o600)
                json.dump(state, temporary_file, separators=(",", ":"))
                temporary_file.write("\n")
            os.replace(temporary_path, self.restart_state_path)
        except OSError as e:
            log.warning("restart state write failed: %s", e)
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _prune_restart_state(self, now: float | None = None) -> None:
        """移除過期窗口；冷卻結束時清空本輪風暴紀錄。"""
        now = time.monotonic() if now is None else now
        changed = False
        if self.cooldown_until and now >= self.cooldown_until:
            self.cooldown_until = 0.0
            self.restarts = []
            changed = True
        elif not self.cooldown_until:
            active = [value for value in self.restarts if now - value < RESTART_WINDOW]
            changed = active != self.restarts
            self.restarts = active
        if changed:
            self._persist_restart_state()

    # ------------------------------------------------------------ crawler

    def _crawler_cmd(self) -> list[str]:
        """回傳啟動爬蟲子程序的命令列。"""
        return [
            sys.executable,
            "-m",
            "partsouq_catalog.run_crawl",
            "--workers",
            str(self.workers),
        ]

    def _kill_other_crawlers(self) -> bool | None:
        """清除其他正在跑的爬蟲程序（排除自己啟動的那隻）。

        用一次 ps 依完整命令列特徵搜尋，排除掉自己啟動的爬蟲。若找到
        代表上一次的 supervisor 已死亡、留下孤兒爬蟲，或有人手動又拉了
        一隻 —— 全部清掉再重新啟動。續爬機制（crawl_state）保證重啟後
        進度不中斷，但絕不能讓兩隻爬蟲同時寫入同一個資料庫。

        排除方式用「pid == self.proc.pid」：self.proc.pid 正是子程序
        的 pid（不是 supervisor 自己的 pid），直接精確對應 ps 列表，
        zombie 狀態下 pid 仍保留在 ps 中，不會誤殺自己的爬蟲。

        回傳 True 代表已確認無 stray；False 代表已找到 stray 但無法
        確認終止；None 代表 ps 等觀測本身失敗。
        """
        try:
            # 用一次 ps 抓「pid、ppid、完整命令列」，直接對 args 比對，
            # 不依賴 comm 欄位（macOS 會把它截斷成不固定的長度，例如
            # /Library/Framewo 或 .../Python.frame，根本無法預期是否
            # 包含 python 字樣 —— 修復前的預先過濾會漏掉真 crawler）。
            ps_out = subprocess.run(
                ["ps", "-eo", "pid=,ppid=,args="],
                capture_output=True,
                text=True,
                timeout=5,
            )
            ps_rc = getattr(ps_out, "returncode", 0)
            if isinstance(ps_rc, int) and ps_rc != 0:
                log.error("cannot enumerate crawler processes: ps rc=%s", ps_out.returncode)
                return None
            crawler_pids: list[tuple[int, int]] = []
            for line in ps_out.stdout.splitlines():
                parts = line.split(None, 2)
                if len(parts) < 3:
                    continue
                pid_text, ppid_text, args = parts[0], parts[1], parts[2]
                try:
                    pid_i, ppid_i = int(pid_text), int(ppid_text)
                except ValueError:
                    continue
                # 精確比對：命令列是「python[3] -m src.run_crawl ...」
                # 或「python[3] /path/to/src/run_crawl.py ...」。
                # 直譯器大小寫不敏感（真實環境是 .../MacOS/Python），
                # 且不是 shell 或帶其他字元的監控命令。
                if CRAWLER_CMDLINE_RE.search(args):
                    crawler_pids.append((pid_i, ppid_i))
            mine: set[int] = {self.proc.pid} if self.proc else set()
            others = [pid for pid, _ in crawler_pids if pid not in mine]

            def crawler_is_running(pid: int) -> bool:
                confirm = subprocess.run(
                    ["ps", "-o", "args=", "-p", str(pid)],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                return confirm.returncode == 0 and bool(
                    CRAWLER_CMDLINE_RE.search(confirm.stdout.strip())
                )

            for pid in others:
                # 診斷：被殺的進程是什麼（完整命令 + PPID）
                try:
                    diag = subprocess.run(
                        ["ps", "-o", "ppid=,command=", "-p", str(pid)],
                        capture_output=True,
                        text=True,
                        timeout=3,
                    ).stdout.strip()[:200]
                except Exception:
                    diag = "?"
                log.warning("stopping stray crawler pid=%d (%s)", pid, diag)
                killed = subprocess.run(["kill", "-TERM", str(pid)], capture_output=True)
                kill_rc = getattr(killed, "returncode", 0)
                if isinstance(kill_rc, int) and kill_rc != 0:
                    # ps 與 kill 間的自然退出是正常 race；若仍是 crawler，
                    # 失敗可能是權限問題，不可放行另一個 writer。
                    if not crawler_is_running(pid):
                        continue
                    log.error(
                        "failed to terminate live stray crawler pid=%d (rc=%s)",
                        pid,
                        killed.returncode,
                    )
                    return False
                deadline = time.monotonic() + CHILD_TERMINATE_GRACE_SECONDS
                while crawler_is_running(pid) and time.monotonic() < deadline:
                    time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
                if not crawler_is_running(pid):
                    continue
                log.warning("stray crawler pid=%d ignored SIGTERM; sending SIGKILL", pid)
                killed = subprocess.run(["kill", "-9", str(pid)], capture_output=True)
                if getattr(killed, "returncode", 0) != 0 and crawler_is_running(pid):
                    log.error("failed to kill live stray crawler pid=%d", pid)
                    return False
                deadline = time.monotonic() + 5
                while crawler_is_running(pid) and time.monotonic() < deadline:
                    time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
                if crawler_is_running(pid):
                    log.error("stray crawler pid=%d is still alive", pid)
                    return False
            return True
        except Exception as e:
            log.warning("stray-crawler check failed: %s", e)
            return None

    def start(self) -> bool:
        """啟動爬蟲子程序。若立刻退出則回傳 False。

        啟動前先清掉任何孤兒/重複爬蟲，確保同一時間只有一隻爬蟲
        在寫資料庫。
        """
        if self._kill_other_crawlers() is not True:
            log.error("refusing to start while another crawler may still be alive")
            return False
        log.info("starting crawler child (workers=%d)", self.workers)
        self.proc = subprocess.Popen(
            self._crawler_cmd(),
            cwd=str(Path(__file__).resolve().parents[2]),
            # 子程序的 stdout 不走 crawl.log：run_crawl 自己用
            # RotatingFileHandler 寫 crawl.log，兩者共用同一檔案會
            # 在輪替後造成 fd 失效（內容寫進舊檔）。
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        self.crawler_started_at = time.monotonic()
        return True

    def restart(self, reason: str) -> None:
        """殺掉目前的爬蟲（若有的話）並重新啟動；記錄這次重啟。

        超過窗口內的重啟次數上限時進入冷卻，拒絕繼續重啟。
        冷卻期間（cooldown_until 內）每次 restart 都直接拒絕，
        真正的 30 分鐘風暴保護（P1 修復：cooldown_until 原先只被
        設定、從未被讀取，約 15 分鐘窗口過後就會再次重啟）。
        """
        now = time.monotonic()
        self._prune_restart_state(now)
        if now < self.cooldown_until:
            log.error(
                "cooldown active (until +%.0fs); not restarting: %s",
                self.cooldown_until - now,
                reason,
            )
            # 若進入冷卻時舊 child 無法終止，保留的 proc reference 必須
            # 每 tick 繼續回收；不能因 cooldown 反而 30 分鐘都不再 kill。
            if self.proc is not None:
                self._kill_current(reason)
            if self.proc is None:
                self._kill_other_crawlers()
            return
        # SOL review P1：先納入本次事件再判斷 —— 舊碼在加入前檢查
        # 門檻，固定週期卡死時第 4 次重啟剛好把窗口邊界上的第 1 次
        # 排除（now - t == W），永遠只有 3 筆、永不進冷卻。
        self.restarts.append(now)
        if len(self.restarts) > RESTART_MAX:
            self.cooldown_until = now + COOLDOWN
            self.summary["cooldowns"] += 1
            self._persist_restart_state()
            log.error(
                "restart storm (%d in window): cooldown until +%.0fs", len(self.restarts), COOLDOWN
            )
            # SOL review P1：進冷卻前先終止故障的 child —— 若當下是
            # 「仍存活但卡死」的 crawler（心跳檢查觸發風暴），舊碼直接
            # return 讓它繼續存在整段 30 分鐘冷卻期，持續打網站。
            self._kill_current(reason)
            return
        self._persist_restart_state()
        self.summary["restarts"].append(
            {
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "reason": reason,
            }
        )
        log.warning("restarting crawler: %s", reason)
        if not self._kill_current(reason):
            log.error("cannot restart: old crawler not confirmed dead (%s)", reason)
            return
        self.start()

    def _kill_current(self, reason: str) -> bool:
        """強制結束目前的爬蟲子程序。

        SIGTERM → 等待完整 browser 清理預算 → SIGKILL。回傳 True 代表程序已確認終止
        或本來就不存在；False 代表程序可能仍在執行（D-state 或例外）。

        P1 修復：回傳值讓呼叫端決定是否應啟動新 child —— 舊 PID 無法
        終止時再開新 crawler 會形成雙寫 DB。
        """
        if self.proc is None:
            return True
        pid = getattr(self.proc, "pid", None)
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=CHILD_TERMINATE_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    log.warning("child %s didn't exit after SIGTERM; SIGKILL", pid)
                    self.proc.kill()
                    try:
                        self.proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        log.error("child %s unkillable (D-state?); not restarting", pid)
                        return False
            self.proc = None
            return True
        except Exception as e:
            log.error("kill_current failed (%s): %s", reason, e)
            # P1 修復：不要清除 self.proc —— terminate()/kill() 丟例外
            # 時（PermissionError、ProcessLookupError 等），舊 child
            # 可能仍存活。若清成 None，下一個 tick 就會啟動新 crawler，
            # 造成雙寫 DB。保留 reference，讓後續 tick 的 poll/terminate
            # 有機會再試（或自然死）。
            return False

    # ------------------------------------------------------------ checks

    def _progress_stalled(self) -> bool:
        """判斷爬蟲是否卡住：HANG_TIMEOUT 內都沒有寫入任何零件。

        以 parts.updated_at 為活動訊號（每次 upsert 的 UPDATE 都會觸發
        ON UPDATE CURRENT_TIMESTAMP，即使資料值不變也會前進，因此
        「健康地重爬既有資料」不會被誤判）。

        基準是單一的 crawler_started_at（start() 設定）：
          - 有新鮮寫入（last_write 在 HANG_TIMEOUT 內）→ 健康
          - 超過 HANG_TIMEOUT 無任何零件寫入（含空表、寫入停滯）→
            若目前 crawler 子程序已存活超過 HANG_TIMEOUT 才判定卡死；
            剛重啟的 crawler 給整個 HANG 寬限期（還沒機會寫第一筆）。

        修復前的問題：last_progress 在「寫入新鮮」期間每 tick 重置，
        寫入停滯後要先等老化 HANG_TIMEOUT，再從最後一次重置等
        HANG_TIMEOUT —— 實際約 40 分鐘才判定卡死。現在統一以單一基準
        HANG_TIMEOUT，寫入停滯 20 分鐘即偵測。
        """
        try:
            db = self.db
            if db is None:
                raise RuntimeError("supervisor database is not connected")
            row = db.query_one(PROGRESS_QUERY)
            last_write = (row or {}).get("last_write")
            if last_write is not None and not isinstance(last_write, (datetime, str, int, float)):
                raise TypeError("last_write has an unsupported type")
            if last_write is not None and self._row_age_seconds(last_write) < HANG_TIMEOUT:
                # 資料仍在持續寫入：健康
                return False
            # 到此代表「已超過 HANG_TIMEOUT 沒有任何零件寫入」
            # （含空表、寫入停滯）。
            if self.proc and self.proc.poll() is None:
                return (time.monotonic() - self.crawler_started_at) >= HANG_TIMEOUT
            return True
        except Exception as e:
            log.warning("progress query failed: %s", e)
            return False

    @staticmethod
    def _row_age_seconds(dt: datetime | str | int | float | None) -> float:
        """把 MySQL 的 DATETIME / epoch 整數心跳值換算成距今秒數。"""
        if dt is None:
            return float("inf")
        if isinstance(dt, (int, float)):
            return time.time() - float(dt)
        if isinstance(dt, str):
            dt = datetime.fromisoformat(dt)
        return time.time() - dt.timestamp()

    def _memory_over_limit(self) -> bool:
        """判斷爬蟲子程序的 RSS 是否超過 MEMORY_LIMIT_MB。"""
        if self.proc is None or self.proc.poll() is not None:
            return False
        try:
            pid = self.proc.pid
            out = subprocess.run(
                ["ps", "-o", "rss=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            rss_kb = int(out.stdout.strip() or 0)
            return rss_kb > MEMORY_LIMIT_MB * 1024
        except Exception:
            return False

    def _disk_low(self) -> bool:
        """判斷磁碟剩餘空間是否低於安全門檻。"""
        try:
            free = shutil.disk_usage(str(LOG_DIR)).free / (1024 * 1024)
            if free < DISK_MIN_FREE_MB:
                log.error("disk low: %.0f MB free (limit %d MB)", free, DISK_MIN_FREE_MB)
                return True
        except Exception as e:
            log.warning("disk check failed: %s", e)
        return False

    def _db_alive(self) -> bool:
        """判斷資料庫是否還回應（SELECT 1）。"""
        try:
            db = self.db
            if db is None:
                raise RuntimeError("supervisor database is not connected")
            row = db.query_one("SELECT 1 AS x")
            return bool(row)
        except Exception as e:
            log.error("mysql health check failed: %s", e)
            return False

    def _cookie_fresh(self) -> bool:
        """只讀檢查 cookie 檔案的新鮮度，不觸發瀏覽器刷新。

        瀏覽器刷新是 crawler 子程序自己的職責（http_client 的
        ensure_fresh 在每個請求前檢查、403 時觸發 refresh_session，
        single-flight 保證併發 worker 不會重複刷新）。supervisor
        若在這裡呼叫 get_session() 刷新，會與 crawler 進程各自持有一份
        空的 session 狀態，兩邊同時把同一隻 CloakBrowser 當成
        「stale browser」互相殺掉重啟 —— 永遠無法進入正常爬取。
        """
        try:
            age = time.time() - COOKIE_FILE.stat().st_mtime
            return age < COOKIE_TTL
        except OSError:
            return False

    def _crawl_done(self) -> bool:
        """判斷「當月的爬取」是否完成：當月 run_key 有 success 紀錄。

        每月只跑一次（P0 修復）：若當月的 run 已完成，直接退出；
        若機器在月中重啟，會檢查當月 run 是否 success —— 是就退出，
        否則讓 crawler 續爬。不會被上個月的 success 誤導。
        """
        try:
            run_key = time.strftime("%Y-%m")
            db = self.db
            if db is None:
                raise RuntimeError("supervisor database is not connected")
            row = db.query_one(
                "SELECT status FROM crawl_runs WHERE run_key = %s ORDER BY id DESC LIMIT 1",
                (run_key,),
            )
            return bool(row and row.get("status") == "success")
        except Exception as e:
            log.warning("run-status query failed: %s", e)
            return False

    def _cleanup_stale_runs(self) -> None:
        """把卡在 running 狀態的舊爬取紀錄標記為 error。

        爬蟲被強殺（kill -9）時 finish_run 來不及執行，會留下
        永遠 running 的紀錄。啟動時清一次，避免誤判（例如把
        上一趟的 running 當成「正在進行」）。

        F1a 連帶修正：判斷基準是「started_at 早於本月一號」（起始月份
        比當前月更早的 running 才是跨月殘留）而非「距今超過 24 小時」
        —— started_at 現在是 logical run 起點（同月重啟不移動），當月
        run 的 started_at 恆在月初，用 24h 判斷會把「正常進行中、只是
        被重啟打斷」的當月 run 誤標 error。
        """
        db = self.db
        if db is None:
            raise RuntimeError("supervisor database is not connected")
        try:
            admission_connection = db._thread_conn()
            admission_lock = acquire_catalog_writer_admission(admission_connection)
            try:
                db._execute(
                    "UPDATE crawl_runs SET status = 'error', "
                    "error_msg = CONCAT(error_msg, ' | stale running cleaned by supervisor') "
                    "WHERE status = 'running' AND started_at < DATE_FORMAT(NOW(), '%Y-%m-01')"
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                release_catalog_writer_admission(admission_connection, admission_lock)
        except Exception:
            # release helper 在 ownership 不明時會關閉 connection；無論哪個
            # cleanup 階段失敗，都丟棄 thread-local，避免 Supervisor 下次
            # 健康檢查沿用失敗交易或已關閉的 owner session。
            db._discard_thread_conn()
            raise

    def _write_summary(self, status: str) -> None:
        """把這趟的統計寫入 logs/summary.json（事後 10 秒內可判讀）。"""
        self.summary["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.summary["status"] = status
        try:
            summary_path = LOG_DIR / "summary.json"
            descriptor = open_private_state_file(
                summary_path,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            )
            try:
                summary_file = os.fdopen(descriptor, "w", encoding="utf-8")
            except BaseException:
                os.close(descriptor)
                raise
            with summary_file:
                json.dump(self.summary, summary_file, indent=2, ensure_ascii=False)
            log.info("summary written to %s (status=%s)", LOG_DIR / "summary.json", status)
        except Exception as e:
            log.warning("summary write failed: %s", e)

    # -------------------------------------------------------------- loop

    def run(self) -> int:
        """監督迴圈主體：連接 DB、啟動爬蟲、每 CHECK_INTERVAL 檢查一次。"""
        self._load_restart_state()
        self.db = Database().connect()
        try:
            try:
                self._cleanup_stale_runs()
            except AdmissionLockBusy:
                log.warning("schema migration in progress; supervisor deferred before child start")
                self._write_summary("deferred-schema-migration")
                return 75
            except Exception:
                log.exception("stale-run cleanup failed; refusing to start crawler")
                self._write_summary("startup-error")
                return 1
            if self._crawl_done():
                log.info("crawl already completed; nothing to do")
                if self._kill_other_crawlers() is not True:
                    log.error("completed run has an unconfirmed crawler process")
                    return 1
                self._write_summary("already-complete")
                return 0
            if time.monotonic() < self.cooldown_until:
                log.warning("restored restart cooldown; crawler remains stopped")
                if self._kill_other_crawlers() is not True:
                    log.error("restored cooldown; stray-crawler cleanup is inconclusive")
            elif not self.start():
                return 1
            while True:
                time.sleep(CHECK_INTERVAL)
                self._tick()
        finally:
            for attempt in range(3):
                if self.proc is None or self._kill_current("supervisor exiting"):
                    break
                if attempt < 2:
                    time.sleep(1)
            if self.proc is not None:
                log.critical("supervisor exiting with child ownership unresolved")
            if self.db:
                self.db.close()

    def _tick(self) -> None:
        """單次健康檢查（迴圈的核心）。

        依序檢查：程序存活 → 重複爬蟲 → 心跳 → 記憶體 → 磁碟 → DB
        → cookie → 完成 → 時限。整段用 try/except 包住：任何一個
        檢查炸掉都不許讓監督迴圈本身死亡（它是唯一會把爬蟲拉回
        來的東西）。
        """
        try:
            self._tick_inner()
        except Exception:
            log.exception("tick failed; supervisor continues")

    def _tick_inner(self) -> None:
        """實際的檢查順序（見 _tick 的 docstring）。"""
        self._prune_restart_state()
        # 1. 程序存活：崩潰的子程序必須被拉回來
        if self.proc is not None:
            rc = self.proc.poll()
            if rc is not None:
                if self._crawl_done():
                    log.info("crawler exited (rc=%s) and crawl marked success: done", rc)
                    self._write_summary("success")
                    sys.exit(0)
                if rc == 75:
                    # migration admission busy 是預期延後，不是 crawler crash；
                    # 不累加 restart storm，也不立即重啟。下一個健康 tick
                    # 才重新嘗試，且 run_crawl 在 admission 前不會開瀏覽器。
                    log.info("crawler deferred by schema migration; retrying next check")
                    self.proc = None
                    return
                self.restart(f"crawler exited with rc={rc}")
                return
            if time.monotonic() < self.cooldown_until:
                if not self._kill_current("cooldown active"):
                    log.error("cooldown child still alive; will retry next tick")
                return
        else:
            if time.monotonic() < self.cooldown_until:
                if self._kill_other_crawlers() is not True:
                    log.error("cooldown active; stray-crawler cleanup is inconclusive")
                log.error("cooldown active; not starting crawler")
                return
            self.start()
            return

        # 1b. 重複爬蟲：若有人又拉了第二隻（例如手動），清掉
        stray_status = self._kill_other_crawlers()
        if stray_status is False:
            # 已確認另一隻 crawler 存在且殺不掉；不能讓 owned
            # child 繼續雙寫。留下 proc=None，下一 tick 再 fail-closed 清理。
            self._kill_current("unresolved stray crawler")
            return
        if stray_status is None:
            # 只是 ps 觀測失敗時，owned child 的 crawler.lock 仍是單實例
            # 保護；啟動新 child 的 start() 仍會 fail closed。
            log.warning("stray-crawler check inconclusive; crawler lock remains authoritative")

        # 2. 心跳：太久沒有寫入資料庫 => 卡死，重啟
        if self._progress_stalled():
            self.restart(f"no parts written for > {HANG_TIMEOUT // 60} minutes")
            return

        # 2b. 記憶體：RSS 無上限成長 => 洩漏，重啟（續爬機制保證安全）
        if self._memory_over_limit():
            self.restart(f"crawler RSS exceeded {MEMORY_LIMIT_MB}MB")
            return

        # 2c. 磁碟：空間不足 => 記錄並提前退場（寫進去的資料最值錢）
        if self._disk_low():
            log.error("disk space critical: aborting this run")
            if not self._kill_current("disk full"):
                log.error("disk-low child still alive; supervisor will retry next tick")
                return
            self._write_summary("disk-full-abort")
            sys.exit(1)

        # 2d. 資料庫健康：連不上就沒必要繼續檢查下去
        if not self._db_alive():
            log.error("mysql unreachable; skipping remaining checks")
            return

        # 3. cookie 新鮮度：過期就記 warning（刷新是 crawler 自己的職責，
        #    supervisor 只觀察、不碰瀏覽器，避免與 crawler 搶同一隻）
        if not self._cookie_fresh():
            log.warning("cookie file older than TTL; crawler will refresh on demand")
        # 4. 完成：最後一次爬取已成功 => 收工
        #    （爬蟲本身也會自行退出，這裡是兜底處理）
        if self._crawl_done():
            log.info("crawl completed successfully")
            if not self._kill_current("crawl completed"):
                log.error("completed child still alive; supervisor will retry next tick")
                return
            self._write_summary("success")
            sys.exit(0)

        # 5. 總時限：超過 25 天 => 強制結束，讓下個月乾淨開場
        if time.monotonic() - self.started_at > MAX_RUN_SECONDS:
            log.error("max run time reached; forcing clean exit")
            if not self._kill_current("max run time reached"):
                log.error("timed-out child still alive; supervisor will retry next tick")
                return
            self._write_summary("timeout-abort")
            sys.exit(1)


def main() -> int:
    ensure_private_state_directory(LOG_DIR)
    handlers: list[logging.Handler] = [
        PrivateRotatingFileHandler(
            LOG_DIR / "supervisor.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
        ),
        # launchd 會把 stdout 寫到無上限的 launchd.out.log；
        # 在 launchd 環境下不要重複寫 stdout
        *([] if "LAUNCHD_JOB" in os.environ else [logging.StreamHandler(sys.stdout)]),
    ]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=handlers,
    )
    # P1 修復（單實例鎖）：watchdog（每小時）與 launchd 每月 job 都可能
    # 拉起 supervisor；兩隻並存時各自的 _kill_other_crawlers 會互殺對方
    # 的爬蟲，形成永不停歇的重啟爭奪。flock 拿到獨佔鎖的才是唯一實例，
    # 後到者直接乾淨退場（exit 0，watchdog 視為健康）。
    import fcntl

    lock_fd = None
    signal_installed = False
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    try:
        import argparse

        parser = argparse.ArgumentParser(description="PartSouq 爬蟲監督迴圈")
        parser.add_argument("--workers", type=int, default=int(CRAWL.get("workers", 4)))
        args = parser.parse_args()

        lock_path = LOG_DIR / "supervisor.lock"
        ensure_private_state_directory(lock_path.parent)
        lock_descriptor = open_private_state_file(
            lock_path,
            os.O_RDWR | os.O_CREAT | os.O_APPEND,
        )
        try:
            lock_fd = os.fdopen(lock_descriptor, "a")
        except BaseException:
            os.close(lock_descriptor)
            raise
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("another supervisor holds the lock; exiting")
            return 0

        # launchd 以 SIGTERM 停服務時，預設處理會直接終止 interpreter，
        # Supervisor.run 的 finally 不一定有機會回收 child。轉成 SystemExit
        # 後仍保留標準退出語意，並確保 finally 執行。
        def _terminate(signum: int, _frame: FrameType | None) -> None:
            # 第一次 TERM 轉成 SystemExit 讓 run.finally 回收 child；隨即忽略
            # 後續 TERM，避免 cleanup sleep/kill 被第二個 signal 重入中斷。
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            raise SystemExit(128 + signum)

        try:
            signal.signal(signal.SIGTERM, _terminate)
            signal_installed = True
        except ValueError:
            # 測試可在非 main thread 呼叫 main；production launchd 一定在
            # main thread，會安裝 handler。
            pass
        return Supervisor(workers=args.workers).run()
    finally:
        if signal_installed:
            signal.signal(signal.SIGTERM, previous_sigterm)
        if lock_fd is not None:
            lock_fd.close()
        root_logger = logging.getLogger()
        for handler in handlers:
            root_logger.removeHandler(handler)
            handler.close()


if __name__ == "__main__":
    sys.exit(main())
