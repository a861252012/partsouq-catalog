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
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import cast

from .config import CRAWL
from .db import Database, Row
from .evidence import (
    SANITIZER_VERSION,
    RecordEvidence,
    SanitizedBody,
    assert_no_secret_material,
    brand_natural_key,
    canonical_parser_context,
    canonical_sha256,
    category_natural_key,
    dataset_sha256,
    group_natural_key,
    model_natural_key,
    part_natural_key,
    public_source_url,
    replay_catalog_records,
    restore_sanitized_body,
    vehicle_natural_key,
)

log = logging.getLogger("repos")

# 零件的搜尋頁網址模板（PartSouq 的零件查詢入口）
PART_URL_TEMPLATE = "https://partsouq.com/en/search/all?q={part_number}"

type IntCompatible = str | bytes | bytearray | int | float

_SHA256_LENGTH = 64
_EVIDENCE_PARENT_TYPE = {
    "model": "brand",
    "vehicle": "model",
    "category": "vehicle",
    "group": "category",
    "part": "group",
    "quarantine_part": "group",
}
_EVIDENCE_PAGE_PARSERS = {
    ("genuine", "parse_brands"),
    ("locate", "parse_brand_index"),
    ("pick", "parse_vehicles"),
    ("vehicle", "parse_category_links"),
    ("vehicle", "parse_groups"),
    ("category", "parse_groups"),
    ("unit", "parse_parts"),
}


def _db_int(value: object) -> int:
    """將已知為數值欄位的 DB 值轉成 int，保留原有 int() 語意。"""
    return int(cast(IntCompatible, value))


def _evidence_record_set_sha256(records: Sequence[RecordEvidence]) -> str:
    """Hash a deterministic record set, including every parent-chain edge."""
    return dataset_sha256(records)


def _evidence_record_key(record: RecordEvidence) -> tuple[str, str, str | None, str]:
    return (
        record.record_type,
        record.natural_key_sha256,
        record.parent_natural_key_sha256,
        record.record_sha256,
    )


def _require_sha256(value: str, field_name: str) -> None:
    if len(value) != _SHA256_LENGTH or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256")


def _validated_bounded_scope(
    scope_brand: str | None,
    scope_model: str | None,
    scope_vehicle_year_floor: int | None,
) -> tuple[str | None, str | None, int | None]:
    """正規化正式 bounded scope；舊版全 NULL run 保持相容。"""

    brand = scope_brand.strip().casefold() if scope_brand is not None else None
    model = scope_model.strip().casefold() if scope_model is not None else None
    if brand == "" or model == "":
        raise ValueError("bounded scope brand/model cannot be blank")
    if brand is None and model is None and scope_vehicle_year_floor is None:
        return None, None, None
    if brand is None or model is None or scope_vehicle_year_floor is None:
        raise ValueError("bounded scope brand, model and vehicle year floor must be set together")
    if not 1886 <= scope_vehicle_year_floor <= 2100:
        raise ValueError("bounded scope vehicle year floor must be between 1886 and 2100")
    return brand, model, scope_vehicle_year_floor


def _canonical_unit_url_sql(column: str, uid_column: str | None = None) -> str:
    """回傳與 public_source_url 相同邊界的 secret-free unit URL 條件。"""

    contract = (
        f"REGEXP_LIKE({column}, "
        "'^https://partsouq[.]com/en/catalog/genuine/unit[?]"
        "((c|model|vid|cid|cname|uid|q)=[^&#]*&)*"
        "(c|model|vid|cid|cname|uid|q)=[^&#]*$', 'c') "
        f"AND REGEXP_LIKE({column}, '(^|[?&])uid=[^&#]+(&|$)', 'c')"
    )
    if uid_column is None:
        return contract
    return (
        f"{contract} "
        f"AND REGEXP_LIKE({uid_column}, '^[A-Za-z0-9._~-]+$', 'c') "
        f"AND REGEXP_INSTR({column}, '(^|[?&])uid=', 1, 2, 0, 'c') = 0 "
        "AND BINARY SUBSTRING_INDEX("
        f"REGEXP_SUBSTR({column}, '(^|[?&])uid=[^&#]+', 1, 1, 'c'), "
        f"'uid=', -1) = BINARY {uid_column}"
    )


@dataclass(frozen=True, slots=True)
class _RunEvidenceSummary:
    manifest_sha256: str
    dataset_sha256: str
    artifact_count: int
    accepted_record_count: int
    original_bytes: int
    stored_bytes: int


@dataclass
class GroupIdentity:
    """單一 category 內 group 身分的記憶體快取（SOL review P2）。

    - by_key：((code, uid) → id)，供「目標 (code, uid) 是否已存在」判斷。
    - image_by_uid：(uid → id of the code='' 列)，供圖片→文字升級。
    由 preload_group_identity 建立；upsert_group 會在升級/插入後就地
    更新兩份 map，因此同一 category 內後續呼叫仍正確（不會把同一列
    重複升級，也不會漏掉同月新插入的圖片列）。

    注意：圖片→文字升級後，by_key 仍保留升級前的 ("", uid) → id 項目
    （指向已變成文字列的同一列）。沒有程式碼會用空 code 查 by_key
    （image_by_uid 才是空 code 的查詢入口），因此無害；但 by_key 不再
    精確鏡像 DB，不要把它當成 DB 的完整快照。
    """

    by_key: dict[tuple[str, str], int] = field(default_factory=dict)
    image_by_uid: dict[str, int] = field(default_factory=dict)


def vehicle_identity_hash(model_id: int, vehicle: Mapping[str, object]) -> str:
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
        return [cast(str, r["name"]) for r in cur.fetchall()]

    def _brand_id(self, name: str) -> int:
        """依品牌名稱查詢 id（upsert 回傳值為 0 時的備援查詢）。"""
        cur = self.db._execute("SELECT id FROM brands WHERE name = %s", (name,))
        row = cur.fetchone()
        return cast(int, row["id"]) if row else 0

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
        return cast(int, cur.lastrowid)

    def list_models(self, brand_id: int) -> Sequence[Row]:
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
        return [cast(str, r["name"]) for r in cur.fetchall()]


