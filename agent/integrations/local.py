"""本地(无 Docker)裸开发沙箱回退:直接在宿主工作目录跑命令。

仅用于没有 Docker 的环境;命令**直接在宿主执行**,无隔离。
每线程一个根目录 ``LOCAL_SANDBOX_ROOT_DIR/<thread_id 前 12 位>``,
git 全局配置指向线程内文件(不污染宿主 ``~/.gitconfig``)。
"""

import os
from pathlib import Path

from deepagents.backends.local_shell import LocalShellBackend

# 线程内 git 全局配置文件名
SANDBOX_GITCONFIG = ".gitconfig-sandbox"


def _thread_root(thread_id: str) -> str:
    """线程 id → 宿主工作目录(不存在则建)。"""
    root = Path(os.environ.get("LOCAL_SANDBOX_ROOT_DIR", "/tmp/tailorbird-sandboxes"))
    target = root / "local" / str(thread_id)[:12]
    target.mkdir(parents=True, exist_ok=True)
    return str(target)


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


def create_local_sandbox(sandbox_id: str | None = None) -> LocalShellBackend:
    """创建本地裸沙箱(每线程一个根目录)。

    参数:
        sandbox_id: 线程 id(命名根目录;None 用 default)。
    """
    thread_id = str(sandbox_id) if sandbox_id else "default"
    root_dir = _thread_root(thread_id)
    env = dict(os.environ)
    if not os.getenv("GIT_CONFIG_GLOBAL"):
        env.update(_scoped_git_config_env(root_dir))
    return LocalShellBackend(
        root_dir=root_dir,
        virtual_mode=True,
        inherit_env=False,
        env=env,
    )
