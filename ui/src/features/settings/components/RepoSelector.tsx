import { useEffect, useMemo, useRef, useState } from "react"
import {
  ArrowsClockwiseIcon,
  CaretDownIcon,
  FolderIcon,
  FolderPlusIcon,
  TrashIcon,
  XIcon,
} from "@phosphor-icons/react"

import {
  Tooltip,
  TooltipPopup,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { useRefreshRepos } from "@/lib/profile"
import { cn } from "@/lib/utils"

type RepoOption = { full_name: string; local_path?: string | null }

interface RepoSelectorProps {
  repos?: Array<RepoOption>
  selectedRepo?: string | null
  onRepoChange: (repo: string | null) => void
  placeholder?: string
  emptySelectionLabel?: string
  searchPlaceholder?: string
  noMatchesLabel?: string
  /** 提供后在下拉框底部出现"添加路径"入口(full_name 即宿主绝对路径) */
  onAddPath?: (path: string) => Promise<string | null>
  /** 提供后路径仓库行出现删除按钮;返回错误文案或 null(成功) */
  onDeletePath?: (fullName: string) => Promise<string | null>
  className?: string
  triggerClassName?: string
  dropdownClassName?: string
  disabled?: boolean
}

export function RepoSelector({
  repos,
  selectedRepo = null,
  onRepoChange,
  placeholder = "Select repository",
  emptySelectionLabel = "No repository",
  searchPlaceholder = "Search repositories…",
  noMatchesLabel = "No matches",
  onAddPath,
  onDeletePath,
  className,
  triggerClassName,
  dropdownClassName,
  disabled = false,
}: RepoSelectorProps) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState("")
  const [addOpen, setAddOpen] = useState(false)
  const [pathInput, setPathInput] = useState("")
  const [adding, setAdding] = useState(false)
  const [addError, setAddError] = useState<string | null>(null)
  const [deleting, setDeleting] = useState<string | null>(null)
  const dropdownRef = useRef<HTMLDivElement>(null)
  const addInputRef = useRef<HTMLInputElement>(null)
  const refresh = useRefreshRepos()

  const filteredRepos = useMemo(() => {
    const all = repos ?? []
    const q = query.trim().toLowerCase()
    if (!q) return all
    return all.filter((repo) => repo.full_name.toLowerCase().includes(q))
  }, [repos, query])

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      const target = e.target as Node
      if (dropdownRef.current && !dropdownRef.current.contains(target)) {
        setOpen(false)
        setAddOpen(false)
        setAddError(null)
      }
    }
    document.addEventListener("mousedown", handleClickOutside)
    return () => document.removeEventListener("mousedown", handleClickOutside)
  }, [])

  useEffect(() => {
    if (addOpen) addInputRef.current?.focus()
  }, [addOpen])

  function close() {
    setOpen(false)
    setQuery("")
    setAddOpen(false)
    setAddError(null)
  }

  async function handleAddPath(e: React.FormEvent) {
    e.preventDefault()
    if (!onAddPath || adding) return
    const path = pathInput.trim()
    if (!path) return
    setAdding(true)
    setAddError(null)
    const error = await onAddPath(path).catch((err: unknown) =>
      err instanceof Error ? err.message : "Failed to add path"
    )
    setAdding(false)
    if (error) {
      setAddError(error)
      return
    }
    setPathInput("")
    setAddOpen(false)
  }

  return (
    <div ref={dropdownRef} className={cn("relative min-w-0 shrink", className)}>
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen((value) => !value)}
        className={cn(
          "flex max-w-[260px] cursor-pointer items-center gap-1 text-muted-foreground transition-opacity hover:opacity-80 disabled:cursor-default disabled:opacity-60",
          triggerClassName
        )}
      >
        <FolderIcon className="size-3.5 shrink-0" />
        <span className="flex-1 truncate text-left">
          {selectedRepo || placeholder}
        </span>
        <CaretDownIcon className="size-3 shrink-0 opacity-70" />
      </button>
      {open && (
        <div
          className={cn(
            "absolute top-full left-0 z-50 mt-1 flex w-72 flex-col overflow-hidden rounded border border-border bg-popover text-xs text-popover-foreground shadow-lg",
            dropdownClassName
          )}
        >
          <div className="flex items-center border-b border-border">
            <input
              autoFocus
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={searchPlaceholder}
              className="min-w-0 flex-1 bg-transparent px-2 py-1.5 text-foreground outline-none placeholder:text-muted-foreground"
            />
            <button
              type="button"
              title="Refresh repositories"
              aria-label="Refresh repositories"
              disabled={refresh.isPending}
              onClick={() => refresh.mutate()}
              className="mr-1 cursor-pointer rounded p-1 text-muted-foreground transition-colors hover:bg-muted disabled:cursor-default"
            >
              <ArrowsClockwiseIcon
                className={cn("size-3.5", refresh.isPending && "animate-spin")}
              />
            </button>
          </div>
          <div className="max-h-60 overflow-y-auto">
            <button
              type="button"
              onClick={() => {
                onRepoChange(null)
                close()
              }}
              className={cn(
                "flex w-full items-center px-2 py-1.5 text-left transition-colors hover:bg-muted",
                selectedRepo ? "text-muted-foreground" : "text-foreground"
              )}
            >
              {emptySelectionLabel}
              {!selectedRepo && (
                <span className="ml-auto pl-3 text-muted-foreground">✓</span>
              )}
            </button>
            {filteredRepos.length === 0 ? (
              <div className="px-2 py-1.5 text-muted-foreground">
                {noMatchesLabel}
              </div>
            ) : (
              filteredRepos.map((repo) => {
                const selected = repo.full_name === selectedRepo
                // 悬停提示:该项对应的宿主实际路径(后端解析)
                const tip =
                  repo.local_path ||
                  (repo.full_name.startsWith("/")
                    ? repo.full_name
                    : repo.full_name)
                const isPathRepo = repo.full_name.startsWith("/")
                const canDelete = isPathRepo && Boolean(onDeletePath)
                const row = (
                  <div
                    className={cn(
                      "group flex w-full items-center pr-1 transition-colors hover:bg-muted",
                      selected ? "text-foreground" : "text-muted-foreground"
                    )}
                  >
                    <button
                      type="button"
                      onClick={() => {
                        onRepoChange(repo.full_name)
                        close()
                      }}
                      className="flex min-w-0 flex-1 cursor-pointer items-center px-2 py-1.5 text-left"
                    >
                      <span className="truncate">{repo.full_name}</span>
                      {selected && (
                        <span className="ml-auto pl-3 text-muted-foreground">
                          ✓
                        </span>
                      )}
                    </button>
                    {canDelete && (
                      <button
                        type="button"
                        title="Remove from list"
                        aria-label={`Remove ${repo.full_name}`}
                        disabled={deleting === repo.full_name}
                        onClick={async (e) => {
                          e.stopPropagation()
                          setDeleting(repo.full_name)
                          const error = await onDeletePath!(repo.full_name).catch(
                            (err: unknown) =>
                              err instanceof Error
                                ? err.message
                                : "Failed to remove"
                          )
                          setDeleting(null)
                          if (error) setAddError(error)
                        }}
                        className="mr-1 flex size-5 cursor-pointer items-center justify-center rounded text-muted-foreground opacity-0 transition-opacity hover:text-destructive group-hover:opacity-100 disabled:cursor-default disabled:opacity-40"
                      >
                        <TrashIcon className="size-3.5" />
                      </button>
                    )}
                  </div>
                )
                return (
                  <Tooltip key={repo.full_name}>
                    <TooltipTrigger render={row} />
                    <TooltipPopup
                      side="right"
                      align="start"
                      variant="glass"
                      className="max-w-80 font-mono text-[11px]"
                    >
                      {tip}
                    </TooltipPopup>
                  </Tooltip>
                )
              })
            )}
          </div>
          {onAddPath && (
            <div className="border-t border-border">
              {!addOpen ? (
                <button
                  type="button"
                  onClick={() => {
                    setAddOpen(true)
                    setAddError(null)
                  }}
                  className="flex w-full items-center gap-1.5 px-2 py-1.5 text-left text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                >
                  <FolderPlusIcon className="size-3.5 shrink-0" />
                  <span>Add path…</span>
                </button>
              ) : (
                <form onSubmit={handleAddPath} className="px-2 py-1.5">
                  <div className="flex items-center gap-1">
                    <input
                      ref={addInputRef}
                      value={pathInput}
                      onChange={(e) => {
                        setPathInput(e.target.value)
                        setAddError(null)
                      }}
                      placeholder="/path/to/repo"
                      spellCheck={false}
                      className="min-w-0 flex-1 rounded border border-border bg-background px-1.5 py-1 font-mono text-[11px] text-foreground outline-none placeholder:font-sans placeholder:text-muted-foreground"
                    />
                    <button
                      type="submit"
                      disabled={adding || !pathInput.trim()}
                      className="cursor-pointer rounded border border-border bg-muted px-1.5 py-1 text-foreground transition-colors hover:bg-accent disabled:cursor-default disabled:opacity-50"
                    >
                      {adding ? "…" : "Add"}
                    </button>
                    <button
                      type="button"
                      aria-label="Cancel add path"
                      onClick={() => {
                        setAddOpen(false)
                        setPathInput("")
                        setAddError(null)
                      }}
                      className="cursor-pointer rounded p-1 text-muted-foreground transition-colors hover:bg-muted"
                    >
                      <XIcon className="size-3" />
                    </button>
                  </div>
                  {addError && (
                    <p className="mt-1 text-[11px] text-destructive">
                      {addError}
                    </p>
                  )}
                </form>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
