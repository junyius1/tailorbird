"""本地(无 Docker)裸开发沙箱回退:直接在宿主工作目录跑命令。

仅用于没有 Docker 的环境;命令**直接在宿主执行**,无隔离。
目录与 docker provider **共用同一份宿主字节**(``agent.sandbox_layout``
统一布局),local ↔ docker 来回切换不丢状态:

- 普通仓库/无仓库:根 = ``<thread_root>``(= ``$LOCAL_SANDBOX_ROOT_DIR/
  threads/<tid 前 12 位>``),shell cwd = 系统提示 working_dir =
  ``<thread_root>/<name>``(或线程根;与 docker 容器内
  ``/workspace/<name>`` 后缀一致);
- 路径仓库(``repo.local_path``):根 = workdir = 宿主原路径
  (本来就是本宿主,直接可用)。

``virtual_mode=False``:文件工具与 shell 统一使用宿主真实路径(无虚拟
映射;local 定位即无隔离裸开发)。git 全局配置指向线程内文件(不污染宿主
``~/.gitconfig``)。
"""

import os
from pathlib import Path

from deepagents.backends.local_shell import LocalShellBackend

from ..sandbox_layout import migrate_legacy_dirs, resolve_thread_layout

# 线程内 git 全局配置文件名
SANDBOX_GITCONFIG = ".gitconfig-sandbox"


def _scoped_git_config_env(root_dir: str) -> dict[str, str]:
    """把 ``GIT_CONFIG_GLOBAL`` 指向线程内配置文件(序列化宿主身份,不 include)。"""
    scoped = Path(root_dir) / SANDBOX_GITCONFIG
    if not scoped.exists():
        # 序列化宿主 gitconfig 的 user.name/email(不做 [include],避免容器/线程互相污染)
        from configparser import ConfigParser

        config = ConfigParser(strict=False)  # 宿主 gitconfig 可能有重复键(如多 [safe] directory)
        host = Path.home() / ".gitconfig"
        if host.is_file():
            config.read(host)
        lines = ["[user]"]
        if config.has_section("user"):
            name = config.get("user", "name", fallback=None)
            email = config.get("user", "email", fallback=None)
            if name:
                lines.append(f"    name = {name}")
            if email:
                lines.append(f"    email = {email}")
        lines.append("[safe]")
        lines.append("    directory = *")
        scoped.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"GIT_CONFIG_GLOBAL": str(scoped)}


def create_local_sandbox(
    sandbox_id: str | None = None,
    *,
    repo: dict | None = None,
) -> LocalShellBackend:
    """创建本地裸沙箱(统一布局;与 docker provider 共用宿主目录)。

    参数:
        sandbox_id: 线程 id(布局命名键;None 用 default)。
        repo: 仓库元信息(解析 root/workdir;路径仓库直达宿主原路径)。
    """
    thread_id = str(sandbox_id) if sandbox_id else "default"
    # 旧布局(<root>/local/<tid12>) best-effort 迁移到统一 thread_root
    migrate_legacy_dirs(thread_id, repo)
    layout = resolve_thread_layout(thread_id, repo)
    Path(layout.local_root).mkdir(parents=True, exist_ok=True)
    if layout.local_workdir and layout.local_workdir != layout.local_root:
        Path(layout.local_workdir).mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    if not os.getenv("GIT_CONFIG_GLOBAL"):
        env.update(_scoped_git_config_env(layout.local_root))
    backend = LocalShellBackend(
        root_dir=layout.local_root,
        virtual_mode=False,
        inherit_env=False,
        env=env,
    )
    # workdir 供系统提示注入(shell cwd 由 LocalShellBackend 根决定;
    # 普通仓库时 cwd 应为仓库目录,用 wrapper 对齐 docker 的 -w 语义)
    _attach_workdir(backend, layout.local_workdir)
    return backend


def _attach_workdir(backend: LocalShellBackend, workdir: str) -> None:
    """把 LocalShellBackend 的 shell cwd 对齐 layout.workdir(幂等补丁)。

    ``LocalShellBackend`` 用构造时的 ``root_dir`` 作 cwd;统一布局下普通仓库
    的 workdir 是根的子目录,这里覆盖其 cwd 与 ``workdir`` 属性(文件工具
    在 ``virtual_mode=False`` 下用绝对路径,cwd 只影响相对命令)。
    """
    backend.cwd = Path(workdir)
    backend.workdir = workdir  # type: ignore[attr-defined]
