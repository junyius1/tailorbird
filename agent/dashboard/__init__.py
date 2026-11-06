"""dashboard 业务层(线程/消息/plan/workflow/schedules/skills/认证)。

路由挂载约定(见 agent/api/app.py):
- ``dashboard_router`` 前缀 ``/dashboard/api``
- ``plan_router`` 前缀 ``/dashboard/api/plan``
- ``workflow_approval_router`` 前缀 ``/dashboard/api/workflow-approval``
- ``health_router`` 挂 ``/health`` ``/ok`` ``/webhooks`` ``/debug``

本变体删除:OAuth 登录链、GitHub 仓库/PR/review/Slack/Notion/Linear/团队
多用户映射等全部端点;认证 = 本地静态 token 换 per-user 会话 cookie(§3)。
"""
