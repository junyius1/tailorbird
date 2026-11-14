"""store HTTP API(替代 langgraph_api 的 ``/store/*``;后端为 AsyncPostgresStore)。

端点与 SDK 客户端(langgraph_sdk._async.store)逐一对齐:

- ``PUT /store/items`` body ``{namespace:[..], key, value?, index?, ttl?}`` → 200
- ``GET /store/items?namespace=a.b&key=k[&refresh_ttl=]`` → Item 或 404
- ``DELETE /store/items`` body ``{namespace:[..], key}`` → 204
- ``POST /store/items/search`` body ``{namespace_prefix:[..], filter?, limit?, offset?}``
  → ``{"items": [...]}``
- ``POST /store/namespaces`` body ``{prefix?, suffix?, max_depth?, limit?, offset?}``
  → ``{"namespaces": [...]}``

Item 形状(SDK 文档):``{namespace, key, value, created_at, updated_at}``。
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from ..deps import get_store

router = APIRouter(prefix="/store", tags=["store"])


def _ns_from_dotted(value: str) -> list[str]:
    return [part for part in value.split(".") if part != ""]


def _item_dict(item: Any) -> dict[str, Any]:
    """store.Item → SDK 线形。"""
    namespace = list(item.namespace) if item.namespace is not None else []
    created = getattr(item, "created_at", None)
    updated = getattr(item, "updated_at", None)
    return {
        "namespace": list(namespace),
        "key": item.key,
        "value": item.value,
        "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
        "updated_at": updated.isoformat() if hasattr(updated, "isoformat") else updated,
    }


@router.put("/items")
async def put_item(request: Request) -> JSONResponse:
    body = await _json_body(request)
    namespace = _require_namespace(body)
    key = body.get("key")
    if not isinstance(key, str) or not key:
        raise HTTPException(status_code=422, detail="key is required")
    value = body.get("value")
    store = get_store(request)
    kwargs: dict[str, Any] = {}
    if "index" in body and body["index"] is not None:
        kwargs["index"] = body["index"]
    if "ttl" in body and body["ttl"] is not None:
        kwargs["ttl"] = body["ttl"]
    await store.aput(tuple(namespace), key, value, **kwargs)
    return JSONResponse(content={})


@router.get("/items")
async def get_item(
    request: Request,
    namespace: str = Query(...),
    key: str = Query(...),
) -> JSONResponse:
    store = get_store(request)
    item = await store.aget(tuple(_ns_from_dotted(namespace)), key)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return JSONResponse(content=_item_dict(item))


@router.delete("/items")
async def delete_item(request: Request) -> JSONResponse:
    body = await _json_body(request)
    namespace = _require_namespace(body)
    key = body.get("key")
    if not isinstance(key, str) or not key:
        raise HTTPException(status_code=422, detail="key is required")
    store = get_store(request)
    await store.adelete(tuple(namespace), key)
    return JSONResponse(content={}, status_code=204)


@router.post("/items/search")
async def search_items(request: Request) -> JSONResponse:
    body = await _json_body(request)
    prefix = body.get("namespace_prefix") or []
    if isinstance(prefix, str):
        prefix = _ns_from_dotted(prefix)
    store = get_store(request)
    result = await store.asearch(
        tuple(prefix),
        filter=body.get("filter"),
        limit=int(body.get("limit") or 10),
        offset=int(body.get("offset") or 0),
    )
    return JSONResponse(content={"items": [_item_dict(it) for it in result]})


@router.post("/namespaces")
async def list_namespaces(request: Request) -> JSONResponse:
    body = await _json_body(request)

    def _to_tuple(v: Any) -> tuple[str, ...] | None:
        if v is None:
            return None
        if isinstance(v, str):
            return tuple(_ns_from_dotted(v))
        if isinstance(v, list):
            return tuple(v)
        return None

    store = get_store(request)
    result = await store.alist_namespaces(
        prefix=_to_tuple(body.get("prefix")),
        suffix=_to_tuple(body.get("suffix")),
        max_depth=body.get("max_depth"),
        limit=int(body.get("limit") or 100),
        offset=int(body.get("offset") or 0),
    )
    namespaces = list(result) if isinstance(result, list) else list(result or [])
    return JSONResponse(
        content={
            "namespaces": [
                list(ns) if isinstance(ns, (list, tuple)) else ns for ns in namespaces
            ]
        }
    )


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=415, detail="unsupported media type")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be an object")
    return body


def _require_namespace(body: dict[str, Any]) -> list[str]:
    ns = body.get("namespace")
    if isinstance(ns, str):
        return _ns_from_dotted(ns)
    if isinstance(ns, list) and all(isinstance(p, str) for p in ns):
        return list(ns)
    raise HTTPException(status_code=422, detail="namespace is required")
