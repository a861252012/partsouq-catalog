from __future__ import annotations

import re

import pytest

from partsouq_station_admin.app import create_app
from partsouq_station_admin.config import AdminConfig
from partsouq_station_admin.db import SqlParams
from partsouq_station_admin.query_trace import QueryTrace
from partsouq_station_admin.repository import AdminRepository

from .fakes import ScriptedDatabase


def _app(
    databases: list[ScriptedDatabase],
    *,
    dataset_size: int = 1,
    config: AdminConfig | None = None,
) -> object:
    def factory(_config: AdminConfig, trace: QueryTrace) -> ScriptedDatabase:
        database = ScriptedDatabase(trace, dataset_size=dataset_size)
        databases.append(database)
        return database

    app = create_app(
        config or AdminConfig(secret_key="test-secret", page_size=25),
        database_factory=factory,
    )
    app.testing = True
    return app


def _csrf_token(client: object, path: str) -> str:
    response = client.get(path)
    match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
    assert match is not None
    return match.group(1).decode()


def _login(client: object, *, next_path: str = "/") -> object:
    token = _csrf_token(client, "/login")
    return client.post(
        "/login",
        data={
            "csrf_token": token,
            "username": "admin",
            "password": "password",
            "next": next_path,
        },
    )


def test_health_exercises_database_readiness_before_reporting_ok() -> None:
    databases: list[ScriptedDatabase] = []
    app = _app(databases)

    response = app.test_client().get("/health")

    assert response.status_code == 200
    assert response.json["status"] == "ok"
    assert response.json["entities"] == 10
    tags = [call.tag for call in databases[-1].calls]
    assert tags == [
        "health.published-provenance",
        "health.quarantine-list",
        "health.quarantine-run-key",
        "health.backoffice-schema",
    ]


def test_authenticated_health_stays_public_but_opens_database() -> None:
    databases: list[ScriptedDatabase] = []
    app = _app(
        databases,
        config=AdminConfig(
            secret_key="test-secret",
            username="admin",
            password="password",
            page_size=25,
        ),
    )

    response = app.test_client().get("/health")

    assert response.status_code == 200
    assert response.json == {"entities": 10, "status": "ok"}
    assert [call.tag for call in databases[-1].calls] == [
        "health.published-provenance",
        "health.quarantine-list",
        "health.quarantine-run-key",
        "health.backoffice-schema",
    ]


def test_health_fails_closed_when_published_provenance_contract_is_stale() -> None:
    databases: list[ScriptedDatabase] = []

    def factory(_config: AdminConfig, trace: QueryTrace) -> ScriptedDatabase:
        database = ScriptedDatabase(trace, readiness_contract_ready=False)
        databases.append(database)
        return database

    app = create_app(
        AdminConfig(secret_key="test-secret"),
        database_factory=factory,
    )
    app.testing = True

    response = app.test_client().get("/health")

    assert response.status_code == 503
    assert b"migration 036" in response.data
    assert databases[-1].calls[-1].tag == "health.published-provenance"


def test_host_allowlist_rejects_untrusted_host_before_opening_database() -> None:
    databases: list[ScriptedDatabase] = []
    app = _app(databases)

    rejected = app.test_client().get("/", headers={"Host": "evil.example"})
    allowed = app.test_client().get(
        "/health",
        headers={"Host": "admin.partsouq.localhost:8086"},
    )

    assert rejected.status_code == 400
    assert allowed.status_code == 200
    assert len(databases) == 1