class VehicleRepository:
    """車型、分類、零件組的資料存取（車型 → 分類 → 零件組的樹狀結構）。"""

    def __init__(self, db: Database):
        self.db = db

    def upsert_vehicle(self, model_id: int, vehicle: Mapping[str, object]) -> int:
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
        return cast(int, cur.lastrowid)

    def list_vehicles(self, model_id: int) -> Sequence[Row]:
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
            return cast(int, cur.lastrowid)

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
            return cast(int, row["id"])
        cur = self.db._execute(
            "INSERT INTO categories (vehicle_id, name, cid) VALUES (%s, %s, %s) AS new "
            "ON DUPLICATE KEY UPDATE name = new.name, cid = new.cid, fetched_at = NOW(), "
            "id = LAST_INSERT_ID(id)",
            (vehicle_id, name, cid),
        )
        return cast(int, cur.lastrowid)

    def list_categories(self, vehicle_id: int) -> Sequence[Row]:
        """列出某車型下的所有分類（依 id 排序）。"""
        cur = self.db._execute(
            "SELECT id, name, cid FROM categories WHERE vehicle_id = %s ORDER BY id",
            (vehicle_id,),
        )
        return cur.fetchall()

    def preload_group_identity(self, category_id: int) -> GroupIdentity:
        """一次載入某 category 下所有 group 身分，供同 category 內逐組
        upsert_group 使用，避免每組都多一次 SELECT（SOL review P2）。

        回傳的 GroupIdentity 會被 upsert_group 就地更新（升級/插入後
        同步 map），因此同 category 內 image→text 的跨列升級仍正確。
        """
        cur = self.db._execute(
            "SELECT id, code, uid FROM groups_t WHERE category_id = %s", (category_id,)
        )
        identity = GroupIdentity({}, {})
        for row in cur.fetchall():
            code = cast(str, row["code"] or "")
            uid = cast(str, row["uid"] or "")
            if uid == "":
                # legacy NULL 回推列沒有站方身分可依，不參與 by-uid 對帳
                continue
            identity.by_key[(code, uid)] = cast(int, row["id"])
            if code == "":
                identity.image_by_uid[uid] = cast(int, row["id"])
        return identity

    def upsert_group(
        self,
        category_id: int,
        code: str | None,
        name: str | None,
        uid: str | None,
        url: str | None,
        identity: GroupIdentity | None = None,
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

        identity（SOL review P2）：呼叫端以 preload_group_identity 一次
        載入該 category 的 GroupIdentity 後傳入，可省掉每組的 image-row
        存在性 SELECT。map 會被就地更新，升級/插入後同 category 內的
        後續呼叫仍然正確。None 時走原本的逐次 SELECT 路徑（測試/相容）。
        """
        code = code or ""
        uid = uid or ""
        if code and identity is not None:
            image_id = identity.image_by_uid.get(uid) if uid else None
            if image_id is not None and (code, uid) not in identity.by_key:
                # 目標 (code, uid) 已存在（identity 快取內）時不升級，
                # 退回標準 upsert —— 避免撞唯一鍵（與無快取路徑一致）。
                self.db._execute(
                    "UPDATE groups_t SET code = %s, name = %s, url = %s, "
                    "fetched_at = NOW() WHERE id = %s",
                    (code, name, url, image_id),
                )
                identity.image_by_uid.pop(uid, None)
                identity.by_key[(code, uid)] = image_id
                return image_id
        elif code:
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
                    return cast(int, image_row["id"])
        cur = self.db._execute(
            "INSERT INTO groups_t (category_id, code, name, uid, url) "
            "VALUES (%s, %s, %s, %s, %s) AS new "
            "ON DUPLICATE KEY UPDATE name = new.name, url = new.url, "
            "fetched_at = NOW(), id = LAST_INSERT_ID(id)",
            (category_id, code, name, uid, url),
        )
        row_id = cast(int, cur.lastrowid)
        if identity is not None:
            # 同步快取：無論是全新插入或 ON DUPLICATE 更新既有列，
            # LAST_INSERT_ID(id) 都已回傳該身分真正的 id。
            identity.by_key[(code, uid)] = row_id
            if code == "" and uid != "":
                identity.image_by_uid[uid] = row_id
        return row_id

    def list_group_identities_for_category(self, vehicle_id: int, cid: str) -> dict[str, set[str]]:
        """回傳某車輛某 cid 下 DB 已知的 group 身分（uid → code 集合）。

        SOL review P1：closure 對帳必須以「uid → code 集合」為單位，不能
        只看 uid —— parser 與資料庫唯一鍵 (category_id, code, uid) 都允許
        同 uid 不同 code 的多筆 group（變體專屬資料）；若壓成 uid 集合，
        其中一個 code 變體從頁面消失時 closure 仍會通過，缺漏偵測不到。

        code 為 '' 代表圖片-only 列（站方合法版型）。呼叫端對帳時：
        已知的圖片-only code（''）由「uid 出現」即滿足；已知的文字 code
        必須以同 code 出現才滿足（uid 以圖片-only 出現 = 呈現降級，
        另行告警，不算 group 消失）。
        """
        cur = self.db._execute(
            "SELECT g.uid, g.code FROM groups_t g "
            "JOIN categories c ON c.id = g.category_id "
            "WHERE c.vehicle_id = %s AND c.cid = %s AND g.uid <> ''",
            (vehicle_id, cid),
        )
        identities: dict[str, set[str]] = {}
        for row in cur.fetchall():
            identities.setdefault(cast(str, row["uid"]), set()).add(cast(str, row["code"] or ""))
        return identities


class PartRepository:
    """零件的資料存取（目錄的葉節點層）。"""

    def __init__(self, db: Database):
        self.db = db

    def upsert_parts(
        self,
        group_id: int,
        parts: Sequence[Mapping[str, object]],
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

    def clear_group_membership(self, group_id: int, run_id: int | None = None) -> int:
        """清除單一 group 的舊 run membership；與後續 upsert 同交易。"""
        if run_id is None:
            cursor = self.db._execute(
                "UPDATE parts SET seen_run_id = NULL WHERE group_id = %s",
                (group_id,),
            )
        else:
            cursor = self.db._execute(
                "UPDATE parts SET seen_run_id = NULL WHERE group_id = %s AND seen_run_id = %s",
                (group_id, run_id),
            )
        return cursor.rowcount

    def quarantine_parts(
        self,
        group_id: int,
        run_key: str,
        rows: Sequence[Mapping[str, object]],
        reason: str = "nameless",
    ) -> int:
        """記錄站方合法存在但無法發布的零件列（SOL review P1 起）。

        rows 通常是 parse_parts(diagnostics=True) 回傳的 skipped_rows（純
        料號列，無可驗證產品名稱）；bounded 品質閘門排除的具名稱列也會
        以不同 reason 留存。它們不落 parts 表，寫進 part_quarantine 作為
        可追查紀錄。

        無名稱列採「忽略 + 紀錄」政策：含該列的 group 可照常完成；品質
        閘門排除的列則會讓 receipt 保持 partial 或整組失敗。兩者都用
        quarantine 表追蹤，站方修正來源後，下次 run 會重新判定。

        resolved_at / resolution（migration 012）供運維標記處置狀態：
        管理員核對後可填寫，作為審計紀錄；純紀錄用途，不影響流程。

        以 (group_id, part_number, range_str, reason) 為唯一鍵，重複發現
        時就地更新（冪等）。同一料號在後續 run **再次出現**時（新的
        occurrence），會重開處置狀態：清掉 resolved_at / resolution，
        讓 count_quarantined 重新計入（SOL review P1：不能把新發生的
        異常藏在舊的「已處置」紀錄下）。
        """
        if not rows:
            return 0
        self.db._executemany(
            "INSERT INTO part_quarantine (group_id, part_number, range_str, reason, "
            "code, quantity, note, run_key) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) AS new "
            "ON DUPLICATE KEY UPDATE reason = new.reason, code = new.code, "
            "quantity = new.quantity, note = new.note, run_key = new.run_key, "
            "resolved_at = NULL, resolution = NULL, "
            "updated_at = CURRENT_TIMESTAMP",
            [
                (
                    group_id,
                    row.get("part_number") or "",
                    row.get("range_str") or "",
                    reason,
                    row.get("code"),
                    row.get("quantity"),
                    row.get("note"),
                    run_key,
                )
                for row in rows
            ],
        )
        return len(rows)

    def _clear_stale_group_membership(self, group_id: int, run_id: int) -> None:
        """只清除本次 payload 已不存在的 membership。"""
        self.db._execute(
            "UPDATE parts SET seen_run_id = NULL WHERE group_id = %s AND seen_run_id <> %s",
            (group_id, run_id),
        )

    def count_parts_in_group(self, group_id: int) -> int:
        """統計某零件組下的零件數量（供驗證與監督使用）。"""
        cur = self.db._execute("SELECT COUNT(*) AS n FROM parts WHERE group_id = %s", (group_id,))
        row = cast(Row, cur.fetchone())
        return cast(int, row["n"])

    def bounded_group_context(self, group_id: int) -> Row | None:
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
            f"AND {_canonical_unit_url_sql('g.url', 'g.uid')}) AS source_valid "
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
        return {(cast(str, row["part_number"]), cast(str, row["range_str"])) for row in rows}

    def part_ids_for_evidence(
        self,
        group_id: int,
        parts: Sequence[Mapping[str, object]],
    ) -> list[tuple[int, Mapping[str, object]]]:
        """依 parts 唯一鍵找回 evidence 要綁定的 DB id。

        只查一次整個 group，並保留輸入順序。重複 natural key
        會使「解析列數」與「可發布零件數」語意不明，因此
        fail closed；找不到列也不允許用舊 id 或推測值補齊。
        本方法不 commit，必須與 upsert_parts、artifact、receipt 在同一
        group transaction 內呼叫。
        """
        if not parts:
            return []
        rows = self.db._execute(
            "SELECT id, part_number, range_str FROM parts WHERE group_id = %s",
            (group_id,),
        ).fetchall()
        ids = {
            (cast(str, row["part_number"]), cast(str, row["range_str"])): cast(int, row["id"])
            for row in rows
        }
        result: list[tuple[int, Mapping[str, object]]] = []
        seen: set[tuple[str, str]] = set()
        for part in parts:
            key = (str(part.get("part_number") or ""), str(part.get("range_str") or ""))
            if not key[0]:
                raise ValueError("part evidence requires a non-empty part_number")
            if key in seen:
                raise ValueError(f"duplicate part evidence natural key: {key!r}")
            seen.add(key)
            part_id = ids.get(key)
            if part_id is None:
                raise RuntimeError(
                    f"part evidence row was not upserted: group_id={group_id}, key={key!r}"
                )
            result.append((part_id, part))
        return result

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
                "OR NOT (BINARY fetched_status = BINARY 'done' "
                "OR BINARY fetched_status = BINARY 'not_found')",
                (run_key,),
            )
        else:
            cur = self.db._execute("SELECT COUNT(*) AS n FROM groups_t")
        row = cast(Row, cur.fetchone())
        return cast(int, row["n"])

    def list_null_groups(
        self,
        limit: int = 500,
        *,
        run_key: str | None = None,
        run_id: int | None = None,
        after_id: int = 0,
    ) -> Sequence[Row]:
        """回傳沒有有效 receipt 的零件組與所屬車型。

        獨立 recover 只處理歷史 NULL；full run 收尾時傳入 run_key，補抓
        所有不屬於本 run 的 receipt，以及 receipt 與本 run 零件數不符的
        group。after_id 供同一收尾流程用 keyset 分批掃完，不重複打失敗列。
        """
        if limit <= 0:
            raise ValueError("recover group limit must be positive")
        if after_id < 0:
            raise ValueError("recover group after_id cannot be negative")
        if (run_key is None) != (run_id is None):
            raise ValueError("full group recovery requires both run_key and run_id")

        where_clause = "groups_t.fetched_status IS NULL"
        params: list[object]
        if run_key is not None:
            where_clause = (
                "groups_t.fetched_run_key IS NULL OR groups_t.fetched_run_key <> %s "
                "OR groups_t.fetched_status IS NULL OR NOT ("
                "BINARY groups_t.fetched_status = BINARY 'done' OR "
                "BINARY groups_t.fetched_status = BINARY 'not_found') OR "
                "(BINARY groups_t.fetched_status = BINARY 'done' AND "
                "COALESCE(groups_t.fetched_row_count, -1) <> "
                "(SELECT COUNT(*) FROM parts WHERE parts.group_id = groups_t.id "
                "AND parts.seen_run_id = %s)) OR "
                "(BINARY groups_t.fetched_status = BINARY 'not_found' AND ("
                "COALESCE(groups_t.fetched_row_count, -1) <> 0 OR "
                "EXISTS (SELECT 1 FROM parts WHERE parts.group_id = groups_t.id "
                "AND parts.seen_run_id = %s)))"
            )
            params = [after_id, run_key, run_id, run_id, limit]
        else:
            params = [after_id, limit]
        cur = self.db._execute(
            "SELECT groups_t.id, groups_t.category_id, groups_t.code, groups_t.name, "
            "groups_t.uid, groups_t.url, categories.vehicle_id, "
            "categories.name AS category_name, categories.cid "
            "FROM groups_t "
            "JOIN categories ON categories.id = groups_t.category_id "
            f"WHERE groups_t.id > %s AND ({where_clause}) "
            "ORDER BY groups_t.id LIMIT %s",
            tuple(params),
        )
        rows = cur.fetchall()
        return rows if rows is not None else []

    def count_quarantined(self, run_key: str = "") -> int:
        """回傳指定 run 列進 part_quarantine 的料號列數（供運維查詢）。

        無名稱純料號列是「忽略 + 紀錄」政策（使用者決定）：quarantine
        表是完整紀錄，不阻擋任何發布 gate。resolved_at 已填的列視為
        已處置，不計入。
        """
        if not run_key:
            return 0
        cur = self.db._execute(
            "SELECT COUNT(*) AS n FROM part_quarantine WHERE run_key = %s AND resolved_at IS NULL",
            (run_key,),
        )
        row = cast(Row, cur.fetchone())
        return cast(int, row["n"])

    def mark_done(self, scope: str, key: str) -> None:
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

    def mark_error(self, scope: str, key: str, msg: str) -> None:
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
        row = cast(Row, cur.fetchone())
        return cast(int, row["n"])

    def count_failures(self, run_key: str = "") -> int:
        """只統計真正失敗的項目；sample 預期留下的 pending 不算失敗。"""
        cur = self.db._execute(
            "SELECT COUNT(*) AS n FROM crawl_state WHERE run_key = %s AND status = 'error'",
            (run_key,),
        )
        row = cast(Row, cur.fetchone())
        return cast(int, row["n"])

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
            "AND g.fetched_run_key = %s "
            "AND (BINARY g.fetched_status = BINARY 'done' "
            "OR BINARY g.fetched_status = BINARY 'not_found') "
            f"AND ({_canonical_unit_url_sql('g.url', 'g.uid')}) LIMIT 1",
            (vehicle_id, group_code, group_uid, run_key),
        )
        return cur.fetchone() is not None

    def fetched_group_map(
        self, vehicle_id: int, run_key: str = ""
    ) -> dict[tuple[str, str, str], int]:
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
            "WHERE c.vehicle_id = %s AND g.fetched_run_key = %s "
            "AND (BINARY g.fetched_status = BINARY 'done' "
            "OR BINARY g.fetched_status = BINARY 'not_found') "
            f"AND ({_canonical_unit_url_sql('g.url', 'g.uid')})",
            (vehicle_id, run_key),
        )
        return {
            (
                str(r["cid"] or ""),
                cast(str, r["code"]),
                cast(str, r["uid"]),
            ): _db_int(r["fetched_row_count"] or 0)
            for r in cur.fetchall()
        }

    def previous_row_count_map(
        self, vehicle_id: int, run_key: str = ""
    ) -> dict[tuple[str, str, str], int]:
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
            (
                str(r["cid"] or ""),
                cast(str, r["code"]),
                cast(str, r["uid"]),
            ): _db_int(r["row_count"] or 0)
            for r in cur.fetchall()
        }

    def previous_row_count(self, group_id: int) -> int:
        """回傳某零件組歷史上已驗證的最高 row_count（無則 0）。

        縮水偵測的後備路徑（未提供 prev_rows map 時逐組查詢，僅測試/
        相容使用；正式爬取一律用 previous_row_count_map 一次載入）。
        """
        cur = self.db._execute(
            "SELECT verified_row_count AS n FROM groups_t WHERE id = %s", (group_id,)
        )
        row = cast(Row, cur.fetchone())
        return cast(int, row["n"] or 0)

    def mark_group_fetched(
        self, group_id: int, run_key: str = "", status: str = "done", row_count: int = 0
    ) -> None:
        """標記某零件組已在本次 run 抓取完成（durable receipt，F1b/F5）。

        status 區分完成種類：'done'（有零件）、'not_found'（404，網站
        端「此組無資料」的合法訊號）。HTTP 200 但解析 0 零件一律視為
        異常（反爬/版型變更）並拋錯，**不寫** receipt（SOL P2：沒有
        可驗證的「合法空組」DOM 訊號前不猜測，避免把封鎖頁當成空組
        標 done）。row_count 記錄本組零件筆數 —— 配合 fetched_run_key
        讓續爬「不再重抓 404 或已完成組」，也為 content hash 增量
        更新打基礎。

        站方合法存在但無法發布的無名稱純料號列：由呼叫端寫進
        part_quarantine 記錄（見 quarantine_parts），組本身照常標
        done —— 「忽略 + 紀錄」政策（使用者決定），不阻擋發布。
        只有 status='done' 會更新 verified_row_count。

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

    def record_bounded_group_receipt(
        self,
        run_id: int,
        group_id: int,
        source_artifact_id: int,
        *,
        status: str,
        parsed_part_count: int,
        accepted_part_count: int,
        skipped_record_count: int,
    ) -> None:
        """保存正式 bounded snapshot 的不可變群組收據。

        ``groups_t.fetched_*`` 是下一次爬取可覆寫的續爬快取，不能拿來
        證明已發布 snapshot 的歷史邊界。這份 receipt 綁定同一 transaction
        的 verified unit artifact 與實際納入配額的 part 數；quota 在群組中途
        達標時明確記為 ``partial``，不把未納入的來源列假稱已發布。
        """
        if run_id <= 0 or group_id <= 0 or source_artifact_id <= 0:
            raise ValueError("bounded group receipt requires positive identifiers")
        if min(parsed_part_count, accepted_part_count, skipped_record_count) < 0:
            raise ValueError("bounded group receipt counts cannot be negative")
        if status == "done":
            if accepted_part_count != parsed_part_count:
                raise ValueError("done bounded receipt must accept every parsed row")
        elif status == "partial":
            if not 0 < accepted_part_count < parsed_part_count:
                raise ValueError("partial bounded receipt must retain a strict parsed subset")
        else:
            raise ValueError("unsupported bounded group receipt status")

        run = self.db._execute(
            "SELECT dataset_kind, target_parts, status FROM crawl_runs WHERE id = %s FOR UPDATE",
            (run_id,),
        ).fetchone()
        if (
            not run
            or run.get("dataset_kind") != "bounded"
            or _db_int(run.get("target_parts") or 0) != 10_000
            or run.get("status") != "running"
        ):
            raise RuntimeError(f"bounded group receipt run {run_id} is not active formal crawl")

        artifact = self.db._execute(
            "SELECT id FROM partsouq_http_artifacts "
            "WHERE id = %s AND crawl_run_id = %s "
            "AND capture_kind = 'live_http' AND page_type = 'unit' "
            "AND parser_name = 'parse_parts' AND verification_status = 'verified' FOR UPDATE",
            (source_artifact_id, run_id),
        ).fetchone()
        if not artifact:
            raise RuntimeError("bounded group receipt artifact is not verified unit evidence")

        evidence_counts = self.db._execute(
            "SELECT "
            "SUM(record_type = 'part') AS parsed_part_count, "
            "SUM(record_type = 'part' AND accepted = 1) AS accepted_part_count, "
            "COUNT(DISTINCT CASE WHEN record_type = 'part' AND accepted = 1 "
            "THEN part_id END) AS accepted_part_ids, "
            "SUM(record_type = 'quarantine_part') AS skipped_record_count "
            "FROM partsouq_artifact_records WHERE artifact_id = %s AND crawl_run_id = %s",
            (source_artifact_id, run_id),
        ).fetchone()
        if (
            _db_int((evidence_counts or {}).get("parsed_part_count", 0)) != parsed_part_count
            or _db_int((evidence_counts or {}).get("accepted_part_count", 0)) != accepted_part_count
            or _db_int((evidence_counts or {}).get("accepted_part_ids", 0)) != accepted_part_count
            or _db_int((evidence_counts or {}).get("skipped_record_count", 0))
            != skipped_record_count
        ):
            raise RuntimeError("bounded group receipt does not match its artifact records")

        self.db._execute(
            "INSERT INTO bounded_group_receipts ("
            "crawl_run_id, group_id, source_artifact_id, status, parsed_part_count, "
            "accepted_part_count, skipped_record_count) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) AS new "
            "ON DUPLICATE KEY UPDATE source_artifact_id = new.source_artifact_id, "
            "status = new.status, parsed_part_count = new.parsed_part_count, "
            "accepted_part_count = new.accepted_part_count, "
            "skipped_record_count = new.skipped_record_count, recorded_at = UTC_TIMESTAMP(6)",
            (
                run_id,
                group_id,
                source_artifact_id,
                status,
                parsed_part_count,
                accepted_part_count,
                skipped_record_count,
            ),
        )

    def seen(self, scope: str, key: str) -> None:
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
        return {cast(str, r["scope_key"]) for r in cur.fetchall()}

    def reset_scope(self, scope: str, run_key: str = "") -> None:
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

    def reset_run_state(self, run_key: str) -> None:
        """清除指定 run 的所有 scope，包含舊版或未來新增的 scope。"""
        self.db._execute("DELETE FROM crawl_state WHERE run_key = %s", (run_key,))

    def reset_group_receipts(self, run_key: str | None = None) -> None:
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

    def reset_part_markers(self, run_id: int) -> None:
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
        scope_brand: str | None = None,
        scope_model: str | None = None,
        scope_vehicle_year_floor: int | None = None,
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
        scope_brand, scope_model, scope_vehicle_year_floor = _validated_bounded_scope(
            scope_brand,
            scope_model,
            scope_vehicle_year_floor,
        )
        if dataset_kind == "bounded" and scope_brand is not None:
            desired_scope = self.db._execute(
                "SELECT scope_brand, scope_model, scope_vehicle_year_floor "
                "FROM catalog_desired_bounded_scope "
                "WHERE singleton_id = 1 FOR SHARE"
            ).fetchone()
            desired_identity = (
                cast(str | None, (desired_scope or {}).get("scope_brand")),
                cast(str | None, (desired_scope or {}).get("scope_model")),
                (
                    _db_int(desired_scope["scope_vehicle_year_floor"])
                    if desired_scope and desired_scope.get("scope_vehicle_year_floor") is not None
                    else None
                ),
            )
            requested_scope = (scope_brand, scope_model, scope_vehicle_year_floor)
            if desired_identity != requested_scope:
                raise RuntimeError(
                    "bounded run scope does not match catalog desired scope: "
                    f"desired={desired_identity!r}, requested={requested_scope!r}"
                )
        existing = self.db._execute(
            "SELECT id, status, dataset_kind, target_parts, scheduled_job_run_id, "
            "scope_brand, scope_model, scope_vehicle_year_floor FROM crawl_runs "
            "WHERE run_key = %s FOR UPDATE",
            (run_key,),
        ).fetchone()
        if existing:
            existing_target = existing.get("target_parts")
            if existing_target is not None:
                existing_target = _db_int(existing_target)
            requested_identity = (
                dataset_kind,
                target_parts,
                scheduled_job_run_id is not None,
                scope_brand,
                scope_model,
                scope_vehicle_year_floor,
            )
            try:
                existing_scope = _validated_bounded_scope(
                    cast(str | None, existing.get("scope_brand")),
                    cast(str | None, existing.get("scope_model")),
                    (
                        _db_int(existing["scope_vehicle_year_floor"])
                        if existing.get("scope_vehicle_year_floor") is not None
                        else None
                    ),
                )
            except ValueError as exc:
                raise RuntimeError(f"crawl run {run_key} has invalid persisted scope") from exc
            existing_identity = (
                existing.get("dataset_kind"),
                existing_target,
                existing.get("scheduled_job_run_id") is not None,
                *existing_scope,
            )
            if existing_identity != requested_identity:
                raise RuntimeError(
                    f"crawl run {run_key} identity mismatch: "
                    f"existing={existing_identity!r}, requested={requested_identity!r}"
                )
            if (
                existing.get("status") == "running"
                and existing.get("scheduled_job_run_id") != scheduled_job_run_id
            ):
                raise RuntimeError(
                    f"crawl run {run_key} is already owned by scheduler "
                    f"{existing.get('scheduled_job_run_id')!r}; refusing owner "
                    f"{scheduled_job_run_id!r}"
                )
            if fresh and existing.get("status") == "bounded_success":
                raise RuntimeError(
                    "published bounded run cannot be reused with --fresh; use a new run key"
                )
        if fresh:
            cur = self.db._execute(
                "INSERT INTO crawl_runs (run_key, started_at, status, dataset_kind, "
                "target_parts, scheduled_job_run_id, scope_brand, scope_model, "
                "scope_vehicle_year_floor) "
                "VALUES (%s, NOW(), 'running', %s, %s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE started_at = NOW(), finished_at = NULL, "
                "status = 'running', brands_ok = 0, models_ok = 0, vehicles_ok = 0, "
                "groups_ok = 0, parts_ok = 0, parts_new = 0, error_msg = NULL, "
                "evidence_status = 'missing', evidence_manifest_sha256 = NULL, "
                "evidence_dataset_sha256 = NULL, evidence_artifact_count = 0, "
                "evidence_record_count = 0, evidence_original_bytes = 0, "
                "evidence_stored_bytes = 0, evidence_verified_at = NULL, "
                "dataset_kind = VALUES(dataset_kind), target_parts = VALUES(target_parts), "
                "scheduled_job_run_id = VALUES(scheduled_job_run_id), "
                "scope_brand = VALUES(scope_brand), scope_model = VALUES(scope_model), "
                "scope_vehicle_year_floor = VALUES(scope_vehicle_year_floor), "
                "id = LAST_INSERT_ID(id)",
                (
                    run_key,
                    dataset_kind,
                    target_parts,
                    scheduled_job_run_id,
                    scope_brand,
                    scope_model,
                    scope_vehicle_year_floor,
                ),
            )
        else:
            cur = self.db._execute(
                "INSERT INTO crawl_runs (run_key, started_at, status, dataset_kind, "
                "target_parts, scheduled_job_run_id, scope_brand, scope_model, "
                "scope_vehicle_year_floor) "
                "VALUES (%s, NOW(), 'running', %s, %s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE "
                "status = IF(status IN ('success', 'bounded_success'), status, 'running'), "
                "finished_at = IF(status IN ('success', 'bounded_success'), finished_at, NULL), "
                "error_msg = IF(status IN ('success', 'bounded_success'), error_msg, NULL), "
                "dataset_kind = IF(status IN ('success', 'bounded_success'), "
                "dataset_kind, VALUES(dataset_kind)), "
                "target_parts = IF(status IN ('success', 'bounded_success'), "
                "target_parts, VALUES(target_parts)), "
                "scheduled_job_run_id = IF(status IN ('success', 'bounded_success'), "
                "scheduled_job_run_id, VALUES(scheduled_job_run_id)), "
                "scope_brand = IF(status IN ('success', 'bounded_success'), "
                "scope_brand, VALUES(scope_brand)), "
                "scope_model = IF(status IN ('success', 'bounded_success'), "
                "scope_model, VALUES(scope_model)), "
                "scope_vehicle_year_floor = IF(status IN ('success', 'bounded_success'), "
                "scope_vehicle_year_floor, VALUES(scope_vehicle_year_floor)), "
                "id = LAST_INSERT_ID(id)",
                (
                    run_key,
                    dataset_kind,
                    target_parts,
                    scheduled_job_run_id,
                    scope_brand,
                    scope_model,
                    scope_vehicle_year_floor,
                ),
            )
        run_id = cast(int, cur.lastrowid)
        if fresh:
            # --fresh 會重用同一 logical run id；舊 artifact／diagnostic
            # 不能混進新 HTTP attempt。CAS body 保留供其他 run 去重。
            body_hashes = tuple(
                cast(str, row["body_sha256"])
                for row in self.db._execute(
                    "SELECT DISTINCT body_sha256 FROM partsouq_http_artifacts "
                    "WHERE crawl_run_id = %s FOR UPDATE",
                    (run_id,),
                ).fetchall()
            )
            self.db._execute(
                "DELETE FROM partsouq_http_diagnostics WHERE crawl_run_id = %s",
                (run_id,),
            )
            self.db._execute(
                "DELETE FROM bounded_group_receipts WHERE crawl_run_id = %s",
                (run_id,),
            )
            self.db._execute(
                "DELETE FROM partsouq_http_artifacts WHERE crawl_run_id = %s",
                (run_id,),
            )
            if body_hashes:
                placeholders = ",".join("%s" for _body_hash in body_hashes)
                self.db._execute(
                    "DELETE FROM partsouq_response_bodies "
                    f"WHERE body_sha256 IN ({placeholders}) "
                    "AND NOT EXISTS (SELECT 1 FROM partsouq_http_artifacts "
                    "WHERE partsouq_http_artifacts.body_sha256 = "
                    "partsouq_response_bodies.body_sha256)",
                    body_hashes,
                )
        return run_id

    def run_status(self, run_id: int) -> str | None:
        """讀取指定 run 的目前狀態（commit 結果不明時用來對帳）。"""
        cur = self.db._execute("SELECT status FROM crawl_runs WHERE id = %s", (run_id,))
        row = cur.fetchone()
        return cast(str, row["status"]) if row else None

    def count_run_parts(self, run_id: int) -> int:
        """以 DB membership 作為筆數受限 run 的續爬配額基線。"""
        row = self.db._execute(
            "SELECT COUNT(*) AS row_count FROM parts WHERE seen_run_id = %s",
            (run_id,),
        ).fetchone()
        return _db_int((row or {}).get("row_count", 0))

    def discard_invalid_bounded_membership(
        self,
        run_id: int,
        *,
        scope_brand: str | None = None,
        scope_model: str | None = None,
        scope_vehicle_year_floor: int | None = None,
    ) -> int:
        """解除不符合正式 bounded 欄位／年份門檻的配額 membership。"""
        scope_brand, scope_model, scope_vehicle_year_floor = _validated_bounded_scope(
            scope_brand,
            scope_model,
            scope_vehicle_year_floor,
        )
        scope_clause = ""
        params: list[object] = [run_id]
        if scope_brand is not None:
            scope_clause = (
                "OR CAST(LOWER(TRIM(brand.name)) AS BINARY) <> CAST(%s AS BINARY) "
                "OR CAST(LOWER(TRIM(model.name)) AS BINARY) <> CAST(%s AS BINARY) "
                "OR (vehicle.production_to IS NOT NULL "
                "AND CAST(LEFT(vehicle.production_to, 4) AS UNSIGNED) < %s) "
            )
            params.extend((scope_brand, scope_model, scope_vehicle_year_floor))
        cur = self.db._execute(
            "UPDATE parts AS part "
            "JOIN groups_t AS source_group ON source_group.id = part.group_id "
            "JOIN categories AS category ON category.id = source_group.category_id "
            "JOIN vehicles AS vehicle ON vehicle.id = category.vehicle_id "
            "JOIN models AS model ON model.id = vehicle.model_id "
            "JOIN brands AS brand ON brand.id = model.brand_id "
            "SET part.seen_run_id = NULL WHERE part.seen_run_id = %s AND ("
            "NULLIF(TRIM(part.part_number), '') IS NULL "
            "OR NULLIF(TRIM(part.name), '') IS NULL "
            "OR NULLIF(TRIM(part.code), '') IS NULL "
            "OR NULLIF(TRIM(brand.name), '') IS NULL "
            "OR NULLIF(TRIM(model.name), '') IS NULL "
            "OR NULLIF(TRIM(vehicle.name), '') IS NULL "
            "OR NULLIF(TRIM(vehicle.model_code), '') IS NULL "
            "OR NULLIF(TRIM(vehicle.vid), '') IS NULL "
            "OR NULLIF(TRIM(category.cid), '') IS NULL "
            "OR NULLIF(TRIM(category.name), '') IS NULL "
            "OR NULLIF(TRIM(source_group.name), '') IS NULL "
            "OR NULLIF(TRIM(source_group.code), '') IS NULL "
            "OR NULLIF(TRIM(source_group.uid), '') IS NULL "
            "OR (vehicle.production_from IS NULL AND vehicle.production_to IS NULL "
            "AND part.part_from IS NULL AND part.part_to IS NULL) "
            "OR (part.part_to IS NOT NULL AND vehicle.production_from IS NOT NULL "
            "AND part.part_to < vehicle.production_from) "
            "OR (vehicle.production_to IS NOT NULL AND part.part_from IS NOT NULL "
            "AND vehicle.production_to < part.part_from) "
            f"{scope_clause}"
            "OR NULLIF(TRIM(source_group.url), '') IS NULL OR NOT ("
            f"{_canonical_unit_url_sql('source_group.url', 'source_group.uid')}))",
            tuple(params),
        )
        return cur.rowcount

    def resumable_bounded_run_key(
        self,
        target_parts: int,
        *,
        scheduled_job_run_id: int | None,
        scope_brand: str | None = None,
        scope_model: str | None = None,
        scope_vehicle_year_floor: int | None = None,
    ) -> str | None:
        """取得同 target、同 daemon/direct provenance 的最新未完成 run。

        新 daemon attempt 只能接手已明確失敗且有結束碼的舊 attempt；
        artifact 保留原 scheduler id，最終 evidence 會逐 attempt 驗證時間窗。
        已耗盡配額卻仍有 scope error 的 run 無法再取得爬取額度，必須拒絕並
        由 scheduler 建立新的 logical run，避免 remaining=0 的永久重試。
        """
        scope_brand, scope_model, scope_vehicle_year_floor = _validated_bounded_scope(
            scope_brand,
            scope_model,
            scope_vehicle_year_floor,
        )
        if scheduled_job_run_id is None:
            row = self.db._execute(
                "SELECT candidate.id, candidate.run_key, candidate.evidence_status, "
                "NOT (BINARY candidate.evidence_status = BINARY 'missing' OR "
                "BINARY candidate.evidence_status = BINARY 'collecting' OR "
                "BINARY candidate.evidence_status = BINARY 'verified') AS bad_run_status, "
                "EXISTS (SELECT 1 FROM partsouq_http_artifacts AS artifact "
                "WHERE artifact.crawl_run_id = candidate.id "
                "AND (BINARY artifact.sanitizer_version <> BINARY %s OR NOT ("
                "BINARY artifact.verification_status = BINARY 'verified' OR "
                "BINARY artifact.verification_status = BINARY 'superseded'))) "
                "AS bad_evidence, "
                "((SELECT COUNT(*) FROM parts AS quota_part "
                "WHERE quota_part.seen_run_id = candidate.id) >= candidate.target_parts "
                "AND EXISTS (SELECT 1 FROM crawl_state AS failed_scope "
                "WHERE failed_scope.run_key = candidate.run_key "
                "AND BINARY failed_scope.status = BINARY 'error')) "
                "AS exhausted_with_failures, "
                "EXISTS (SELECT 1 FROM groups_t AS receipt_group "
                "WHERE receipt_group.fetched_run_key = candidate.run_key "
                "AND (receipt_group.fetched_status IS NULL OR NOT ("
                "BINARY receipt_group.fetched_status = BINARY 'done' OR "
                "BINARY receipt_group.fetched_status = BINARY 'not_found') "
                "OR NULLIF(TRIM(receipt_group.url), '') IS NULL OR NOT ("
                f"{_canonical_unit_url_sql('receipt_group.url', 'receipt_group.uid')}))) "
                "AS bad_receipt, "
                "((SELECT COUNT(*) FROM parts AS quota_part "
                "WHERE quota_part.seen_run_id = candidate.id) >= candidate.target_parts "
                "AND ((SELECT COUNT(DISTINCT membership_part.group_id) "
                "FROM parts AS membership_part "
                "WHERE membership_part.seen_run_id = candidate.id) <> ("
                "SELECT COUNT(*) FROM bounded_group_receipts AS bounded_receipt "
                "WHERE bounded_receipt.crawl_run_id = candidate.id) OR EXISTS ("
                "SELECT 1 FROM parts AS membership_part "
                "WHERE membership_part.seen_run_id = candidate.id "
                "AND NOT EXISTS (SELECT 1 FROM bounded_group_receipts AS membership_receipt "
                "WHERE membership_receipt.crawl_run_id = candidate.id "
                "AND membership_receipt.group_id = membership_part.group_id)) OR EXISTS ("
                "SELECT 1 FROM bounded_group_receipts AS bounded_receipt "
                "WHERE bounded_receipt.crawl_run_id = candidate.id AND ("
                "NOT ((bounded_receipt.status = 'done' "
                "AND bounded_receipt.accepted_part_count = bounded_receipt.parsed_part_count) "
                "OR (bounded_receipt.status = 'partial' "
                "AND bounded_receipt.accepted_part_count > 0 "
                "AND bounded_receipt.accepted_part_count < bounded_receipt.parsed_part_count)) "
                "OR bounded_receipt.accepted_part_count <> (SELECT COUNT(*) "
                "FROM parts AS receipt_part "
                "WHERE receipt_part.seen_run_id = candidate.id "
                "AND receipt_part.group_id = bounded_receipt.group_id) "
                "OR NOT EXISTS (SELECT 1 FROM partsouq_http_artifacts AS receipt_artifact "
                "WHERE receipt_artifact.id = bounded_receipt.source_artifact_id "
                "AND receipt_artifact.crawl_run_id = candidate.id "
                "AND receipt_artifact.capture_kind = 'live_http' "
                "AND receipt_artifact.page_type = 'unit' "
                "AND receipt_artifact.parser_name = 'parse_parts' "
                "AND receipt_artifact.verification_status = 'verified'))))) "
                "AS bad_bounded_receipt "
                "FROM (SELECT id, run_key, evidence_status, target_parts FROM crawl_runs "
                "WHERE dataset_kind = 'bounded' "
                "AND target_parts = %s AND status IN ('running', 'error', 'interrupted') "
                "AND CAST(LOWER(TRIM(scope_brand)) AS BINARY) "
                "<=> CAST(%s AS BINARY) "
                "AND CAST(LOWER(TRIM(scope_model)) AS BINARY) "
                "<=> CAST(%s AS BINARY) "
                "AND scope_vehicle_year_floor <=> %s "
                "AND scheduled_job_run_id IS NULL "
                "ORDER BY started_at DESC, id DESC LIMIT 1) AS candidate",
                (
                    SANITIZER_VERSION,
                    target_parts,
                    scope_brand,
                    scope_model,
                    scope_vehicle_year_floor,
                ),
            ).fetchone()
        else:
            row = self.db._execute(
                "SELECT candidate.id, candidate.run_key, candidate.evidence_status, "
                "NOT (BINARY candidate.evidence_status = BINARY 'missing' OR "
                "BINARY candidate.evidence_status = BINARY 'collecting' OR "
                "BINARY candidate.evidence_status = BINARY 'verified') AS bad_run_status, "
                "EXISTS (SELECT 1 FROM partsouq_http_artifacts AS artifact "
                "WHERE artifact.crawl_run_id = candidate.id "
                "AND (BINARY artifact.sanitizer_version <> BINARY %s OR NOT ("
                "BINARY artifact.verification_status = BINARY 'verified' OR "
                "BINARY artifact.verification_status = BINARY 'superseded'))) "
                "AS bad_evidence, "
                "((SELECT COUNT(*) FROM parts AS quota_part "
                "WHERE quota_part.seen_run_id = candidate.id) >= candidate.target_parts "
                "AND EXISTS (SELECT 1 FROM crawl_state AS failed_scope "
                "WHERE failed_scope.run_key = candidate.run_key "
                "AND BINARY failed_scope.status = BINARY 'error')) "
                "AS exhausted_with_failures, "
                "EXISTS (SELECT 1 FROM groups_t AS receipt_group "
                "WHERE receipt_group.fetched_run_key = candidate.run_key "
                "AND (receipt_group.fetched_status IS NULL OR NOT ("
                "BINARY receipt_group.fetched_status = BINARY 'done' OR "
                "BINARY receipt_group.fetched_status = BINARY 'not_found') "
                "OR NULLIF(TRIM(receipt_group.url), '') IS NULL OR NOT ("
                f"{_canonical_unit_url_sql('receipt_group.url', 'receipt_group.uid')}))) "
                "AS bad_receipt, "
                "((SELECT COUNT(*) FROM parts AS quota_part "
                "WHERE quota_part.seen_run_id = candidate.id) >= candidate.target_parts "
                "AND ((SELECT COUNT(DISTINCT membership_part.group_id) "
                "FROM parts AS membership_part "
                "WHERE membership_part.seen_run_id = candidate.id) <> ("
                "SELECT COUNT(*) FROM bounded_group_receipts AS bounded_receipt "
                "WHERE bounded_receipt.crawl_run_id = candidate.id) OR EXISTS ("
                "SELECT 1 FROM parts AS membership_part "
                "WHERE membership_part.seen_run_id = candidate.id "
                "AND NOT EXISTS (SELECT 1 FROM bounded_group_receipts AS membership_receipt "
                "WHERE membership_receipt.crawl_run_id = candidate.id "
                "AND membership_receipt.group_id = membership_part.group_id)) OR EXISTS ("
                "SELECT 1 FROM bounded_group_receipts AS bounded_receipt "
                "WHERE bounded_receipt.crawl_run_id = candidate.id AND ("
                "NOT ((bounded_receipt.status = 'done' "
                "AND bounded_receipt.accepted_part_count = bounded_receipt.parsed_part_count) "
                "OR (bounded_receipt.status = 'partial' "
                "AND bounded_receipt.accepted_part_count > 0 "
                "AND bounded_receipt.accepted_part_count < bounded_receipt.parsed_part_count)) "
                "OR bounded_receipt.accepted_part_count <> (SELECT COUNT(*) "
                "FROM parts AS receipt_part "
                "WHERE receipt_part.seen_run_id = candidate.id "
                "AND receipt_part.group_id = bounded_receipt.group_id) "
                "OR NOT EXISTS (SELECT 1 FROM partsouq_http_artifacts AS receipt_artifact "
                "WHERE receipt_artifact.id = bounded_receipt.source_artifact_id "
                "AND receipt_artifact.crawl_run_id = candidate.id "
                "AND receipt_artifact.capture_kind = 'live_http' "
                "AND receipt_artifact.page_type = 'unit' "
                "AND receipt_artifact.parser_name = 'parse_parts' "
                "AND receipt_artifact.verification_status = 'verified'))))) "
                "AS bad_bounded_receipt "
                "FROM (SELECT cr.id, cr.run_key, cr.evidence_status, cr.target_parts "
                "FROM scheduled_job_runs AS current_job "
                "JOIN crawl_runs AS cr ON cr.dataset_kind = 'bounded' "
                "AND cr.target_parts = %s "
                "AND cr.status IN ('running', 'error', 'interrupted') "
                "AND CAST(LOWER(TRIM(cr.scope_brand)) AS BINARY) "
                "<=> CAST(%s AS BINARY) "
                "AND CAST(LOWER(TRIM(cr.scope_model)) AS BINARY) "
                "<=> CAST(%s AS BINARY) "
                "AND cr.scope_vehicle_year_floor <=> %s "
                "JOIN scheduled_job_runs AS previous_job "
                "ON previous_job.id = cr.scheduled_job_run_id "
                "AND previous_job.job_name = 'catalog' "
                "AND previous_job.trigger_mode = 'daemon' "
                "WHERE current_job.id = %s AND current_job.job_name = 'catalog' "
                "AND current_job.trigger_mode = 'daemon' AND current_job.status = 'running' "
                "AND (previous_job.id = current_job.id OR ("
                "previous_job.status = 'failed' AND previous_job.finished_at IS NOT NULL "
                "AND previous_job.exit_code IS NOT NULL AND previous_job.exit_code <> 0)) "
                "ORDER BY cr.started_at DESC, cr.id DESC LIMIT 1) AS candidate",
                (
                    SANITIZER_VERSION,
                    target_parts,
                    scope_brand,
                    scope_model,
                    scope_vehicle_year_floor,
                    scheduled_job_run_id,
                ),
            ).fetchone()
        if not row or not row.get("run_key"):
            return None
        incompatible = (
            _db_int(row.get("bad_run_status") or 0) != 0
            or _db_int(row.get("bad_evidence") or 0) != 0
            or _db_int(row.get("bad_receipt") or 0) != 0
            or _db_int(row.get("bad_bounded_receipt") or 0) != 0
            or _db_int(row.get("exhausted_with_failures") or 0) != 0
        )
        if incompatible:
            self.reject_run_evidence(_db_int(row["id"]))
            return None
        return str(row["run_key"])

    def _evidence_storage_usage(self, run_id: int) -> tuple[int, int]:
        """Return all artifact rows and distinct referenced CAS bytes for one run."""

        row = self.db._execute(
            "SELECT (SELECT COUNT(*) FROM partsouq_http_artifacts AS counted_artifact "
            "WHERE counted_artifact.crawl_run_id = %s) AS artifact_count, "
            "COALESCE((SELECT SUM(response_body.original_bytes) "
            "FROM partsouq_response_bodies AS response_body JOIN ("
            "SELECT DISTINCT referenced_artifact.body_sha256 "
            "FROM partsouq_http_artifacts AS referenced_artifact "
            "WHERE referenced_artifact.crawl_run_id = %s"
            ") AS referenced_body "
            "ON referenced_body.body_sha256 = response_body.body_sha256), 0) "
            "AS original_bytes",
            (run_id, run_id),
        ).fetchone()
        return (
            _db_int((row or {}).get("artifact_count", 0)),
            _db_int((row or {}).get("original_bytes", 0)),
        )

    def record_http_evidence(
        self,
        run_id: int,
        scheduled_job_run_id: int,
        *,
        page_type: str,
        public_url: str,
        raw_body_sha256: str,
        status_code: int,
        content_type: str,
        fetched_at: datetime,
        elapsed_ms: int,
        attempt: int,
        sanitized_body: SanitizedBody,
        parser_name: str,
        parser_version: str,
        parser_context: Mapping[str, object],
        parsed_records: Sequence[RecordEvidence],
        replayed_records: Sequence[RecordEvidence],
        accepted_records: Sequence[tuple[int, RecordEvidence]],
        malformed_rows: int = 0,
        skipped_record_count: int = 0,
    ) -> int:
        """Persist one secret-safe HTTP response and its replayed parser result.

        ``parsed_records`` is the complete live-parser result, including
        ``quarantine_part`` diagnostics. ``accepted_records`` is only the quota
        subset written to ``parts``; this keeps the final truncated unit page
        reproducible without pretending every parsed row was published.

        The caller must invoke this after the normalized upsert and before the
        group receipt commit. No commit occurs here, so body/artifact/records,
        normalized rows and receipt share one transaction boundary.
        """
        if page_type not in {"genuine", "locate", "pick", "vehicle", "category", "unit"}:
            raise ValueError("unsupported PartSouq evidence page_type")
        canonical_url = public_source_url(public_url)
        if canonical_url != public_url:
            raise ValueError("public_url must already be canonical and secret-free")
        _require_sha256(raw_body_sha256, "raw_body_sha256")
        _require_sha256(sanitized_body.body_sha256, "sanitized body_sha256")
        if status_code != 200 or not content_type.lower().startswith("text/html"):
            raise ValueError("verified PartSouq evidence requires an HTTP 200 HTML response")
        if elapsed_ms < 0 or attempt <= 0:
            raise ValueError("invalid PartSouq evidence timing metadata")
        if not parser_name.strip() or not parser_version.strip():
            raise ValueError("PartSouq evidence parser identity is required")
        if (page_type, parser_name) not in _EVIDENCE_PAGE_PARSERS:
            raise ValueError("PartSouq evidence page/parser contract mismatch")
        if malformed_rows != 0:
            raise ValueError("malformed parser rows cannot become verified evidence")
        # 合法空組（unit 頁零件表殼存在但站方零資料列，實證 TOYOTA1000
        # KP30 BODY STRIPE）是預期狀態：unit 頁允許 0 筆 parser 結果，
        # 且下方 replay 比對會驗證「原始解析與重放皆為空」的一致性。
        # 非 unit 頁的空結果仍視為版型異常。
        if not parsed_records and page_type != "unit":
            raise ValueError("PartSouq evidence cannot contain an empty parser result")
        quarantine_record_count = sum(
            record.record_type == "quarantine_part" for record in parsed_records
        )
        if (page_type == "unit" and skipped_record_count != quarantine_record_count) or (
            page_type != "unit" and quarantine_record_count
        ):
            raise ValueError("skipped_record_count does not match quarantine evidence rows")

        parser_context_json = canonical_parser_context(parser_name, parser_context)
        parser_context_sha256 = hashlib.sha256(parser_context_json).hexdigest()
        independently_replayed, replay_malformed, replay_skipped = replay_catalog_records(
            sanitized_body.body,
            parser_name=parser_name,
            parser_version=parser_version,
            context=parser_context,
        )
        if (replay_malformed, replay_skipped) != (malformed_rows, skipped_record_count):
            raise RuntimeError("sanitized HTML replay diagnostics do not match the live parser")

        parsed_keys = [_evidence_record_key(record) for record in parsed_records]
        replayed_keys = [_evidence_record_key(record) for record in replayed_records]
        independent_keys = [_evidence_record_key(record) for record in independently_replayed]
        if len(set((key[0], key[1]) for key in parsed_keys)) != len(parsed_keys):
            raise ValueError("duplicate parser evidence natural key")
        if sorted(parsed_keys) != sorted(replayed_keys) or sorted(parsed_keys) != sorted(
            independent_keys
        ):
            raise RuntimeError("sanitized HTML replay does not match the live parser result")

        parsed_by_key = {
            (record.record_type, record.natural_key_sha256): record for record in parsed_records
        }
        accepted_by_key: dict[tuple[str, str], int] = {}
        accepted_part_ids: set[int] = set()
        accepted_evidence: list[RecordEvidence] = []
        for part_id, record in accepted_records:
            key = (record.record_type, record.natural_key_sha256)
            if part_id <= 0 or record.record_type != "part":
                raise ValueError("accepted evidence must identify a persisted part row")
            if parsed_by_key.get(key) != record:
                raise ValueError("accepted evidence is not an exact parsed-record subset")
            if key in accepted_by_key or part_id in accepted_part_ids:
                raise ValueError("duplicate accepted PartSouq evidence row")
            accepted_by_key[key] = part_id
            accepted_part_ids.add(part_id)
            accepted_evidence.append(record)

        max_body_bytes = int(CRAWL["evidence_max_body_bytes"])
        if (
            sanitized_body.original_bytes <= 0
            or sanitized_body.original_bytes > max_body_bytes
            or sanitized_body.original_bytes != len(sanitized_body.body)
            or sanitized_body.stored_bytes != len(sanitized_body.compressed)
        ):
            raise ValueError(
                "sanitized PartSouq evidence body exceeds or violates its size contract"
            )
        if sanitized_body.sanitizer_version != SANITIZER_VERSION:
            raise ValueError("unsupported PartSouq evidence sanitizer version")
        restored = restore_sanitized_body(
            "zlib",
            sanitized_body.compressed,
            expected_size=sanitized_body.original_bytes,
            max_bytes=max_body_bytes,
        )
        if restored != sanitized_body.body:
            raise ValueError("sanitized PartSouq evidence zlib round trip mismatch")
        if hashlib.sha256(restored).hexdigest() != sanitized_body.body_sha256:
            raise ValueError("sanitized PartSouq evidence body hash mismatch")
        assert_no_secret_material(restored)

        run = self.db._execute(
            "SELECT cr.started_at, cr.status, cr.dataset_kind, cr.target_parts, "
            "cr.scheduled_job_run_id, sj.job_name AS scheduled_job_name, "
            "sj.trigger_mode AS scheduled_trigger_mode, sj.status AS scheduled_job_status "
            "FROM crawl_runs AS cr "
            "LEFT JOIN scheduled_job_runs AS sj ON sj.id = cr.scheduled_job_run_id "
            "WHERE cr.id = %s FOR UPDATE",
            (run_id,),
        ).fetchone()
        if not run or run.get("status") != "running":
            raise RuntimeError(f"evidence crawl run {run_id} is not running")
        if _db_int(run.get("scheduled_job_run_id") or 0) != scheduled_job_run_id:
            raise RuntimeError("evidence scheduler id does not match its crawl run")
        if (
            run.get("scheduled_job_name") != "catalog"
            or run.get("scheduled_trigger_mode") != "daemon"
            or run.get("scheduled_job_status") != "running"
        ):
            raise RuntimeError("evidence crawl run has invalid scheduler provenance")
        started_at = cast(datetime, run["started_at"])
        latest_allowed = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=5)
        if fetched_at < started_at or fetched_at > latest_allowed:
            raise RuntimeError("evidence fetched_at is outside the active crawl window")

        self.db._execute(
            "INSERT INTO partsouq_response_bodies ("
            "body_sha256, compression, body_blob, original_bytes, stored_bytes, "
            "sanitizer_version) VALUES (%s, 'zlib', %s, %s, %s, %s) AS new "
            "ON DUPLICATE KEY UPDATE body_sha256 = new.body_sha256",
            (
                sanitized_body.body_sha256,
                sanitized_body.compressed,
                sanitized_body.original_bytes,
                sanitized_body.stored_bytes,
                sanitized_body.sanitizer_version,
            ),
        )
        source_url_sha256 = hashlib.sha256(public_url.encode("utf-8")).hexdigest()
        parsed_records_sha256 = _evidence_record_set_sha256(parsed_records)
        accepted_records_sha256 = _evidence_record_set_sha256(accepted_evidence)
        artifact_cursor = self.db._execute(
            "INSERT INTO partsouq_http_artifacts ("
            "crawl_run_id, scheduled_job_run_id, capture_kind, page_type, "
            "public_source_url, source_url_sha256, raw_body_sha256, body_sha256, "
            "sanitizer_version, "
            "http_status, content_type, challenge_detected, fetched_at, elapsed_ms, attempt, "
            "parser_name, parser_version, parser_context_json, parser_context_sha256, "
            "malformed_row_count, skipped_record_count, "
            "parsed_record_count, parsed_records_sha256, accepted_record_count, "
            "accepted_records_sha256, verification_status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, %s, %s, %s, "
            "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending') AS new "
            "ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id), "
            "capture_kind = new.capture_kind, page_type = new.page_type, "
            "public_source_url = new.public_source_url, "
            "sanitizer_version = new.sanitizer_version, http_status = new.http_status, "
            "content_type = new.content_type, challenge_detected = new.challenge_detected, "
            "fetched_at = new.fetched_at, elapsed_ms = new.elapsed_ms, attempt = new.attempt, "
            "malformed_row_count = new.malformed_row_count, "
            "skipped_record_count = new.skipped_record_count, "
            "parsed_record_count = new.parsed_record_count, "
            "parsed_records_sha256 = new.parsed_records_sha256, "
            "accepted_record_count = new.accepted_record_count, "
            "accepted_records_sha256 = new.accepted_records_sha256, "
            "verification_status = 'pending', verified_at = NULL",
            (
                run_id,
                scheduled_job_run_id,
                "live_http",
                page_type,
                public_url,
                source_url_sha256,
                raw_body_sha256,
                sanitized_body.body_sha256,
                sanitized_body.sanitizer_version,
                status_code,
                content_type[:128],
                fetched_at,
                elapsed_ms,
                attempt,
                parser_name[:128],
                parser_version[:64],
                parser_context_json.decode("utf-8"),
                parser_context_sha256,
                malformed_rows,
                skipped_record_count,
                len(parsed_records),
                parsed_records_sha256,
                len(accepted_evidence),
                accepted_records_sha256,
            ),
        )
        artifact_id = cast(int, artifact_cursor.lastrowid)
        self.db._execute(
            "DELETE FROM partsouq_artifact_records WHERE artifact_id = %s",
            (artifact_id,),
        )
        self.db._executemany(
            "INSERT INTO partsouq_artifact_records ("
            "artifact_id, crawl_run_id, record_type, natural_key_sha256, "
            "parent_natural_key_sha256, record_sha256, accepted, part_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            [
                (
                    artifact_id,
                    run_id,
                    record.record_type,
                    record.natural_key_sha256,
                    record.parent_natural_key_sha256,
                    record.record_sha256,
                    1 if (record.record_type, record.natural_key_sha256) in accepted_by_key else 0,
                    accepted_by_key.get((record.record_type, record.natural_key_sha256)),
                )
                for record in parsed_records
            ],
        )
        self.db._execute(
            "UPDATE partsouq_http_artifacts SET verification_status = 'superseded' "
            "WHERE crawl_run_id = %s AND source_url_sha256 = %s AND page_type = %s "
            "AND parser_name = %s AND parser_context_sha256 = %s "
            "AND BINARY verification_status = BINARY 'verified' AND id <> %s",
            (
                run_id,
                source_url_sha256,
                page_type,
                parser_name,
                parser_context_sha256,
                artifact_id,
            ),
        )
        self.db._execute(
            "UPDATE partsouq_http_artifacts SET verification_status = 'verified', "
            "verified_at = UTC_TIMESTAMP(6) WHERE id = %s",
            (artifact_id,),
        )
        artifact_count, original_bytes = self._evidence_storage_usage(run_id)
        if artifact_count > int(CRAWL["evidence_max_artifacts"]) or original_bytes > int(
            CRAWL["evidence_max_run_bytes"]
        ):
            raise RuntimeError("PartSouq evidence run exceeded its fail-closed storage budget")
        self.db._execute(
            "UPDATE crawl_runs SET evidence_status = 'collecting', "
            "evidence_manifest_sha256 = NULL, evidence_dataset_sha256 = NULL, "
            "evidence_artifact_count = 0, evidence_record_count = 0, "
            "evidence_original_bytes = 0, evidence_stored_bytes = 0, "
            "evidence_verified_at = NULL WHERE id = %s",
            (run_id,),
        )
        return artifact_id

    def record_http_diagnostic(
        self,
        run_id: int,
        scheduled_job_run_id: int,
        group_id: int,
        *,
        public_url: str,
        raw_body_sha256: str,
        status_code: int,
        content_type: str,
        fetched_at: datetime,
        elapsed_ms: int,
        attempt: int,
        sanitized_body: SanitizedBody,
        parser_name: str,
        parser_version: str,
        parser_context: Mapping[str, object],
        reason: str,
    ) -> int:
        """保存與正式 evidence 隔離的 unit HTTP 失敗診斷。"""

        if reason not in {"http_not_found", "empty_parse"}:
            raise ValueError("unsupported PartSouq diagnostic reason")
        canonical_url = public_source_url(public_url)
        if canonical_url != public_url:
            raise ValueError("diagnostic public_url must be canonical and secret-free")
        _require_sha256(raw_body_sha256, "diagnostic raw_body_sha256")
        _require_sha256(sanitized_body.body_sha256, "diagnostic body_sha256")
        expected_status = 404 if reason == "http_not_found" else 200
        if status_code != expected_status or content_type != "text/html":
            raise ValueError("PartSouq diagnostic HTTP metadata does not match its reason")
        if elapsed_ms < 0 or attempt <= 0:
            raise ValueError("invalid PartSouq diagnostic timing metadata")
        if parser_name != "parse_parts" or not parser_version.strip():
            raise ValueError("unsupported PartSouq diagnostic parser identity")

        max_body_bytes = int(CRAWL["evidence_max_body_bytes"])
        if (
            sanitized_body.original_bytes <= 0
            or sanitized_body.original_bytes > max_body_bytes
            or sanitized_body.original_bytes != len(sanitized_body.body)
            or sanitized_body.stored_bytes != len(sanitized_body.compressed)
            or sanitized_body.sanitizer_version != SANITIZER_VERSION
        ):
            raise ValueError("sanitized PartSouq diagnostic violates its size contract")
        restored = restore_sanitized_body(
            "zlib",
            sanitized_body.compressed,
            expected_size=sanitized_body.original_bytes,
            max_bytes=max_body_bytes,
        )
        if restored != sanitized_body.body:
            raise ValueError("sanitized PartSouq diagnostic round trip mismatch")
        if hashlib.sha256(restored).hexdigest() != sanitized_body.body_sha256:
            raise ValueError("sanitized PartSouq diagnostic body hash mismatch")
        assert_no_secret_material(restored)

        parser_context_json = canonical_parser_context(parser_name, parser_context)
        parser_context_sha256 = hashlib.sha256(parser_context_json).hexdigest()
        run = self.db._execute(
            "SELECT cr.started_at, cr.status, cr.scheduled_job_run_id, "
            "sj.job_name, sj.trigger_mode, sj.status AS scheduled_status "
            "FROM crawl_runs AS cr JOIN scheduled_job_runs AS sj "
            "ON sj.id = cr.scheduled_job_run_id WHERE cr.id = %s FOR UPDATE",
            (run_id,),
        ).fetchone()
        if not run or run.get("status") != "running":
            raise RuntimeError(f"diagnostic crawl run {run_id} is not running")
        if _db_int(run.get("scheduled_job_run_id") or 0) != scheduled_job_run_id:
            raise RuntimeError("diagnostic scheduler id does not match its crawl run")
        if (
            run.get("job_name") != "catalog"
            or run.get("trigger_mode") != "daemon"
            or run.get("scheduled_status") != "running"
        ):
            raise RuntimeError("diagnostic crawl run has invalid scheduler provenance")
        started_at = cast(datetime, run["started_at"])
        latest_allowed = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=5)
        if fetched_at < started_at or fetched_at > latest_allowed:
            raise RuntimeError("diagnostic fetched_at is outside the active crawl window")

        source_url_sha256 = hashlib.sha256(public_url.encode("utf-8")).hexdigest()
        cursor = self.db._execute(
            "INSERT INTO partsouq_http_diagnostics ("
            "crawl_run_id, scheduled_job_run_id, group_id, reason, public_source_url, "
            "source_url_sha256, raw_body_sha256, body_sha256, compression, body_blob, "
            "original_bytes, stored_bytes, sanitizer_version, http_status, content_type, "
            "fetched_at, elapsed_ms, attempt, "
            "parser_name, parser_version, parser_context_json, parser_context_sha256) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'zlib', %s, %s, %s, %s, %s, "
            "%s, %s, %s, %s, %s, %s, %s, %s) AS new ON DUPLICATE KEY UPDATE "
            "id = LAST_INSERT_ID(id), scheduled_job_run_id = new.scheduled_job_run_id, "
            "public_source_url = new.public_source_url, "
            "source_url_sha256 = new.source_url_sha256, "
            "raw_body_sha256 = new.raw_body_sha256, body_sha256 = new.body_sha256, "
            "compression = new.compression, body_blob = new.body_blob, "
            "original_bytes = new.original_bytes, stored_bytes = new.stored_bytes, "
            "sanitizer_version = new.sanitizer_version, http_status = new.http_status, "
            "content_type = new.content_type, fetched_at = new.fetched_at, "
            "elapsed_ms = new.elapsed_ms, attempt = new.attempt, "
            "parser_version = new.parser_version, parser_context_json = new.parser_context_json, "
            "parser_context_sha256 = new.parser_context_sha256, "
            "updated_at = UTC_TIMESTAMP(6)",
            (
                run_id,
                scheduled_job_run_id,
                group_id,
                reason,
                public_url,
                source_url_sha256,
                raw_body_sha256,
                sanitized_body.body_sha256,
                sanitized_body.compressed,
                sanitized_body.original_bytes,
                sanitized_body.stored_bytes,
                sanitized_body.sanitizer_version,
                status_code,
                content_type[:128],
                fetched_at,
                elapsed_ms,
                attempt,
                parser_name,
                parser_version[:64],
                parser_context_json.decode("utf-8"),
                parser_context_sha256,
            ),
        )
        budget = self.db._execute(
            "SELECT COUNT(*) AS diagnostic_count, "
            "COALESCE(SUM(original_bytes), 0) AS original_bytes "
            "FROM partsouq_http_diagnostics WHERE crawl_run_id = %s",
            (run_id,),
        ).fetchone()
        if _db_int((budget or {}).get("diagnostic_count", 0)) > int(
            CRAWL["evidence_max_artifacts"]
        ) or _db_int((budget or {}).get("original_bytes", 0)) > int(
            CRAWL["evidence_max_run_bytes"]
        ):
            raise RuntimeError("PartSouq diagnostic run exceeded its fail-closed storage budget")
        return cast(int, cursor.lastrowid)

    def supersede_http_evidence(
        self,
        run_id: int,
        public_url: str,
        *,
        parser_name: str,
        parser_context: Mapping[str, object],
    ) -> int:
        """來源變成 404 時停用同 run、同 parser context 的舊正式 evidence。"""

        canonical_url = public_source_url(public_url)
        if canonical_url != public_url:
            raise ValueError("evidence public_url must be canonical and secret-free")
        parser_context_json = canonical_parser_context(parser_name, parser_context)
        cursor = self.db._execute(
            "UPDATE partsouq_http_artifacts SET verification_status = 'superseded' "
            "WHERE crawl_run_id = %s AND source_url_sha256 = %s "
            "AND parser_name = %s AND parser_context_sha256 = %s "
            "AND BINARY verification_status = BINARY 'verified'",
            (
                run_id,
                hashlib.sha256(public_url.encode("utf-8")).hexdigest(),
                parser_name,
                hashlib.sha256(parser_context_json).hexdigest(),
            ),
        )
        self.db._execute(
            "UPDATE crawl_runs SET evidence_status = 'collecting', "
            "evidence_manifest_sha256 = NULL, evidence_dataset_sha256 = NULL, "
            "evidence_artifact_count = 0, evidence_record_count = 0, "
            "evidence_original_bytes = 0, evidence_stored_bytes = 0, "
            "evidence_verified_at = NULL WHERE id = %s",
            (run_id,),
        )
        return cursor.rowcount

    def verify_run_evidence(self, run_id: int) -> tuple[str, str]:
        """Replay-check and seal a complete formal bounded evidence manifest."""
        run = self.db._execute(
            "SELECT cr.started_at, cr.status, cr.dataset_kind, cr.target_parts, "
            "cr.scope_brand, cr.scope_model, cr.scope_vehicle_year_floor, "
            "cr.scheduled_job_run_id, sj.job_name AS scheduled_job_name, "
            "sj.trigger_mode AS scheduled_trigger_mode, sj.status AS scheduled_job_status, "
            "(SELECT COUNT(*) FROM crawl_runs AS linked "
            "WHERE linked.scheduled_job_run_id = cr.scheduled_job_run_id) AS scheduled_crawl_count "
            "FROM crawl_runs AS cr "
            "LEFT JOIN scheduled_job_runs AS sj ON sj.id = cr.scheduled_job_run_id "
            "WHERE cr.id = %s FOR UPDATE",
            (run_id,),
        ).fetchone()
        if not run or (
            run.get("status") != "running"
            or run.get("dataset_kind") != "bounded"
            or _db_int(run.get("target_parts") or 0) != 10_000
        ):
            raise RuntimeError(f"run {run_id} is not an active formal bounded crawl")
        scheduled_job_run_id = _db_int(run.get("scheduled_job_run_id") or 0)
        if scheduled_job_run_id <= 0 or (
            run.get("scheduled_job_name") != "catalog"
            or run.get("scheduled_trigger_mode") != "daemon"
            or run.get("scheduled_job_status") != "running"
            or _db_int(run.get("scheduled_crawl_count") or 0) != 1
        ):
            raise RuntimeError(f"run {run_id} has invalid evidence scheduler provenance")
        summary = self._calculate_run_evidence(
            run_id,
            scheduled_job_run_id,
            cast(datetime, run["started_at"]),
            10_000,
            scope_brand=cast(str | None, run.get("scope_brand")),
            scope_model=cast(str | None, run.get("scope_model")),
            scope_vehicle_year_floor=(
                _db_int(run["scope_vehicle_year_floor"])
                if run.get("scope_vehicle_year_floor") is not None
                else None
            ),
        )
        self.db._execute(
            "UPDATE crawl_runs SET evidence_status = 'verified', "
            "evidence_manifest_sha256 = %s, evidence_dataset_sha256 = %s, "
            "evidence_artifact_count = %s, evidence_record_count = %s, "
            "evidence_original_bytes = %s, evidence_stored_bytes = %s, "
            "evidence_verified_at = UTC_TIMESTAMP(6) WHERE id = %s",
            (
                summary.manifest_sha256,
                summary.dataset_sha256,
                summary.artifact_count,
                summary.accepted_record_count,
                summary.original_bytes,
                summary.stored_bytes,
                run_id,
            ),
        )
        return summary.manifest_sha256, summary.dataset_sha256

    def audit_run_evidence(
        self,
        run_id: int,
        expected_parts: int = 10_000,
        *,
        allow_running_scheduler: bool = False,
        allow_failed_scheduler: bool = False,
    ) -> dict[str, object]:
        """Read back and independently replay a completed formal evidence run."""

        if expected_parts != 10_000:
            raise ValueError("formal evidence expected_parts must be exactly 10000")
        run = self.db._execute(
            "SELECT cr.started_at, cr.finished_at, cr.status, cr.dataset_kind, "
            "cr.target_parts, cr.scope_brand, cr.scope_model, "
            "cr.scope_vehicle_year_floor, cr.scheduled_job_run_id, cr.evidence_status, "
            "cr.evidence_manifest_sha256, cr.evidence_dataset_sha256, "
            "cr.evidence_artifact_count, cr.evidence_record_count, "
            "cr.evidence_original_bytes, cr.evidence_stored_bytes, "
            "cr.evidence_verified_at, sj.job_name AS scheduled_job_name, "
            "sj.trigger_mode AS scheduled_trigger_mode, sj.status AS scheduled_job_status, "
            "sj.exit_code AS scheduled_job_exit_code, "
            "sj.finished_at AS scheduled_job_finished_at, "
            "(SELECT COUNT(*) FROM crawl_runs AS linked "
            "WHERE linked.scheduled_job_run_id = cr.scheduled_job_run_id) "
            "AS scheduled_crawl_count FROM crawl_runs AS cr "
            "LEFT JOIN scheduled_job_runs AS sj ON sj.id = cr.scheduled_job_run_id "
            "WHERE cr.id = %s",
            (run_id,),
        ).fetchone()
        if not run or (
            run.get("status") != "bounded_success"
            or run.get("finished_at") is None
            or run.get("dataset_kind") != "bounded"
            or _db_int(run.get("target_parts") or 0) != expected_parts
        ):
            raise RuntimeError(f"run {run_id} is not a completed formal bounded crawl")
        scheduled_job_status = run.get("scheduled_job_status")
        scheduled_job_exit_code = run.get("scheduled_job_exit_code")
        scheduler_provenance_valid = (
            scheduled_job_status == "completed"
            and scheduled_job_exit_code is not None
            and _db_int(scheduled_job_exit_code) == 0
        ) or (allow_running_scheduler and scheduled_job_status == "running")
        if allow_failed_scheduler and scheduled_job_status == "failed":
            scheduler_provenance_valid = (
                scheduled_job_exit_code is not None
                and _db_int(scheduled_job_exit_code) != 0
                and run.get("scheduled_job_finished_at") is not None
            )
        if (
            run.get("scheduled_job_name") != "catalog"
            or run.get("scheduled_trigger_mode") != "daemon"
            or not scheduler_provenance_valid
            or _db_int(run.get("scheduled_crawl_count") or 0) != 1
        ):
            raise RuntimeError(f"run {run_id} has no completed scheduler provenance")
        self._assert_verified_run_evidence(
            run_id,
            run,
            expected_parts,
            allow_failed_current_scheduler=allow_failed_scheduler,
            use_bounded_snapshot=True,
        )
        return {
            "run_id": run_id,
            "expected_parts": expected_parts,
            "artifact_count": _db_int(run.get("evidence_artifact_count") or 0),
            "record_count": _db_int(run.get("evidence_record_count") or 0),
            "manifest_sha256": run["evidence_manifest_sha256"],
            "dataset_sha256": run["evidence_dataset_sha256"],
            "verified": True,
        }

    def _calculate_run_evidence(
        self,
        run_id: int,
        current_scheduled_job_run_id: int,
        run_started_at: datetime,
        target_parts: int,
        *,
        scope_brand: str | None = None,
        scope_model: str | None = None,
        scope_vehicle_year_floor: int | None = None,
        allow_failed_current_scheduler: bool = False,
        use_bounded_snapshot: bool = False,
    ) -> _RunEvidenceSummary:
        """Recompute the manifest, hierarchy chain and exact accepted dataset."""
        scope_brand, scope_model, scope_vehicle_year_floor = _validated_bounded_scope(
            scope_brand,
            scope_model,
            scope_vehicle_year_floor,
        )
        incomplete = self.db._execute(
            "SELECT COUNT(*) AS row_count FROM partsouq_http_artifacts "
            "WHERE crawl_run_id = %s "
            "AND NOT (BINARY verification_status = BINARY 'verified' OR "
            "BINARY verification_status = BINARY 'superseded')",
            (run_id,),
        ).fetchone()
        if _db_int((incomplete or {}).get("row_count", 0)):
            raise RuntimeError(f"run {run_id} contains incomplete HTTP evidence artifacts")

        stored_artifact_count, stored_original_bytes = self._evidence_storage_usage(run_id)
        if stored_artifact_count > int(
            CRAWL["evidence_max_artifacts"]
        ) or stored_original_bytes > int(CRAWL["evidence_max_run_bytes"]):
            raise RuntimeError(f"run {run_id} exceeds the HTTP evidence storage budget")

        artifacts = list(
            self.db._execute(
                "SELECT artifact.id, artifact.scheduled_job_run_id, artifact.capture_kind, "
                "artifact.page_type, artifact.public_source_url, artifact.source_url_sha256, "
                "artifact.raw_body_sha256, artifact.body_sha256, artifact.sanitizer_version, "
                "artifact.http_status, "
                "artifact.content_type, artifact.challenge_detected, artifact.fetched_at, "
                "artifact.elapsed_ms, artifact.attempt, artifact.parser_name, "
                "artifact.parser_version, artifact.parser_context_json, "
                "artifact.parser_context_sha256, artifact.malformed_row_count, "
                "artifact.skipped_record_count, artifact.parsed_record_count, "
                "artifact.parsed_records_sha256, artifact.accepted_record_count, "
                "artifact.accepted_records_sha256, artifact.verified_at, "
                "evidence_job.job_name AS evidence_job_name, "
                "evidence_job.trigger_mode AS evidence_trigger_mode, "
                "evidence_job.status AS evidence_job_status, "
                "evidence_job.exit_code AS evidence_job_exit_code, "
                "evidence_job.started_at AS evidence_job_started_at, "
                "evidence_job.finished_at AS evidence_job_finished_at "
                "FROM partsouq_http_artifacts AS artifact "
                "JOIN scheduled_job_runs AS evidence_job "
                "ON evidence_job.id = artifact.scheduled_job_run_id "
                "WHERE artifact.crawl_run_id = %s "
                "AND BINARY artifact.verification_status = BINARY 'verified' "
                "ORDER BY artifact.source_url_sha256, artifact.page_type, artifact.id",
                (run_id,),
            ).fetchall()
        )
        if not artifacts:
            raise RuntimeError(f"run {run_id} has no verified HTTP evidence")
        if len(artifacts) > int(CRAWL["evidence_max_artifacts"]):
            raise RuntimeError(f"run {run_id} exceeds the HTTP evidence artifact budget")

        body_rows = self.db._execute(
            "SELECT body.body_sha256, body.compression, body.body_blob, "
            "body.original_bytes, body.stored_bytes, body.sanitizer_version "
            "FROM partsouq_response_bodies AS body JOIN ("
            "SELECT DISTINCT body_sha256 FROM partsouq_http_artifacts "
            "WHERE crawl_run_id = %s "
            "AND BINARY verification_status = BINARY 'verified'"
            ") AS referenced ON referenced.body_sha256 = body.body_sha256",
            (run_id,),
        ).fetchall()
        bodies = {cast(str, row["body_sha256"]): row for row in body_rows}

        record_rows = self.db._execute(
            "SELECT records.artifact_id, records.record_type, "
            "records.natural_key_sha256, records.parent_natural_key_sha256, "
            "records.record_sha256, records.accepted, records.part_id "
            "FROM partsouq_artifact_records AS records "
            "JOIN partsouq_http_artifacts AS artifact ON artifact.id = records.artifact_id "
            "AND artifact.crawl_run_id = records.crawl_run_id "
            "WHERE records.crawl_run_id = %s "
            "AND BINARY artifact.verification_status = BINARY 'verified' "
            "ORDER BY records.artifact_id, records.record_type, records.natural_key_sha256",
            (run_id,),
        ).fetchall()
        records_by_artifact: dict[int, list[tuple[RecordEvidence, bool, int | None]]] = {}
        active_records: dict[tuple[str, str], RecordEvidence] = {}
        accepted_records: list[RecordEvidence] = []
        accepted_part_ids: set[int] = set()
        for row in record_rows:
            artifact_id = _db_int(row["artifact_id"])
            record = RecordEvidence(
                record_type=cast(str, row["record_type"]),
                natural_key_sha256=cast(str, row["natural_key_sha256"]),
                record_sha256=cast(str, row["record_sha256"]),
                parent_natural_key_sha256=cast(str | None, row["parent_natural_key_sha256"]),
            )
            key = (record.record_type, record.natural_key_sha256)
            if key in active_records:
                raise RuntimeError(f"run {run_id} contains duplicate active evidence key {key!r}")
            active_records[key] = record
            accepted = bool(_db_int(row.get("accepted") or 0))
            part_id = _db_int(row["part_id"]) if row.get("part_id") is not None else None
            if accepted:
                if record.record_type != "part" or part_id is None or part_id in accepted_part_ids:
                    raise RuntimeError(f"run {run_id} contains invalid accepted part evidence")
                accepted_part_ids.add(part_id)
                accepted_records.append(record)
            elif part_id is not None:
                raise RuntimeError(f"run {run_id} contains an unaccepted evidence part id")
            records_by_artifact.setdefault(artifact_id, []).append((record, accepted, part_id))

        for record in active_records.values():
            if record.record_type == "brand":
                if record.parent_natural_key_sha256 is not None:
                    raise RuntimeError(f"run {run_id} contains a parented brand evidence row")
                continue
            parent_type = _EVIDENCE_PARENT_TYPE.get(record.record_type)
            if parent_type is None or record.parent_natural_key_sha256 is None:
                raise RuntimeError(f"run {run_id} contains an unsupported evidence chain row")
            if (parent_type, record.parent_natural_key_sha256) not in active_records:
                raise RuntimeError(
                    f"run {run_id} has a broken {record.record_type}->{parent_type} evidence chain"
                )

        if use_bounded_snapshot:
            source_part_rows = self.db._execute(
                "SELECT bounded_part.part_id AS id, bounded_part.part_number, "
                "bounded_part.part_name AS name, bounded_part.code, bounded_part.note, "
                "bounded_part.quantity, bounded_part.part_range AS range_str, "
                "bounded_part.part_from, bounded_part.part_to, bounded_part.group_code, "
                "bounded_part.group_uid AS uid, bounded_part.category_cid AS cid, "
                "bounded_part.category_main AS category_name, "
                "bounded_part.vehicle_name, bounded_part.vehicle_code AS model_code, "
                "bounded_part.prod_period, bounded_part.production_from, "
                "bounded_part.production_to, bounded_part.engine, bounded_part.trim_name, "
                "bounded_part.vehicle_vid AS vid, bounded_part.model AS model_name, "
                "bounded_part.brand AS brand_name, bounded_part.source_url, "
                "bounded_part.evidence_record_sha256 "
                "FROM bounded_parts AS bounded_part "
                "WHERE bounded_part.crawl_run_id = %s ORDER BY bounded_part.part_id",
                (run_id,),
            ).fetchall()
        else:
            source_part_rows = self.db._execute(
                "SELECT part.id, part.part_number, part.name, part.code, part.note, "
                "part.quantity, part.range_str, part.part_from, part.part_to, "
                "source_group.code AS group_code, source_group.uid, "
                "category.cid, category.name AS category_name, "
                "vehicle.name AS vehicle_name, vehicle.model_code, "
                "vehicle.prod_period, vehicle.production_from, vehicle.production_to, "
                "vehicle.engine, vehicle.grade AS trim_name, vehicle.vid, "
                "model.name AS model_name, brand.name AS brand_name, "
                "source_group.url AS source_url "
                "FROM parts AS part "
                "JOIN groups_t AS source_group ON source_group.id = part.group_id "
                "JOIN categories AS category ON category.id = source_group.category_id "
                "JOIN vehicles AS vehicle ON vehicle.id = category.vehicle_id "
                "JOIN models AS model ON model.id = vehicle.model_id "
                "JOIN brands AS brand ON brand.id = model.brand_id "
                "WHERE part.seen_run_id = %s ORDER BY part.id",
                (run_id,),
            ).fetchall()
        source_part_ids = {_db_int(row["id"]) for row in source_part_rows}
        if (
            len(source_part_ids) != target_parts
            or len(accepted_part_ids) != target_parts
            or accepted_part_ids != source_part_ids
        ):
            raise RuntimeError(
                f"run {run_id} HTTP evidence coverage mismatch: "
                f"source={len(source_part_ids)}, accepted={len(accepted_part_ids)}, "
                f"target={target_parts}"
            )
        if scope_brand is not None:
            invalid_scope_rows = 0
            for row in source_part_rows:
                production_to = row.get("production_to")
                try:
                    production_to_year = (
                        int(str(production_to)[:4]) if production_to is not None else None
                    )
                except ValueError:
                    production_to_year = 0
                if (
                    str(row.get("brand_name") or "").strip().casefold() != scope_brand
                    or str(row.get("model_name") or "").strip().casefold() != scope_model
                    or (
                        production_to_year is not None
                        and production_to_year < cast(int, scope_vehicle_year_floor)
                    )
                ):
                    invalid_scope_rows += 1
            if invalid_scope_rows:
                raise RuntimeError(
                    f"run {run_id} declared model scope mismatch: "
                    f"invalid={invalid_scope_rows}, target={target_parts}"
                )
        accepted_by_part_id = {
            part_id: (record, artifact_id)
            for artifact_id, artifact_values in records_by_artifact.items()
            for record, is_accepted, part_id in artifact_values
            if is_accepted and part_id is not None
        }
        artifact_by_id = {_db_int(artifact["id"]): artifact for artifact in artifacts}
        for row in source_part_rows:
            part_id = _db_int(row["id"])
            brand_key = brand_natural_key(row["brand_name"])
            model_key = model_natural_key(row["brand_name"], row["model_name"])
            vehicle_key = vehicle_natural_key(
                row["brand_name"],
                row["model_name"],
                name=row["vehicle_name"],
                model_code=row["model_code"],
                prod_period=row["prod_period"],
                production_from=row["production_from"],
                production_to=row["production_to"],
                engine=row["engine"],
                trim_name=row["trim_name"],
                vid=row["vid"],
            )
            category_key = category_natural_key(
                vehicle_key,
                row["cid"],
                row["category_name"],
            )
            group_key = group_natural_key(
                category_key,
                row["group_code"],
                row["uid"],
            )
            part_key = part_natural_key(
                group_key,
                row["part_number"],
                row["range_str"],
            )
            required_chain = (
                ("brand", canonical_sha256(brand_key)),
                ("model", canonical_sha256(model_key)),
                ("vehicle", canonical_sha256(vehicle_key)),
                ("category", canonical_sha256(category_key)),
                ("group", canonical_sha256(group_key)),
            )
            missing_chain = [
                record_type
                for record_type, natural_key_sha256 in required_chain
                if (record_type, natural_key_sha256) not in active_records
            ]
            if missing_chain:
                raise RuntimeError(
                    f"run {run_id} part {part_id} lacks live hierarchy evidence: "
                    f"{','.join(missing_chain)}"
                )
            accepted_record, accepted_artifact_id = accepted_by_part_id[part_id]
            accepted_artifact = artifact_by_id[accepted_artifact_id]
            source_url = cast(str, row["source_url"])
            accepted_source_url = cast(str, accepted_artifact["public_source_url"])
            try:
                normalized_source_url = public_source_url(source_url)
                normalized_accepted_source_url = public_source_url(accepted_source_url)
            except ValueError as error:
                raise RuntimeError(
                    f"run {run_id} part {part_id} evidence source URL is invalid"
                ) from error
            if (
                normalized_source_url != source_url
                or normalized_accepted_source_url != accepted_source_url
                or normalized_source_url != normalized_accepted_source_url
            ):
                raise RuntimeError(f"run {run_id} part {part_id} evidence source URL mismatch")
            expected_hash = canonical_sha256(
                {
                    "group": group_key,
                    "part_number": row["part_number"],
                    "name": row["name"],
                    "code": row["code"],
                    "note": row["note"],
                    "quantity": row["quantity"],
                    "range_str": row["range_str"],
                    "part_from": row["part_from"],
                    "part_to": row["part_to"],
                }
            )
            if (
                accepted_record.natural_key_sha256 != canonical_sha256(part_key)
                or accepted_record.parent_natural_key_sha256 != canonical_sha256(group_key)
                or accepted_record.record_sha256 != expected_hash
            ):
                raise RuntimeError(f"run {run_id} part {part_id} evidence payload hash mismatch")
            if use_bounded_snapshot and (
                row.get("evidence_record_sha256") != accepted_record.record_sha256
            ):
                raise RuntimeError(f"run {run_id} part {part_id} snapshot evidence digest mismatch")

        manifest_items: list[dict[str, object]] = []
        original_bytes = 0
        stored_bytes = 0
        latest_allowed = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=5)
        for artifact in artifacts:
            artifact_id = _db_int(artifact["id"])
            artifact_scheduler_id = _db_int(artifact.get("scheduled_job_run_id") or 0)
            evidence_job_status = artifact.get("evidence_job_status")
            evidence_job_exit_code = artifact.get("evidence_job_exit_code")
            current_attempt = artifact_scheduler_id == current_scheduled_job_run_id
            scheduler_provenance_valid = (
                current_attempt
                and (
                    evidence_job_status == "running"
                    or (
                        evidence_job_status == "completed"
                        and evidence_job_exit_code is not None
                        and _db_int(evidence_job_exit_code) == 0
                    )
                    or (
                        allow_failed_current_scheduler
                        and evidence_job_status == "failed"
                        and evidence_job_exit_code is not None
                        and _db_int(evidence_job_exit_code) != 0
                        and artifact.get("evidence_job_finished_at") is not None
                    )
                )
            ) or (
                not current_attempt
                and evidence_job_status == "failed"
                and evidence_job_exit_code is not None
                and _db_int(evidence_job_exit_code) != 0
            )
            if (
                artifact.get("capture_kind") != "live_http"
                or artifact.get("evidence_job_name") != "catalog"
                or artifact.get("evidence_trigger_mode") != "daemon"
                or not scheduler_provenance_valid
                or _db_int(artifact.get("http_status") or 0) != 200
                or not str(artifact.get("content_type") or "").lower().startswith("text/html")
                or bool(_db_int(artifact.get("challenge_detected") or 0))
                or artifact.get("sanitizer_version") != SANITIZER_VERSION
                or _db_int(artifact.get("malformed_row_count") or 0) != 0
                or artifact.get("verified_at") is None
                or (artifact.get("page_type"), artifact.get("parser_name"))
                not in _EVIDENCE_PAGE_PARSERS
            ):
                raise RuntimeError(f"run {run_id} artifact {artifact_id} is not verified live HTTP")
            fetched_at = cast(datetime, artifact["fetched_at"])
            evidence_job_started_at = cast(datetime, artifact["evidence_job_started_at"])
            evidence_job_finished_at = cast(datetime | None, artifact["evidence_job_finished_at"])
            artifact_latest_allowed = (
                evidence_job_finished_at + timedelta(minutes=5)
                if evidence_job_finished_at is not None
                else latest_allowed
            )
            if (
                fetched_at < run_started_at
                or fetched_at < evidence_job_started_at
                or fetched_at > artifact_latest_allowed
                or (evidence_job_status != "running" and evidence_job_finished_at is None)
            ):
                raise RuntimeError(f"run {run_id} artifact {artifact_id} is outside the run window")
            url = cast(str, artifact["public_source_url"])
            if public_source_url(url) != url:
                raise RuntimeError(f"run {run_id} artifact {artifact_id} retained a secret URL")
            if hashlib.sha256(url.encode()).hexdigest() != artifact.get("source_url_sha256"):
                raise RuntimeError(f"run {run_id} artifact {artifact_id} source URL hash mismatch")
            _require_sha256(cast(str, artifact["raw_body_sha256"]), "raw_body_sha256")

            body_sha256 = cast(str, artifact["body_sha256"])
            body = bodies.get(body_sha256)
            if body is None:
                raise RuntimeError(f"run {run_id} artifact {artifact_id} has no CAS body")
            body_original_bytes = _db_int(body["original_bytes"])
            body_stored_bytes = _db_int(body["stored_bytes"])
            compressed = cast(bytes, body["body_blob"])
            if (
                body_original_bytes <= 0
                or body_original_bytes > int(CRAWL["evidence_max_body_bytes"])
                or body_stored_bytes != len(compressed)
            ):
                raise RuntimeError(f"run {run_id} artifact {artifact_id} violates the body budget")
            restored = restore_sanitized_body(
                cast(str, body["compression"]),
                compressed,
                expected_size=body_original_bytes,
                max_bytes=int(CRAWL["evidence_max_body_bytes"]),
            )
            if (
                len(restored) != body_original_bytes
                or hashlib.sha256(restored).hexdigest() != body_sha256
            ):
                raise RuntimeError(f"run {run_id} artifact {artifact_id} CAS body mismatch")
            assert_no_secret_material(restored)

            artifact_records = records_by_artifact.get(artifact_id, [])
            parsed = [record for record, _accepted, _part_id in artifact_records]
            artifact_accepted_records = [
                record for record, is_accepted, _part_id in artifact_records if is_accepted
            ]
            quarantine_record_count = sum(
                record.record_type == "quarantine_part" for record in parsed
            )
            raw_context = artifact.get("parser_context_json")
            if isinstance(raw_context, Mapping):
                parser_context = dict(raw_context)
            else:
                if isinstance(raw_context, (bytes, bytearray)):
                    raw_context = bytes(raw_context).decode("utf-8")
                try:
                    parser_context = json.loads(cast(str, raw_context))
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    raise RuntimeError(
                        f"run {run_id} artifact {artifact_id} parser context is invalid"
                    ) from error
            if not isinstance(parser_context, dict):
                raise RuntimeError(
                    f"run {run_id} artifact {artifact_id} parser context is not an object"
                )
            try:
                canonical_context = canonical_parser_context(
                    cast(str, artifact["parser_name"]), parser_context
                )
            except (KeyError, TypeError, ValueError) as error:
                raise RuntimeError(
                    f"run {run_id} artifact {artifact_id} parser context is invalid"
                ) from error
            if hashlib.sha256(canonical_context).hexdigest() != artifact.get(
                "parser_context_sha256"
            ):
                raise RuntimeError(
                    f"run {run_id} artifact {artifact_id} parser context hash mismatch"
                )
            try:
                replayed, replay_malformed, replay_skipped = replay_catalog_records(
                    restored,
                    parser_name=cast(str, artifact["parser_name"]),
                    parser_version=cast(str, artifact["parser_version"]),
                    context=parser_context,
                )
            except (KeyError, TypeError, ValueError) as error:
                raise RuntimeError(
                    f"run {run_id} artifact {artifact_id} parser replay is invalid"
                ) from error
            if (
                len(parsed) != _db_int(artifact["parsed_record_count"])
                or len(artifact_accepted_records) != _db_int(artifact["accepted_record_count"])
                or (
                    artifact.get("page_type") == "unit"
                    and quarantine_record_count != _db_int(artifact["skipped_record_count"])
                )
                or (artifact.get("page_type") != "unit" and quarantine_record_count != 0)
                or replay_malformed != _db_int(artifact["malformed_row_count"])
                or replay_skipped != _db_int(artifact["skipped_record_count"])
                or sorted(map(_evidence_record_key, replayed))
                != sorted(map(_evidence_record_key, parsed))
                or _evidence_record_set_sha256(parsed) != artifact.get("parsed_records_sha256")
                or _evidence_record_set_sha256(artifact_accepted_records)
                != artifact.get("accepted_records_sha256")
            ):
                raise RuntimeError(f"run {run_id} artifact {artifact_id} parser evidence mismatch")
            original_bytes += body_original_bytes
            stored_bytes += body_stored_bytes
            manifest_items.append(
                {
                    "capture_kind": artifact["capture_kind"],
                    "scheduled_job_run_id": artifact["scheduled_job_run_id"],
                    "page_type": artifact["page_type"],
                    "source_url_sha256": artifact["source_url_sha256"],
                    "raw_body_sha256": artifact["raw_body_sha256"],
                    "body_sha256": body_sha256,
                    "sanitizer_version": artifact["sanitizer_version"],
                    "http_status": artifact["http_status"],
                    "content_type": artifact["content_type"],
                    "fetched_at": fetched_at.isoformat(timespec="microseconds"),
                    "elapsed_ms": artifact["elapsed_ms"],
                    "attempt": artifact["attempt"],
                    "parser_name": artifact["parser_name"],
                    "parser_version": artifact["parser_version"],
                    "parser_context_sha256": artifact["parser_context_sha256"],
                    "parsed_record_count": len(parsed),
                    "parsed_records_sha256": artifact["parsed_records_sha256"],
                    "accepted_record_count": len(artifact_accepted_records),
                    "accepted_records_sha256": artifact["accepted_records_sha256"],
                }
            )

        if original_bytes > int(CRAWL["evidence_max_run_bytes"]):
            raise RuntimeError(f"run {run_id} exceeds the HTTP evidence byte budget")
        manifest: object = manifest_items
        if scope_brand is not None:
            manifest = {
                "scope": {
                    "brand": scope_brand,
                    "model": scope_model,
                    "vehicle_year_floor": scope_vehicle_year_floor,
                },
                "artifacts": manifest_items,
            }
        return _RunEvidenceSummary(
            manifest_sha256=canonical_sha256(manifest),
            dataset_sha256=_evidence_record_set_sha256(accepted_records),
            artifact_count=len(artifacts),
            accepted_record_count=len(accepted_records),
            original_bytes=original_bytes,
            stored_bytes=stored_bytes,
        )

    def _assert_verified_run_evidence(
        self,
        run_id: int,
        run: Mapping[str, object],
        target_parts: int,
        *,
        allow_failed_current_scheduler: bool = False,
        use_bounded_snapshot: bool = False,
    ) -> None:
        if (
            run.get("evidence_status") != "verified"
            or run.get("evidence_verified_at") is None
            or not run.get("evidence_manifest_sha256")
            or not run.get("evidence_dataset_sha256")
        ):
            raise RuntimeError(f"bounded run {run_id} has no verified live HTTP evidence")
        summary = self._calculate_run_evidence(
            run_id,
            _db_int(run.get("scheduled_job_run_id") or 0),
            cast(datetime, run["started_at"]),
            target_parts,
            scope_brand=cast(str | None, run.get("scope_brand")),
            scope_model=cast(str | None, run.get("scope_model")),
            scope_vehicle_year_floor=(
                _db_int(run["scope_vehicle_year_floor"])
                if run.get("scope_vehicle_year_floor") is not None
                else None
            ),
            allow_failed_current_scheduler=allow_failed_current_scheduler,
            use_bounded_snapshot=use_bounded_snapshot,
        )
        if (
            summary.manifest_sha256 != run.get("evidence_manifest_sha256")
            or summary.dataset_sha256 != run.get("evidence_dataset_sha256")
            or summary.artifact_count != _db_int(run.get("evidence_artifact_count") or 0)
            or summary.accepted_record_count != _db_int(run.get("evidence_record_count") or 0)
            or summary.original_bytes != _db_int(run.get("evidence_original_bytes") or 0)
            or summary.stored_bytes != _db_int(run.get("evidence_stored_bytes") or 0)
        ):
            raise RuntimeError(f"bounded run {run_id} HTTP evidence manifest/dataset mismatch")

    def _assert_bounded_group_receipts(self, run_id: int, target_parts: int) -> None:
        """確認每個待發布群組都有可重放的 immutable receipt。"""
        receipt_summary = self.db._execute(
            "WITH source_groups AS ("
            "SELECT part.group_id, COUNT(*) AS source_part_count "
            "FROM parts AS part WHERE part.seen_run_id = %s GROUP BY part.group_id"
            "), receipt_artifact_counts AS ("
            "SELECT receipt.group_id, receipt.source_artifact_id, "
            "SUM(artifact_record.record_type = 'part') AS parsed_part_count, "
            "SUM(artifact_record.record_type = 'part' AND artifact_record.accepted = 1) "
            "AS accepted_part_count, "
            "COUNT(DISTINCT CASE WHEN artifact_record.record_type = 'part' "
            "AND artifact_record.accepted = 1 THEN artifact_record.part_id END) "
            "AS accepted_part_id_count, "
            "SUM(artifact_record.record_type = 'quarantine_part') "
            "AS skipped_record_count "
            "FROM bounded_group_receipts AS receipt "
            "JOIN source_groups AS source_group ON source_group.group_id = receipt.group_id "
            "JOIN partsouq_artifact_records AS artifact_record "
            "ON artifact_record.artifact_id = receipt.source_artifact_id "
            "AND artifact_record.crawl_run_id = receipt.crawl_run_id "
            "WHERE receipt.crawl_run_id = %s "
            "GROUP BY receipt.group_id, receipt.source_artifact_id"
            "), receipt_source_members AS ("
            "SELECT receipt.group_id, COUNT(DISTINCT part.id) AS accepted_source_part_count "
            "FROM bounded_group_receipts AS receipt "
            "JOIN source_groups AS source_group ON source_group.group_id = receipt.group_id "
            "JOIN partsouq_artifact_records AS artifact_record "
            "ON artifact_record.artifact_id = receipt.source_artifact_id "
            "AND artifact_record.crawl_run_id = receipt.crawl_run_id "
            "AND artifact_record.record_type = 'part' AND artifact_record.accepted = 1 "
            "JOIN parts AS part ON part.id = artifact_record.part_id "
            "AND part.group_id = source_group.group_id AND part.seen_run_id = %s "
            "GROUP BY receipt.group_id"
            "), receipt_integrity AS ("
            "SELECT source_group.group_id, source_group.source_part_count, "
            "CASE WHEN receipt.source_artifact_id IS NOT NULL "
            "AND artifact.crawl_run_id = %s "
            "AND artifact.capture_kind = 'live_http' AND artifact.page_type = 'unit' "
            "AND artifact.parser_name = 'parse_parts' "
            "AND artifact.verification_status = 'verified' AND artifact.http_status = 200 "
            "AND artifact.challenge_detected = 0 "
            "AND LOWER(artifact.content_type) LIKE 'text/html%%' "
            "AND artifact.malformed_row_count = 0 AND artifact.verified_at IS NOT NULL "
            "AND receipt_artifact_counts.parsed_part_count = receipt.parsed_part_count "
            "AND receipt_artifact_counts.accepted_part_count = receipt.accepted_part_count "
            "AND receipt_artifact_counts.accepted_part_id_count = receipt.accepted_part_count "
            "AND receipt_artifact_counts.skipped_record_count = receipt.skipped_record_count "
            "AND artifact.parsed_record_count = (receipt.parsed_part_count "
            "+ receipt.skipped_record_count) "
            "AND artifact.accepted_record_count = receipt.accepted_part_count "
            "AND artifact.skipped_record_count = receipt.skipped_record_count "
            "AND receipt_source_members.accepted_source_part_count = receipt.accepted_part_count "
            "AND source_group.source_part_count = receipt.accepted_part_count "
            "AND ((receipt.status = 'done' "
            "AND receipt.accepted_part_count = receipt.parsed_part_count) "
            "OR (receipt.status = 'partial' AND receipt.accepted_part_count > 0 "
            "AND receipt.accepted_part_count < receipt.parsed_part_count)) "
            "THEN 1 ELSE 0 END AS is_verified "
            "FROM source_groups AS source_group "
            "LEFT JOIN bounded_group_receipts AS receipt "
            "ON receipt.crawl_run_id = %s AND receipt.group_id = source_group.group_id "
            "LEFT JOIN partsouq_http_artifacts AS artifact "
            "ON artifact.id = receipt.source_artifact_id "
            "AND artifact.crawl_run_id = receipt.crawl_run_id "
            "LEFT JOIN receipt_artifact_counts "
            "ON receipt_artifact_counts.group_id = source_group.group_id "
            "AND receipt_artifact_counts.source_artifact_id = receipt.source_artifact_id "
            "LEFT JOIN receipt_source_members "
            "ON receipt_source_members.group_id = source_group.group_id"
            ") SELECT COUNT(*) AS group_count, "
            "COALESCE(SUM(source_part_count), 0) AS source_part_count, "
            "COALESCE(SUM(is_verified), 0) AS verified_group_count, "
            "COALESCE(SUM(CASE WHEN is_verified = 1 THEN source_part_count ELSE 0 END), 0) "
            "AS verified_part_count FROM receipt_integrity",
            (run_id, run_id, run_id, run_id, run_id),
        ).fetchone()
        group_count = _db_int((receipt_summary or {}).get("group_count", 0))
        source_part_count = _db_int((receipt_summary or {}).get("source_part_count", 0))
        verified_group_count = _db_int((receipt_summary or {}).get("verified_group_count", 0))
        verified_part_count = _db_int((receipt_summary or {}).get("verified_part_count", 0))
        if (
            group_count <= 0
            or source_part_count != target_parts
            or verified_group_count != group_count
            or verified_part_count != target_parts
        ):
            raise RuntimeError(
                f"bounded run {run_id} has invalid group receipts: "
                f"groups={verified_group_count}/{group_count}, "
                f"parts={verified_part_count}/{source_part_count}"
            )

    def reject_run_evidence(self, run_id: int) -> None:
        """讓不可能完成的 bounded run 不再被後續 scheduler 接手。"""
        self.db._execute(
            "UPDATE crawl_runs SET evidence_status = 'rejected', status = 'error', "
            "finished_at = COALESCE(finished_at, UTC_TIMESTAMP()), "
            "error_msg = CASE WHEN NULLIF(TRIM(error_msg), '') IS NULL "
            "THEN 'bounded run evidence rejected; a new logical run is required' "
            "ELSE error_msg END, "
            "evidence_manifest_sha256 = NULL, evidence_dataset_sha256 = NULL, "
            "evidence_artifact_count = 0, evidence_record_count = 0, "
            "evidence_original_bytes = 0, evidence_stored_bytes = 0, "
            "evidence_verified_at = NULL WHERE id = %s "
            "AND status IN ('running', 'error', 'interrupted')",
            (run_id,),
        )

    def finish_run(
        self,
        run_id: int,
        status: str,
        counts: Mapping[str, int],
        error: str | None = None,
    ) -> None:
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

    def archive_full_candidate_parts(
        self,
        run_id: int,
        *,
        expected_scheduled_job_run_id: int | None = None,
    ) -> int:
        """在同一交易內更新 full candidate archive。

        normalized tables 可被後續 failed/partial attempt 原地 upsert；因此
        current view 不直接 join 它們。這個 archive 僅保留後續診斷或重建所需的
        raw full 結果，不能當成正式資料或 VIN mapping 來源。舊表名
        published_parts 為既有 schema，相容期間不改名；每次完整 full run 會
        以本次 logical run 取代 archive，並保存上一份 archive。與
        finish_run(success) 同次 commit，任一步失敗都 rollback。
        """
        run = self.db._execute(
            "SELECT cr.run_key, cr.dataset_kind, cr.target_parts, cr.status, "
            "cr.scheduled_job_run_id, sj.job_name AS scheduled_job_name, "
            "sj.trigger_mode AS scheduled_trigger_mode, sj.status AS scheduled_job_status, "
            "(SELECT COUNT(*) FROM crawl_runs AS linked "
            "WHERE linked.scheduled_job_run_id = cr.scheduled_job_run_id) "
            "AS scheduled_crawl_count "
            "FROM crawl_runs AS cr "
            "LEFT JOIN scheduled_job_runs AS sj ON sj.id = cr.scheduled_job_run_id "
            "WHERE cr.id = %s FOR UPDATE",
            (run_id,),
        ).fetchone()
        if not run:
            raise RuntimeError(f"full run {run_id} does not exist")
        if (
            run.get("dataset_kind") != "full"
            or run.get("target_parts") is not None
            or run.get("status") != "running"
        ):
            raise RuntimeError(f"run {run_id} is not a matching running full crawl")
        if run.get("scheduled_job_run_id") is None or (
            run.get("scheduled_job_name") != "catalog"
            or run.get("scheduled_trigger_mode") != "daemon"
            or run.get("scheduled_job_status") != "running"
            or _db_int(run.get("scheduled_crawl_count") or 0) != 1
        ):
            raise RuntimeError(f"full run {run_id} has invalid scheduler provenance")
        if (
            expected_scheduled_job_run_id is not None
            and run.get("scheduled_job_run_id") != expected_scheduled_job_run_id
        ):
            raise RuntimeError(
                f"full run {run_id} scheduler owner changed from "
                f"{expected_scheduled_job_run_id} to {run.get('scheduled_job_run_id')}"
            )

        run_key = str(run.get("run_key") or "")
        if not run_key:
            raise RuntimeError(f"full run {run_id} has no run key")
        if run_key.startswith("recover-"):
            raise RuntimeError(f"full run {run_id} is a recover-only maintenance run")
        failure_row = self.db._execute(
            "SELECT COUNT(*) AS row_count FROM crawl_state WHERE run_key = %s AND status = 'error'",
            (run_key,),
        ).fetchone()
        failure_count = _db_int((failure_row or {}).get("row_count", 0))
        if failure_count:
            raise RuntimeError(f"full run {run_id} has crawl failures: count={failure_count}")
        pending_row = self.db._execute(
            "SELECT COUNT(*) AS row_count FROM crawl_state "
            "WHERE run_key = %s AND status = 'pending'",
            (run_key,),
        ).fetchone()
        pending_count = _db_int((pending_row or {}).get("row_count", 0))
        if pending_count:
            raise RuntimeError(
                f"full run {run_id} has incomplete crawl state: count={pending_count}"
            )

        missing_receipts = self.remaining_group_count(run_key)
        if missing_receipts:
            raise RuntimeError(
                f"full run {run_id} has incomplete group receipts: count={missing_receipts}"
            )

        receipt_quality = self.db._execute(
            "SELECT COUNT(*) AS row_count FROM groups_t "
            "LEFT JOIN (SELECT parts.group_id, COUNT(*) AS row_count FROM parts "
            "WHERE parts.seen_run_id = %s GROUP BY parts.group_id) AS current_group_parts "
            "ON current_group_parts.group_id = groups_t.id "
            "WHERE groups_t.fetched_run_key = %s AND ("
            "(BINARY groups_t.fetched_status = BINARY 'done' "
            "AND COALESCE(groups_t.fetched_row_count, -1) "
            "<> COALESCE(current_group_parts.row_count, 0)) OR "
            "(BINARY groups_t.fetched_status = BINARY 'not_found' AND ("
            "COALESCE(groups_t.fetched_row_count, -1) <> 0 "
            "OR COALESCE(current_group_parts.row_count, 0) <> 0)))",
            (run_id, run_key),
        ).fetchone()
        receipt_mismatches = _db_int((receipt_quality or {}).get("row_count", 0))
        if receipt_mismatches:
            raise RuntimeError(
                f"full run {run_id} has group receipt/part mismatches: count={receipt_mismatches}"
            )

        source_row = self.db._execute(
            "SELECT COUNT(*) AS row_count FROM parts WHERE seen_run_id = %s",
            (run_id,),
        ).fetchone()
        source_count = _db_int((source_row or {}).get("row_count", 0))
        if source_count <= 0:
            raise RuntimeError(f"run {run_id} produced an empty full candidate archive")

        quality = self.db._execute(
            "SELECT COUNT(*) AS invalid_rows FROM parts AS p "
            "LEFT JOIN groups_t AS g ON g.id = p.group_id "
            "LEFT JOIN categories AS c ON c.id = g.category_id "
            "LEFT JOIN vehicles AS v ON v.id = c.vehicle_id "
            "LEFT JOIN models AS m ON m.id = v.model_id "
            "LEFT JOIN brands AS b ON b.id = m.brand_id "
            "WHERE p.seen_run_id = %s AND ("
            "g.id IS NULL OR c.id IS NULL OR v.id IS NULL OR m.id IS NULL OR b.id IS NULL "
            "OR NULLIF(TRIM(p.part_number), '') IS NULL "
            "OR NULLIF(TRIM(UPPER(REGEXP_REPLACE(p.part_number, '[[:space:]-]+', ''))), '') "
            "IS NULL "
            "OR NULLIF(TRIM(p.name), '') IS NULL OR NULLIF(TRIM(p.code), '') IS NULL "
            "OR NULLIF(TRIM(b.name), '') IS NULL OR NULLIF(TRIM(m.name), '') IS NULL "
            "OR NULLIF(TRIM(v.name), '') IS NULL OR NULLIF(TRIM(v.model_code), '') IS NULL "
            "OR NULLIF(TRIM(v.vid), '') IS NULL OR NULLIF(TRIM(c.cid), '') IS NULL "
            "OR NULLIF(TRIM(c.name), '') IS NULL OR NULLIF(TRIM(g.name), '') IS NULL "
            "OR NULLIF(TRIM(g.code), '') IS NULL OR NULLIF(TRIM(g.uid), '') IS NULL "
            "OR NULLIF(TRIM(g.url), '') IS NULL OR NOT ("
            f"{_canonical_unit_url_sql('g.url', 'g.uid')}) "
            "OR (v.production_from IS NULL AND v.production_to IS NULL "
            "AND p.part_from IS NULL AND p.part_to IS NULL) "
            "OR (p.part_to IS NOT NULL AND v.production_from IS NOT NULL "
            "AND p.part_to < v.production_from) "
            "OR (v.production_to IS NOT NULL AND p.part_from IS NOT NULL "
            "AND v.production_to < p.part_from))",
            (run_id,),
        ).fetchone()
        invalid_rows = _db_int((quality or {}).get("invalid_rows", 0))
        if invalid_rows:
            raise RuntimeError(
                f"full run {run_id} failed source/field quality gate: invalid_rows={invalid_rows}"
            )

        # 保留上一份 raw archive，方便診斷 full crawl 的資料變化。正式資料一律
        # 由 verified bounded view 決定，這裡不會影響正式後台或 VIN mapping。
        current_snapshot = self.db._execute(
            "SELECT COUNT(*) AS row_count, COUNT(crawl_run_id) AS provenance_count, "
            "COUNT(DISTINCT crawl_run_id) AS run_count, "
            "MIN(crawl_run_id) AS min_run_id, MAX(crawl_run_id) AS max_run_id "
            "FROM published_parts"
        ).fetchone()
        current_count = _db_int((current_snapshot or {}).get("row_count", 0))
        current_run_id = _db_int((current_snapshot or {}).get("min_run_id") or 0)
        current_is_single_snapshot = (
            current_count > 0
            and _db_int((current_snapshot or {}).get("provenance_count", 0)) == current_count
            and _db_int((current_snapshot or {}).get("run_count", 0)) == 1
            and current_run_id == _db_int((current_snapshot or {}).get("max_run_id") or 0)
        )
        if current_is_single_snapshot:
            qualified = self.db._execute(
                "SELECT cr.id FROM crawl_runs AS cr "
                "JOIN scheduled_job_runs AS sj ON sj.id = cr.scheduled_job_run_id "
                "WHERE cr.id = %s AND cr.dataset_kind = 'full' "
                "AND cr.target_parts IS NULL AND cr.status = 'success' "
                "AND cr.finished_at IS NOT NULL AND cr.error_msg IS NULL "
                "AND sj.job_name = 'catalog' AND sj.trigger_mode = 'daemon' "
                "AND sj.status = 'completed' AND sj.finished_at IS NOT NULL "
                "AND sj.exit_code = 0 "
                "AND (SELECT COUNT(*) FROM crawl_runs AS linked "
                "WHERE linked.scheduled_job_run_id = sj.id) = 1",
                (current_run_id,),
            ).fetchone()
            if qualified:
                self.db._execute("DELETE FROM published_parts_previous")
                copied = self.db._execute(
                    "INSERT INTO published_parts_previous SELECT * FROM published_parts"
                ).rowcount
                if copied != current_count:
                    raise RuntimeError(
                        f"full candidate archive fallback copy mismatch: "
                        f"source={current_count}, copied={copied}"
                    )

        self.db._execute(
            "INSERT INTO published_parts ("
            "part_id, crawl_run_id, vehicle_id, model_id, vehicle_vid, "
            "brand, model, vehicle_name, vehicle_code, prod_period, "
            "production_from, production_to, engine, trim_name, "
            "part_name, part_number, part_number_normalized, category_id, category_cid, "
            "category_main, category_group, group_id, group_code, group_uid, "
            "part_range, part_from, part_to, source_url, note, quantity, code, "
            "snapshot_at) "
            "SELECT source.part_id, source.crawl_run_id, source.vehicle_id, "
            "source.model_id, source.vehicle_vid, "
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
            "SELECT p.id AS part_id, %s AS crawl_run_id, v.id AS vehicle_id, "
            "m.id AS model_id, "
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
            "crawl_run_id = source.crawl_run_id, vehicle_id = source.vehicle_id, "
            "model_id = source.model_id, "
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
            (run_id, run_id),
        )
        self.db._execute(
            "DELETE pp FROM published_parts pp "
            "LEFT JOIN parts p ON p.id = pp.part_id AND p.seen_run_id = %s "
            "WHERE p.id IS NULL",
            (run_id,),
        )
        published_row = self.db._execute(
            "SELECT COUNT(*) AS row_count, COUNT(crawl_run_id) AS provenance_count, "
            "COUNT(DISTINCT crawl_run_id) AS run_count, "
            "MIN(crawl_run_id) AS min_run_id, MAX(crawl_run_id) AS max_run_id "
            "FROM published_parts"
        ).fetchone()
        published_count = _db_int((published_row or {}).get("row_count", 0))
        if (
            published_count != source_count
            or _db_int((published_row or {}).get("provenance_count", 0)) != published_count
            or _db_int((published_row or {}).get("run_count", 0)) != 1
            or _db_int((published_row or {}).get("min_run_id") or 0) != run_id
            or _db_int((published_row or {}).get("max_run_id") or 0) != run_id
        ):
            raise RuntimeError(
                f"run {run_id} snapshot identity/count mismatch: "
                f"source={source_count}, published={published_count}"
            )
        return published_count

    def publish_bounded_parts(self, run_id: int, target_parts: int) -> int:
        """原子發布精確達標的正式有界資料集。

        bounded_parts 與全站 published_parts 的語意分離。本方法只在
        run metadata、筆數、來源 ID 關聯與必填欄位全部通過時
        更換 current bounded snapshot；交易失敗會保留上一版。
        """
        if target_parts != 10_000:
            raise ValueError("formal bounded target_parts must be exactly 10000")

        run = self.db._execute(
            "SELECT cr.run_key, cr.started_at, cr.dataset_kind, cr.target_parts, cr.status, "
            "cr.scope_brand, cr.scope_model, cr.scope_vehicle_year_floor, "
            "cr.evidence_status, cr.evidence_manifest_sha256, "
            "cr.evidence_dataset_sha256, cr.evidence_artifact_count, "
            "cr.evidence_record_count, cr.evidence_original_bytes, "
            "cr.evidence_stored_bytes, cr.evidence_verified_at, "
            "cr.scheduled_job_run_id, sj.job_name AS scheduled_job_name, "
            "sj.trigger_mode AS scheduled_trigger_mode, sj.status AS scheduled_job_status, "
            "(SELECT COUNT(*) FROM crawl_runs AS linked "
            "WHERE linked.scheduled_job_run_id = cr.scheduled_job_run_id) "
            "AS scheduled_crawl_count "
            "FROM crawl_runs AS cr "
            "LEFT JOIN scheduled_job_runs AS sj ON sj.id = cr.scheduled_job_run_id "
            "WHERE cr.id = %s FOR UPDATE",
            (run_id,),
        ).fetchone()
        if not run:
            raise RuntimeError(f"bounded run {run_id} does not exist")
        if (
            run.get("dataset_kind") != "bounded"
            or _db_int(run.get("target_parts") or 0) != target_parts
            or run.get("status") != "running"
        ):
            raise RuntimeError(f"run {run_id} is not a matching running bounded crawl")
        if run.get("scheduled_job_run_id") is None or (
            run.get("scheduled_job_name") != "catalog"
            or run.get("scheduled_trigger_mode") != "daemon"
            or run.get("scheduled_job_status") != "running"
            or _db_int(run.get("scheduled_crawl_count") or 0) != 1
        ):
            raise RuntimeError(f"bounded run {run_id} has invalid scheduler provenance")

        try:
            scope_brand, scope_model, scope_vehicle_year_floor = _validated_bounded_scope(
                cast(str | None, run.get("scope_brand")),
                cast(str | None, run.get("scope_model")),
                (
                    _db_int(run["scope_vehicle_year_floor"])
                    if run.get("scope_vehicle_year_floor") is not None
                    else None
                ),
            )
        except ValueError as exc:
            raise RuntimeError(f"bounded run {run_id} has invalid model scope") from exc

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
        source_count = _db_int((source_row or {}).get("row_count", 0))
        if source_count != target_parts:
            raise RuntimeError(
                f"bounded run {run_id} source count mismatch: "
                f"source={source_count}, target={target_parts}"
            )
        if scope_brand is not None:
            scoped_source = self.db._execute(
                "SELECT COUNT(*) AS row_count FROM parts AS part "
                "JOIN groups_t AS source_group ON source_group.id = part.group_id "
                "JOIN categories AS category ON category.id = source_group.category_id "
                "JOIN vehicles AS vehicle ON vehicle.id = category.vehicle_id "
                "JOIN models AS model ON model.id = vehicle.model_id "
                "JOIN brands AS brand ON brand.id = model.brand_id "
                "WHERE part.seen_run_id = %s "
                "AND CAST(LOWER(TRIM(brand.name)) AS BINARY) = CAST(%s AS BINARY) "
                "AND CAST(LOWER(TRIM(model.name)) AS BINARY) = CAST(%s AS BINARY) "
                "AND (vehicle.production_to IS NULL "
                "OR CAST(LEFT(vehicle.production_to, 4) AS UNSIGNED) >= %s)",
                (run_id, scope_brand, scope_model, scope_vehicle_year_floor),
            ).fetchone()
            scoped_count = _db_int((scoped_source or {}).get("row_count", 0))
            if scoped_count != target_parts:
                raise RuntimeError(
                    f"bounded run {run_id} model scope mismatch: "
                    f"scoped={scoped_count}, target={target_parts}"
                )

        # 正式 snapshot 的第一個 mutation 是下方 DELETE。必須先在同一個
        # locked run transaction 內重算並比對完整 live HTTP evidence，
        # 否則任何缺頁、fixture、跨排程或 9,999/10,000 覆蓋都 fail closed，
        # 並保留上一版 bounded_parts。
        self._assert_verified_run_evidence(run_id, run, target_parts)
        self._assert_bounded_group_receipts(run_id, target_parts)

        desired_scope = self.db._execute(
            "SELECT scope_brand, scope_model, scope_vehicle_year_floor "
            "FROM catalog_desired_bounded_scope "
            "WHERE singleton_id = 1 FOR UPDATE"
        ).fetchone()
        desired_identity = (
            cast(str | None, (desired_scope or {}).get("scope_brand")),
            cast(str | None, (desired_scope or {}).get("scope_model")),
            (
                _db_int(desired_scope["scope_vehicle_year_floor"])
                if desired_scope and desired_scope.get("scope_vehicle_year_floor") is not None
                else None
            ),
        )
        run_scope = (scope_brand, scope_model, scope_vehicle_year_floor)
        if desired_identity != run_scope:
            raise RuntimeError(
                f"bounded run {run_id} scope no longer matches catalog desired scope: "
                f"desired={desired_identity!r}, run={run_scope!r}"
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
            "part_range, part_from, part_to, source_url, note, quantity, code, "
            "evidence_record_sha256, snapshot_at) "
            "SELECT p.id, %s, v.id, m.id, v.vid, b.name, m.name, v.name, "
            "v.model_code, v.prod_period, v.production_from, v.production_to, "
            "v.engine, v.grade, p.name, p.part_number, "
            "UPPER(REGEXP_REPLACE(p.part_number, '[[:space:]-]+', '')), "
            "c.id, c.cid, c.name, g.name, g.id, g.code, g.uid, p.range_str, "
            "p.part_from, p.part_to, g.url, p.note, p.quantity, p.code, "
            "evidence_record.record_sha256, NOW() "
            "FROM parts AS p "
            "JOIN groups_t AS g ON g.id = p.group_id "
            "JOIN categories AS c ON c.id = g.category_id "
            "JOIN vehicles AS v ON v.id = c.vehicle_id "
            "JOIN models AS m ON m.id = v.model_id "
            "JOIN brands AS b ON b.id = m.brand_id "
            "JOIN partsouq_artifact_records AS evidence_record "
            "ON evidence_record.crawl_run_id = %s "
            "AND evidence_record.part_id = p.id "
            "AND evidence_record.record_type = 'part' "
            "AND evidence_record.accepted = 1 "
            "JOIN partsouq_http_artifacts AS evidence_artifact "
            "ON evidence_artifact.id = evidence_record.artifact_id "
            "AND evidence_artifact.crawl_run_id = evidence_record.crawl_run_id "
            "AND evidence_artifact.verification_status = 'verified' "
            "AND evidence_artifact.capture_kind = 'live_http' "
            "WHERE p.seen_run_id = %s",
            (run_id, run_id, run_id),
        )

        snapshot = self.db._execute(
            "SELECT COUNT(*) AS row_count, "
            "COUNT(DISTINCT crawl_run_id) AS run_count, "
            "MIN(crawl_run_id) AS min_run_id, MAX(crawl_run_id) AS max_run_id "
            "FROM bounded_parts"
        ).fetchone()
        if (
            _db_int((snapshot or {}).get("row_count", 0)) != target_parts
            or _db_int((snapshot or {}).get("run_count", 0)) != 1
            or _db_int((snapshot or {}).get("min_run_id") or 0) != run_id
            or _db_int((snapshot or {}).get("max_run_id") or 0) != run_id
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
            "OR NOT ("
            f"{_canonical_unit_url_sql('bp.source_url', 'bp.group_uid')}))",
            (run_id,),
        ).fetchone()
        invalid_rows = _db_int((quality or {}).get("invalid_rows", 0))
        if invalid_rows:
            raise RuntimeError(
                f"bounded run {run_id} failed source/field quality gate: "
                f"invalid_rows={invalid_rows}"
            )
        # 發布後僅以 bounded_parts 重播，確定這一版 immutable snapshot 的
        # part identity 與 payload digest 仍逐列對應已接受的 HTTP evidence。
        # 之後任何 raw parts 的 seen_run_id 更新不會參與這個驗證。
        self._assert_verified_run_evidence(
            run_id,
            run,
            target_parts,
            use_bounded_snapshot=True,
        )
        return target_parts
