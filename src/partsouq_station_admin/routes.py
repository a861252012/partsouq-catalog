from __future__ import annotations

import hmac
import json
import re
import secrets
from collections.abc import Callable
from typing import Any, cast

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask.typing import ResponseReturnValue

from partsouq_crawler.nhtsa.api import normalize_vin
from partsouq_station_admin.config import AdminConfig
from partsouq_station_admin.db import RequestDatabase
from partsouq_station_admin.query_trace import QueryTrace
from partsouq_station_admin.repository import (
    ENTITY_SPECS,
    PAGE_SIZES,
    AdminDataError,
    AdminReadinessError,
    AdminRepository,
    EntitySpec,
    RecordNotFoundError,
    RevisionConflictError,
    entity_spec,
    field_kind,
    field_label,
)

DatabaseFactory = Callable[[AdminConfig, QueryTrace], RequestDatabase]

bp = Blueprint(
    "admin",
    __name__,
    template_folder="templates",
    static_folder="static",
    static_url_path="/admin-static",
)

PUBLIC_ENDPOINTS = frozenset({"admin.login", "admin.static"})
AUTHENTICATION_EXEMPT_ENDPOINTS = PUBLIC_ENDPOINTS | {"admin.health"}


def _config() -> AdminConfig:
    return cast(AdminConfig, current_app.extensions["partsouq_admin_config"])


def _repository() -> AdminRepository:
    return AdminRepository(cast(RequestDatabase, g.partsouq_admin_database))


def _audit_actor(submitted_actor: str) -> str:
    config = _config()
    return config.username if config.auth_required else submitted_actor


def _display_actor() -> str:
    config = _config()
    return config.username if config.auth_required else config.default_actor


def _csrf_token() -> str:
    token = session.get("csrf_token")
    if not isinstance(token, str):
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


@bp.before_app_request
def require_login() -> ResponseReturnValue | None:
    config = _config()
    if not config.auth_required or request.endpoint in AUTHENTICATION_EXEMPT_ENDPOINTS:
        return None
    username = session.get("admin_username")
    if (
        session.get("admin_authenticated") is True
        and isinstance(username, str)
        and hmac.compare_digest(username, config.username)
    ):
        return None
    # 未登入頁面可能與瀏覽器自動要求的 favicon 平行發生。若每個受保護
    # GET 都 clear 整個 session，favicon 的 redirect 會把已渲染登入表單
    # 的 CSRF token 清掉，使用者第一次送出必定得到 400。只移除失效的
    # authentication 欄位；成功登入仍會 clear 全部並輪替 CSRF。
    session.pop("admin_authenticated", None)
    session.pop("admin_username", None)
    return redirect(url_for("admin.login", next=request.full_path.rstrip("?")))


@bp.before_app_request
def open_database() -> None:
    if request.endpoint in PUBLIC_ENDPOINTS:
        return
    trace = QueryTrace()
    factory = cast(DatabaseFactory, current_app.extensions["partsouq_admin_database_factory"])
    g.partsouq_admin_query_trace = trace
    g.partsouq_admin_database = factory(_config(), trace)


@bp.before_app_request
def verify_csrf() -> None:
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return
    supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
    expected = session.get("csrf_token", "")
    if (
        not supplied
        or not isinstance(expected, str)
        or not expected
        or not hmac.compare_digest(supplied, expected)
    ):
        abort(400, description="CSRF 驗證失敗")


