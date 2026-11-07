// Dashboard API 客户端(本地自托管变体)。
//
// 所有请求经同 origin 的 /dashboard/api 代理(credentials 带 osw_session
// cookie),浏览器不接触任何 token。后端:tailorbird dashboard(:8002)。
import { dashboardApiUrl } from "./dashboard-fetch"

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message)
    this.name = "ApiError"
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(dashboardApiUrl(path), {
    ...init,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...init.headers,
    },
  })
  if (!res.ok) {
    let message = res.statusText
    try {
      const body = await res.json()
      if (body?.detail)
        message =
          typeof body.detail === "string"
            ? body.detail
            : JSON.stringify(body.detail)
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, message)
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

// ---------------------------------------------------------------------------
// 会话
// ---------------------------------------------------------------------------

export interface SessionUser {
  login: string
  email: string | null
  avatar_url: string | null
  is_admin: boolean
}

// dashboard /me 返回 { sub, name, email, is_admin } → SessionUser
export function normalizeMe(raw: {
  sub: string
  name: string | null
  email: string | null
  is_admin: boolean
}): SessionUser {
  return {
    login: raw.sub,
    email: raw.email,
    avatar_url: null,
    is_admin: raw.is_admin,
  }
}

// 语音转写:本地变体不支持 → 明确报错(UI 据此禁用录音)
export async function transcribeAudio(_audio: Blob): Promise<string> {
  throw new ApiError(501, "本地变体不支持语音转写")
}

// ---------------------------------------------------------------------------
// 模型 / 环境选项(后端 /options 形态归一化)
// ---------------------------------------------------------------------------

export interface ModelOption {
  id: string
  label: string
  efforts: Array<string>
  default_effort: string
  supports_images: boolean
  context_window?: number | null
}

export interface OptionsPayload {
  models: Array<ModelOption>
  default_agent_model: string
  default_agent_reasoning_effort: string
  default_agent_subagent_model: string
  default_agent_subagent_reasoning_effort: string
}

// 后端 /options 原始形态(本地模型固定 effort 集)
interface RawModelOption {
  id: string
  label: string
  effort_options: Array<string>
  supports_images?: boolean
  context_window?: number | null
}

interface RawOptionsPayload {
  models: Array<RawModelOption>
  default_agent_model: string
  default_agent_reasoning_effort: string
}

function normalizeOptions(raw: RawOptionsPayload): OptionsPayload {
  const defaultEffort = raw.default_agent_reasoning_effort || "medium"
  const models = raw.models.map((m) => ({
    id: m.id,
    label: m.label,
    efforts: m.effort_options,
    default_effort: m.effort_options.includes(defaultEffort)
      ? defaultEffort
      : (m.effort_options[0] ?? "medium"),
    supports_images: m.supports_images ?? false,
    context_window: m.context_window ?? null,
  }))
  return {
    models,
    default_agent_model: raw.default_agent_model,
    default_agent_reasoning_effort: defaultEffort,
    default_agent_subagent_model: raw.default_agent_model,
    default_agent_subagent_reasoning_effort: defaultEffort,
  }
}

// ---------------------------------------------------------------------------
// 个人资料 / 仓库(本地变体:profile 持久化在后端 store,仓库固定当前仓)
// ---------------------------------------------------------------------------

export interface Profile {
  login?: string
  email?: string
  default_model?: string
  reasoning_effort?: string
  default_subagent_model?: string | null
  subagent_reasoning_effort?: string | null
  default_repo?: string | null
  base_branch?: string | null
  branch_prefix?: string | null
  auto_fix_ci?: boolean
  draft_prs?: boolean
  review_draft_prs?: boolean | null
  updated_at?: string
}

export interface ProfileUpdate {
  default_model: string
  reasoning_effort: string
  default_subagent_model?: string | null
  subagent_reasoning_effort?: string | null
  default_repo?: string | null
  base_branch?: string | null
  branch_prefix?: string | null
  auto_fix_ci?: boolean
  draft_prs?: boolean
  review_draft_prs?: boolean | null
}

export interface Repository {
  full_name: string
  private: boolean
}

export interface Installation {
  id: number
  account: string | null
  account_type: string | null
}

export interface ReposPayload {
  installations: Array<Installation>
  repositories: Array<Repository>
}

// ---------------------------------------------------------------------------
// 技能
// ---------------------------------------------------------------------------

export interface Skill {
  name: string
  description: string
  instructions: string
  created_at?: string
  updated_at?: string
}

export interface SkillInput {
  description: string
  instructions: string
}

export interface SkillsPage {
  items: Array<Skill>
  next_offset: number | null
}

export interface OrganizationSkillsPage {
  items: Array<Skill>
  next_cursor: string | null
}

export interface EnvironmentOption {
  slug: string
  name: string
  has_snapshot: boolean
}

export interface EnvironmentOptionList {
  environments: Array<EnvironmentOption>
  default_slug: string
}

// ---------------------------------------------------------------------------
// API 面
// ---------------------------------------------------------------------------

export const api = {
  me: async () => normalizeMe(await request("/me")),
  options: async () => normalizeOptions(await request<RawOptionsPayload>("/options")),
  profile: () => request<Profile>("/profile"),
  saveProfile: (body: ProfileUpdate) =>
    request<Profile>("/profile", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  repos: () => request<ReposPayload>("/repositories"),
  logout: () => request<{ ok: boolean }>("/auth/logout", { method: "POST" }),
  slackConnectUrl: () => "/dashboard/api/auth/slack/connect",

  listSkills: (offset = 0) =>
    request<Array<Skill>>(`/skills?limit=100&offset=${offset}`).then(
      (items) => ({ items, next_offset: null }),
    ),
  createSkill: (name: string, body: SkillInput) =>
    request<Skill>("/skills", {
      method: "POST",
      body: JSON.stringify({ name, ...body }),
    }),
  saveSkill: (name: string, body: SkillInput) =>
    request<Skill>(`/skills/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  deleteSkill: (name: string) =>
    request<void>(`/skills/${encodeURIComponent(name)}`, { method: "DELETE" }),
  listOrganizationSkills: (cursor: string | null = null) =>
    request<Array<Skill>>(
      `/organization-skills?limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
    ).then((items) => ({ items, next_cursor: null })),
  createOrganizationSkill: (name: string, body: SkillInput) =>
    request<Skill>("/organization-skills", {
      method: "POST",
      body: JSON.stringify({ name, ...body }),
    }),
  saveOrganizationSkill: (name: string, body: SkillInput) =>
    request<Skill>(`/organization-skills/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  deleteOrganizationSkill: (name: string) =>
    request<void>(`/organization-skills/${encodeURIComponent(name)}`, {
      method: "DELETE",
    }),
  listEnvironmentOptions: () =>
    request<EnvironmentOptionList>("/environments/options"),
}
