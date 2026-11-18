"""运行时配置(完全替代 langgraph dev 的启动配置面)。

只保留 tailorbird 实际用到的配置项;来源优先级与 ``agent.api.app`` 一致
(.env 优先于进程 env)。
"""

import os
from dataclasses import dataclass, field

from ..postgres_persistence import _database_uri

# .env 路径(与 agent.api.app 同一份)
_ENV_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", ".env"
)


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, "")
    return value.strip() if value.strip() else default


def _bool(name: str, default: bool = False) -> bool:
    raw = _env(name, "").lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


@dataclass(frozen=True)
class RuntimeConfig:
    database_uri: str
    # 图名 → 工厂模块路径(与 langgraph.json 的 graphs 保持一致)
    graphs: dict[str, str] = field(default_factory=lambda: {
        "agent": "agent.graphs.agent:traced_agent",
        "scheduler": "agent.graphs.scheduler:get_scheduler",
    })
    # 运行时自身地址(loopback webhook / CORS 用)
    self_url: str = "http://localhost:2026"
    # run 的 stream_mode 白名单
    run_stream_modes: tuple[str, ...] = (
        "values",
        "updates",
        "messages",
        "custom",
        "tasks",
        "checkpoints",
    )
    # state 快照 offload 预算(秒);超时 503 而非降级空值
    state_read_budget: float = 45.0
    # SSE 心跳间隔(秒)
    sse_heartbeat_seconds: float = 5.0
    # 事件环形缓冲(每线程)
    event_buffer_size: int = 20000
    cors_origins: tuple[str, ...] = (
        "http://localhost:3002",
        "http://127.0.0.1:3002",
        "http://localhost:8002",
        "http://127.0.0.1:8002",
    )
    # checkpoint TTL(与 langgraph.json 的 ttl 配置一致)
    checkpoint_ttl_minutes: int = 43200
    checkpoint_ttl_sweep_minutes: int = 60


def _self_url() -> str:
    explicit = os.environ.get("RUNTIME_SELF_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    port = os.environ.get("RUNTIME_PORT", "").strip()
    if port:
        return f"http://localhost:{port}"
    return "http://localhost:2026"


def load_config() -> RuntimeConfig:
    try:
        from dotenv import load_dotenv as _load_dotenv

        _load_dotenv(_ENV_FILE, override=True)
    except Exception:  # dotenv 缺失不阻断(与 agent.api.app 一致)
        pass
    return RuntimeConfig(
        database_uri=_database_uri() or os.environ.get("DATABASE_URI", "").strip(),
        self_url=_self_url(),
        state_read_budget=float(
            _env("RUNTIME_STATE_READ_BUDGET", str(45.0)) or "45.0"
        ),
        sse_heartbeat_seconds=float(
            _env("RUNTIME_SSE_HEARTBEAT", "5") or "5"
        ),
        event_buffer_size=int(_env("RUNTIME_EVENT_BUFFER", "20000") or "20000"),
        checkpoint_ttl_minutes=int(
            _env("RUNTIME_CHECKPOINT_TTL_MINUTES", "43200") or "43200"
        ),
        checkpoint_ttl_sweep_minutes=int(
            _env("RUNTIME_TTL_SWEEP_MINUTES", "60") or "60"
        ),
    )