def test_authenticated_session_binds_username_and_ignores_submitted_actor() -> None:
    databases: list[ScriptedDatabase] = []
    app = _app(
        databases,
        config=AdminConfig(
            secret_key="station-session-secret",
            username="admin",
            password="password",
            page_size=25,
        ),
    )
    client = app.test_client()

    assert client.get("/entities/part_numbers/new").status_code == 302
    login_response = _login(client)
    assert login_response.status_code == 302
    with client.session_transaction() as authenticated_session:
        assert authenticated_session["admin_username"] == "admin"

    editor = client.get("/entities/part_numbers/new")
    assert editor.status_code == 200
    assert b'name="actor" value="admin" required maxlength="191" readonly' in editor.data
    token = _csrf_token(client, "/entities/part_numbers/new")
    created = client.post(
        "/entities/part_numbers/new",
        data={
            "csrf_token": token,
            "field__number_raw": "P-1",
            "field__name_en_raw": "Fixture part",
            "actor": "forged-browser-actor",
            "reason": "authenticated correction",
        },
    )

    assert created.status_code == 302
    event = next(
        call for call in databases[-1].calls if call.tag == "write.append-event.part_numbers"
    )
    assert event.params[-2:] == ("admin", "authenticated correction")


def test_legacy_boolean_only_session_is_not_authenticated() -> None:
    databases: list[ScriptedDatabase] = []
    app = _app(
        databases,
        config=AdminConfig(
            secret_key="station-session-secret",
            username="admin",
            password="password",
        ),
    )
    client = app.test_client()
    with client.session_transaction() as legacy_session:
        legacy_session["admin_authenticated"] = True

    response = client.get("/")

    assert response.status_code == 302
    assert response.headers["Location"].startswith("/login?next=")
    assert not databases
    with client.session_transaction() as cleared_session:
        assert "admin_authenticated" not in cleared_session


def test_login_rejects_external_redirect_target() -> None:
    databases: list[ScriptedDatabase] = []
    app = _app(
        databases,
        config=AdminConfig(
            secret_key="station-session-secret",
            username="admin",
            password="password",
        ),
    )

    response = _login(app.test_client(), next_path="//evil.example/admin")

    assert response.status_code == 302
    assert response.headers["Location"] == "/"


def test_bad_login_and_logout_csrf_keep_authenticated_session_fail_closed() -> None:
    databases: list[ScriptedDatabase] = []
    app = _app(
        databases,
        config=AdminConfig(
            secret_key="station-session-secret",
            username="admin",
            password="password",
            require_auth=True,
        ),
    )
    client = app.test_client()
    login_token = _csrf_token(client, "/login")

    rejected_login = client.post(
        "/login",
        data={
            "csrf_token": login_token,
            "username": "admin",
            "password": "wrong-password",
        },
    )
    assert rejected_login.status_code == 200
    assert "帳號或密碼錯誤。".encode() in rejected_login.data
    assert not databases

    authenticated = _login(client)
    assert authenticated.status_code == 302
    rejected_logout = client.post("/logout")
    assert rejected_logout.status_code == 400
    with client.session_transaction() as current_session:
        assert current_session["admin_authenticated"] is True
        logout_token = current_session["csrf_token"]

    logged_out = client.post("/logout", data={"csrf_token": logout_token})
    assert logged_out.status_code == 302
    assert logged_out.headers["Location"] == "/login"
    assert client.get("/").status_code == 302
    with client.session_transaction() as logged_out_session:
        assert "admin_authenticated" not in logged_out_session
        assert "admin_username" not in logged_out_session


def test_parallel_unauthenticated_get_does_not_invalidate_login_csrf() -> None:
    databases: list[ScriptedDatabase] = []
    app = _app(
        databases,
        config=AdminConfig(
            secret_key="station-session-secret",
            username="admin",
            password="password",
            require_auth=True,
        ),
    )
    client = app.test_client()
    login_token = _csrf_token(client, "/login?next=/entities/part_numbers")

    favicon = client.get("/favicon.ico")
    assert favicon.status_code == 302
    assert favicon.headers["Location"].startswith("/login?next=")

    rejected_login = client.post(
        "/login?next=/entities/part_numbers",
        data={
            "csrf_token": login_token,
            "username": "admin",
            "password": "wrong-password",
        },
    )
    assert rejected_login.status_code == 200
    assert "帳號或密碼錯誤。".encode() in rejected_login.data
    assert not databases


