"""仓库解析:文本里的 ``repo:`` 语法 + 本地路径映射(无 GitHub 访问路径)。

设计书 §11:repo 解析为**本地路径**。``repo: owner/name`` 语法保留,
映射到宿主绝对路径的规则:
1. ``REPO_LOCAL_PATHS`` 环境变量显式映射(``owner/name:/path`` 逗号分隔);
2. 否则缺省本地根 ``LOCAL_SANDBOX_ROOT_DIR/repos/<name>``。
服务器不持有任何 GitHub token;``gh`` CLI 不代理、不要求登录。
"""

import os
import re

# 默认 owner(repo: 只给 name 时补全;本变体默认空)
_DEFAULT_REPO_OWNER = os.environ.get("DEFAULT_REPO_OWNER", "")
# 本地仓库根(与 integrations/docker.py 保持一致)
_LOCAL_ROOT = os.environ.get("LOCAL_SANDBOX_ROOT_DIR", "/tmp/tailorbird-sandboxes")
# repo: 语法匹配
_REPO_SYNTAX_RE = re.compile(r"repo[: ]([a-zA-Z0-9_.\-/]+)")


def extract_repo_from_text(text: str) -> dict[str, str] | None:
    """从文本里提取 ``owner/name``;找不到返回 None。

    只认 ``repo: owner/name`` / ``repo owner/name`` 语法(不再解析 GitHub URL,
    避免任何 GitHub 访问路径)。
    """
    if "repo:" not in text and "repo " not in text:
        return None
    match = _REPO_SYNTAX_RE.search(text)
    if not match:
        return None
    value = match.group(1).rstrip("/")
    if "/" in value:
        owner, name = value.split("/", 1)
    else:
        owner, name = _DEFAULT_REPO_OWNER, value
    if not name:
        return None
    repo: dict[str, str] = {"name": name}
    if owner:
        repo["owner"] = owner
    return repo


def _local_paths_map() -> dict[str, str]:
    """解析 REPO_LOCAL_PATHS(owner/name 或 name → 宿主绝对路径)。"""
    mapping: dict[str, str] = {}
    for entry in os.environ.get("REPO_LOCAL_PATHS", "").split(","):
        if not entry.strip():
            continue
        key, sep, value = entry.partition(":")
        if sep:
            mapping[key.strip()] = value.strip()
    return mapping


def resolve_local_repo_path(repo: dict[str, str] | None) -> str:
    """仓库元信息 → 宿主绝对路径(不存在则创建)。"""
    if not repo:
        base = Path(_LOCAL_ROOT) / "workspace"
        base.mkdir(parents=True, exist_ok=True)
        return str(base)
    name = str(repo.get("name") or "workspace").strip()
    owner = str(repo.get("owner") or "").strip()
    mapping = _local_paths_map()
    host = mapping.get(f"{owner}/{name}" if owner else name) or mapping.get(name)
    if not host:
        host = str(Path(_LOCAL_ROOT) / "repos" / name)
    Path(host).mkdir(parents=True, exist_ok=True)
    return host
