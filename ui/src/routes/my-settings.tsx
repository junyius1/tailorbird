import { createFileRoute } from "@tanstack/react-router"
import { useSession } from "@/lib/session"

export const Route = createFileRoute("/my-settings")({
  component: MySettingsStub,
})

function MySettingsStub() {
  const session = useSession()
  return (
    <main className="mx-auto max-w-3xl p-8">
      <h1 className="text-xl font-semibold">Settings</h1>
      <p className="mt-2 text-sm text-muted-foreground">
        设置页在本变体未启用。当前用户:
        {session.data ? session.data.login : "…"}
      </p>
    </main>
  )
}
