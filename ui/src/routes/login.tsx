// 登录页:本地 token 登录(username + 共享 token)。
import { createFileRoute } from "@tanstack/react-router"
import { useState } from "react"

import { login } from "@/lib/session"

// 登录路由(独立于登录门)
export const Route = createFileRoute("/login")({
  component: LoginPage,
})

// 登录表单组件
function LoginPage() {
  // 用户名输入态
  const [username, setUsername] = useState("")
  // 共享 token 输入态
  const [token, setToken] = useState("")
  // 提交中
  const [busy, setBusy] = useState(false)
  // 错误信息
  const [error, setError] = useState<string | null>(null)

  // 提交:登录 → 回首页
  async function onSubmit(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await login(username, token)
      // 登录成功:跳回 agents 首页
      window.location.assign("/agents")
    } catch (err) {
      setError(err instanceof Error ? err.message : "登录失败")
      setBusy(false)
    }
  }

  return (
    <div className="login-wrap">
      <form className="card login-card" onSubmit={onSubmit}>
        <h1>Tailorbird</h1>
        <p>本地自托管编程 agent。输入你的用户名与共享 token 登录。</p>
        <label htmlFor="username">用户名</label>
        <input
          id="username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          placeholder="例如:alice"
          autoComplete="username"
          required
        />
        <label htmlFor="token">共享 Token</label>
        <input
          id="token"
          type="password"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          placeholder="OPEN_SWE_LOCAL_AUTH_TOKEN"
          autoComplete="current-password"
          required
        />
        {error ? <p style={{ color: "var(--danger)" }}>{error}</p> : null}
        <div style={{ marginTop: 18 }}>
          <button type="submit" disabled={busy}>
            {busy ? "登录中…" : "登录"}
          </button>
        </div>
      </form>
    </div>
  )
}
