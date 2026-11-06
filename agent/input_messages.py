"""面向模型的输入消息:类型化构造 + XML 信封序列化。

设计书 §8:所有入站消息统一序列化为用户消息里的 XML 信封,让模型
结构化感知发送者/渠道/系统:

    <input-message sender="alice" surface="web" kind="human">
      <content>…正文(或 content blocks)…</content>
    </input-message>

本变体:身份不再来自 GitHub/Slack 映射;dashboard 来源直接给一个
**当前登录 username** 的 ``<dynamic-context>``(platform=open-swe)。
"""

import hashlib
import re
from collections.abc import Mapping
from html import escape
from typing import Any, Literal, NotRequired, TypedDict
from xml.etree import ElementTree

from langchain_core.messages import AnyMessage, BaseMessage

# 写入 thread metadata 的键:记录已注入过身份介绍的动态上下文 hash(去重用)
INJECTED_DYNAMIC_CONTEXT_HASHES_KEY = "injected_dynamic_context_hashes"
# deepagents 摘要中间件写入的键:摘要消息 + messages[cutoff_index:]
SUMMARIZATION_EVENT_KEY = "_summarization_event"

# 本变体触发源:web(dashboard/浏览器) / desktop / automation(scheduler 等)
Surface = Literal["web", "desktop", "automation"]
# 实体种类:person / channel / system
EntityKind = Literal["person", "channel", "system"]
# 消息种类:human(人类发送) / system(系统/自动化)
MessageKind = Literal["human", "system"]


class PersonIdentity(TypedDict):
    """一个人类发送者的身份(本地登录用户)。"""

    id: str  # 命名空间化实体 id,如 "open-swe:alice"
    display_name: NotRequired[str]  # 展示名(缺省 = username)
    handle: NotRequired[str]  # 句柄(缺省 = username)
    platform: NotRequired[str]  # 平台(本变体恒 "open-swe")
    email: NotRequired[str]  # 本变体恒 None/缺省(无外部邮箱)
    timezone: NotRequired[str]  # 时区(可缺省)


class ChannelIdentity(TypedDict):
    """一个渠道的身份(本变体保留结构;dashboard 来源一般不产生渠道)。"""

    id: str  # 渠道 id
    platform: str  # 平台名
    name: NotRequired[str]  # 渠道名
    thread_id: NotRequired[str]  # 渠道内线程 id
    topic: NotRequired[str]  # 主题(不可信字段)
    purpose: NotRequired[str]  # 用途(不可信字段)


class SystemIdentity(TypedDict):
    """一个系统实体的身份(无人类发送者时合成,如 "system:automation")。"""

    id: str  # 系统实体 id
    display_name: str  # 展示名
    platform: NotRequired[str]  # 平台名
    sender_type: NotRequired[str]  # 发送者类型描述


# 三种实体身份的二选一/三选联合
Identity = PersonIdentity | ChannelIdentity | SystemIdentity


class InputMessageContext(TypedDict):
    """一条入站消息的上下文(决定信封的 sender/surface/kind)。"""

    sender_id: str  # 发送者实体 id
    surface: Surface  # 触发面
    kind: MessageKind  # human / system
    channel_id: NotRequired[str]  # 所属渠道 id(可缺)
    data: NotRequired[dict[str, object]]  # 附加结构化数据(如 repo、时区)


class RunMessage(TypedDict):
    """run 输入里的一条消息(SDK 形态)。"""

    role: Literal["user", "system"]  # 角色
    content: str | list[dict[str, Any]]  # 正文(str)或 content blocks
    id: NotRequired[str]  # 可选消息 id


class RunInput(TypedDict):
    """一次 run 的完整输入。"""

    messages: list[RunMessage]  # 消息列表(身份介绍 + 真正的输入)
    files: NotRequired[dict[str, Any]]  # 可选文件负载