def test_list_allows_supported_page_size_and_keeps_it_on_next_link() -> None:
    databases: list[ScriptedDatabase] = []
    app = _app(databases, dataset_size=1000)

    response = app.test_client().get("/entities/part_numbers?pageSize=200")

    assert response.status_code == 200
    assert response.headers["X-Admin-Query-Count"] == "4"
    assert b'<option value="200" selected>' in response.data
    assert b'<option value="formal" selected>' in response.data
    assert "目前正式 snapshot（已通過發布閘門）".encode() in response.data
    assert "歷史 sample（非正式）".encode() in response.data
    assert b"pageSize=200" in response.data
    assert "顯示 1 到 200，共 1000 筆記錄".encode() in response.data
    assert "共 5 頁".encode() in response.data
    assert (
        len([row for row in databases[-1].calls if row.tag == "list.source-batch.part_numbers"])
        == 1
    )
    assert databases[-1].closed


def test_part_list_can_explicitly_show_historical_sample() -> None:
    databases: list[ScriptedDatabase] = []
    app = _app(databases)

    response = app.test_client().get("/entities/part_numbers?dataset=historical_sample&pageSize=25")

    assert response.status_code == 200
    assert b'<option value="historical_sample" selected>' in response.data
    sql = "\n".join(call.sql for call in databases[-1].calls)
    assert "station_admin_historical_sample_part_numbers" in sql


def test_list_rejects_unsupported_page_size_before_query() -> None:
    databases: list[ScriptedDatabase] = []
    app = _app(databases)

    response = app.test_client().get("/entities/part_numbers?pageSize=15")

    assert response.status_code == 400
    assert not databases[-1].calls


def test_monitoring_uses_unified_scheduler_tables() -> None:
    databases: list[ScriptedDatabase] = []
    app = _app(databases)

    response = app.test_client().get("/monitoring")

    assert response.status_code == 200
    assert b"daemon" in response.data
    assert response.headers["X-Admin-Query-Tags"].split(",") == [
        "monitor.scheduled-job-runs",
        "monitor.crawl-runs",
        "monitor.admin-crawl-requests",
    ]
    assert b"monthly-2099-01-partsouq" in response.data
    assert b"TES**********0000" in response.data


def test_dashboard_separates_sample_published_and_nhtsa_vin_layers() -> None:
    databases: list[ScriptedDatabase] = []
    app = _app(databases)

    response = app.test_client().get("/")

    assert response.status_code == 200
    assert b"1000" in response.data
    assert b"923" in response.data
    assert b"10000" in response.data
    assert b"9234" in response.data
    assert b"137120" in response.data
    assert "正式排程限量資料已完成 10000 / 10000 筆".encode() in response.data
    assert b"crawl run 42" in response.data
    assert b"scheduler run 77" in response.data
    assert "完整全量 snapshot 尚未發布".encode() in response.data
    assert "目前正式不重複料號".encode() in response.data
    assert "目前已有已發布的 NHTSA 參考資料".encode() in response.data
    assert "目前沒有已發布的 NHTSA 參考資料".encode() not in response.data
    assert "逐 VIN 解碼需由站方輸入合法 VIN".encode() in response.data


def test_dashboard_does_not_accept_raw_bounded_rows_without_verified_current_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = AdminRepository.system_data_summary

    def mismatched_current_snapshot(repository: AdminRepository) -> dict[str, object]:
        summary = original(repository)
        summary["partsouq_current_crawl_run_id"] = None
        summary["partsouq_current_rows"] = 0
        summary["partsouq_current_scope"] = None
        return summary

    monkeypatch.setattr(AdminRepository, "system_data_summary", mismatched_current_snapshot)

    response = _app([]).test_client().get("/")

    assert response.status_code == 200
    assert "正式排程限量資料已完成".encode() not in response.data
    assert "正式排程限量資料尚未通過驗收".encode() in response.data


