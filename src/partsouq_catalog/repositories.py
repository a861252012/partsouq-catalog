"""Repository 層：每個聚合（aggregate）各自的資料存取物件（Laravel 風格）。

每個 repository 包住共用的 :class:`~src.db.Database` 連線管理員，
並擁有自己聚合的所有 SQL。服務層（如 crawler）依賴 repository，
絕不直接接觸原始 SQL —— 這就是資料存取與業務邏輯的分界。

聚合對應（一對一表格群）：
    brands   → brands（品牌）、models（型號）
    vehicles → vehicles（車型）、categories（分類）、groups_t（零件組）
    parts    → parts（零件）
    crawl    → crawl_state（爬取進度）、crawl_runs（爬取紀錄）
"""

import hashlib
import logging

from .db import Database

log = logging.getLogger("repos")

# 零件的搜尋頁網址模板（PartSouq 的零件查詢入口）
PART_URL_TEMPLATE = "https://partsouq.com/en/search/all?q={part_number}"


def vehicle_identity_hash(model_id: int, vehicle: dict) -> str:
    """回傳與 vehicles 唯一鍵一致的穩定 SHA256 identity。

    ssd / vid / url 都是請求用 token 或參數，不屬於車型身分；它們輪替
    時必須更新同一列，而不是建立另一台車或另一個 resume key。
    """
    values = (
        str(model_id),
        str(vehicle.get("model_code") or ""),
        str(vehicle.get("name") or ""),
        str(vehicle.get("description") or ""),
        str(vehicle.get("options") or ""),
        str(vehicle.get("prod_period") or ""),
        str(vehicle.get("grade") or ""),
        str(vehicle.get("market") or ""),
        str(vehicle.get("engine") or ""),
        str(vehicle.get("transmission") or ""),
        str(vehicle.get("body_style") or ""),
    )
    raw = "".join(f"{len(value.encode('utf-8'))}:{value}" for value in values)
    return hashlib.sha256(raw.encode()).hexdigest()


class BrandRepository:
    """品牌與型號的資料存取（目錄最上層的兩階）。"""

    def __init__(self, db: Database):
        self.db = db

    def upsert_brand(self, name: str, url: str | None) -> int:
        """新增或更新品牌（以 name 為唯一鍵）。回傳品牌 id。"""
        cur = self.db._execute(
            "INSERT INTO brands (name, url) VALUES (%s, %s) AS new "
            "ON DUPLICATE KEY UPDATE url = new.url, id = LAST_INSERT_ID(id)",
            (name, url),
        )
        return cur.lastrowid or self._brand_id(name)

    def list_brands(self) -> list[str]:
        """列出資料庫中已知的所有品牌名稱（判定全站完成用）。"""
        cur = self.db._execute("SELECT name FROM brands ORDER BY name")
        return [r["name"] for r in cur.fetchall()]

    def _brand_id(self, name: str) -> int:
        """依品牌名稱查詢 id（upsert 回傳值為 0 時的備援查詢）。"""
        cur = self.db._execute("SELECT id FROM brands WHERE name = %s", (name,))
        row = cur.fetchone()
        return row["id"] if row else 0

    def upsert_model(self, brand_id: int, name: str, ssd: str | None, url: str | None) -> int:
        """新增或更新型號（以品牌 + 名稱唯一）。回傳型號 id。

        ssd 採用 COALESCE：既有資料有 ssd 時不覆寫為 NULL。
        """
        cur = self.db._execute(
            "INSERT INTO models (brand_id, name, ssd, url) VALUES (%s, %s, %s, %s) AS new "
            "ON DUPLICATE KEY UPDATE ssd = COALESCE(new.ssd, models.ssd), "
            "url = new.url, fetched_at = NOW(), id = LAST_INSERT_ID(id)",
            (brand_id, name, ssd, url),
        )
        return cur.lastrowid

    def list_models(self, brand_id: int) -> list[dict]:
        """列出某品牌下的所有型號（依 id 排序）。"""
        cur = self.db._execute(
            "SELECT id, name, ssd, url FROM models WHERE brand_id = %s ORDER BY id",
            (brand_id,),
        )
        return cur.fetchall()

    def list_model_names(self, brand: str) -> list[str]:
        """列出某品牌下 DB 已知的所有型號名稱（閉合對帳用，F1b）。"""
        cur = self.db._execute(
            "SELECT m.name FROM models m "
            "JOIN brands b ON b.id = m.brand_id WHERE b.name = %s ORDER BY m.name",
            (brand,),
        )
        return [r["name"] for r in cur.fetchall()]