# 各实体种类 → 输出为 <data> 子元素的字段表(顺序敏感)
_ENTITY_FIELDS: dict[EntityKind, tuple[str, ...]] = {
    "person": ("display_name", "handle", "platform", "email", "timezone"),
    "channel": ("platform", "name", "thread_id", "topic", "purpose"),
    "system": ("display_name", "platform", "sender_type"),
}
# 不可信字段:来自外部平台内容,展示时必须转义(本变体基本不产生)
_UNTRUSTED_ENTITY_FIELDS = frozenset({"topic", "purpose"})
# 无人类发送者时合成的系统身份 id
_SYSTEM_ENTITY_ID = "system:open-swe"
# 系统提示包裹标记(幂等 splice 用)
_SYSTEM_WRAPPER_MARKER = '<system-instructions format="open-swe-v1">'
# 实体 id 允许字符:非空白、无 XML 危险字符
_ENTITY_ID_RE = re.compile(r"^[^\s<>\"]+$")


def _xml_text(value: object) -> str:
    """转义为 XML 文本节点内容(不转义引号)。"""
    return escape(str(value), quote=False)


def _xml_attr(value: object) -> str:
    """转义为 XML 属性值(转义引号)。"""
    return escape(str(value), quote=True)


def _validate_entity_id(entity_id: str) -> str:
    """校验实体 id:非空、带命名空间(含冒号)、无空白/XML 危险字符。"""
    if not isinstance(entity_id, str) or not entity_id.strip() or ":" not in entity_id:
        raise ValueError("entity id must be a non-empty namespaced identifier")
    if not _ENTITY_ID_RE.match(entity_id) or "'" in entity_id:
        raise ValueError("entity id contains invalid characters")
    return entity_id


def injected_dynamic_context_hashes_from_metadata(metadata: object) -> set[str]:
    """从 thread metadata 读已注入的动态上下文 hash 集合(防重复注入)。"""
    if not isinstance(metadata, dict):
        return set()
    values = metadata.get(INJECTED_DYNAMIC_CONTEXT_HASHES_KEY)
    if not isinstance(values, list):
        return set()
    return {value for value in values if isinstance(value, str) and value}


def message_sender_id(content: object) -> str | None:
    """从消息内容里解析发送者实体 id(输入信封或动态上下文块)。"""
    text = content if isinstance(content, str) else None
    if text is None:
        # content blocks:取第一个 text 块
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    value = block.get("text")
                    if isinstance(value, str):
                        text = value
                        break
    if text is None:
        return None
    root = _parse_first_element(text)
    if root is None:
        return None
    sender = root.get("sender") or root.get("id")
    return sender


def input_message_text(content: object) -> str | None:
    """取输入信封 ``<input-message>`` 的 ``<content>`` 正文。"""
    if not isinstance(content, str):
        return None
    root = _parse_first_element(content)
    if root is None or root.tag != "input-message":
        return None
    node = root.find("content")
    return None if node is None else node.text


def dynamic_context_hash(content: object) -> str | None:
    """对一条动态上下文消息计算稳定 hash(去重键)。"""
    if not isinstance(content, str):
        return None
    root = _parse_first_element(content)
    if root is None or root.tag != "dynamic-context":
        return None
    digest = hashlib.sha256(_normalize_element(root).encode("utf-8")).hexdigest()
    return digest


def _parse_first_element(text: str) -> ElementTree.Element | None:
    """解析文本里的第一个 XML 元素;不是纯 XML 时返回 None。"""
    stripped = text.strip()
    if not stripped.startswith("<"):
        return None
    try:
        # 只取第一个顶层元素:找到与之配对的结束标签
        root = ElementTree.fromstring(stripped)
    except ElementTree.ParseError:
        return None
    return root


def _normalize_element(element: ElementTree.Element) -> str:
    """元素规范化(属性排序),用于稳定 hash。"""
    attributes = " ".join(
        f'{key}="{_xml_attr(value)}"' for key, value in sorted(element.attrib.items())
    )
    children = "".join(_normalize_element(child) for child in element)
    text = element.text or ""
    return f"<{element.tag} {attributes}>{text}{children}</{element.tag}>"