def test_dashboard_explains_bounded_scope_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = AdminRepository.system_data_summary

    def mismatched_scope(repository: AdminRepository) -> dict[str, object]:
        summary = original(repository)
        summary["partsouq_current_rows"] = 0
        summary["partsouq_current_scope"] = None
        summary["partsouq_bounded_rows"] = 0
        summary["bounded_scope_matches_desired"] = False
        summary["bounded_scope_blocking_reason"] = "bounded_scope_mismatch"
        summary["latest_bounded_run_scope"] = {
            "brand": "toyota",
            "model": "1000",
            "vehicle_year_floor": 1969,
        }
        return summary

    monkeypatch.setattr(AdminRepository, "system_data_summary", mismatched_scope)

    response = _app([]).test_client().get("/")

    assert response.status_code == 200
    assert "正式排程限量資料已完成".encode() not in response.data
    assert "不符合的 snapshot 已隱藏".encode() in response.data


def test_dashboard_does_not_claim_empty_nhtsa_reference_is_synced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = AdminRepository.system_data_summary

    def empty_nhtsa_reference(repository: AdminRepository) -> dict[str, object]:
        summary = original(repository)
        summary["nhtsa_current_records"] = 0
        summary["nhtsa_vin_decodes"] = 0
        summary["nhtsa_terminal_undecodable_vins"] = 0
        return summary

    monkeypatch.setattr(AdminRepository, "system_data_summary", empty_nhtsa_reference)
    databases: list[ScriptedDatabase] = []
    response = _app(databases).test_client().get("/")

    assert response.status_code == 200
    assert "目前沒有已發布的 NHTSA 參考資料".encode() in response.data
    assert "目前已有已發布的 NHTSA 參考資料".encode() not in response.data


def test_dashboard_does_not_claim_terminal_vin_was_never_provided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = AdminRepository.system_data_summary

    def terminal_vin(repository: AdminRepository) -> dict[str, object]:
        summary = original(repository)
        summary["nhtsa_vin_decodes"] = 0
        summary["nhtsa_terminal_undecodable_vins"] = 1
        return summary

    monkeypatch.setattr(AdminRepository, "system_data_summary", terminal_vin)
    response = _app([]).test_client().get("/")

    assert response.status_code == 200
    assert "已受理 1 筆 VIN，但 NHTSA 均無法產生可用解碼".encode() in response.data
    assert "這不代表尚未提供 VIN".encode() in response.data
    assert "逐 VIN 解碼需由站方輸入合法 VIN".encode() not in response.data


def test_part_detail_does_not_present_adapter_metadata_as_raw_http_evidence() -> None:
    databases: list[ScriptedDatabase] = []
    app = _app(databases)

    response = app.test_client().get("/entities/part_numbers/source:1")

    assert response.status_code == 200
    assert "零件表 Code／圖號呼叫碼".encode() in response.data
    assert "不代表已保留原始 HTTP 回應或 hash".encode() in response.data