@bp.route("/login", methods=["GET", "POST"])
def login() -> ResponseReturnValue:
    config = _config()
    if not config.auth_required:
        return redirect(url_for("admin.dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if hmac.compare_digest(username.encode(), config.username.encode()) and hmac.compare_digest(
            password.encode(), config.password.encode()
        ):
            session.clear()
            session["admin_authenticated"] = True
            session["admin_username"] = config.username
            session["csrf_token"] = secrets.token_urlsafe(32)
            destination = request.form.get("next", "")
            if (
                not destination.startswith("/")
                or destination.startswith("//")
                or "\\" in destination
            ):
                destination = url_for("admin.dashboard")
            return redirect(destination)
        flash("帳號或密碼錯誤。", "error")
    return render_template(
        "login.html",
        next_path=request.args.get("next", ""),
    )


@bp.post("/logout")
def logout() -> ResponseReturnValue:
    session.clear()
    return redirect(url_for("admin.login"))


@bp.teardown_app_request
def close_database(_error: BaseException | None) -> None:
    database = getattr(g, "partsouq_admin_database", None)
    if database is not None:
        database.close()


@bp.after_app_request
def add_query_headers(response: Response) -> Response:
    trace = cast(QueryTrace | None, getattr(g, "partsouq_admin_query_trace", None))
    if trace is not None:
        response.headers["X-Admin-Query-Count"] = str(trace.count)
        response.headers["X-Admin-Query-Tags"] = ",".join(trace.tags)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self'; img-src 'self'; "
        "script-src 'none'; frame-ancestors 'none'; base-uri 'none'"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@bp.app_context_processor
def template_context() -> dict[str, Any]:
    return {
        "entity_specs": ENTITY_SPECS,
        "csrf_token": _csrf_token,
        "field_kind": field_kind,
        "field_label": field_label,
        "query_trace": getattr(g, "partsouq_admin_query_trace", QueryTrace()),
        "page_sizes": PAGE_SIZES,
    }


@bp.app_errorhandler(RecordNotFoundError)
def record_not_found(error: RecordNotFoundError) -> tuple[str, int]:
    return render_template("error.html", message=str(error)), 404


@bp.app_errorhandler(RevisionConflictError)
def revision_conflict(error: RevisionConflictError) -> tuple[str, int]:
    return render_template("error.html", message=str(error)), 409


@bp.app_errorhandler(AdminDataError)
def invalid_data(error: AdminDataError) -> tuple[str, int]:
    return render_template("error.html", message=str(error)), 400


@bp.app_errorhandler(AdminReadinessError)
def service_not_ready(error: AdminReadinessError) -> tuple[str, int]:
    return render_template("error.html", message=str(error)), 503


@bp.get("/")
def dashboard() -> str:
    repository = _repository()
    return render_template(
        "dashboard.html",
        counts=repository.dashboard_counts(),
        system_summary=repository.system_data_summary(),
        quarantine_summary=repository.quarantine_summary(),
    )


@bp.get("/health")
def health() -> dict[str, object]:
    repository = _repository()
    repository.check_readiness()
    return {"status": "ok", "entities": len(ENTITY_SPECS)}


@bp.get("/monitoring")
def monitoring() -> str:
    return render_template("monitoring.html", monitor=_repository().crawl_monitoring())


@bp.get("/quarantine")
def quarantine_list() -> str:
    state = request.args.get("state", "unresolved")
    run_key = request.args.get("run_key") or None
    try:
        page_number = int(request.args.get("page", "1"))
    except ValueError as error:
        raise AdminDataError("頁碼格式錯誤") from error
    requested_page_size = request.args.get("pageSize")
    try:
        page_size = int(requested_page_size) if requested_page_size else _config().page_size
    except ValueError as error:
        raise AdminDataError("每頁筆數格式錯誤") from error
    page = _repository().list_quarantine(
        state=state,
        run_key=run_key,
        page=page_number,
        limit=page_size,
    )
    return render_template(
        "quarantine.html",
        page=page,
        state=state,
        run_key=run_key or "",
    )


@bp.post("/quarantine/<int:row_id>/resolve")
def quarantine_resolve(row_id: int) -> ResponseReturnValue:
    resolution = request.form.get("resolution", "").strip()
    _repository().resolve_quarantine(
        row_id,
        resolution,
        expected_run_key=request.form.get("expected_run_key", ""),
    )
    flash("已標記處置（同一料號後續 run 再現時會自動重開）。", "success")
    return redirect(
        url_for(
            "admin.quarantine_list",
            state=request.form.get("state", "unresolved"),
            run_key=request.form.get("run_key") or None,
            page=request.form.get("page", "1"),
            pageSize=request.form.get("pageSize"),
        )
    )


@bp.get("/entities/<entity_type>")
def entity_list(entity_type: str) -> str:
    spec = entity_spec(entity_type)
    source_scope = request.args.get("dataset", "formal")
    include_retired = request.args.get("include_retired") == "1"
    try:
        page_number = int(request.args.get("page", "1"))
    except ValueError as error:
        raise AdminDataError("頁碼格式錯誤") from error
    requested_page_size = request.args.get("pageSize")
    try:
        page_size = int(requested_page_size) if requested_page_size else _config().page_size
    except ValueError as error:
        raise AdminDataError("每頁筆數格式錯誤") from error
    if page_size not in PAGE_SIZES:
        raise AdminDataError("每頁筆數只接受 10、25、30、50、100 或 200")
    page = _repository().list_records(
        entity_type,
        query=request.args.get("q", ""),
        page=page_number,
        limit=page_size,
        include_retired=include_retired,
        source_scope=source_scope,
    )
    return render_template("list.html", spec=spec, page=page, source_scope=source_scope)


@bp.route("/entities/<entity_type>/new", methods=["GET", "POST"])
def entity_create(entity_type: str) -> ResponseReturnValue:
    spec = entity_spec(entity_type)
    if not spec.editable_fields:
        raise AdminDataError("此資料類型為唯讀；請使用專用確認流程")
    if request.method == "GET":
        return render_template(
            "edit.html",
            spec=spec,
            record=None,
            payload_json="{}",
            edit_payload={},
            actor=_display_actor(),
            actor_locked=_config().auth_required,
            mode="create",
        )
    payload = _payload_from_form(spec)
    identity_key = _repository().create_manual(
        entity_type,
        payload,
        actor=_audit_actor(request.form.get("actor", "")),
        reason=request.form.get("reason", ""),
    )
    flash("已建立人工資料；來源型錄資料未被修改。", "success")
    return redirect(
        url_for("admin.entity_detail", entity_type=entity_type, identity_key=identity_key)
    )


@bp.get("/entities/<entity_type>/<identity_key>")
def entity_detail(entity_type: str, identity_key: str) -> str:
    spec = entity_spec(entity_type)
    detail = _repository().get_record(entity_type, identity_key)
    return render_template(
        "detail.html",
        spec=spec,
        detail=detail,
        actor=_display_actor(),
        actor_locked=_config().auth_required,
    )


@bp.get("/entities/<entity_type>/<identity_key>/edit")
def entity_edit(entity_type: str, identity_key: str) -> str:
    spec = entity_spec(entity_type)
    if not spec.editable_fields:
        raise AdminDataError("此資料類型為唯讀；請使用專用確認流程")
    detail = _repository().get_record(entity_type, identity_key)
    editable = {
        field: detail.record.payload.get(field)
        for field in spec.editable_fields
        if field in detail.record.payload
    }
    return render_template(
        "edit.html",
        spec=spec,
        record=detail.record,
        payload_json=json.dumps(editable, ensure_ascii=False, indent=2, default=str),
        edit_payload=editable,
        actor=_display_actor(),
        actor_locked=_config().auth_required,
        mode="update",
    )


@bp.post("/entities/<entity_type>/<identity_key>/update")
def entity_update(entity_type: str, identity_key: str) -> ResponseReturnValue:
    spec = entity_spec(entity_type)
    if not spec.editable_fields:
        raise AdminDataError("此資料類型為唯讀；請使用專用確認流程")
    _repository().update_record(
        entity_type,
        identity_key,
        _payload_from_form(spec),
        expected_revision=_revision_from_form(),
        expected_base_sha256=_base_sha256_from_form(),
        actor=_audit_actor(request.form.get("actor", "")),
        reason=request.form.get("reason", ""),
    )
    flash("已新增一筆覆寫版本；來源型錄資料未被修改。", "success")
    return redirect(
        url_for("admin.entity_detail", entity_type=entity_type, identity_key=identity_key)
    )


@bp.route("/station/vins/request", methods=["GET", "POST"])
def vin_decode_request() -> ResponseReturnValue:
    if request.method == "GET":
        return render_template(
            "vin_request.html",
            actor=_display_actor(),
            actor_locked=_config().auth_required,
        )
    try:
        vin = normalize_vin(request.form.get("vin", ""))
    except ValueError as error:
        raise AdminDataError(str(error)) from error
    _repository().request_vin_decode(
        vin,
        actor=_audit_actor(request.form.get("actor", "")),
    )
    flash(
        "VIN 已加入 NHTSA 解碼佇列；請執行 partsouq-scheduler --job pending，或由部署端排程消費。",
        "success",
    )
    return redirect(url_for("admin.entity_list", entity_type="vin_vehicle_mappings"))


@bp.get("/station/vins/candidates")
def vin_vehicle_candidates() -> ResponseReturnValue:
    vin_input = request.args.get("vin", "").strip()
    vin: str | None = None
    decode: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] | None = None
    if vin_input:
        try:
            vin = normalize_vin(vin_input)
        except ValueError as error:
            raise AdminDataError(str(error)) from error
        decode = _repository().vin_decode_mapping_status(vin)
        if decode is not None:
            candidates = _repository().vin_vehicle_candidates(vin)
    return render_template(
        "vin_candidates.html",
        vin=vin,
        decode=decode,
        candidates=candidates,
    )


