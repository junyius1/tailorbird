// 登录页:本地 token 登录(username + 共享 token)→ tb_session cookie。
import { createFileRoute } from "@tanstack/react-router"
import { useEffect, useMemo } from "react"
import { useState } from "react"

import { Skeleton } from "@/components/ui/skeleton"
import {
  DEFAULT_AUTH_REDIRECT,
  consumeAuthRedirect,
  getRememberedAuthRedirect,
  rememberAuthRedirect,
} from "@/lib/auth-redirect"
import { login, useSession } from "@/lib/session"

type LoginSearch = { redirect?: string }

export const Route = createFileRoute("/login")({
  validateSearch: (search: Record<string, unknown>): LoginSearch => ({
    redirect: typeof search.redirect === "string" ? search.redirect : undefined,
  }),
  component: LoginPage,
})

function LoginPage() {
  const session = useSession()
  const search = Route.useSearch()
  const [username, setUsername] = useState("")
  const [token, setToken] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const intendedPath = useMemo(
    () =>
      search.redirect
        ? rememberAuthRedirect(search.redirect)
        : (getRememberedAuthRedirect() ?? DEFAULT_AUTH_REDIRECT),
    [search.redirect]
  )
  const authenticatedRedirect = useMemo(
    () => (session.data ? consumeAuthRedirect(search.redirect) : null),
    [search.redirect, session.data]
  )

  useEffect(() => {
    if (authenticatedRedirect) window.location.replace(authenticatedRedirect)
  }, [authenticatedRedirect])

  if (session.isLoading) {
    return (
      <main className="flex min-h-svh items-center justify-center p-6">
        <Skeleton className="h-40 w-80" />
      </main>
    )
  }

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await login(username, token)
      window.location.replace(intendedPath)
    } catch (err) {
      setError(err instanceof Error ? err.message : "登录失败")
      setBusy(false)
    }
  }

  return (
    <main className="flex min-h-svh items-center justify-center p-6">
      <form
        onSubmit={onSubmit}
        className="w-full max-w-sm rounded-xl border border-border bg-card p-8 text-card-foreground"
      >
        <h1 className="text-xl font-semibold">Tailorbird</h1>
        <p className="mt-1 mb-6 text-sm text-muted-foreground">
          本地自托管编程 agent。输入你的用户名与共享 token 登录。
        </p>
        <label className="block text-xs text-muted-foreground" htmlFor="tb-username">
          用户名
        </label>
        <input
          id="tb-username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          placeholder="例如:alice"
          autoComplete="username"
          required
          className="mt-1 mb-4 w-full rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-ring"
        />
        <label className="block text-xs text-muted-foreground" htmlFor="tb-token">
          共享 Token
        </label>
        <input
          id="tb-token"
          type="password"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          placeholder="OPEN_SWE_LOCAL_AUTH_TOKEN"
          autoComplete="current-password"
          required
          className="mt-1 mb-4 w-full rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-ring"
        />
        {error ? <p className="mb-3 text-xs text-destructive">{error}</p> : null}
        <button
          type="submit"
          disabled={busy}
          className="w-full rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
        >
          {busy ? "登录中…" : "登录"}
        </button>
      </form>
    </main>
  )
}