def test_unpublished_raw_source_id_is_404_for_all_formal_part_routes() -> None:
    databases: list[ScriptedDatabase] = []

    def factory(_config: AdminConfig, trace: QueryTrace) -> ScriptedDatabase:
        database = ScriptedDatabase(trace)
        original_fetch_one = database.fetch_one

        def unpublished_source(
            tag: str,
            sql: str,
            params: SqlParams = None,
        ) -> dict[str, object] | None:
            if tag in {"detail.base.part_numbers", "write.lock-base.part_numbers"}:
                assert "FROM station_admin_formal_part_numbers" in sql
                return None
            return original_fetch_one(tag, sql, params)

        database.fetch_one = unpublished_source  # type: ignore[method-assign]
        databases.append(database)
        return database

    app = create_app(
        AdminConfig(secret_key="test-secret", page_size=25),
        database_factory=factory,
    )
    app.testing = True
    client = app.test_client()
    token = _csrf_token(client, "/entities/part_numbers/new")

    responses = (
        client.get("/entities/part_numbers/source:1"),
        client.get("/entities/part_numbers/source:1/edit"),
        client.post(
            "/entities/part_numbers/source:1/update",
            data={
                "csrf_token": token,
                "revision": "0",
                "base_sha256": "0" * 64,
                "actor": "tester",
                "reason": "unpublished raw row",
            },
        ),
        client.post(
            "/entities/part_numbers/source:1/retire",
            data={
                "csrf_token": token,
                "revision": "0",
                "actor": "tester",
                "reason": "unpublished raw row",
            },
        ),
        client.post(
            "/entities/part_numbers/source:1/restore",
            data={
                "csrf_token": token,
                "revision": "0",
                "actor": "tester",
                "reason": "unpublished raw row",
            },
        ),
    )

    assert all(response.status_code == 404 for response in responses)
    assert all("找不到來源資料".encode() in response.data for response in responses)
    assert not any(
        call.tag.startswith(("write.insert-head", "write.update-head", "write.append-event"))
        for database in databases
        for call in database.calls
    )


def test_vin_mapping_pages_only_offer_dedicated_decode_and_confirmation_workflow() -> None:
    databases: list[ScriptedDatabase] = []
    app = _app(databases)
    client = app.test_client()

    listing = client.get("/entities/vin_vehicle_mappings")
    assert listing.status_code == 200
    assert "加入 VIN 解碼".encode() in listing.data
    assert "新增人工資料".encode() not in listing.data

    detail = client.get("/entities/vin_vehicle_mappings/source:1")
    assert detail.status_code == 200
    assert "編輯覆寫".encode() not in detail.data
    assert b'<section class="actions">' not in detail.data


def test_vin_mapping_generic_mutation_routes_fail_closed() -> None:
    databases: list[ScriptedDatabase] = []
    app = _app(databases)
    client = app.test_client()
    token = _csrf_token(client, "/entities/part_numbers/new")

    responses = (
        client.get("/entities/vin_vehicle_mappings/new"),
        client.post(
            "/entities/vin_vehicle_mappings/new",
            data={
                "csrf_token": token,
                "field__vin": "TEST0000000000000",
                "actor": "tester",
                "reason": "must use dedicated workflow",
            },
        ),
        client.get("/entities/vin_vehicle_mappings/source:1/edit"),
        client.post(
            "/entities/vin_vehicle_mappings/source:1/update",
            data={
                "csrf_token": token,
                "revision": "0",
                "base_sha256": "0" * 64,
                "actor": "tester",
                "reason": "must use dedicated workflow",
            },
        ),
        client.post(
            "/entities/vin_vehicle_mappings/source:1/retire",
            data={
                "csrf_token": token,
                "revision": "0",
                "actor": "tester",
                "reason": "must use dedicated workflow",
            },
        ),
        client.post(
            "/entities/vin_vehicle_mappings/source:1/restore",
            data={
                "csrf_token": token,
                "revision": "0",
                "actor": "tester",
                "reason": "must use dedicated workflow",
            },
        ),
    )

    assert all(response.status_code == 400 for response in responses)
    assert all("此資料類型為唯讀".encode() in response.data for response in responses)
    assert not any(
        call.tag.startswith("write.") for database in databases for call in database.calls
    )


