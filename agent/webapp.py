"""FastAPI 应用入口。

同一份 app 有两种部署形态(共用同一套认证/权限/代理代码):
- 同进程:被 `langgraph.json` 的 ``http.app`` 挂进运行时(缺省 :2026);
- 双进程:``uvicorn agent.webapp:app --port 8002``,浏览器只连 :8002,
  内部以复合 bearer 调运行时(缺省 :2026)。
"""

from agent.api.app import app

__all__ = ["app"]