def dynamic_context_messages(messages: object) -> list[AnyMessage]:
    """从 state 消息里挑出动态上下文消息(身份介绍)。"""
    result: list[AnyMessage] = []
    for message in messages or []:
        if not isinstance(message, BaseMessage):
            continue
        content = message.content
        if dynamic_context_hash(content) is not None:
            result.append(message)
    return result


def dynamic_context_hashes_from_messages(messages: object) -> set[str]:
    """消息里已存在的动态上下文 hash 集合。"""
    hashes: set[str] = set()
    for message in messages or []:
        if not isinstance(message, BaseMessage):
            continue
        digest = dynamic_context_hash(message.content)
        if digest is not None:
            hashes.add(digest)
    return hashes


def visible_dynamic_context_hashes(state: Mapping[str, Any]) -> set[str]:
    """state 里"可见"的动态上下文 hash(当前 messages 通道)。"""
    messages = state.get("messages") if isinstance(state, Mapping) else None
    return dynamic_context_hashes_from_messages(messages)


def _entity_message(identity: Identity, kind: EntityKind) -> RunMessage:
    """把一个实体身份序列化成 ``<dynamic-context>`` 用户消息。"""
    entity_id = _validate_entity_id(identity["id"])
    data_parts: list[str] = []
    # 按字段表输出 <data> 子元素
    for field in _ENTITY_FIELDS[kind]:
        value = identity.get(field)  # type: ignore[union-attr]
        if value is None or value == "":
            continue
        data_parts.append(_data_element(field, value))
    data_block = "".join(data_parts)
    body = (
        f'<dynamic-context kind="{_xml_attr(kind)}" id="{_xml_attr(entity_id)}">'
        f"{data_block}</dynamic-context>"
    )
    return {"role": "user", "content": body}


def person_introduction(person: PersonIdentity) -> RunMessage:
    """人类发送者的身份介绍消息。"""
    return _entity_message(person, "person")


def channel_introduction(channel: ChannelIdentity) -> RunMessage:
    """渠道身份介绍消息。"""
    return _entity_message(channel, "channel")


def system_introduction(system: SystemIdentity) -> RunMessage:
    """系统实体身份介绍消息。"""
    return _entity_message(system, "system")


def _data_element(name: str, value: object) -> str:
    """单个 <data> 子元素;不可信字段包 <dangerous-external-untrusted-users-comment>。"""
    text = _xml_text(value)
    if name in _UNTRUSTED_ENTITY_FIELDS:
        text = (
            "<dangerous-external-untrusted-users-comment>"
            f"{text}"
            "</dangerous-external-untrusted-users-comment>"
        )
    return f"<data name=\"{_xml_attr(name)}\">{text}</data>"


def _serialize_message(text: str, context: InputMessageContext) -> str:
    """把正文包进输入信封;正文本身已是信封时幂等返回。"""
    root = _parse_first_element(text)
    if root is not None and root.tag == "input-message":
        return text
    return (
        f'<input-message sender="{_xml_attr(context["sender_id"])}" '
        f'surface="{_xml_attr(context["surface"])}" kind="{_xml_attr(context["kind"])}">\n'
        f"<content>{_xml_text(text)}</content>\n"
        "</input-message>"
    )


def _splice_envelope_data(text: str, element: str) -> str | None:
    """把结构化数据元素拼进信封的 <content> 之后;找不到信封返回 None。"""
    marker = "</content>"
    index = text.find(marker)
    if index < 0:
        return None
    return text[: index + len(marker)] + element + text[index + len(marker) :]


def append_message_data(
    content: str, data: dict[str, object] | None
) -> str:
    """给已有信封消息追加 <message-data> 结构化数据(幂等)。"""
    if not data:
        return content
    parts = [
        _data_element(key, value) for key, value in sorted(data.items())
    ]
    if not parts:
        return content
    result = _splice_envelope_data(content, "".join(parts))
    return content if result is None else result


