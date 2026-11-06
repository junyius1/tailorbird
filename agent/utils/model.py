"""Chat model 构建(多 provider + local: 本地 vLLM + 推理流开关)。

``model_id`` 解析规则(make_model 的单一事实源):
- ``local:<name>`` 或 env ``LOCAL_LLM_MODEL_ID`` 形式 → 本地 vLLM(OpenAI 兼容,
  ``base_url=LOCAL_LLM_BASE_URL``),用 langchain_openai.ChatOpenAI;
- 其它 → ``langchain.chat_models.init_chat_model``(anthropic/openai/… 按 key 解析)。

推理流开关(设计书 §10.6,仅 ``local:`` 模型生效;一个开关管两半):
- Inherit(缺省):跟 env ``LOCAL_LLM_ENABLE_THINKING``(缺省 true);
- On:强制 ``extra_body.enable_thinking = true`` + 实例打
  ``_tailorbird_local_reasoning`` 标记(→ 捕获并发 reasoning 流事件);
- Off:``extra_body.enable_thinking = false``,不打标记(不捕获、不推送)。
取值优先级:线程属主 profile 的 ``reasoning_stream`` > env 部署默认。
独立"仅显示"开关 ``LOCAL_LLM_STREAM_REASONING``(缺省 true)只控显示/推送半。
"""

import asyncio
import logging
import os
from collections.abc import Mapping
from typing import Any

from langchain_core.language_models import BaseChatModel

logger = logging.getLogger(__name__)

# 进程内模型缓存:key = (model_id, effort, reasoning_stream, loop_id)。
# 本地部署单 LLM 后端,缓存复用连接;app 退出时由 close_cached_models() 关闭。
_MODEL_CACHE: dict[tuple[str, str | None, bool | None, int | None], Any] = {}


def _loop_id() -> int | None:
    """当前事件循环 id(单循环铁律下恒同值;用于缓存键)。"""
    try:
        return id(asyncio.get_running_loop())
    except RuntimeError:
        return None

# 缺省请求超时(秒):本地模型长推理需要宽松上限
DEFAULT_REQUEST_TIMEOUT_SECONDS = float(os.environ.get("OPEN_SWE_MODEL_CALL_TIMEOUT_SECONDS", "600"))
# 实例标记属性名:reasoning 捕获层据此决定是否捕获/流式推送
_REASONING_MARK_ATTR = "_tailorbird_local_reasoning"
# local: 模型名(缺省)
_LOCAL_MODEL_ID_ENV = "LOCAL_LLM_MODEL_ID"


def _env_bool(name: str, default: bool) -> bool:
    """env 布尔解析(1/true/yes/on 为真)。"""
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def local_model_id() -> str:
    """本地 vLLM 的模型 id(缺省 Qwen)。"""
    return os.environ.get(_LOCAL_MODEL_ID_ENV, "Qwen")


def _resolve_reasoning_stream(reasoning_stream: bool | None) -> bool:
    """解析推理流最终开关值(§10.6 优先级:profile > env 部署默认)。"""
    if reasoning_stream is not None:
        return reasoning_stream
    return _env_bool("LOCAL_LLM_ENABLE_THINKING", True)


def _stream_reasoning_enabled() -> bool:
    """"仅显示/推送"独立开关(缺省 true)。"""
    return _env_bool("LOCAL_LLM_STREAM_REASONING", True)


def _provider_model_kwargs(
    model_id: str,
    effort: str | None = None,
    reasoning_stream: bool | None = None,
) -> dict[str, Any]:
    """按 provider 构造 model 级 kwargs(含 local: 的 enable_thinking 注入)。

    注意:local: 的 ``enable_thinking`` **只在本函数一处注入**(单一事实源),
    其它地方不得再写,否则会互相覆盖(§10.6 注意项)。
    """
    kwargs: dict[str, Any] = {"timeout": DEFAULT_REQUEST_TIMEOUT_SECONDS}
    if model_id.startswith("local:"):
        # 拉取半:enable_thinking 由解析后的 reasoning_stream 决定
        enabled = _resolve_reasoning_stream(reasoning_stream)
        kwargs["extra_body"] = {"enable_thinking": enabled}
        if effort:
            # vLLM 的 chat_template_kwargs 透传(可选)
            kwargs["extra_body"]["chat_template_kwargs"] = {"reasoning_effort": effort}
    return kwargs


