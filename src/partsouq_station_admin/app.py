from __future__ import annotations

from collections.abc import Callable

from flask import Flask

from partsouq_station_admin.config import AdminConfig
from partsouq_station_admin.db import AdminDatabase, RequestDatabase
from partsouq_station_admin.query_trace import QueryTrace
from partsouq_station_admin.routes import bp

DatabaseFactory = Callable[[AdminConfig, QueryTrace], RequestDatabase]


def _default_database_factory(config: AdminConfig, trace: QueryTrace) -> AdminDatabase:
    return AdminDatabase.connect(config, trace)


def create_app(
    config: AdminConfig | None = None,
    *,
    database_factory: DatabaseFactory = _default_database_factory,
) -> Flask:
    resolved = config or AdminConfig.from_env()
    resolved.validate_server_mode()
    app = Flask(__name__)
    app.secret_key = resolved.resolved_secret_key()
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=resolved.secure_cookie,
        MAX_CONTENT_LENGTH=1_000_000,
    )
    app.extensions["partsouq_admin_config"] = resolved
    app.extensions["partsouq_admin_database_factory"] = database_factory
    app.register_blueprint(bp)
    return app


def main() -> None:
    config = AdminConfig.from_env()
    app = create_app(config)
    app.run(host=config.bind_host, port=config.bind_port, debug=False)


if __name__ == "__main__":
    main()
