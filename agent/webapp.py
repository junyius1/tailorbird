"""FastAPI 应用入口。

同一份 app 有两种部署形态(共用同一套认证/权限/代理代码):
- 同进程:被 `langgraph.json` 的 ``http.app`` 挂进 :2024 运行时;
- 双进程:``uvicorn agent.webapp:app --port 8001``,浏览器只连 :8001,
  内部以复合 bearer 调 :2024 运行时。
"""

from agent.api.app import app

__all__ = ["app"]