async def make_model(
    model_id: str | None = None,
    *,
    effort: str | None = None,
    reasoning_stream: bool | None = None,
) -> BaseChatModel:
    """构建 chat model(带缓存语义由调用方负责)。

    参数:
        model_id: 模型标识;None 用本地缺省。``local:`` 前缀 = 本地 vLLM。
        effort: 推理力度(仅 local: 透传;其它 provider 忽略)。
        reasoning_stream: 推理流开关(§10.6);None = Inherit(env 默认)。

    返回:
        配置好的 BaseChatModel;local: 且最终开关为 On 时实例带
        ``_tailorbird_local_reasoning = True`` 标记。
    """
    resolved = model_id or f"local:{local_model_id()}"
    # 缓存命中直接复用(避免每 run 重建 HTTP 连接)
    cache_key = (resolved, effort, reasoning_stream, _loop_id())
    cached_model = _MODEL_CACHE.get(cache_key)
    if cached_model is not None:
        return cached_model
    if resolved.startswith("local:"):
        name = resolved.split(":", 1)[1] or local_model_id()
        from langchain_openai import ChatOpenAI

        kwargs = _provider_model_kwargs(resolved, effort, reasoning_stream)
        model = ChatOpenAI(
            model=name,
            base_url=os.environ.get("LOCAL_LLM_BASE_URL", "http://localhost:8000/v1"),
            api_key=os.environ.get("LOCAL_LLM_API_KEY", "sk-local"),
            **kwargs,
        )
        # 显示/推送半:最终开关 On 且"仅显示"开关未关 → 打捕获标记
        stream_on = _resolve_reasoning_stream(reasoning_stream) and _stream_reasoning_enabled()
        setattr(model, _REASONING_MARK_ATTR, bool(stream_on))
        _MODEL_CACHE[cache_key] = model
        return model
    # 其它 provider:init_chat_model 按 key 自动解析
    from langchain.chat_models import init_chat_model

    kwargs = _provider_model_kwargs(resolved, effort, reasoning_stream)
    kwargs.pop("extra_body", None)  # init_chat_model 不认 extra_body
    model = init_chat_model(resolved, **kwargs)
    _MODEL_CACHE[cache_key] = model
    return model


def model_has_reasoning_mark(model: Any) -> bool:
    """判断模型实例是否带 reasoning 捕获标记(reasoning patch 层使用)。"""
    return bool(getattr(model, _REASONING_MARK_ATTR, False))


def validate_local_dev_llm_config() -> None:
    """启动校验:本地 LLM 可达性提示(不阻断启动——模型可能后配)。

    仅当 ``LOCAL_LLM_BASE_URL`` 已设且明显不可达时打 warning;
    不抛异常(模型 key/URL 缺失属于运行期错误,由 run 自身暴露)。
    """
    base_url = os.environ.get("LOCAL_LLM_BASE_URL", "")
    if not base_url:
        logger.warning("LOCAL_LLM_BASE_URL 未设置;local: 模型调用将失败")
        return
    logger.info("本地 LLM base_url=%s(并发上限=%s)", base_url, os.environ.get("OPEN_SWE_LLM_MAX_CONCURRENCY", "1"))


# ---------------------------------------------------------------------------
# 本地 LLM 推理捕获 patch(ChatOpenAI 会丢弃 provider 专属字段,如 Qwen 的
# reasoning_content;不打补丁,思考 token 到不了 AIMessageChunk,content_blocks
# 解析不出 reasoning 块,UI 就只剩工具调用+正文)。
# 只对被标记的 local: 模型实例生效(见 _REASONING_MARK_ATTR)。
# ---------------------------------------------------------------------------

# 把 response_metadata 的 model_provider 重标为 "deepseek":
# Python 侧无 deepseek translator,content_blocks 会回退到
# additional_kwargs["reasoning_content"] 提取路径(JS 侧有对应 translator)。
_LOCAL_REASONING_MODEL_PROVIDER = "deepseek"
_LOCAL_REASONING_MODEL_PROVIDER_KEY = "model_provider"
# OpenAI 兼容服务对思考字段命名不一致:Qwen/DashScope/部分 vLLM 模板发
# reasoning_content,其它发 reasoning。两者承载同样的思考 token。
_LOCAL_REASONING_FIELD_KEYS = ("reasoning_content", "reasoning")
_local_reasoning_installed = False


def _delta_reasoning_text(chunk: Mapping[str, Any]) -> str | None:
    """从 chat-completions 流式 chunk 的 delta 里提取思考片段。"""
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    choice = choices[0]
    if not isinstance(choice, Mapping):
        return None
    delta = choice.get("delta")
    if not isinstance(delta, Mapping):
        return None
    return _first_reasoning_field(delta)


