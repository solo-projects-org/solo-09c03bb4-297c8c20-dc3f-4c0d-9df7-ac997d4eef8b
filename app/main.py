"""FastAPI 应用：网页 + JSON API。"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import db, handover, logic
from .errors import DomainError

STATIC_DIR = Path(__file__).parent / "static"


class VersionIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    csv_text: str = Field(min_length=1)


class HandoverIn(BaseModel):
    handover_no: str = Field(min_length=1, max_length=handover.MAX_NO)
    name: str = Field(min_length=1, max_length=200)
    kind: str
    upstream_no: str | None = Field(default=None, max_length=handover.MAX_NO)
    csv_text: str = Field(min_length=1)


class MarkerIn(BaseModel):
    version_id: int
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    note: str = Field(default="", max_length=500)


class LiftIn(BaseModel):
    version_id: int
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)


def create_app(db_path: str | None = None) -> FastAPI:
    path = db_path or os.environ.get("DB_PATH", os.path.join("data", "app.db"))
    db.init_db(path)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # 重启后接着处理原来的待办：把已齐备的暂存表入库
        conn = db.connect(path)
        try:
            handover.run_admission_pass(conn)
        except Exception:
            # 放行待办失败时保留原状，不阻断启动；仍可在页面点「处理待办」重试
            logging.getLogger(__name__).exception("启动时放行暂存交接表失败，保留待办")
        finally:
            conn.close()
        yield

    app = FastAPI(title="口述史馆禁播消音标记管理", version="1.0.0", lifespan=lifespan)
    app.state.db_path = path

    @contextmanager
    def session():
        conn = db.connect(app.state.db_path)
        try:
            yield conn
        finally:
            conn.close()

    @app.exception_handler(DomainError)
    async def domain_error_handler(_request: Request, exc: DomainError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    # ------------------------------------------------------------- 网页

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    # ------------------------------------------------------------- 剪辑版

    @app.post("/api/versions", status_code=201)
    def api_create_version(body: VersionIn):
        with session() as conn:
            return logic.create_version(conn, body.name, body.csv_text)

    @app.get("/api/versions")
    def api_list_versions():
        with session() as conn:
            return logic.list_versions(conn)

    @app.get("/api/versions/{version_id}")
    def api_get_version(version_id: int):
        with session() as conn:
            return logic.get_version(conn, version_id)

    @app.get("/api/versions/{version_id}/muted")
    def api_muted(version_id: int):
        with session() as conn:
            return logic.muted_for_version(conn, version_id)

    # ------------------------------------------------------------- 交接收表

    @app.post("/api/handovers", status_code=201)
    def api_create_handover(body: HandoverIn):
        with session() as conn:
            detail, created = handover.create_handover(
                conn, body.handover_no, body.name, body.kind, body.upstream_no, body.csv_text
            )
        if not created:
            # 同编号同内容重送：幂等返回既有表
            return JSONResponse(status_code=200, content=detail)
        return detail

    @app.get("/api/handovers")
    def api_list_handovers():
        with session() as conn:
            return handover.list_handovers(conn)

    @app.get("/api/handovers/{handover_no}")
    def api_get_handover(handover_no: str):
        with session() as conn:
            return handover.get_handover(conn, handover_no)

    @app.put("/api/handovers/{handover_no}")
    def api_replace_handover(handover_no: str, body: HandoverIn):
        # 以路径中的交接编号为准，避免误改其他表
        if body.handover_no.strip() != handover_no.strip():
            raise DomainError(422, "路径中的交接编号与提交内容不一致")
        with session() as conn:
            return handover.replace_handover(
                conn, handover_no, body.name, body.kind, body.upstream_no, body.csv_text
            )

    @app.post("/api/handovers/process")
    def api_process_handovers():
        with session() as conn:
            handover.run_admission_pass(conn)
            return handover.list_handovers(conn)

    # ------------------------------------------------------------- 标记

    @app.post("/api/markers", status_code=201)
    def api_create_marker(body: MarkerIn):
        with session() as conn:
            return logic.create_marker(
                conn, body.version_id, body.start_ms, body.end_ms, body.note
            )

    @app.get("/api/markers")
    def api_list_markers():
        with session() as conn:
            return logic.list_markers(conn)

    @app.get("/api/markers/{marker_id}")
    def api_get_marker(marker_id: int):
        with session() as conn:
            return logic.get_marker_detail(conn, marker_id)

    @app.post("/api/markers/{marker_id}/withdraw")
    def api_withdraw_marker(marker_id: int):
        with session() as conn:
            return logic.withdraw_marker(conn, marker_id)

    @app.post("/api/markers/{marker_id}/lifts", status_code=201)
    def api_create_lift(marker_id: int, body: LiftIn):
        with session() as conn:
            return logic.create_lift(
                conn, marker_id, body.version_id, body.start_ms, body.end_ms
            )

    @app.get("/api/markers/{marker_id}/impact")
    def api_marker_impact(marker_id: int):
        with session() as conn:
            return logic.marker_impact(conn, marker_id)

    return app


app = create_app()