@bp.post("/station/vins/confirm")
def vin_vehicle_confirm() -> ResponseReturnValue:
    try:
        vin = normalize_vin(request.form.get("vin", ""))
    except ValueError as error:
        raise AdminDataError(str(error)) from error
    vehicle_input = request.form.get("partsouq_vehicle_id", "")
    try:
        partsouq_vehicle_id = int(vehicle_input)
    except ValueError as error:
        raise AdminDataError("車款 ID 不合法") from error
    _repository().confirm_vin_vehicle_mapping(
        vin,
        partsouq_vehicle_id,
        allow_name_override=request.form.get("allow_name_override") == "on",
        source_reference=request.form.get("source_reference", ""),
    )
    flash("車款對應已建立；VIN 零件適配會即時生效。", "success")
    return redirect(url_for("admin.entity_list", entity_type="vin_vehicle_mappings"))


@bp.post("/entities/<entity_type>/<identity_key>/retire")
def entity_retire(entity_type: str, identity_key: str) -> ResponseReturnValue:
    if not entity_spec(entity_type).editable_fields:
        raise AdminDataError("此資料類型為唯讀；請使用專用確認流程")
    _repository().retire_record(
        entity_type,
        identity_key,
        expected_revision=_revision_from_form(),
        actor=_audit_actor(request.form.get("actor", "")),
        reason=request.form.get("reason", ""),
    )
    flash("資料已停用；沒有刪除來源資料。", "success")
    return redirect(
        url_for("admin.entity_detail", entity_type=entity_type, identity_key=identity_key)
    )


