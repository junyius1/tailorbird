"""v3 协议事件 → V2 wire 信封转换(单线程内同步调用)。

驱动端:``graph.astream_events(input, config, version="v3")`` 产出
ProtocolEvent ``{"type":"event","method":...,"params":{"namespace":[...],"data":...}}``。
本模块把它翻成线形 V2 信封(与 langchain_protocol 逐字段对齐):

    {"type":"event","event_id":...,"seq":...,"method":...,
     "params":{"namespace":[...],"timestamp":ms,"data":...}}

语义(对参考实现 langgraph_api.event_streaming.session 的简化,无 Redis):
- ``values``:data 里剥掉 ``__interrupt__``(若有);``params.interrupts``
  (v3 在 values 快照上附带的 Interrupt 列表)→ 先发 ``input.requested``
  信封再发 values 信封;
- ``messages``:data 是 ``(content_block_dict, metadata)`` 元组,只转发
  满足 ``isinstance(dict) and isinstance(d.get("event"), str)`` 的
  content-block 形态,丢弃 legacy AIMessageChunk 元组;
- ``updates``:归一化成 ``{node?, values}``;``node=="__interrupt__"`` →
  只发 input.requested;
- ``checkpoints``/``tasks``/``lifecycle``/``custom``:data 直接转发
  (custom:<name> → method=custom + data={name,payload});
- 事件序:每个 v3 事件可产生多个 V2 信封(input.requested 在前),
  ``seq`` 由调用方(引擎)单调分配。
"""

from __future__ import annotations

import time
from typing import Any

from .state import _jsonable


def _now_ms() -> int:
    return int(time.time() * 1000)


def _interrupt_field(value: Any) -> Any:
    """Interrupt 对象 → 线形(与 state.py 的 _patch_interrupt 同语义)。"""
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump())
    return _jsonable(value)


def _as_interrupt_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and isinstance(value.get("__interrupt__"), list):
        return value["__interrupt__"]
    return []


def _interrupt_requests(interrupts: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for it in interrupts:
        iid = getattr(it, "id", None)
        if iid is None and isinstance(it, dict):
            iid = it.get("id")
        if not isinstance(iid, str) or not iid:
            continue
        item: dict[str, Any] = {"interrupt_id": iid}
        value = getattr(it, "value", None)
        if value is None and isinstance(it, dict):
            value = it.get("value")
        if value is not None:
            item["payload"] = _interrupt_field(value)
        out.append(item)
    return out


def _envelope(
    seq: int,
    event_id: str,
    method: str,
    namespace: list[str],
    data: Any,
    node: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "namespace": list(namespace),
        "timestamp": _now_ms(),
        "data": data,
    }
    if node is not None:
        params["node"] = node
    return {
        "type": "event",
        "event_id": event_id,
        "seq": seq,
        "method": method,
        "params": params,
    }


def _checkpoint_envelope(data: Any) -> dict[str, Any] | None:
    """v3 checkpoints data → CDDL Checkpoint {id, parent_id?, step?, source?}。

    v3 原形是 ``{config, parent_config, tasks, values, metadata, next}``
    (config 只含 configurable,checkpoint_id 在 ``configurable.checkpoint_id``);
    也兼容已整形的 ``{id,...}`` 形态(防御未来上游变化)。
    """
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("id"), str):
        return data
    cfg = data.get("config") or {}
    conf = cfg.get("configurable") or {}
    cp_id = conf.get("checkpoint_id")
    if not isinstance(cp_id, str) or not cp_id:
        return None
    out: dict[str, Any] = {"id": cp_id}
    pconf = (data.get("parent_config") or {}).get("configurable") or {}
    p_id = pconf.get("checkpoint_id")
    if isinstance(p_id, str) and p_id:
        out["parent_id"] = p_id
    meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    if isinstance(meta.get("source"), str):
        out["source"] = meta["source"]
    if isinstance(meta.get("step"), int):
        out["step"] = meta["step"]
    return out


def _is_v2_message(data: Any) -> bool:
    return isinstance(data, dict) and isinstance(data.get("event"), str)


def _messages_payload(data: Any) -> dict[str, Any] | None:
    """v3 messages 的 data 元组 → content-block dict(legacy 元组返回 None)。"""
    if isinstance(data, tuple) and len(data) >= 1 and _is_v2_message(data[0]):
        return data[0]
    if _is_v2_message(data):
        return data
    return None


def _update_values(value: Any) -> Any:
    return value if isinstance(value, dict) else {"value": value}


def _strip_interrupts(value: Any) -> tuple[list[dict[str, Any]], Any]:
    """data 里的 __interrupt__ → (input_requests, 干净 data)。"""
    if isinstance(value, dict) and "__interrupt__" in value:
        requests = [
            r
            for it in _as_interrupt_list(value["__interrupt__"])
            for r in _interrupt_requests([it])
        ]
        cleaned = {k: v for k, v in value.items() if k != "__interrupt__"}
        return requests, cleaned
    return [], value


