import { useEffect, useMemo, useRef, useState } from "react"
import {
  ArrowsClockwiseIcon,
  CaretDownIcon,
  FolderIcon,
  FolderPlusIcon,
  XIcon,
} from "@phosphor-icons/react"

import { useRefreshRepos } from "@/lib/profile"
import { cn } from "@/lib/utils"

type RepoOption = { full_name: string }

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
                return (
                  <button
                    key={repo.full_name}
                    type="button"
                    onClick={() => {
                      onRepoChange(repo.full_name)
                      close()
                    }}
                    title={repo.full_name}
                    className={cn(
                      "flex w-full items-center px-2 py-1.5 text-left transition-colors hover:bg-muted",
                      selected ? "text-foreground" : "text-muted-foreground"
                    )}
                  >
                    <span className="truncate">{repo.full_name}</span>
                    {selected && (
                      <span className="ml-auto pl-3 text-muted-foreground">
                        ✓
                      </span>
                    )}
                  </button>
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
