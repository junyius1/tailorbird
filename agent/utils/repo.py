"""仓库解析:文本里的 ``repo:`` 语法 + 本地路径映射(无 GitHub 访问路径)。

设计书 §11:repo 解析为**本地路径**。``repo: owner/name`` 语法保留,
映射到宿主绝对路径的规则:
1. ``REPO_LOCAL_PATHS`` 环境变量显式映射(``owner/name:/path`` 逗号分隔);
2. 否则缺省本地根 ``LOCAL_SANDBOX_ROOT_DIR/repos/<name>``。
服务器不持有任何 GitHub token;``gh`` CLI 不代理、不要求登录。

repo 元信息统一形态 ``{"name", "owner"?, "local_path"?}``:``local_path``
存在时直达该宿主路径(UI "按路径添加" 的仓库),优先级高于 env 映射;
``normalize_repo_meta`` 把字符串(full_name 或本地绝对路径)归一化为该形态。
"""

import os
import re
from pathlib import Path

# 默认 owner(repo: 只给 name 时补全;本变体默认空)
_DEFAULT_REPO_OWNER = os.environ.get("DEFAULT_REPO_OWNER", "")
# 本地仓库根(与 integrations/docker.py 保持一致)
_LOCAL_ROOT = os.environ.get("LOCAL_SANDBOX_ROOT_DIR", "/tmp/tailorbird-sandboxes")
# repo: 语法匹配
_REPO_SYNTAX_RE = re.compile(r"repo[: ]([a-zA-Z0-9_.\-/]+)")


def normalize_repo_meta(
    repo: dict | str | None, *, create: bool = True
) -> dict[str, str] | None:
    """归一化 repo 元信息:dict(原样)/ ``owner/name`` 字符串 / 本地绝对路径。

    - dict:补默认值后原样返回(保留 ``local_path``/``owner``);
    - 字符串以 ``/`` 开头:宿主本地路径 → ``{"name": basename,
      "local_path": 规范化路径}``(``create=True`` 时目录不存在则创建);
    - 其它字符串:``owner/name`` 或 ``name`` 语义(与 extract_repo_from_text 一致);
    - 空/None:None。
    """
    if not repo:
        return None
    if isinstance(repo, dict):
        name = str(repo.get("name") or "").strip()
        if not name:
            return None
        meta: dict[str, str] = {"name": name}
        owner = str(repo.get("owner") or "").strip()
        if owner:
            meta["owner"] = owner
        local_path = str(repo.get("local_path") or "").strip()
        if local_path:
            meta["local_path"] = str(Path(local_path).expanduser().resolve())
        return meta
    value = str(repo).strip().rstrip("/")
    if not value:
        return None
    if value.startswith("/"):
        host = Path(value).expanduser().resolve()
        if create:
            host.mkdir(parents=True, exist_ok=True)
        return {"name": host.name or "workspace", "local_path": str(host)}
    if "/" in value:
        owner, name = value.split("/", 1)
        meta = {"name": name}
        if owner:
            meta["owner"] = owner
        return meta
    if _DEFAULT_REPO_OWNER:
        return {"name": value, "owner": _DEFAULT_REPO_OWNER}
    return {"name": value}


def repo_meta_to_full_name(repo: dict | str | None) -> str:
    """repo 元信息 → 展示用 full_name。

    - 路径仓库(``local_path``)→ 路径本身;
    - ``owner/name`` → ``owner/name``;
    - 仅 ``name`` → ``name``;
    - 空 → 空串。
    """
    if not repo:
        return ""
    if isinstance(repo, str):
        repo = normalize_repo_meta(repo, create=False)
        if repo is None:
            return ""
    local_path = str(repo.get("local_path") or "").strip()
    if local_path:
        return local_path
    owner = str(repo.get("owner") or "").strip()
    name = str(repo.get("name") or "").strip()
    if owner and name:
        return f"{owner}/{name}"
    return name


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
    local_path = str(repo.get("local_path") or "").strip()
    if local_path:
        # 路径添加的仓库:直达指定宿主路径,优先级高于 env 映射
        host = str(Path(local_path).expanduser().resolve())
        Path(host).mkdir(parents=True, exist_ok=True)
        return host
    mapping = _local_paths_map()
    host = mapping.get(f"{owner}/{name}" if owner else name) or mapping.get(name)
    if not host:
        host = str(Path(_LOCAL_ROOT) / "repos" / name)
    Path(host).mkdir(parents=True, exist_ok=True)
    return host