class VehicleRepository:
    """車型、分類、零件組的資料存取（車型 → 分類 → 零件組的樹狀結構）。"""

    def __init__(self, db: Database):
        self.db = db

    def upsert_vehicle(self, model_id: int, vehicle: dict) -> int:
        """新增或更新車型（以 model_id + identity_hash 唯一）。回傳 id。"""
        identity_hash = vehicle_identity_hash(model_id, vehicle)
        cur = self.db._execute(
            "INSERT INTO vehicles (model_id, identity_hash, name, description, model_code, options, "
            "prod_period, production_from, production_to, grade, market, engine, transmission, "
            "body_style, ssd, vid, url) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) AS new "
            "ON DUPLICATE KEY UPDATE name = new.name, model_code = new.model_code, "
            "description = new.description, options = new.options, "
            "prod_period = new.prod_period, production_from = new.production_from, "
            "production_to = new.production_to, grade = new.grade, market = new.market, "
            "engine = new.engine, transmission = new.transmission, "
            "body_style = new.body_style, "
            "ssd = COALESCE(new.ssd, vehicles.ssd), "
            "vid = new.vid, url = new.url, "
            "fetched_at = NOW(), id = LAST_INSERT_ID(id)",
            (
                model_id,
                identity_hash,
                vehicle.get("name"),
                vehicle.get("description"),
                vehicle.get("model_code"),
                vehicle.get("options"),
                vehicle.get("prod_period"),
                vehicle.get("production_from"),
                vehicle.get("production_to"),
                vehicle.get("grade"),
                vehicle.get("market"),
                vehicle.get("engine"),
                vehicle.get("transmission"),
                vehicle.get("body_style"),
                vehicle.get("ssd"),
                vehicle.get("vid"),
                vehicle.get("url"),
            ),
        )
        return cur.lastrowid

    def list_vehicles(self, model_id: int) -> list[dict]:
        """列出某型號下的所有車型（依 id 排序）。"""
        cur = self.db._execute(
            "SELECT id, name, model_code, ssd, vid, url FROM vehicles "
            "WHERE model_id = %s ORDER BY id",
            (model_id,),
        )
        return cur.fetchall()

    def list_vehicle_keys(self, brand: str) -> list[str]:
        """列出某品牌下 DB 已知的所有車型 resume key（閉合對帳用，F1b）。

        identity_hash 由 upsert 與 migration 依同一公式產生；scope key 加
        v5 前綴，讓舊公式的 state 能被明確清退。
        """
        cur = self.db._execute(
            "SELECT v.identity_hash "
            "FROM vehicles v "
            "JOIN models m ON m.id = v.model_id "
            "JOIN brands b ON b.id = m.brand_id "
            "WHERE b.name = %s",
            (brand,),
        )
        return [f"v5:{r['identity_hash']}" for r in cur.fetchall()]

    def upsert_category(self, vehicle_id: int, name: str, cid: str | None) -> int:
        """新增或更新分類。cid 存在時以 vehicle_id + cid 為穩定身分。"""
        cid = cid or None
        if cid:
            # 正式爬取的 category 一定有 cid，可直接利用 uq_cat_cid，
            # 省掉每個 group 都先 SELECT、再 UPDATE 的兩次往返。
            cur = self.db._execute(
                "INSERT INTO categories (vehicle_id, name, cid) VALUES (%s, %s, %s) AS new "
                "ON DUPLICATE KEY UPDATE name = new.name, fetched_at = NOW(), "
                "id = LAST_INSERT_ID(id)",
                (vehicle_id, name, cid),
            )
            return cur.lastrowid

        # cid 為 NULL 時 uq_cat_cid 不會判定重複，只能維持以名稱查找的
        # 相容路徑；不能假裝成安全的單句 upsert。
        identity_sql = "cid = %s" if cid else "name = %s"
        identity_value = cid if cid else name
        cur = self.db._execute(
            f"SELECT id FROM categories WHERE vehicle_id = %s AND {identity_sql} "
            "ORDER BY id LIMIT 1",
            (vehicle_id, identity_value),
        )
        row = cur.fetchone()
        if row:
            self.db._execute(
                "UPDATE categories SET name = %s, cid = %s, fetched_at = NOW() WHERE id = %s",
                (name, cid, row["id"]),
            )
            return row["id"]
        cur = self.db._execute(
            "INSERT INTO categories (vehicle_id, name, cid) VALUES (%s, %s, %s) AS new "
            "ON DUPLICATE KEY UPDATE name = new.name, cid = new.cid, fetched_at = NOW(), "
            "id = LAST_INSERT_ID(id)",
            (vehicle_id, name, cid),
        )
        return cur.lastrowid

    def list_categories(self, vehicle_id: int) -> list[dict]:
        """列出某車型下的所有分類（依 id 排序）。"""
        cur = self.db._execute(
            "SELECT id, name, cid FROM categories WHERE vehicle_id = %s ORDER BY id",
            (vehicle_id,),
        )
        return cur.fetchall()

    def upsert_group(
        self, category_id: int, code: str | None, name: str | None, uid: str | None, url: str | None
    ) -> int:
        """新增或更新零件組（以 category_id + code + uid 唯一）。回傳 id。

        code／uid 為 None 時以空字串寫入：MySQL 唯一索引視 NULL 為
        「互不相等」，若放任 NULL 會長出無限多筆重複零件組。

        圖片↔文字跨月轉換：同一組先以圖片-only 呈現（code 空字串）、
        後以文字呈現（code 有值）時，若 DB 只有那一列空 code 列，
        就地更新 code/name/url，避免長出重複 group 列（否則舊列
        殘留）。**只**對「既有列 code 為空字串」做此對帳：
        - 同 uid 不同非空 code 的列是變體專屬資料（parser 刻意保留），
          不得互相覆寫或合併。
        - 文字列不得被圖片月覆寫成空名稱（名稱是已取得的資料）。
        - 目標 (category_id, code, uid) 已被另一列佔用時退回標準
          upsert（不試 UPDATE，避免唯一鍵衝突 —— db._execute 對
          IntegrityError 會回滾整個 transaction）。
        uid 為空的 legacy 列（migration 010 前的 NULL 回推）不參與
        此對帳：它們沒有站方身分可依，只能靠 (code, uid) 唯一鍵。
        """
        code = code or ""
        uid = uid or ""
        if code:
            cur = self.db._execute(
                "SELECT id FROM groups_t WHERE category_id = %s AND uid = %s AND uid <> '' "
                "AND code = '' LIMIT 1",
                (category_id, uid),
            )
            image_row = cur.fetchone()
            if image_row:
                # 目標 (category_id, code, uid) 已被另一列佔用（同 uid 的
                # 變體文字列）時不升級、也不試 UPDATE 觸發唯一鍵衝突 ——
                # db._execute 對 IntegrityError 會回滾整個 transaction，
                # 不能走「try UPDATE 再吞例外」的路線。
                exists = self.db._execute(
                    "SELECT id FROM groups_t WHERE category_id = %s AND code = %s "
                    "AND uid = %s LIMIT 1",
                    (category_id, code, uid),
                ).fetchone()
                if not exists:
                    self.db._execute(
                        "UPDATE groups_t SET code = %s, name = %s, url = %s, "
                        "fetched_at = NOW() WHERE id = %s",
                        (code, name, url, image_row["id"]),
                    )
                    return image_row["id"]
        cur = self.db._execute(
            "INSERT INTO groups_t (category_id, code, name, uid, url) "
            "VALUES (%s, %s, %s, %s, %s) AS new "
            "ON DUPLICATE KEY UPDATE name = new.name, url = new.url, "
            "fetched_at = NOW(), id = LAST_INSERT_ID(id)",
            (category_id, code, name, uid, url),
        )
        return cur.lastrowid

    def list_group_identities_for_category(self, vehicle_id: int, cid: str) -> set[str]:
        """回傳某車輛某 cid 下 DB 已知的 group uid 集合。

        只回 uid（站方每組的穩定身分）：closure 對帳不比 code，因為
        同一組可能以圖片-only（code 空）或文字（code 有值）呈現，
        呈現格式轉換不能誤判成 group 消失。
        """
        cur = self.db._execute(
            "SELECT DISTINCT g.uid FROM groups_t g "
            "JOIN categories c ON c.id = g.category_id "
            "WHERE c.vehicle_id = %s AND c.cid = %s AND g.uid <> ''",
            (vehicle_id, cid),
        )
        return {r["uid"] for r in cur.fetchall()}


