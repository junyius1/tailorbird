#!/usr/bin/env bash
# 引导 3partylibs 源码(仓库内 editable 依赖的源)。
#
# Tailorbird 的 pyproject 用 3partylibs/ 下的 langchain / langgraph /
# deepagents 源码做 editable 安装(为匹配本地单事件循环 / 本地 vLLM 的
# 定制版本)。这些是第三方上游仓库,不进本仓库,由本脚本按钉死的 commit
# 拉取,保证全新机器可复现。
#
# 用法: bash scripts/bootstrap_3partylibs.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$ROOT/3partylibs"

# 钉死的上游仓库 + commit(与当前已验证可运行的版本一致)
declare -A REPO_URL=(
  [langchain]="git@github.com:langchain-ai/langchain.git"
  [langgraph]="git@github.com:langchain-ai/langgraph.git"
  [deepagents]="git@github.com:langchain-ai/deepagents.git"
)
declare -A REPO_SHA=(
  [langchain]="54383cd12c97eca2fd41984dfed99ea4d4ff1ee9"
  [langgraph]="38031739e551638e373fb553453256c23feeb41f"
  [deepagents]="dfde21e379201c833da4162444ef4a13b46980fd"
)

mkdir -p "$DEST"
for name in langchain langgraph deepagents; do
  url="${REPO_URL[$name]}"
  sha="${REPO_SHA[$name]}"
  dir="$DEST/$name"
  if [ -d "$dir/.git" ]; then
    cur="$(git -C "$dir" rev-parse HEAD 2>/dev/null || echo "")"
    if [ "$cur" = "$sha" ]; then
      echo "[skip] $name 已在 $sha"
      continue
    fi
    echo "[update] $name → $sha"
    git -C "$dir" fetch --depth 1 origin "$sha"
    git -C "$dir" checkout --detach "$sha"
  else
    echo "[clone] $name @ $sha"
    rm -rf "$dir"
    git clone --filter=blob:none "$url" "$dir"
    git -C "$dir" fetch --depth 1 origin "$sha"
    git -C "$dir" checkout --detach "$sha"
  fi
done

echo "3partylibs 引导完成。下一步: uv sync"
