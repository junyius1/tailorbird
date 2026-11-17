import { createFileRoute } from "@tanstack/react-router"

export const Route = createFileRoute("/agents/threads")({
  component: ThreadsStub,
})

function ThreadsStub() {
  return (
    <main className="flex min-w-0 flex-1 items-center justify-center p-6">
      <p className="text-sm text-muted-foreground">Kanban 视图在本变体未启用。</p>
    </main>
  )
}