class EventNormalizer:
    """把一个 v3 事件翻成 0..n 个 V2 信封。

    信封的 seq/event_id 由分配器(引擎)注入:``assign()`` 接收
    ``(method, namespace, data, node)`` 返回 (seq, event_id),保持
    "引擎是唯一 seq/event_id 权威"。
    """

    def __init__(self, assign: Any) -> None:
        self._assign = assign

    def _mint(self, method: str, namespace: list[str], data: Any, node: str | None = None) -> dict[str, Any]:
        seq, event_id = self._assign(method, namespace, data, node)
        return _envelope(seq, event_id, method, namespace, data, node)

    def handle(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        """v3 ProtocolEvent → V2 信封列表(按 wire 顺序)。"""
        method = event.get("method")
        if not isinstance(method, str):
            return []
        params = event.get("params") or {}
        raw_ns = params.get("namespace")
        namespace: list[str] = (
            list(raw_ns)
            if isinstance(raw_ns, list) and all(isinstance(s, str) for s in raw_ns)
            else []
        )
        data = params.get("data")
        out: list[dict[str, Any]] = []

        if method.startswith("custom:"):
            name = method[len("custom:"):]
            if name:
                out.append(self._mint("custom", namespace, {"name": name, "payload": _jsonable(data)}))
            return out

        if method == "messages":
            payload = _messages_payload(data)
            if payload is None:
                return []
            out.append(self._mint("messages", namespace, _jsonable(payload)))
            return out

        if method == "values":
            requests = [r for it in (params.get("interrupts") or ()) for r in _interrupt_requests([it])]
            requests += [
                r for it in _as_interrupt_list(data) for r in _interrupt_requests([it])
            ]
            seen: set[str] = set()
            for req in requests:
                iid = req.get("interrupt_id")
                if iid and iid not in seen:
                    seen.add(iid)
                    out.append(self._mint("input.requested", namespace, req))
            cleaned = data
            if isinstance(data, dict) and "__interrupt__" in data:
                _, cleaned = _strip_interrupts(data)
            out.append(self._mint("values", namespace, _jsonable(cleaned)))
            return out

        if method == "updates":
            if isinstance(data, dict) and len(data) == 1:
                node, node_values = next(iter(data.items()))
                normalized: dict[str, Any] = {"node": node, "values": _update_values(node_values)}
            else:
                normalized = {"values": _update_values(data)}
            if normalized.get("node") == "__interrupt__":
                requests = [
                    r for it in _as_interrupt_list(normalized.get("values")) for r in _interrupt_requests([it])
                ]
                for req in requests:
                    out.append(self._mint("input.requested", namespace, req))
                return out
            reqs, cleaned = _strip_interrupts(normalized.get("values"))
            for req in reqs:
                out.append(self._mint("input.requested", namespace, req))
            payload: dict[str, Any] = {"values": _jsonable(cleaned)}
            node = normalized.get("node")
            if isinstance(node, str):
                payload = {"node": node, **payload}
            out.append(self._mint("updates", namespace, payload))
            return out

        if method == "checkpoints":
            env = _checkpoint_envelope(data)
            if env is not None:
                out.append(self._mint("checkpoints", namespace, _jsonable(env)))
            return out

        if method == "lifecycle":
            if isinstance(data, dict) and isinstance(data.get("event"), str):
                payload = _jsonable(data)
                payload.setdefault("namespace", list(namespace))
                out.append(self._mint("lifecycle", namespace, payload))
            return out

        if method == "tasks":
            if data is not None:
                out.append(self._mint("tasks", namespace, _jsonable(data)))
            return out

        if method == "tools":
            node: str | None = None
            if isinstance(data, dict):
                raw_node = data.pop("__node", None)
                if isinstance(raw_node, str) and raw_node:
                    node = raw_node
            out.append(self._mint("tools", namespace, _jsonable(data), node=node))
            return out

        if method == "input":
            out.append(self._mint("input.requested", namespace, _jsonable(data)))
            return out

        # 其余方法(debug 等)不进 wire
        return out


def synth_lifecycle(
    assign: Any,
    namespace: list[str],
    status: str,
    *,
    graph_name: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """合成 lifecycle 信封(started/completed/failed/interrupted)。

    v3 的 LifecycleTransformer 只跟踪**子图** namespace;根级
    (``ns=[]``)的 started/terminal 由运行时合成——与参考实现一致。
    """
    data: dict[str, Any] = {"event": status}
    if graph_name:
        data["graph_name"] = graph_name
    if error is not None:
        data["error"] = error
    seq, event_id = assign("lifecycle", namespace, data)
    return _envelope(seq, event_id, "lifecycle", namespace, data)


def synth_input_requested(
    assign: Any,
    namespace: list[str],
    interrupt_id: str,
    payload: Any = None,
) -> dict[str, Any]:
    """合成 input.requested 信封(如从 thread state 恢复 pending 中断)。"""
    data: dict[str, Any] = {"interrupt_id": interrupt_id}
    if payload is not None:
        data["payload"] = _jsonable(payload)
    seq, event_id = assign("input.requested", namespace, data)
    return _envelope(seq, event_id, "input.requested", namespace, data)
