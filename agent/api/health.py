"""健康检查 + run 完成 webhook + 调试探针路由。

- ``GET /health``:存活探针(无认证);
- ``POST /webhooks/run-complete``:run 终结回调(进程内 loopback 投递;
  token 校验 fail-closed,这是该路由唯一的门);
- ``GET/POST /debug/queue-probe``:队列调试快照/开关(需会话);
- ``GET /debug/llm-gate``:LLM gate 快照(mode/在飞/排队)。
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from ..completion import handle_run_completion, verify_run_complete_token
from ..dashboard.auth import require_session

router = APIRouter()
_SESSION_DEP = Depends(require_session)


@router.get("/health")
async def health_check() -> dict[str, str]:
    """存活探针(无认证;供 UI 代理 / 运维用)。"""
    return {"status": "healthy"}


@router.get("/ok")
async def ok_check() -> dict[str, str]:
    """轻量存活探针(UI vite 代理 /ok 用,无认证)。"""
    return {"status": "ok"}


@router.post("/webhooks/run-complete")
async def run_complete_webhook(request: Request) -> dict[str, Any]:
    """run 完成回调(进程内 loopback;token fail-closed)。"""
    if not verify_run_complete_token(request.query_params.get("token")):
        raise HTTPException(status_code=401, detail="Invalid run-complete token")
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return {"status": "error", "message": "Invalid JSON"}
    if not isinstance(payload, dict):
        return {"status": "ignored", "reason": "payload not an object"}
    return await handle_run_completion(payload)


@router.get("/debug/queue-probe")
async def debug_queue_probe_snapshot(
    session: dict[str, Any] = _SESSION_DEP,
) -> dict[str, Any]:
    """队列调试快照(需会话)。"""
    del session
    from ..utils.queue_debug import snapshot

    return snapshot()


@router.post("/debug/queue-probe")
async def debug_queue_probe_toggle(
    request: Request,
    session: dict[str, Any] = _SESSION_DEP,
) -> dict[str, Any]:
    """队列调试开关(需会话;body ``{"enabled": bool}``)。"""
    del session
    from ..utils.queue_debug import is_enabled, set_enabled, snapshot

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="JSON body required") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("enabled"), bool):
        raise HTTPException(status_code=400, detail='body must be {"enabled": bool}')
    set_enabled(payload["enabled"])
    return {"enabled": is_enabled(), **snapshot()}


@router.get("/debug/llm-gate")
async def debug_llm_gate(session: dict[str, Any] = _SESSION_DEP) -> dict[str, Any]:
    """LLM gate 快照(全进程并发控制;需会话)。"""
    del session
    from ..utils.llm_gate import get_llm_gate

    return get_llm_gate().snapshot()
