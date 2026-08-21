from __future__ import annotations

import re

from partsouq_station_admin.app import create_app
from partsouq_station_admin.config import AdminConfig
from partsouq_station_admin.query_trace import QueryTrace

from .fakes import ScriptedDatabase


def _app(
    databases: list[ScriptedDatabase],
    *,
    dataset_size: int = 1,
) -> object:
    def factory(_config: AdminConfig, trace: QueryTrace) -> ScriptedDatabase:
        database = ScriptedDatabase(trace, dataset_size=dataset_size)
        databases.append(database)
        return database

    app = create_app(
        AdminConfig(secret_key="test-secret", page_size=25),
        database_factory=factory,
    )
    app.testing = True
    return app


def _csrf_token(client: object, path: str) -> str:
    response = client.get(path)
    match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
    assert match is not None
    return match.group(1).decode()


def test_list_allows_supported_page_size_and_keeps_it_on_next_link() -> None:
    databases: list[ScriptedDatabase] = []
    app = _app(databases, dataset_size=1000)

    response = app.test_client().get("/entities/part_numbers?pageSize=200")

    assert response.status_code == 200
    assert response.headers["X-Admin-Query-Count"] == "4"
    assert b'<option value="200" selected>' in response.data
    assert b'<option value="formal" selected>' in response.data
    assert "目前正式資料（full 優先，否則 bounded）".encode() in response.data
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
    assert "逐 VIN 解碼需由站方輸入合法 VIN".encode() in response.data


def test_part_detail_does_not_present_adapter_metadata_as_raw_http_evidence() -> None:
    databases: list[ScriptedDatabase] = []
    app = _app(databases)

    response = app.test_client().get("/entities/part_numbers/source:1")

    assert response.status_code == 200
    assert "零件表 Code／圖號呼叫碼".encode() in response.data
    assert "不代表已保留原始 HTTP 回應或 hash".encode() in response.data


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
    assert resolve_call.params == ("checked, removed from site", 1)


def test_quarantine_rejects_unsupported_page_size_before_query() -> None:
    databases: list[ScriptedDatabase] = []
    app = _app(databases)

    response = app.test_client().get("/quarantine?pageSize=15")

    assert response.status_code == 400
    assert not databases[-1].calls
