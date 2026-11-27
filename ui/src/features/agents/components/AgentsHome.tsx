import { useCallback, useEffect, useRef, useState } from "react"
import { useStreamContext as useAgentThreadStream } from "@langchain/react"
import { useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"

import type { ImageChunk } from "@/features/agents/lib/types"
import type { CreateAgentThreadVariables } from "@/features/agents/lib/queries"
import type { ModelSelection } from "@/features/agents/lib/provider/useModelOptions"
import { AgentPromptBar } from "@/features/agents/components/AgentPromptBar"
import { OnboardingDialog } from "@/features/agents/components/OnboardingDialog"
import { Logo } from "@/features/agents/components/chat/Logo"
import {
  agentThreadKeys,
  invalidateAgentThreadLists,
  optimisticThread,
  seedAgentThreadLists,
  useAgentSkills,
  useEnvironmentOptions,
} from "@/features/agents/lib/queries"
import {
  persistModelSelection,
  useModelOptions,
} from "@/features/agents/lib/provider/useModelOptions"
import {
  useAddLocalRepo,
  useProfile,
  useRemoveLocalRepo,
  useRepos,
} from "@/lib/profile"
import { useSession } from "@/lib/session"
import {
  requestNotificationPermission,
  setNotificationsPref,
} from "@/lib/notifications"

function promptContent(text: string, images: Array<ImageChunk>) {
  const trimmed = text.trim()
  const imageBlocks = images.map((image) => ({
    type: "image",
    base64: image.base64,
    mime_type: image.mimeType,
    ...(image.fileName ? { file_name: image.fileName } : {}),
  }))
  return [...imageBlocks, ...(trimmed ? [{ type: "text", text: trimmed }] : [])]
}

export function AgentsHome() {
  // Submit straight through the layout's persistent stream. The SDK mints the
  // thread id (no client-minted id, no `getState` 404), fires the first
  // `run.start` — which lazily creates + stamps + owns the thread server-side
  // — and keeps streaming after we navigate to the minted thread below.
  const stream = useAgentThreadStream()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const session = useSession()
  const { models, defaultSelection } = useModelOptions()
  const [selection, setSelection] = useState<ModelSelection | null>(null)
  const activeSelection = selection ?? defaultSelection
  const handleSelectionChange = (next: ModelSelection) => {
    setSelection(next)
    persistModelSelection(next, session.data?.login ?? "")
  }
  const [planMode, setPlanMode] = useState(false)
  const [adminThread, setAdminThread] = useState(false)
  // Per-run sandbox provider ("default" = follow server SANDBOX_TYPE)
  const [sandboxType, setSandboxType] = useState<
    "default" | "docker" | "local"
  >("default")
  const cloudEnabled = Boolean(session.data)
  const environmentOptions = useEnvironmentOptions(cloudEnabled)
  const environments = environmentOptions.data?.environments ?? []
  // undefined = untouched, so the run falls back to the default environment.
  const [environmentOverride, setEnvironmentOverride] = useState<string | null>(
    null
  )
  const defaultEnvironmentSlug = environmentOptions.data?.default_slug ?? null
  const selectedEnvironment =
    environmentOverride ??
    (environments.some((env) => env.slug === defaultEnvironmentSlug)
      ? defaultEnvironmentSlug
      : null)
  const [submitting, setSubmitting] = useState(false)

  const reposQuery = useRepos()
  const profileQuery = useProfile()
  const addLocalRepo = useAddLocalRepo()
  const removeLocalRepo = useRemoveLocalRepo()
  const skills = useAgentSkills({ enabled: cloudEnabled })
  // undefined = untouched (fall back to the profile default); null = explicitly "no repo".
  const [repoOverride, setRepoOverride] = useState<string | null | undefined>(
    undefined
  )
  const repo =
    repoOverride === undefined
      ? (profileQuery.data?.default_repo ?? null)
      : repoOverride
  // 下拉框"Add path…":按宿主路径添加仓库,成功后自动选中
  const handleAddPath = useCallback(
    async (path: string): Promise<string | null> => {
      try {
        const repo = await addLocalRepo.mutateAsync(path)
        setRepoOverride(repo.full_name)
        return null
      } catch (error) {
        return error instanceof Error
          ? error.message
          : "Failed to add path"
      }
    },
    [addLocalRepo]
  )
  // 删除已添加的路径仓库;若当前选中项被删则清空选择
  const handleDeletePath = useCallback(
    async (fullName: string): Promise<string | null> => {
      try {
        await removeLocalRepo.mutateAsync(fullName)
        setRepoOverride((prev) => (prev === fullName ? null : prev))
        return null
      } catch (error) {
        return error instanceof Error
          ? error.message
          : "Failed to remove path"
      }
    },
    [removeLocalRepo]
  )

  // Holds the just-submitted prompt until the SDK mints the thread id; the
  // effect then seeds the optimistic summary and navigates exactly once.
  const draftRef = useRef<CreateAgentThreadVariables | null>(null)

  useEffect(() => {
    const id = stream.threadId
    const draft = draftRef.current
    if (!id || !draft) return
    draftRef.current = null
    const thread = optimisticThread(id, draft)
    queryClient.setQueryData(agentThreadKeys.detail(id), thread)
    seedAgentThreadLists(queryClient, thread)
    invalidateAgentThreadLists(queryClient)
    void navigate({ to: "/agents/$threadId", params: { threadId: id } })
  }, [stream.threadId, queryClient, navigate])


  const handleSubmit = async (prompt: string, images: Array<ImageChunk>) => {
    void requestNotificationPermission().then((perm) => {
      if (perm === "granted") setNotificationsPref(true)
    })
    draftRef.current = {
      prompt,
      images,
      repo,
      repo_explicitly_none: repoOverride === null,
      model_id: activeSelection?.modelId ?? null,
      effort: activeSelection?.effort ?? null,
      sandbox_type: sandboxType === "default" ? null : sandboxType,
    }
    setSubmitting(true)

    const configurable: Record<string, unknown> = {}
    if (activeSelection?.modelId && activeSelection.effort) {
      configurable.agent_model_id = activeSelection.modelId
      configurable.agent_effort = activeSelection.effort
    }
    if (repo) configurable.repo = repo
    if (repoOverride === null) configurable.repo_explicitly_none = true
    if (planMode) configurable.plan_mode = true
    if (adminThread) configurable.admin_thread = true
    if (selectedEnvironment) configurable.environment = selectedEnvironment
    if (sandboxType !== "default") configurable.sandbox_type = sandboxType

    await stream
      .submit(
        {
          messages: [{ type: "human", content: promptContent(prompt, images) }],
        },
        { config: { configurable } }
      )
      .catch((error) => {
        // Submit failed before the SDK minted a thread id — re-enable the
        // prompt instead of leaving it disabled until a reload.
        draftRef.current = null
        setSubmitting(false)
        throw error
      })
  }

  return (
    <div className="flex min-w-0 flex-1 flex-col overflow-y-auto px-3 py-6 sm:px-6 sm:py-8">
      {session.data && <OnboardingDialog />}
      <div className="mx-auto flex w-full max-w-2xl flex-1 flex-col items-center justify-center">
        <div className="flex w-full flex-col items-center gap-6">
          <Logo />
          <AgentPromptBar
            autoFocus
            onSubmit={handleSubmit}
            disabled={submitting}
            models={models}
            selection={activeSelection}
            onSelectionChange={handleSelectionChange}
            repos={reposQuery.data?.repositories}
            selectedRepo={repo}
            onRepoChange={setRepoOverride}
            onAddPath={handleAddPath}
            onDeletePath={handleDeletePath}
            planMode={planMode}
            onPlanModeChange={setPlanMode}
            environments={environments}
            selectedEnvironment={selectedEnvironment}
            onEnvironmentChange={setEnvironmentOverride}
            sandboxType={sandboxType}
            onSandboxTypeChange={setSandboxType}
            adminThread={adminThread}
            onAdminThreadChange={
              session.data?.is_admin ? setAdminThread : undefined
            }
            skills={skills.data}
          />
        </div>
      </div>
    </div>
  )
}
