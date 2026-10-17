import re
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import FileResponse, JSONResponse

from ehtg import __version__
from ehtg.analysis import AnalysisQueue
from ehtg.api.middleware import RequestBoundary
from ehtg.api.responses import (
    DeviceList,
    DraftResponse,
    ErrorResponse,
    JobList,
    JobResponse,
    JobState,
    PairingResponse,
    PublicationReceipt,
)
from ehtg.api.schemas import (
    CreateBatch,
    CreateDraft,
    EditBatch,
    EditDraft,
    PublishBatch,
    PublishDraft,
    RedeemCode,
    ReviewJob,
)
from ehtg.artifacts import ArtifactStore
from ehtg.auth import DeviceAuth
from ehtg.domain.errors import AppError
from ehtg.domain.publication import PublicationOptions
from ehtg.domain.selection import PageSelection
from ehtg.publisher import JobControl
from ehtg.service import GalleryService
from ehtg.settings import Settings
from ehtg.source.adapter import GalleryDLSource
from ehtg.source.session import SessionStore
from ehtg.storage.database import Database

_bearer = HTTPBearer(auto_error=False)


def current_device(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AppError("UNAUTHORIZED", "Требуется токен устройства.", 401)
    auth: DeviceAuth = request.app.state.auth
    return auth.authenticate(credentials.credentials, request.headers.get("origin", ""))


def revision(if_match: Annotated[str, Header()]) -> int:
    match = re.fullmatch(r'"([1-9][0-9]{0,8})"', if_match)
    if match is None:
        raise AppError("INVALID_REVISION", 'Укажите If-Match в формате "2".')
    return int(match[1])


def error_response(request: Request, error: AppError) -> JSONResponse:
    headers = {"X-Request-ID": request.state.request_id, "Cache-Control": "no-store"}
    if error.status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(
        {
            "code": error.code,
            "message": error.message,
            "retryable": error.retryable,
            "request_id": request.state.request_id,
            "details": error.details,
        },
        status_code=error.status,
        headers=headers,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings if settings is not None else Settings()
    database = Database(config.database_path)
    service = GalleryService(
        database,
        config.owner_id,
        config.channel_id,
        max_pages=config.max_pages,
        max_queue=config.max_queue,
    )
    auth = DeviceAuth(
        database,
        config.owner_id,
        config.allowed_origins,
        attempts_per_minute=config.pairing_attempts_per_minute,
    )
    analysis = AnalysisQueue(
        database,
        config.owner_id,
        lease_seconds=config.analysis_lease_seconds,
        max_attempts=config.analysis_max_attempts,
        retry_seconds=config.analysis_retry_seconds,
        max_pages=config.max_pages,
        max_queue=config.max_queue,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await run_in_threadpool(database.initialize)
        yield

    app = FastAPI(
        title="E-Hentai → Telegram",
        version=__version__,
        lifespan=lifespan,
        root_path=config.root_path,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.service = service
    app.state.auth = auth
    app.state.analysis = analysis
    controls = JobControl(service)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(config.allowed_hosts))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(config.allowed_origins),
        allow_methods=["GET", "POST", "PATCH"],
        allow_headers=["Authorization", "Content-Type", "If-Match", "Idempotency-Key"],
        expose_headers=["ETag", "X-Request-ID"],
    )
    app.add_middleware(RequestBoundary, max_body_bytes=config.max_body_bytes)

    @app.exception_handler(AppError)
    async def application_error(request: Request, error: AppError) -> JSONResponse:
        return error_response(request, error)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        # Pydantic errors can contain the original body and credentials.
        return error_response(request, AppError("INVALID_REQUEST", "Проверьте поля запроса.", 422))

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> JSONResponse:
        return error_response(
            request, AppError("HTTP_ERROR", "Запрос не поддерживается.", error.status_code)
        )

    @app.exception_handler(sqlite3.OperationalError)
    async def storage_error(request: Request, error: sqlite3.OperationalError) -> JSONResponse:
        return error_response(
            request, AppError("STORAGE_UNAVAILABLE", "Хранилище недоступно.", 503, retryable=True)
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception) -> JSONResponse:
        return error_response(
            request, AppError("INTERNAL_ERROR", "Внутренняя ошибка сервера.", 500)
        )

    @app.get("/healthz", include_in_schema=False)
    def health() -> dict[str, str]:
        with database.read() as connection:
            connection.execute("SELECT 1")
        return {"status": "ok"}

    errors: dict[int | str, dict[str, Any]] = {
        code: {"model": ErrorResponse} for code in (401, 403, 404, 409, 413, 422, 429, 503)
    }
    pairing = APIRouter(prefix="/api/v1", tags=["pairing"], responses=errors)

    @pairing.post("/pairing/redeem", response_model=PairingResponse)
    def redeem(body: RedeemCode, request: Request) -> dict[str, str]:
        return auth.redeem(body.code, body.device_name, request.headers.get("origin", ""))

    api = APIRouter(prefix="/api/v1", dependencies=[Depends(current_device)], responses=errors)

    @api.get("/capabilities")
    def capabilities() -> dict[str, Any]:
        return {
            "api_version": "v1",
            "server_version": __version__,
            "features": {
                "drafts": True,
                "persistent_queue": True,
                "device_pairing": True,
                "source_analysis": config.source_enabled,
                "analysis_worker": True,
                "telegram_publishing": config.bot_token is not None,
                "publication_worker": True,
                "batch": True,
                "caption_preview": True,
                "cover_preview": True,
                "image_url_resolution": True,
            },
            "limits": {
                "max_pages": config.max_pages,
                "max_batch": 50,
                "max_media_bytes": config.media_max_bytes,
                "max_media_pixels": config.media_max_pixels,
                "max_job_bytes": config.job_max_bytes,
                "min_free_bytes": config.min_free_bytes,
                "max_queue": config.max_queue,
                "max_body_bytes": config.max_body_bytes,
                "max_source_html_bytes": config.source_max_html_bytes,
                "max_source_index_pages": config.source_max_index_pages,
                "analysis_max_attempts": config.analysis_max_attempts,
                "analysis_timeout_seconds": config.analysis_timeout,
            },
        }

    @api.get("/status")
    def status() -> dict[str, Any]:
        with database.read() as connection:
            control = connection.execute("SELECT paused,reason_code FROM source_control").fetchone()
            queue_paused = bool(
                connection.execute("SELECT paused FROM queue_control").fetchone()[0]
            )
        return {
            "database": "ready",
            "source": {
                "enabled": config.source_enabled,
                "paused": bool(control["paused"]),
                "reason_code": control["reason_code"],
                "session": SessionStore(config.source_session_path).info(),
            },
            "worker": "analysis_and_publication",
            "telegram": "configured" if config.bot_token else "not_configured",
            "queue_paused": queue_paused,
        }

    @api.get("/devices", response_model=DeviceList)
    def devices() -> dict[str, Any]:
        return {"items": auth.devices()}

    @api.post("/devices/{device_id}/revoke")
    def revoke(device_id: UUID) -> dict[str, bool]:
        auth.revoke(str(device_id))
        return {"revoked": True}

    @api.post("/drafts", status_code=202, response_model=DraftResponse)
    def create_draft(body: CreateDraft, response: Response) -> dict[str, Any]:
        result = service.create_draft(
            body.source_url, PageSelection(**body.page_selection.model_dump())
        )
        response.headers["ETag"] = f'"{result["revision"]}"'
        return result

    @api.get("/drafts/{draft_id}", response_model=DraftResponse)
    def get_draft(draft_id: UUID, response: Response) -> dict[str, Any]:
        result = service.get_draft(str(draft_id))
        response.headers["ETag"] = f'"{result["revision"]}"'
        return result

    @api.get("/drafts/{draft_id}/cover")
    def draft_cover(draft_id: UUID) -> FileResponse:
        draft = service.get_draft(str(draft_id))
        snapshot = draft["snapshot"]
        if snapshot is None or not snapshot["cover_url"]:
            raise AppError("NOT_FOUND", "Обложка недоступна.", 404)
        path = GalleryDLSource(config).cover_path(snapshot["cover_url"])
        if not path.is_file() or path.is_symlink():
            raise AppError("NOT_FOUND", "Обложка не была загружена анализатором.", 404)
        return FileResponse(
            path,
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @api.patch("/drafts/{draft_id}", response_model=DraftResponse)
    def edit_draft(
        draft_id: UUID,
        body: EditDraft,
        response: Response,
        expected: Annotated[int, Depends(revision)],
    ) -> dict[str, Any]:
        result = service.edit_selection(
            str(draft_id),
            expected,
            PageSelection(**body.page_selection.model_dump()),
            PublicationOptions(**body.options.model_dump()) if body.options is not None else None,
        )
        response.headers["ETag"] = f'"{result["revision"]}"'
        return result

    @api.post("/drafts/{draft_id}/publish", status_code=202, response_model=PublicationReceipt)
    def publish(
        draft_id: UUID,
        body: PublishDraft,
        request: Request,
        expected: Annotated[int, Depends(revision)],
        key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, Any]:
        return service.publish(str(draft_id), expected, key, request_id=request.state.request_id)

    @api.post("/drafts/{draft_id}/analysis/cancel", response_model=DraftResponse)
    def cancel_analysis(
        draft_id: UUID, response: Response, expected: Annotated[int, Depends(revision)]
    ) -> dict[str, Any]:
        analysis.cancel(str(draft_id), expected)
        return get_draft(draft_id, response)

    @api.post("/drafts/{draft_id}/analysis/retry", status_code=202, response_model=DraftResponse)
    def retry_analysis(
        draft_id: UUID, response: Response, expected: Annotated[int, Depends(revision)]
    ) -> dict[str, Any]:
        analysis.retry(str(draft_id), expected)
        return get_draft(draft_id, response)

    @api.get("/jobs", response_model=JobList)
    def jobs(
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        state: JobState | None = None,
    ) -> dict[str, Any]:
        return service.list_jobs(after=after, limit=limit, state=state)

    @api.get("/jobs/{job_id}", response_model=JobResponse)
    def get_job(job_id: UUID, response: Response) -> dict[str, Any]:
        result = service.get_job(str(job_id))
        response.headers["ETag"] = f'"{result["revision"]}"'
        return result

    @api.post("/jobs/{job_id}/cancel", response_model=JobResponse)
    def cancel_job(job_id: UUID, expected: Annotated[int, Depends(revision)]) -> dict[str, Any]:
        return controls.action(str(job_id), expected, "cancel")

    @api.post("/jobs/{job_id}/retry", response_model=JobResponse)
    def retry_job(job_id: UUID, expected: Annotated[int, Depends(revision)]) -> dict[str, Any]:
        return controls.action(str(job_id), expected, "retry")

    @api.post("/jobs/{job_id}/review", response_model=JobResponse)
    def review_job(
        job_id: UUID, body: ReviewJob, expected: Annotated[int, Depends(revision)]
    ) -> dict[str, Any]:
        return controls.review(
            str(job_id), expected, body.decision, evidence=body.evidence, page=body.page
        )

    @api.post("/queue/pause")
    def pause_queue() -> dict[str, bool]:
        return controls.pause(True)

    @api.post("/queue/resume")
    def resume_queue() -> dict[str, bool]:
        return controls.pause(False)

    @api.get("/jobs/{job_id}/pages/{page_index}/preview")
    def preview_page(job_id: UUID, page_index: int) -> FileResponse:
        job = service.get_job(str(job_id))
        if page_index not in [page["source_page_index"] for page in job["pages"]]:
            raise AppError("NOT_FOUND", "Страница не найдена.", 404)
        path = ArtifactStore(config, database).path(str(job_id), page_index, "photo")
        if not path.is_file():
            raise AppError("NOT_FOUND", "Предпросмотр ещё не подготовлен.", 404)
        return FileResponse(
            path,
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @api.post("/jobs/{job_id}/repost", status_code=202, response_model=DraftResponse)
    def repost(job_id: UUID) -> dict[str, Any]:
        return service.repost(str(job_id))

    @api.post("/batches", status_code=202)
    def create_batch(body: CreateBatch, response: Response) -> dict[str, Any]:
        result = service.create_batch(body.source_urls)
        response.headers["ETag"] = f'"{result["revision"]}"'
        return result

    @api.get("/batches/{batch_id}")
    def get_batch(batch_id: UUID, response: Response) -> dict[str, Any]:
        result = service.get_batch(str(batch_id))
        response.headers["ETag"] = f'"{result["revision"]}"'
        return result

    @api.patch("/batches/{batch_id}")
    def edit_batch(
        batch_id: UUID,
        body: EditBatch,
        response: Response,
        expected: Annotated[int, Depends(revision)],
    ) -> dict[str, Any]:
        result = service.edit_batch(str(batch_id), expected, body.draft_ids)
        response.headers["ETag"] = f'"{result["revision"]}"'
        return result

    @api.post("/batches/{batch_id}/publish", status_code=202)
    def publish_batch(
        batch_id: UUID,
        body: PublishBatch,
        expected: Annotated[int, Depends(revision)],
        key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, Any]:
        return service.publish_batch(
            str(batch_id), expected, [(item.id, item.revision) for item in body.items], key
        )

    app.include_router(pairing)
    app.include_router(api)
    return app