def test_create_appends_overlay_event_and_vin_request_records_actor() -> None:
    databases: list[ScriptedDatabase] = []
    app = _app(databases)
    client = app.test_client()
    token = _csrf_token(client, "/entities/part_numbers/new")

    created = client.post(
        "/entities/part_numbers/new",
        data={
            "csrf_token": token,
            "field__number_raw": "P-1",
            "field__name_en_raw": "Fixture part",
            "actor": "tester",
            "reason": "manual correction",
        },
    )
    assert created.status_code == 302
    assert [call.tag for call in databases[-1].calls] == [
        "write.create-head.part_numbers",
        "write.append-event.part_numbers",
    ]

    queued = client.post(
        "/station/vins/request",
        data={
            "csrf_token": token,
            "vin": "TEST0000000000000",
            "actor": "tester",
        },
    )
    assert queued.status_code == 302
    assert [call.tag for call in databases[-1].calls] == [
        "write.request-vin-decode",
        "write.audit-vin-decode-request",
    ]
    request_call, audit_call = databases[-1].calls
    assert request_call.params == ("TEST0000000000000",)
    assert "admin_crawl_requests" in request_call.sql
    assert audit_call.params == (77, "tester", "request NHTSA VIN decode")


def test_quarantine_page_lists_and_filters() -> None:
    databases: list[ScriptedDatabase] = []
    app = _app(databases, dataset_size=3)

    response = app.test_client().get("/quarantine?state=all&pageSize=25")

    assert response.status_code == 200
    assert "Quarantine 紀錄".encode() in response.data
    assert b"IMG00001" in response.data
    assert b"IMG00003" in response.data
    assert "已處置 2098-12-31".encode() in response.data
    assert "未處置".encode() in response.data
    assert "共 1 頁".encode() in response.data
    assert b'name="expected_run_key" value="bounded-1"' in response.data
    sql = "\n".join(call.sql for call in databases[-1].calls)
    assert "part_quarantine" in sql
    assert "resolved_at IS NULL" not in sql


def test_quarantine_resolve_requires_csrf_and_redirects() -> None:
    databases: list[ScriptedDatabase] = []
    app = _app(databases, dataset_size=3)
    client = app.test_client()
    token = _csrf_token(client, "/quarantine")

    response = client.post(
        "/quarantine/1/resolve",
        data={
            "csrf_token": token,
            "resolution": "checked, removed from site",
            "state": "all",
            "run_key": "bounded-20260822-007",
            "expected_run_key": "bounded-1",
            "page": "3",
            "pageSize": "25",
        },
    )

    assert response.status_code == 302
    assert response.headers["Location"] == (
        "/quarantine?state=all&run_key=bounded-20260822-007&page=3&pageSize=25"
    )
    assert [call.tag for call in databases[-1].calls] == [
        "quarantine.lock-row",
        "quarantine.resolve",
    ]
    resolve_call = databases[-1].calls[-1]
    assert resolve_call.params == ("checked, removed from site", 1, "bounded-1")


def test_quarantine_resolve_returns_conflict_for_reopened_occurrence() -> None:
    databases: list[ScriptedDatabase] = []
    app = _app(databases, dataset_size=3)
    client = app.test_client()
    token = _csrf_token(client, "/quarantine")

    response = client.post(
        "/quarantine/1/resolve",
        data={
            "csrf_token": token,
            "resolution": "stale attempt",
            "expected_run_key": "bounded-old",
        },
    )

    assert response.status_code == 409
    assert [call.tag for call in databases[-1].calls] == ["quarantine.lock-row"]


def test_quarantine_resolve_requires_expected_run_key() -> None:
    databases: list[ScriptedDatabase] = []
    app = _app(databases, dataset_size=3)
    client = app.test_client()
    token = _csrf_token(client, "/quarantine")

    response = client.post(
        "/quarantine/1/resolve",
        data={"csrf_token": token, "resolution": "missing occurrence key"},
    )

    assert response.status_code == 400
    assert not databases[-1].calls


def test_quarantine_rejects_unsupported_page_size_before_query() -> None:
    databases: list[ScriptedDatabase] = []
    app = _app(databases)

    response = app.test_client().get("/quarantine?pageSize=15")

    assert response.status_code == 400
    assert not databases[-1].calls
