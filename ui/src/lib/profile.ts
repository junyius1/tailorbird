// 个人资料 / 模型选项 / 仓库的查询层(本地自托管变体)。
// profile 持久化在后端 store;仓库列表无 GitHub 集成 → 返回当前仓库。
import { useEffect } from "react"
import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query"

import { ApiError, api } from "./api"
import type { Profile, ProfileUpdate, ReposPayload } from "./api"
import {
  REPOS_CACHE_MAX_AGE_MS,
  readCachedRepos,
  writeCachedRepos,
} from "./repoCache"
import { useSession } from "./session"

export function useProfile() {
  const session = useSession()
  return useQuery({
    queryKey: ["profile"],
    queryFn: api.profile,
    enabled: !!session.data,
  })
}

export function useOptions() {
  return useQuery({
    queryKey: ["options"],
    queryFn: api.options,
  })
}

export const reposQueryKey = (login: string | null) => ["repos", login] as const

const REPOS_STALE_TIME_MS = 10 * 60 * 1000

export function useRepos() {
  const session = useSession()
  const login = session.data?.login ?? null
  const qc = useQueryClient()

  useEffect(() => {
    if (!login) return
    const key = reposQueryKey(login)
    if (qc.getQueryData<ReposPayload>(key)) return
    const cached = readCachedRepos(login)
    if (!cached) return
    qc.setQueryData<ReposPayload>(key, cached.payload, {
      updatedAt: cached.updatedAt,
    })
  }, [login, qc])

  return useQuery({
    queryKey: reposQueryKey(login),
    queryFn: async () => {
      try {
        const payload = await api.repos()
        if (login) writeCachedRepos(login, payload)
        return payload
      } catch (e) {
        if (e instanceof ApiError && e.status === 401)
          return { installations: [], repositories: [] }
        throw e
      }
    },
    enabled: !!session.data,
    staleTime: REPOS_STALE_TIME_MS,
    gcTime: REPOS_CACHE_MAX_AGE_MS,
  })
}

export function useRefreshRepos() {
  const session = useSession()
  const login = session.data?.login ?? null
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => api.repos(),
    onSuccess: (payload) => {
      qc.setQueryData<ReposPayload>(reposQueryKey(login), payload)
      if (login) writeCachedRepos(login, payload)
    },
  })
}

export function useSaveProfile() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: ProfileUpdate) => api.saveProfile(body),
    onSuccess: (saved) => {
      qc.setQueryData(["profile"], saved)
    },
  })
}

export function buildProfileUpdate(
  current: Profile | undefined,
  patch: Partial<ProfileUpdate>,
  fallbackModel: string,
  fallbackEffort: string,
): ProfileUpdate {
  return {
    default_model: current?.default_model ?? fallbackModel,
    reasoning_effort: current?.reasoning_effort ?? fallbackEffort,
    default_subagent_model:
      current?.default_subagent_model ??
      current?.default_model ??
      fallbackModel,
    subagent_reasoning_effort:
      current?.subagent_reasoning_effort ??
      current?.reasoning_effort ??
      fallbackEffort,
    default_repo: current?.default_repo ?? null,
    base_branch: current?.base_branch ?? null,
    branch_prefix: current?.branch_prefix ?? null,
    auto_fix_ci: current?.auto_fix_ci ?? true,
    draft_prs: current?.draft_prs ?? true,
    review_draft_prs: current?.review_draft_prs ?? null,
    ...patch,
  }
}