def _structured_content(
    content: str | list[dict[str, Any]],
) -> tuple[str | list[dict[str, Any]], list[dict[str, object]]]:
    """拆分 content:正文 + 结构化 data 项(供信封序列化)。"""
    if isinstance(content, str):
        return content, []
    text_parts: list[str] = []
    data_items: list[dict[str, object]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            text_parts.append(block["text"])
        else:
            # 非文本块(图片等)原样保留进正文块列表
            data_items.append(block)
    if text_parts:
        return "\n".join(text_parts), data_items
    return content, []


def human_input(
    content: str | list[dict[str, Any]], context: InputMessageContext
) -> RunMessage:
    """人类输入 → 信封化的 user 消息。"""
    text, _data = _structured_content(content)
    serialized = _serialize_message(
        text if isinstance(text, str) else str(text), context
    )
    return {"role": "user", "content": serialized}


def system_input(
    content: str | list[dict[str, Any]], context: InputMessageContext
) -> RunMessage:
    """系统输入 → 信封化的 user 消息(kind=system)。"""
    text, _data = _structured_content(content)
    context = dict(context)
    context["kind"] = "system"
    serialized = _serialize_message(
        text if isinstance(text, str) else str(text), context
    )
    return {"role": "user", "content": serialized}


def filter_new_dynamic_contexts(
    entities: list[tuple[Identity, EntityKind]], already_injected: set[str]
) -> list[RunMessage]:
    """过滤掉已注入过的实体身份介绍(按内容 hash 去重)。"""
    fresh: list[RunMessage] = []
    for identity, kind in entities:
        message = _entity_message(identity, kind)
        digest = dynamic_context_hash(message["content"])
        if digest is None or digest in already_injected:
            continue
        fresh.append(message)
    return fresh


def build_input_messages(
    content: str | list[dict[str, Any]],
    context: InputMessageContext,
    people: list[PersonIdentity] | None = None,
    channels: list[ChannelIdentity] | None = None,
    systems: list[SystemIdentity] | None = None,
    files: dict[str, Any] | None = None,
    injected_dynamic_context_hashes: set[str] | None = None,
) -> list[RunMessage]:
    """构造完整 run 输入消息序列。

    顺序:身份介绍(未注入过的)+ 真正的 human/system 输入。
    """
    # 实体身份列表(种类对齐)
    entities: list[tuple[Identity, EntityKind]] = []
    for person in people or []:
        entities.append((person, "person"))
    for channel in channels or []:
        entities.append((channel, "channel"))
    for system in systems or []:
        entities.append((system, "system"))
    # 过滤已注入的
    fresh = filter_new_dynamic_contexts(
        entities, injected_dynamic_context_hashes or set()
    )
    # 真正的输入消息
    if context["kind"] == "system":
        main = system_input(content, context)
    else:
        main = human_input(content, context)
    messages = fresh + [main]
    # files 由调用方放进 RunInput,不进 messages
    _ = files
    return messages


def build_run_input(
    content: str | list[dict[str, Any]],
    context: InputMessageContext,
    people: list[PersonIdentity] | None = None,
    channels: list[ChannelIdentity] | None = None,
    systems: list[SystemIdentity] | None = None,
    files: dict[str, Any] | None = None,
    injected_dynamic_context_hashes: set[str] | None = None,
) -> RunInput:
    """构造 RunInput(messages + 可选 files)。"""
    messages = build_input_messages(
        content,
        context,
        people=people,
        channels=channels,
        systems=systems,
        injected_dynamic_context_hashes=injected_dynamic_context_hashes,
    )
    run_input: RunInput = {"messages": messages}
    if files:
        run_input["files"] = files
    return run_input


def wrap_system_prompt(prompt: str) -> str:
    """把系统提示包进 <system-instructions> 标记(幂等:已包裹则原样返回)。"""
    if _SYSTEM_WRAPPER_MARKER in prompt:
        return prompt
    return (
        f'{_SYSTEM_WRAPPER_MARKER}\n'
        f"{prompt}\n"
        "</system-instructions>"
    )