def _message_reasoning_text(message: Any) -> str | None:
    """从非流式 chat-completions message(dict 或 SDK 对象)提取思考全文。"""
    if not message:
        return None
    if isinstance(message, Mapping):
        return _first_reasoning_field(message)
    for key in _LOCAL_REASONING_FIELD_KEYS:
        value = getattr(message, key, None)
        if isinstance(value, str) and value:
            return value
    return None


def _first_reasoning_field(mapping: Mapping[str, Any] | None) -> str | None:
    """按字段名优先级取第一个非空思考文本。"""
    if not mapping:
        return None
    for key in _LOCAL_REASONING_FIELD_KEYS:
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def install_local_reasoning_patch() -> None:
    """给本地 LLM chunk 捕获 reasoning_content,使思考能在 UI 渲染。

    做两件事(仅对带标记的 local: 模型实例):
    1. 把每个流式 chunk 的思考片段存进 ``additional_kwargs["reasoning_content"]``;
    2. 重标 ``response_metadata["model_provider"]``,让 content_blocks 走
       reasoning-content 感知的 translator,而不是会静默丢字段的 OpenAI 那个。

    非流式结果路径(``_create_chat_result``)同样捕获:agent 工厂经 ainvoke
    调模型,兼容桥从最终 message 重建 message-start 事件(含累积思考)。
    幂等:重复调用无副作用。
    """
    global _local_reasoning_installed
    if _local_reasoning_installed:
        return
    try:
        from langchain_openai.chat_models import base as lo_base
    except ImportError:
        return
    if getattr(lo_base, "_tailorbird_reasoning_patched", False):
        _local_reasoning_installed = True
        return

    original_convert = lo_base.BaseChatOpenAI._convert_chunk_to_generation_chunk
    original_result = lo_base.BaseChatOpenAI._create_chat_result

    def _patched_convert(self: Any, chunk: Mapping[str, Any], *args: Any) -> Any:
        generation_chunk = original_convert(self, chunk, *args)
        if (
            generation_chunk is None
            or not isinstance(generation_chunk.message, lo_base.AIMessageChunk)
            or not getattr(self, _REASONING_MARK_ATTR, False)
        ):
            return generation_chunk
        message = generation_chunk.message
        message.response_metadata[_LOCAL_REASONING_MODEL_PROVIDER_KEY] = (
            _LOCAL_REASONING_MODEL_PROVIDER
        )
        if (reasoning := _delta_reasoning_text(chunk)) is not None:
            message.additional_kwargs["reasoning_content"] = reasoning
        return generation_chunk

    def _patched_create_chat_result(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = original_result(self, *args, **kwargs)
        if not getattr(self, _REASONING_MARK_ATTR, False):
            return result
        response = args[0] if args else kwargs.get("response")
        reasoning: str | None = None
        model_name: str | None = None
        choices: Any = None
        if isinstance(response, Mapping):
            choices = response.get("choices")
            if (model := response.get("model")) is not None:
                model_name = str(model)
        else:
            choices = getattr(response, "choices", None)
            model_name = getattr(response, "model", None)
        if isinstance(choices, list) and choices:
            choice = choices[0]
            if isinstance(choice, Mapping):
                reasoning = _message_reasoning_text(choice.get("message"))
            else:
                reasoning = _message_reasoning_text(getattr(choice, "message", None))
        for generation in result.generations:
            message = generation.message
            message.response_metadata[_LOCAL_REASONING_MODEL_PROVIDER_KEY] = (
                _LOCAL_REASONING_MODEL_PROVIDER
            )
            if isinstance(model_name, str):
                message.response_metadata.setdefault("model_name", model_name)
            if reasoning is not None:
                message.additional_kwargs["reasoning_content"] = reasoning
        return result

    lo_base.BaseChatOpenAI._convert_chunk_to_generation_chunk = _patched_convert  # type: ignore[method-assign]
    lo_base.BaseChatOpenAI._create_chat_result = _patched_create_chat_result  # type: ignore[method-assign]
    lo_base._tailorbird_reasoning_patched = True  # type: ignore[attr-defined]
    _local_reasoning_installed = True

async def close_cached_models() -> None:
    """关闭进程内缓存的所有模型(app 退出时调用)。"""
    models = list(_MODEL_CACHE.values())
    _MODEL_CACHE.clear()
    for model in models:
        close = getattr(model, "aclose", None)
        if callable(close):
            result = close()
            if asyncio.iscoroutine(result):
                await result
            continue
        close = getattr(model, "close", None)
        if callable(close):
            result = close()
            if asyncio.iscoroutine(result):
                await result