class PartRepository:
    """零件的資料存取（目錄的葉節點層）。"""

    def __init__(self, db: Database):
        self.db = db

    def upsert_parts(
        self,
        group_id: int,
        parts: list[dict],
        run_id: int | None = None,
        *,
        complete_group: bool = True,
    ) -> int:
        """批次新增/更新一個零件組下的所有零件（1 次 SELECT + 1 次批次 INSERT）。

        回傳新插入的筆數。比逐筆 INSERT 快非常多：
        一個約 30 筆零件的零件組，從 30 次往返變成 2 次。

        以 (part_number, range_str) 判定新增：parts 表的唯一鍵就是
        (group_id, part_number, range_str)，同料號不同 range 會真的
        插入兩列，統計必須與唯一鍵一致才不會低估。

        ``complete_group=False`` 用於筆數受限的 run：保留本次截取列，
        但不可把未出現在截取 payload 的既有列誤判為已下架。
        """
        if not parts:
            if run_id is not None and complete_group:
                self.clear_group_membership(group_id)
            return 0
        # 先查出該零件組既有的料號+範圍 → 用來判斷哪些是新插入
        cur = self.db._execute(
            "SELECT part_number, range_str FROM parts WHERE group_id = %s", (group_id,)
        )
        existing = {(row["part_number"], row["range_str"]) for row in cur.fetchall()}

        rows = [
            (
                group_id,
                p.get("part_number") or "",
                p.get("name"),
                p.get("code"),
                p.get("note"),
                p.get("quantity"),
                p.get("range_str") or "",
                p.get("part_from"),
                p.get("part_to"),
                PART_URL_TEMPLATE.format(part_number=p.get("part_number") or ""),
                run_id,
            )
            for p in parts
        ]
        self.db._executemany(
            "INSERT INTO parts (group_id, part_number, name, code, note, quantity, range_str, "
            "part_from, part_to, url, seen_run_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) AS new "
            "ON DUPLICATE KEY UPDATE name = new.name, code = new.code, "
            "note = new.note, quantity = new.quantity, part_from = new.part_from, "
            "part_to = new.part_to, "
            "seen_run_id = new.seen_run_id, "
            "updated_at = CURRENT_TIMESTAMP",
            rows,
        )
        if run_id is not None and complete_group:
            # 先將本次仍存在的列標記為 current，再只清掉未出現在本次
            # payload 的舊列，避免所有現存列每次都先寫 NULL 再寫 run_id。
            self._clear_stale_group_membership(group_id, run_id)
        seen = set()
        new_count = 0
        for p in parts:
            key = (p.get("part_number") or "", p.get("range_str") or "")
            if key not in existing and key not in seen:
                seen.add(key)
                new_count += 1
        return new_count

    def clear_group_membership(self, group_id: int):
        """清除單一 group 的舊 run membership；與後續 upsert 同交易。"""
        self.db._execute("UPDATE parts SET seen_run_id = NULL WHERE group_id = %s", (group_id,))

    def _clear_stale_group_membership(self, group_id: int, run_id: int):
        """只清除本次 payload 已不存在的 membership。"""
        self.db._execute(
            "UPDATE parts SET seen_run_id = NULL WHERE group_id = %s AND seen_run_id <> %s",
            (group_id, run_id),
        )

    def count_parts_in_group(self, group_id: int) -> int:
        """統計某零件組下的零件數量（供驗證與監督使用）。"""
        cur = self.db._execute("SELECT COUNT(*) AS n FROM parts WHERE group_id = %s", (group_id,))
        return cur.fetchone()["n"]

    def bounded_group_context(self, group_id: int) -> dict | None:
        """讀取正式 bounded 配額所需的 group／車款來源欄位。"""
        return self.db._execute(
            "SELECT v.production_from, v.production_to, "
            "(NULLIF(TRIM(b.name), '') IS NOT NULL "
            "AND NULLIF(TRIM(m.name), '') IS NOT NULL "
            "AND NULLIF(TRIM(v.name), '') IS NOT NULL "
            "AND NULLIF(TRIM(v.model_code), '') IS NOT NULL "
            "AND NULLIF(TRIM(v.vid), '') IS NOT NULL "
            "AND NULLIF(TRIM(c.cid), '') IS NOT NULL "
            "AND NULLIF(TRIM(c.name), '') IS NOT NULL "
            "AND NULLIF(TRIM(g.name), '') IS NOT NULL "
            "AND NULLIF(TRIM(g.code), '') IS NOT NULL "
            "AND NULLIF(TRIM(g.uid), '') IS NOT NULL "
            "AND g.url LIKE "
            "'https://partsouq.com/en/catalog/genuine/unit?%%') AS source_valid "
            "FROM groups_t AS g "
            "JOIN categories AS c ON c.id = g.category_id "
            "JOIN vehicles AS v ON v.id = c.vehicle_id "
            "JOIN models AS m ON m.id = v.model_id "
            "JOIN brands AS b ON b.id = m.brand_id "
            "WHERE g.id = %s",
            (group_id,),
        ).fetchone()

    def seen_keys_in_group(self, group_id: int, run_id: int) -> set[tuple[str, str]]:
        """讀取 bounded resume 已納入配額的 natural keys。"""
        rows = self.db._execute(
            "SELECT part_number, range_str FROM parts WHERE group_id = %s AND seen_run_id = %s",
            (group_id, run_id),
        ).fetchall()
        return {(row["part_number"], row["range_str"]) for row in rows}

    def clear_seen_keys(
        self,
        group_id: int,
        run_id: int,
        keys: set[tuple[str, str]],
    ) -> int:
        """解除 bounded run 中已從同一 unit payload 消失的 membership。"""
        if not keys:
            return 0
        self.db._executemany(
            "UPDATE parts SET seen_run_id = NULL WHERE group_id = %s AND seen_run_id = %s "
            "AND part_number = %s AND range_str = %s",
            [(group_id, run_id, part_number, range_str) for part_number, range_str in keys],
        )
        return len(keys)


