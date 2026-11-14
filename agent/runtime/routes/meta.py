"""meta 端点:``/ok`` ``/info`` ``/``(替代 langgraph_api 的 meta 面)。"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

router = APIRouter(tags=["meta"])


@router.get("/", response_class=PlainTextResponse)
async def root() -> str:
    return "ok"


@router.get("/ok")
async def ok(request: Request) -> dict:
    """健康检查。``?check_db=1`` 附带 DB 探测。"""
    result: dict = {"ok": True}
    if request.query_params.get("check_db") in ("1", "true"):
        try:
            from ..postgres_persistence import require_database_uri
            import psycopg

            uri = require_database_uri()
            async with await psycopg.AsyncConnection.connect(
                uri, autocommit=True, connect_timeout=5
            ) as conn:
                await conn.execute("select 1")
            result["database"] = "ok"
        except Exception as exc:  # noqa: BLE001
            result["database"] = f"error: {exc}"
            result["ok"] = False
    return JSONResponse(content=result)


@router.get("/info")
async def info() -> dict:
    """运行时元信息(SDK/CLI 启动探测用)。"""
    import langgraph

    from ..postgres_persistence import require_database_uri

    try:
        require_database_uri()
        db = "ok"
    except Exception:  # noqa: BLE001
        db = "error"
    return JSONResponse(
        content={
            "version": "0.1.0",
            "langgraph_py_version": getattr(langgraph, "__version__", "unknown"),
            "flags": {
                "assistants": True,
                "crons": True,
                "langsmith": False,
                "langsmith_tracing_replicas": False,
                "langsmith_tracing_session_on_runs": False,
            },
            "host": {
                "kind": "self-hosted",
                "project_id": None,
                "revision_id": None,
                "tenant_id": None,
                "database": db,
            },
        }
    )
