"""HTTP 客户端缺省超时。

所有出站 httpx 调用共用同一超时对象:总超时 30s、连接超时 10s。
对上游运行时的 SSE 长流不使用此超时(timeout=None,由流自身的空闲/存活上限控制)。
"""

import httpx

# 缺省 HTTP 超时:总 30 秒,其中连接阶段最多 10 秒
DEFAULT_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
