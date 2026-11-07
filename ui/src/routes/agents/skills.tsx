import { createFileRoute } from "@tanstack/react-router"

export const Route = createFileRoute("/agents/skills")({
  component: SkillsStub,
})

function SkillsStub() {
  return (
    <main className="flex min-w-0 flex-1 items-center justify-center p-6">
      <p className="text-sm text-muted-foreground">Skills 页面在本变体未启用。</p>
    </main>
  )
}