class CrawlRepository:
    """爬取進度（crawl_state）與爬取紀錄（crawl_runs）的資料存取。"""

    def __init__(self, db: Database, run_key: str = ""):
        self.db = db
        # run_key 標記「這趟 run」的範圍（例如 '2026-08'）。空字串 = 相容模式
        # （舊 run 的 done 狀態跨 run 共享）。設定了 run_key 後，done 狀態
        # 按 run 隔離：每個月的新 run 看不到舊 run 的 done，會重新爬取。
        # 用空字串而非 None：MySQL 唯一鍵對 NULL 不視為相同，會破壞
        # ON DUPLICATE 的覆寫語意。
        self.run_key = run_key or ""

    def remaining_group_count(self, run_key: str = "") -> int:
        """回傳指定 run 尚未取得 terminal receipt 的 group 數量。"""
        if run_key:
            cur = self.db._execute(
                "SELECT COUNT(*) AS n FROM groups_t "
                "WHERE fetched_run_key IS NULL OR fetched_run_key <> %s "
                "OR fetched_status IS NULL "
                "OR fetched_status NOT IN ('done', 'not_found')",
                (run_key,),
            )
        else:
            cur = self.db._execute("SELECT COUNT(*) AS n FROM groups_t")
        return cur.fetchone()["n"]

    def mark_done(self, scope: str, key: str):
        """把某範圍的某個鍵標記為「完成」。

        scope 例：'model'（型號）、'vehicle'（車型）；
        key 例：'Toyota::COROLLA' 或 'Toyota::COROLLA::ZRE210'。
        """
        self.db._execute(
            "INSERT INTO crawl_state (run_key, scope, scope_key, status) "
            "VALUES (%s, %s, %s, 'done') "
            "ON DUPLICATE KEY UPDATE status = 'done', error_msg = NULL, "
            "updated_at = NOW()",
            (self.run_key, scope, key),
        )

    def mark_error(self, scope: str, key: str, msg: str):
        """把某範圍的某個鍵標記為「失敗」，並記錄錯誤訊息（截斷 500 字）。"""
        self.db._execute(
            "INSERT INTO crawl_state (run_key, scope, scope_key, status, error_msg) "
            "VALUES (%s, %s, %s, 'error', %s) "
            "ON DUPLICATE KEY UPDATE status = 'error', error_msg = %s, "
            "updated_at = NOW()",
            (self.run_key, scope, key, msg[:500], msg[:500]),
        )

    def is_done(self, scope: str, key: str) -> bool:
        """判斷某範圍的某鍵是否已完成（續爬時用來跳過）。"""
        cur = self.db._execute(
            "SELECT 1 AS x FROM crawl_state WHERE run_key = %s "
            "AND scope = %s AND scope_key = %s AND status = 'done'",
            (self.run_key, scope, key),
        )
        return cur.fetchone() is not None

    def count_errors(self, run_key: str = "") -> int:
        """統計某個 run 內仍處於「未完成」狀態的項目數（error + pending）。

        run 結束時以這個數字做為「是否真的全站成功」的單一事實來源：
        model／vehicle（及品牌層）有任何失敗或尚未完成的項目，都會被
        計入。pending 也必須算：backoff 跳過的 model/車型、以及任何
        未走到收尾狀態的項目，都代表這趟 run 沒有完整閉合（P1 修復）。
        續爬時完成項目會被 mark_done 覆寫，因此真正完成的全站 run
        這個數字必須是 0。
        """
        cur = self.db._execute(
            "SELECT COUNT(*) AS n FROM crawl_state "
            "WHERE run_key = %s AND status IN ('error', 'pending')",
            (run_key,),
        )
        return cur.fetchone()["n"]

    def count_failures(self, run_key: str = "") -> int:
        """只統計真正失敗的項目；sample 預期留下的 pending 不算失敗。"""
        cur = self.db._execute(
            "SELECT COUNT(*) AS n FROM crawl_state WHERE run_key = %s AND status = 'error'",
            (run_key,),
        )
        return cur.fetchone()["n"]

    def is_group_fetched(
        self,
        vehicle_id: int,
        group_code: str,
        group_uid: str,
        run_key: str = "",
    ) -> bool:
        """判斷某車的某零件組是否已在「本 run」抓取完成（有零件或 404）。

        續爬/重試優化用：重試一台失敗車時，只補抓「尚未完成的組」，
        已成功抓過的組直接跳過，避免重抓全部 ~200 個 group 燒光
        rate budget（Agent 分析建議）。

        F1b 修復：完成與否以明確的 group terminal state
        （groups_t.fetched_run_key）為準 —— 舊版用「任一零件
        updated_at >= run 起點」啟發式，頁面只解析出部分非空資料時
        重試會把缺漏固定下來。
        """
        if not run_key:
            return False
        cur = self.db._execute(
            "SELECT 1 FROM groups_t g "
            "JOIN categories c ON c.id = g.category_id "
            "WHERE c.vehicle_id = %s AND g.code = %s AND g.uid = %s "
            "AND g.fetched_run_key = %s LIMIT 1",
            (vehicle_id, group_code, group_uid, run_key),
        )
        return cur.fetchone() is not None

    def fetched_group_map(self, vehicle_id: int, run_key: str = "") -> dict:
        """一次載入某車「本 run 已抓完」的所有零件組（F5 優化）。

        回傳 {(cid, code, uid): row_count, ...}（存在即代表已抓過）。
        以 (cid, code, uid) 為鍵，避免同分類、同 code 的變體零件組
        互相覆蓋、誤 skip。receipt 的 status 詳情留在 DB
        （fetched_status），這裡只做 skip 判斷。
        續爬一臺失敗車時，用這張 map 在記憶體判斷每組是否已完成，
        不必每組各查一次 DB —— 一臺車約 200 組，原本是 200 次往返。
        """
        if not run_key:
            return {}
        cur = self.db._execute(
            "SELECT c.cid, g.code, g.uid, g.fetched_row_count "
            "FROM groups_t g "
            "JOIN categories c ON c.id = g.category_id "
            "WHERE c.vehicle_id = %s AND g.fetched_run_key = %s",
            (vehicle_id, run_key),
        )
        return {
            (str(r["cid"] or ""), r["code"], r["uid"]): r["fetched_row_count"] or 0
            for r in cur.fetchall()
        }

    def previous_row_count_map(self, vehicle_id: int, run_key: str = "") -> dict:
        """一次載入某車每個組已驗證的歷史最高 row_count。

        回傳 {(cid, code, uid): verified_row_count}。
        縮水偵測的參考點：crawl_group 解析出「格式完整但數量遠少於
        歷史最高成功值」的零件時，據此拒絕寫 terminal receipt。
        high-water 只升不降，因此逐月小幅縮水也無法改寫基準。
        """
        if not run_key:
            return {}
        cur = self.db._execute(
            "SELECT c.cid, g.code, g.uid, g.verified_row_count AS row_count "
            "FROM groups_t g "
            "JOIN categories c ON c.id = g.category_id "
            "WHERE c.vehicle_id = %s AND g.verified_row_count > 0",
            (vehicle_id,),
        )
        return {
            (str(r["cid"] or ""), r["code"], r["uid"]): r["row_count"] or 0 for r in cur.fetchall()
        }

    def previous_row_count(self, group_id: int) -> int:
        """回傳某零件組歷史上已驗證的最高 row_count（無則 0）。

        縮水偵測的後備路徑（未提供 prev_rows map 時逐組查詢，僅測試/
        相容使用；正式爬取一律用 previous_row_count_map 一次載入）。
        """
        cur = self.db._execute(
            "SELECT verified_row_count AS n FROM groups_t WHERE id = %s", (group_id,)
        )
        return cur.fetchone()["n"] or 0

    def mark_group_fetched(
        self, group_id: int, run_key: str = "", status: str = "done", row_count: int = 0
    ):
        """標記某零件組已在本次 run 抓取完成（durable receipt，F1b/F5）。

        status 區分完成種類：'done'（有零件）、'not_found'（404，網站
        端「此組無資料」的合法訊號）。HTTP 200 但解析 0 零件一律視為
        異常（反爬/版型變更）並拋錯，**不寫** receipt（SOL P2：沒有
        可驗證的「合法空組」DOM 訊號前不猜測，避免把封鎖頁當成空組
        標 done）。row_count 記錄本組零件筆數 —— 配合 fetched_run_key
        讓續爬「不再重抓 404 或已完成組」，也為 content hash 增量
        更新打基礎。

        與零件的 upsert 同一交易提交（見 crawl_group）：避免「零件寫了
        但狀態沒寫」的靜默缺漏。
        """
        if status == "done":
            self.db._execute(
                "UPDATE groups_t SET fetched_run_key = %s, fetched_status = %s, "
                "fetched_row_count = %s, "
                "verified_row_count = GREATEST(verified_row_count, %s) WHERE id = %s",
                (run_key, status, row_count, row_count, group_id),
            )
        else:
            self.db._execute(
                "UPDATE groups_t SET fetched_run_key = %s, fetched_status = %s, "
                "fetched_row_count = %s WHERE id = %s",
                (run_key, status, row_count, group_id),
            )

    def seen(self, scope: str, key: str):
        """記錄「本 run 遇見」某項目（不改變既有狀態）。

        F1b 修復：閉合對帳需要知道「本 run 從清單層見到過哪些項目」。
        縮水解析（locate/pick 頁只回傳子集）時，未被見到的項目不會有
        crawl_state 行，count_errors 數不到 —— seen 保證每個被解析器
        見到的項目都有一行，run 結束時與 DB 已知集合比對即可抓到縮水。
        """
        self.db._execute(
            "INSERT INTO crawl_state (run_key, scope, scope_key, status) "
            "VALUES (%s, %s, %s, 'pending') "
            "ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id)",
            (self.run_key, scope, key),
        )

    def scope_keys(self, run_key: str, scope: str, prefix: str | None = None) -> set[str]:
        """回傳某 run 中某 scope、以 prefix 開頭的所有 scope_key（閉合對帳用）。

        prefix 為 None 時回傳全部 scope_key（vehicle scope 因 hash key
        格式不再使用 prefix match）。"""
        if prefix is not None:
            escaped_prefix = prefix.replace("!", "!!").replace("%", "!%").replace("_", "!_")
            cur = self.db._execute(
                "SELECT scope_key FROM crawl_state "
                "WHERE run_key = %s AND scope = %s "
                "AND scope_key LIKE CONCAT(%s, '%%') ESCAPE '!'",
                (run_key, scope, escaped_prefix),
            )
        else:
            cur = self.db._execute(
                "SELECT scope_key FROM crawl_state WHERE run_key = %s AND scope = %s",
                (run_key, scope),
            )
        return {r["scope_key"] for r in cur.fetchall()}

    def reset_scope(self, scope: str, run_key: str = ""):
        """清除某範圍的所有進度紀錄（--fresh 模式用）。

        run_key 為空字串時清除「相容（run_key=''）」與全部；指定時只清該 run。
        """
        if run_key:
            self.db._execute(
                "DELETE FROM crawl_state WHERE scope = %s AND run_key = %s",
                (scope, run_key),
            )
        else:
            self.db._execute("DELETE FROM crawl_state WHERE scope = %s", (scope,))

    def reset_run_state(self, run_key: str):
        """清除指定 run 的所有 scope，包含舊版或未來新增的 scope。"""
        self.db._execute("DELETE FROM crawl_state WHERE run_key = %s", (run_key,))

    def reset_group_receipts(self, run_key: str | None = None):
        """清除所有零件組的抓取收據（--fresh 模式用）。

        同月既有 fetched_run_key 會讓 group 在 HTTP 與 upsert 前直接
        跳過，只重設 crawl_state 不足以讓 --fresh 從頭開始爬所有 group。
        """
        if run_key:
            self.db._execute(
                "UPDATE groups_t SET fetched_run_key = NULL, fetched_status = NULL "
                "WHERE fetched_run_key = %s",
                (run_key,),
            )
        else:
            self.db._execute("UPDATE groups_t SET fetched_run_key = NULL, fetched_status = NULL")

    def reset_part_markers(self, run_id: int):
        """清除同月 fresh run 的舊 membership，不影響已發布 snapshot。"""
        self.db._execute("UPDATE parts SET seen_run_id = NULL WHERE seen_run_id = %s", (run_id,))

    def purge_legacy_vehicle_state(self, run_key: str) -> int:
        """一次性相容（P1 修復）：清除舊版 vehicle resume key 格式。

        v5 key 有明確版本前綴。所有不是 ``v5:<64 hex>`` 的 pending /
        error 都不可能再被新版程式覆寫，會永久卡住 count_errors；清掉
        讓它們依新版 identity 重新爬取。
        """
        cur = self.db._execute(
            "DELETE FROM crawl_state WHERE run_key = %s AND scope = 'vehicle' "
            "AND scope_key NOT REGEXP '^v5:[a-f0-9]{64}$' "
            "AND status IN ('pending', 'error')",
            (run_key,),
        )
        return cur.rowcount

    def start_run(
        self,
        run_key: str = "",
        fresh: bool = False,
        *,
        dataset_kind: str = "full",
        target_parts: int | None = None,
        scheduled_job_run_id: int | None = None,
    ) -> int:
        """新增一筆「執行中」的爬取紀錄，回傳 run id。

        run_key 標記這趟 run 的範圍（例如 '2026-08'）；同月再次呼叫
        時因唯一鍵衝突，改用 ON DUPLICATE 更新回 running，並回傳
        既有 id（保證同月只有一筆 run 紀錄）。

        F1a 修復：started_at 是「logical monthly run 起點」，同月
        重啟**不更新** —— 舊碼每次重啟都覆寫成 NOW()，而 resume 會
        跳過已完成的 vehicle（不會更新其零件時間），最後 success 時
        v_parts 會把「先前 attempt 已完成且仍現存」的零件誤排除
        （實測 3 車 15,300 筆零件全部早於被推後的 cutoff）。
        起點只在同 run_key 首次 INSERT 時設定；跨月新 run_key 自動
        得到新的起點。

        若該月已是 success（全站已完整爬完），不覆寫成 running（P2
        修復）—— 之後的 partial run 不該抹掉「全站已完成」的證據。

        本方法不 commit：交易邊界由服務層決定（見 db.py 分層契約）。
        """
        if fresh:
            cur = self.db._execute(
                "INSERT INTO crawl_runs (run_key, started_at, status, dataset_kind, "
                "target_parts, scheduled_job_run_id) "
                "VALUES (%s, NOW(), 'running', %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE started_at = NOW(), finished_at = NULL, "
                "status = 'running', brands_ok = 0, models_ok = 0, vehicles_ok = 0, "
                "groups_ok = 0, parts_ok = 0, parts_new = 0, error_msg = NULL, "
                "dataset_kind = VALUES(dataset_kind), target_parts = VALUES(target_parts), "
                "scheduled_job_run_id = VALUES(scheduled_job_run_id), "
                "id = LAST_INSERT_ID(id)",
                (run_key, dataset_kind, target_parts, scheduled_job_run_id),
            )
        else:
            cur = self.db._execute(
                "INSERT INTO crawl_runs (run_key, started_at, status, dataset_kind, "
                "target_parts, scheduled_job_run_id) "
                "VALUES (%s, NOW(), 'running', %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE "
                "status = IF(status IN ('success', 'bounded_success'), status, 'running'), "
                "finished_at = IF(status IN ('success', 'bounded_success'), finished_at, NULL), "
                "dataset_kind = IF(status IN ('success', 'bounded_success'), "
                "dataset_kind, VALUES(dataset_kind)), "
                "target_parts = IF(status IN ('success', 'bounded_success'), "
                "target_parts, VALUES(target_parts)), "
                "scheduled_job_run_id = IF(status IN ('success', 'bounded_success'), "
                "scheduled_job_run_id, VALUES(scheduled_job_run_id)), "
                "id = LAST_INSERT_ID(id)",
                (run_key, dataset_kind, target_parts, scheduled_job_run_id),
            )
        return cur.lastrowid

    def run_status(self, run_id: int) -> str | None:
        """讀取指定 run 的目前狀態（commit 結果不明時用來對帳）。"""
        cur = self.db._execute("SELECT status FROM crawl_runs WHERE id = %s", (run_id,))
        row = cur.fetchone()
        return row["status"] if row else None

    def count_run_parts(self, run_id: int) -> int:
        """以 DB membership 作為筆數受限 run 的續爬配額基線。"""
        row = self.db._execute(
            "SELECT COUNT(*) AS row_count FROM parts WHERE seen_run_id = %s",
            (run_id,),
        ).fetchone()
        return int((row or {}).get("row_count", 0))

    def discard_invalid_bounded_membership(self, run_id: int) -> int:
        """解除不符合正式 bounded 欄位／年份門檻的配額 membership。"""
        cur = self.db._execute(
            "UPDATE parts AS p "
            "JOIN groups_t AS g ON g.id = p.group_id "
            "JOIN categories AS c ON c.id = g.category_id "
            "JOIN vehicles AS v ON v.id = c.vehicle_id "
            "JOIN models AS m ON m.id = v.model_id "
            "JOIN brands AS b ON b.id = m.brand_id "
            "SET p.seen_run_id = NULL WHERE p.seen_run_id = %s AND ("
            "NULLIF(TRIM(p.part_number), '') IS NULL "
            "OR NULLIF(TRIM(p.name), '') IS NULL "
            "OR NULLIF(TRIM(p.code), '') IS NULL "
            "OR NULLIF(TRIM(b.name), '') IS NULL "
            "OR NULLIF(TRIM(m.name), '') IS NULL "
            "OR NULLIF(TRIM(v.name), '') IS NULL "
            "OR NULLIF(TRIM(v.model_code), '') IS NULL "
            "OR NULLIF(TRIM(v.vid), '') IS NULL "
            "OR NULLIF(TRIM(c.cid), '') IS NULL "
            "OR NULLIF(TRIM(c.name), '') IS NULL "
            "OR NULLIF(TRIM(g.name), '') IS NULL "
            "OR NULLIF(TRIM(g.code), '') IS NULL "
            "OR NULLIF(TRIM(g.uid), '') IS NULL "
            "OR (v.production_from IS NULL AND v.production_to IS NULL "
            "AND p.part_from IS NULL AND p.part_to IS NULL) "
            "OR (p.part_to IS NOT NULL AND v.production_from IS NOT NULL "
            "AND p.part_to < v.production_from) "
            "OR (v.production_to IS NOT NULL AND p.part_from IS NOT NULL "
            "AND v.production_to < p.part_from) "
            "OR NULLIF(TRIM(g.url), '') IS NULL OR g.url NOT LIKE "
            "'https://partsouq.com/en/catalog/genuine/unit?%%')",
            (run_id,),
        )
        return cur.rowcount

    def resumable_bounded_run_key(
        self,
        target_parts: int,
        *,
        scheduled_job_run_id: int | None,
    ) -> str | None:
        """取得同 target、同 daemon/direct provenance 的最新未完成 run。"""
        if scheduled_job_run_id is None:
            row = self.db._execute(
                "SELECT run_key FROM crawl_runs WHERE dataset_kind = 'bounded' "
                "AND target_parts = %s AND status IN ('running', 'error') "
                "AND scheduled_job_run_id IS NULL "
                "ORDER BY started_at DESC, id DESC LIMIT 1",
                (target_parts,),
            ).fetchone()
        else:
            row = self.db._execute(
                "SELECT cr.run_key FROM scheduled_job_runs AS current_job "
                "JOIN crawl_runs AS cr ON cr.dataset_kind = 'bounded' "
                "AND cr.target_parts = %s AND cr.status IN ('running', 'error') "
                "JOIN scheduled_job_runs AS previous_job "
                "ON previous_job.id = cr.scheduled_job_run_id "
                "AND previous_job.job_name = 'catalog' "
                "AND previous_job.trigger_mode = 'daemon' "
                "WHERE current_job.id = %s AND current_job.job_name = 'catalog' "
                "AND current_job.trigger_mode = 'daemon' AND current_job.status = 'running' "
                "ORDER BY cr.started_at DESC, cr.id DESC LIMIT 1",
                (target_parts, scheduled_job_run_id),
            ).fetchone()
        return str(row["run_key"]) if row and row.get("run_key") else None

    def finish_run(self, run_id: int, status: str, counts: dict, error: str | None = None):
        """收尾一筆爬取紀錄：寫入完成時間、狀態、各層計數與錯誤訊息。

        若該 run 已是 success 或 bounded_success，本次收尾不降級
        （P2 修復）—— 不抹掉已原子發布的證據。舊碼先 SELECT status 再 UPDATE：
        併行收尾時兩方同時讀到 running → error 可在 success 後覆寫，
        留下「success + 錯誤訊息 + 錯誤計數」的矛盾紀錄。改為單一
        條件 UPDATE（排除兩種成功狀態），由 DB 保證原子性
        （P2 修復：TOCTOU 競態）。

        本方法不 commit：交易邊界由服務層決定（見 db.py 分層契約）。
        """
        self.db._execute(
            "UPDATE crawl_runs SET finished_at = NOW(), "
            "status = %s, "
            "brands_ok = %s, models_ok = %s, vehicles_ok = %s, "
            "groups_ok = %s, parts_ok = %s, parts_new = %s, error_msg = %s "
            "WHERE id = %s AND status NOT IN ('success', 'bounded_success')",
            (
                status,
                counts.get("brands", 0),
                counts.get("models", 0),
                counts.get("vehicles", 0),
                counts.get("groups", 0),
                counts.get("parts", 0),
                counts.get("parts_new", 0),
                error,
                run_id,
            ),
        )

    def publish_success_parts(self, run_id: int):
        """在同一交易內更新不可變的 current snapshot。

        normalized tables 可被後續 failed/partial attempt 原地 upsert；因此
        current view 不直接 join 它們。先 upsert 本次 logical run 明確
        標記的資料，再刪除不屬於本次 run 的舊列。與 finish_run(success)
        同次 commit；任一步失敗 rollback 後，舊 snapshot 仍完整可讀。
        """
        source_row = self.db._execute(
            "SELECT COUNT(*) AS row_count FROM parts WHERE seen_run_id = %s",
            (run_id,),
        ).fetchone()
        source_count = int((source_row or {}).get("row_count", 0))
        if source_count <= 0:
            raise RuntimeError(f"run {run_id} produced an empty published snapshot")

        self.db._execute(
            "INSERT INTO published_parts ("
            "part_id, vehicle_id, model_id, vehicle_vid, "
            "brand, model, vehicle_name, vehicle_code, prod_period, "
            "production_from, production_to, engine, trim_name, "
            "part_name, part_number, part_number_normalized, category_id, category_cid, "
            "category_main, category_group, group_id, group_code, group_uid, "
            "part_range, part_from, part_to, source_url, note, quantity, code, snapshot_at) "
            "SELECT source.part_id, source.vehicle_id, source.model_id, source.vehicle_vid, "
            "source.brand, source.model, "
            "source.vehicle_name, source.vehicle_code, source.prod_period, "
            "source.production_from, source.production_to, source.engine, source.trim_name, "
            "source.part_name, "
            "source.part_number, source.part_number_normalized, "
            "source.category_id, source.category_cid, "
            "source.category_main, source.category_group, source.group_id, "
            "source.group_code, source.group_uid, "
            "source.part_range, source.part_from, source.part_to, "
            "source.source_url, source.note, source.quantity, source.code, source.snapshot_at FROM ("
            "SELECT p.id AS part_id, v.id AS vehicle_id, m.id AS model_id, "
            "v.vid AS vehicle_vid, b.name AS brand, m.name AS model, "
            "v.name AS vehicle_name, v.model_code AS vehicle_code, "
            "v.prod_period AS prod_period, v.production_from AS production_from, "
            "v.production_to AS production_to, v.engine AS engine, v.grade AS trim_name, "
            "p.name AS part_name, "
            "p.part_number AS part_number, "
            "UPPER(REGEXP_REPLACE(p.part_number, '[[:space:]-]+', '')) "
            "AS part_number_normalized, c.id AS category_id, c.cid AS category_cid, "
            "c.name AS category_main, g.name AS category_group, g.id AS group_id, "
            "g.code AS group_code, g.uid AS group_uid, "
            "p.range_str AS part_range, p.part_from AS part_from, p.part_to AS part_to, "
            "g.url AS source_url, p.note AS note, p.quantity AS quantity, "
            "p.code AS code, NOW() AS snapshot_at "
            "FROM parts p "
            "JOIN groups_t g ON g.id = p.group_id "
            "JOIN categories c ON c.id = g.category_id "
            "JOIN vehicles v ON v.id = c.vehicle_id "
            "JOIN models m ON m.id = v.model_id "
            "JOIN brands b ON b.id = m.brand_id "
            "WHERE p.seen_run_id = %s) AS source "
            "ON DUPLICATE KEY UPDATE "
            "vehicle_id = source.vehicle_id, model_id = source.model_id, "
            "vehicle_vid = source.vehicle_vid, brand = source.brand, model = source.model, "
            "vehicle_name = source.vehicle_name, vehicle_code = source.vehicle_code, "
            "prod_period = source.prod_period, production_from = source.production_from, "
            "production_to = source.production_to, engine = source.engine, "
            "trim_name = source.trim_name, part_name = source.part_name, "
            "part_number = source.part_number, "
            "part_number_normalized = source.part_number_normalized, "
            "category_id = source.category_id, "
            "category_cid = source.category_cid, category_main = source.category_main, "
            "category_group = source.category_group, group_id = source.group_id, "
            "group_code = source.group_code, group_uid = source.group_uid, "
            "part_range = source.part_range, part_from = source.part_from, "
            "part_to = source.part_to, source_url = source.source_url, note = source.note, "
            "quantity = source.quantity, code = source.code, "
            "snapshot_at = source.snapshot_at",
            (run_id,),
        )
        self.db._execute(
            "DELETE pp FROM published_parts pp "
            "LEFT JOIN parts p ON p.id = pp.part_id AND p.seen_run_id = %s "
            "WHERE p.id IS NULL",
            (run_id,),
        )
        published_row = self.db._execute(
            "SELECT COUNT(*) AS row_count FROM published_parts"
        ).fetchone()
        published_count = int((published_row or {}).get("row_count", 0))
        if published_count != source_count:
            raise RuntimeError(
                f"run {run_id} snapshot row count mismatch: "
                f"source={source_count}, published={published_count}"
            )
        return published_count

    def publish_bounded_parts(self, run_id: int, target_parts: int) -> int:
        """原子發布精確達標的正式有界資料集。

        bounded_parts 與全站 published_parts 的語意分離。本方法只在
        run metadata、筆數、來源 ID 關聯與必填欄位全部通過時
        更換 current bounded snapshot；交易失敗會保留上一版。
        """
        if target_parts <= 0:
            raise ValueError("bounded target_parts must be positive")

        run = self.db._execute(
            "SELECT cr.run_key, cr.dataset_kind, cr.target_parts, cr.status, "
            "cr.scheduled_job_run_id, sj.job_name AS scheduled_job_name, "
            "sj.trigger_mode AS scheduled_trigger_mode, sj.status AS scheduled_job_status "
            "FROM crawl_runs AS cr "
            "LEFT JOIN scheduled_job_runs AS sj ON sj.id = cr.scheduled_job_run_id "
            "WHERE cr.id = %s FOR UPDATE",
            (run_id,),
        ).fetchone()
        if not run:
            raise RuntimeError(f"bounded run {run_id} does not exist")
        if (
            run.get("dataset_kind") != "bounded"
            or int(run.get("target_parts") or 0) != target_parts
            or run.get("status") != "running"
        ):
            raise RuntimeError(f"run {run_id} is not a matching running bounded crawl")
        if run.get("scheduled_job_run_id") is None or (
            run.get("scheduled_job_name") != "catalog"
            or run.get("scheduled_trigger_mode") != "daemon"
            or run.get("scheduled_job_status") != "running"
        ):
            raise RuntimeError(f"bounded run {run_id} has invalid scheduler provenance")

        run_key = str(run.get("run_key") or "")
        if not run_key:
            raise RuntimeError(f"bounded run {run_id} has no run key")
        failure_rows = self.db._execute(
            "SELECT id FROM crawl_state WHERE run_key = %s AND status = 'error' FOR UPDATE",
            (run_key,),
        ).fetchall()
        if failure_rows:
            raise RuntimeError(
                f"bounded run {run_id} has crawl failures: count={len(failure_rows)}"
            )

        source_row = self.db._execute(
            "SELECT COUNT(*) AS row_count FROM parts WHERE seen_run_id = %s",
            (run_id,),
        ).fetchone()
        source_count = int((source_row or {}).get("row_count", 0))
        if source_count != target_parts:
            raise RuntimeError(
                f"bounded run {run_id} source count mismatch: "
                f"source={source_count}, target={target_parts}"
            )

        # current-only snapshot：DELETE + INSERT 都在 crawler 收尾交易內。
        # 讀者在 commit 前仍看到上一版，失敗時則整筆 rollback。
        self.db._execute("DELETE FROM bounded_parts")
        self.db._execute(
            "INSERT INTO bounded_parts ("
            "part_id, crawl_run_id, vehicle_id, model_id, vehicle_vid, "
            "brand, model, vehicle_name, vehicle_code, prod_period, "
            "production_from, production_to, engine, trim_name, part_name, "
            "part_number, part_number_normalized, category_id, category_cid, "
            "category_main, category_group, group_id, group_code, group_uid, "
            "part_range, part_from, part_to, source_url, note, quantity, code, snapshot_at) "
            "SELECT p.id, %s, v.id, m.id, v.vid, b.name, m.name, v.name, "
            "v.model_code, v.prod_period, v.production_from, v.production_to, "
            "v.engine, v.grade, p.name, p.part_number, "
            "UPPER(REGEXP_REPLACE(p.part_number, '[[:space:]-]+', '')), "
            "c.id, c.cid, c.name, g.name, g.id, g.code, g.uid, p.range_str, "
            "p.part_from, p.part_to, g.url, p.note, p.quantity, p.code, NOW() "
            "FROM parts AS p "
            "JOIN groups_t AS g ON g.id = p.group_id "
            "JOIN categories AS c ON c.id = g.category_id "
            "JOIN vehicles AS v ON v.id = c.vehicle_id "
            "JOIN models AS m ON m.id = v.model_id "
            "JOIN brands AS b ON b.id = m.brand_id "
            "WHERE p.seen_run_id = %s",
            (run_id, run_id),
        )

        snapshot = self.db._execute(
            "SELECT COUNT(*) AS row_count, "
            "COUNT(DISTINCT crawl_run_id) AS run_count, "
            "MIN(crawl_run_id) AS min_run_id, MAX(crawl_run_id) AS max_run_id "
            "FROM bounded_parts"
        ).fetchone()
        if (
            int((snapshot or {}).get("row_count", 0)) != target_parts
            or int((snapshot or {}).get("run_count", 0)) != 1
            or int((snapshot or {}).get("min_run_id") or 0) != run_id
            or int((snapshot or {}).get("max_run_id") or 0) != run_id
        ):
            raise RuntimeError(f"bounded run {run_id} snapshot identity/count mismatch")

        quality = self.db._execute(
            "SELECT COUNT(*) AS invalid_rows FROM bounded_parts AS bp "
            "LEFT JOIN parts AS p ON p.id = bp.part_id "
            "LEFT JOIN groups_t AS g ON g.id = bp.group_id "
            "LEFT JOIN categories AS c ON c.id = bp.category_id "
            "LEFT JOIN vehicles AS v ON v.id = bp.vehicle_id "
            "LEFT JOIN models AS m ON m.id = bp.model_id "
            "WHERE bp.crawl_run_id = %s AND ("
            "p.id IS NULL OR g.id IS NULL OR c.id IS NULL OR v.id IS NULL OR m.id IS NULL "
            "OR p.group_id <> bp.group_id OR g.category_id <> bp.category_id "
            "OR c.vehicle_id <> bp.vehicle_id OR v.model_id <> bp.model_id "
            "OR NULLIF(TRIM(bp.part_number), '') IS NULL "
            "OR NULLIF(TRIM(bp.part_number_normalized), '') IS NULL "
            "OR bp.part_number_normalized <> "
            "UPPER(REGEXP_REPLACE(bp.part_number, '[[:space:]-]+', '')) "
            "OR NULLIF(TRIM(bp.part_name), '') IS NULL "
            "OR NULLIF(TRIM(bp.brand), '') IS NULL OR NULLIF(TRIM(bp.model), '') IS NULL "
            "OR NULLIF(TRIM(bp.vehicle_name), '') IS NULL "
            "OR NULLIF(TRIM(bp.vehicle_code), '') IS NULL "
            "OR NULLIF(TRIM(bp.vehicle_vid), '') IS NULL "
            "OR NULLIF(TRIM(bp.category_cid), '') IS NULL "
            "OR NULLIF(TRIM(bp.category_main), '') IS NULL "
            "OR NULLIF(TRIM(bp.category_group), '') IS NULL "
            "OR NULLIF(TRIM(bp.group_code), '') IS NULL "
            "OR NULLIF(TRIM(bp.group_uid), '') IS NULL "
            "OR NULLIF(TRIM(bp.code), '') IS NULL "
            "OR (bp.production_from IS NULL AND bp.production_to IS NULL "
            "AND bp.part_from IS NULL AND bp.part_to IS NULL) "
            "OR (bp.part_to IS NOT NULL AND bp.production_from IS NOT NULL "
            "AND bp.part_to < bp.production_from) "
            "OR (bp.production_to IS NOT NULL AND bp.part_from IS NOT NULL "
            "AND bp.production_to < bp.part_from) "
            "OR bp.source_url NOT LIKE "
            "'https://partsouq.com/en/catalog/genuine/unit?%%')",
            (run_id,),
        ).fetchone()
        invalid_rows = int((quality or {}).get("invalid_rows", 0))
        if invalid_rows:
            raise RuntimeError(
                f"bounded run {run_id} failed source/field quality gate: "
                f"invalid_rows={invalid_rows}"
            )
        return target_parts