@bp.post("/entities/<entity_type>/<identity_key>/restore")
def entity_restore(entity_type: str, identity_key: str) -> ResponseReturnValue:
    if not entity_spec(entity_type).editable_fields:
        raise AdminDataError("此資料類型為唯讀；請使用專用確認流程")
    _repository().restore_record(
        entity_type,
        identity_key,
        expected_revision=_revision_from_form(),
        actor=_audit_actor(request.form.get("actor", "")),
        reason=request.form.get("reason", ""),
    )
    flash("資料已恢復啟用。", "success")
    return redirect(
        url_for("admin.entity_detail", entity_type=entity_type, identity_key=identity_key)
    )


def _payload_from_form(spec: EntitySpec) -> dict[str, Any]:
    if "payload_json" in request.form:
        raw = request.form.get("payload_json", "")
        try:
            decoded_payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise AdminDataError(f"JSON 格式錯誤：{error.msg}") from error
        if not isinstance(decoded_payload, dict):
            raise AdminDataError("資料內容必須是 JSON object")
        return cast(dict[str, Any], decoded_payload)

    typed_payload: dict[str, Any] = {}
    for field in spec.editable_fields:
        form_key = f"field__{field}"
        if form_key not in request.form:
            continue
        raw_value = request.form.get(form_key, "").strip()
        kind = field_kind(field)
        if not raw_value:
            if request.form.get("form_mode") == "update":
                typed_payload[field] = None
            continue
        try:
            if kind == "json":
                value: Any = json.loads(raw_value)
            elif kind == "boolean":
                if raw_value not in {"0", "1"}:
                    raise ValueError
                value = raw_value == "1"
            elif kind == "integer":
                value = int(raw_value)
            elif kind == "number":
                value = float(raw_value)
            else:
                value = raw_value
        except (ValueError, json.JSONDecodeError) as error:
            raise AdminDataError(f"{field_label(field)}格式錯誤") from error
        typed_payload[field] = value
    return typed_payload


def _revision_from_form() -> int:
    try:
        revision = int(request.form.get("revision", ""))
    except ValueError as error:
        raise AdminDataError("版本號格式錯誤") from error
    if revision < 0:
        raise AdminDataError("版本號格式錯誤")
    return revision


def _base_sha256_from_form() -> str:
    value = request.form.get("base_sha256", "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise AdminDataError("來源版本格式錯誤")
    return value
