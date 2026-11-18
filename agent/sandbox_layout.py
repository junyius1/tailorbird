"""线程沙箱统一布局(docker 与 local 的共同事实源)。

切换 provider 不丢状态的关键:**每线程一个宿主目录**
``$LOCAL_SANDBOX_ROOT_DIR/threads/<thread_id 前 12 位>``,docker 与 local
共用同一份宿主字节,provider 只是"解释器":

- docker:按 ``mounts`` 挂载清单逐个 bind 进容器(多目录);
- local:直接以 ``local_root`` 为 ``LocalShellBackend`` 根
  (``virtual_mode=False``,文件工具/shell 统一宿主真实路径)。

repo 形态 → 目录规则:

===================  ==============================  ===========================
repo                宿主仓库目录 repo_host            说明
===================  ==============================  ===========================
路径仓库 local_path  local_path 原路径                 同路径挂载(现状保持)
普通仓库             ``<thread_root>/<name>``            新布局(旧 ``<root>/repos/<name>``)
无仓库               ``<thread_root>``                  整个线程根即工作目录
===================  ==============================  ===========================

本模块除 ``migrate_legacy_dirs`` 的 best-effort 文件移动外无 I/O,
docker/local 集成与 dashboard diff 接口都以这里为准,不再各自维护布局。
"""

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# 每线程宿主根 = 本地根/threads/<tid 前 12 位>
_THREADS_SUBDIR = "threads"
# 容器内工作目录根(普通仓库/无仓库 bind 目标)
_WORKSPACE_ROOT = "/workspace"
# 容器内额外挂载的根目录(避免与 /workspace 及常用路径冲突)
EXTRA_MOUNT_ROOT = "/host"


def _local_root() -> Path:
    return Path(os.environ.get("LOCAL_SANDBOX_ROOT_DIR", "/tmp/tailorbird-sandboxes"))


def thread_root_dir(thread_id: str) -> Path:
    """线程宿主根目录(docker bind 源 / local 后端根;不存在则建)。"""
    root = _local_root() / _THREADS_SUBDIR / str(thread_id)[:12]
    root.mkdir(parents=True, exist_ok=True)
    return root


@dataclass(frozen=True)
class Mount:
    """一条 docker bind 挂载:宿主路径 → 容器内路径。"""

    host: str
    target: str
    read_only: bool = False


@dataclass
class ThreadLayout:
    """线程沙箱布局(纯计算结果;docker/local 共同事实源)。

    - ``thread_root``:每线程宿主根(docker bind 源 / local 后端根);
    - ``workdir``:docker 容器内工作目录(进系统提示);
    - ``repo_host``:宿主仓库目录(diff 接口 / 迁移);
    - ``mounts``:docker 挂载清单(仓库 + 路径仓库同路径 + extras,多目录);
    - ``local_root``:local ``LocalShellBackend.root_dir``(= shell cwd);
    - ``local_workdir``:local 系统提示 working_dir(宿主真实路径)。
    """

    thread_id: str
    thread_root: str
    workdir: str
    repo_host: str
    mounts: list[Mount] = field(default_factory=list)
    local_root: str = ""
    local_workdir: str = ""


def _repo_name(repo: dict | None) -> str:
    return str((repo or {}).get("name") or "").strip()


def _repo_local_path(repo: dict | None) -> str:
    return str((repo or {}).get("local_path") or "").strip()


def _repo_host_path(repo: dict | None, thread_root: Path) -> Path:
    """repo → 宿主仓库目录(与 utils/repo.py 的 REPO_LOCAL_PATHS 语义一致)。

    路径仓库(local_path)= 原路径;其余 = ``<thread_root>/<name>``
    (REPO_LOCAL_PATHS 显式映射保留:该仓库固定在映射宿主路径)。
    """
    local_path = _repo_local_path(repo)
    if local_path:
        return Path(local_path).expanduser().resolve()
    name = _repo_name(repo)
    if not name:
        return thread_root
    owner = str((repo or {}).get("owner") or "").strip()
    key = f"{owner}/{name}" if owner else name
    mapping_raw = os.environ.get("REPO_LOCAL_PATHS", "")
    for entry in mapping_raw.split(","):
        if not entry.strip():
            continue
        map_key, sep, map_value = entry.partition(":")
        if sep and map_key.strip() == key:
            return Path(map_value.strip()).expanduser().resolve()
    return thread_root / name


def resolve_thread_layout(
    thread_id: str,
    repo: dict | None = None,
) -> ThreadLayout:
    """线程 + repo → 统一布局(docker 挂载清单 / local 根与 workdir)。

    规则(见模块 docstring 表;docker 容器路径与 local 宿主路径的后缀一致,
    切换 provider 时模型提示里的路径按各自前缀自然对应):
    - 路径仓库:同路径挂载,workdir = 宿主原路径(docker/local 一致);
    - 普通仓库:``<thread_root>:/workspace``,workdir = ``/workspace/<name>``
      (= 宿主 ``<thread_root>/<name>``);
    - 无仓库:``<thread_root>:/workspace``,workdir = ``/workspace``
      (= 宿主 ``<thread_root>``);
    - ``DOCKER_SANDBOX_EXTRA_MOUNTS`` 追加到 ``mounts``(local 不额外处理,
      宿主路径对 local 命令本就可达)。
    """
    tid = str(thread_id)
    troot = thread_root_dir(tid)
    local_path = _repo_local_path(repo)

    if local_path:
        # 路径仓库:同路径挂载(容器内路径 = 宿主原路径,现状保持)
        host = str(Path(local_path).expanduser().resolve())
        layout = ThreadLayout(
            thread_id=tid,
            thread_root=str(troot),
            workdir=host,
            repo_host=host,
            mounts=[Mount(host=host, target=host)],
            local_root=host,
            local_workdir=host,
        )
    else:
        name = _repo_name(repo)
        repo_host = _repo_host_path(repo, troot)
        if name:
            workdir = f"{_WORKSPACE_ROOT}/{name}"
            local_workdir = str(repo_host)
        else:
            workdir = _WORKSPACE_ROOT
            local_workdir = str(troot)
        layout = ThreadLayout(
            thread_id=tid,
            thread_root=str(troot),
            workdir=workdir,
            repo_host=str(repo_host),
            mounts=[Mount(host=str(troot), target=_WORKSPACE_ROOT)],
            local_root=str(troot),
            local_workdir=local_workdir,
        )

    # 附加宿主挂载(docker 专用;local 命令直接可及宿主路径,等价)
    layout.mounts.extend(parse_extra_mounts())
    return layout


def parse_extra_mounts() -> list[Mount]:
    """解析 ``DOCKER_SANDBOX_EXTRA_MOUNTS`` → Mount 清单。

    格式:逗号分隔,每项 ``宿主路径:容器内路径[:ro]``(宿主路径支持 ``~``;
    容器内路径缺省 ``/host/<绝对形式去前导 />``;缺省只读)。
    配置错误(空项/缺冒号/非法模式)告警跳过,不阻断沙箱创建。
    """
    raw = os.environ.get("DOCKER_SANDBOX_EXTRA_MOUNTS", "")
    mounts: list[Mount] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            parts = entry.split(":")
            if len(parts) < 2 or not parts[0].strip() or not parts[1].strip():
                raise ValueError(f"格式应为 宿主路径:容器内路径[:ro],实际为 {entry!r}")
            host_part = parts[0].strip()
            container_part = parts[1].strip()
            mode = ":".join(parts[2:]).strip() if len(parts) > 2 else ""
            if mode not in ("", "ro", "rw"):
                raise ValueError(f"挂载模式只支持 ro/rw,实际为 {mode!r}")
            if not host_part.startswith("/") and not host_part.startswith("~"):
                raise ValueError(f"宿主路径必须是绝对路径(支持 ~): {host_part!r}")
            host = str(Path(host_part).expanduser().resolve())
            if not container_part.startswith("/"):
                container_part = f"{EXTRA_MOUNT_ROOT}/{container_part}"
            mounts.append(Mount(host=host, target=container_part, read_only=mode != "rw"))
        except ValueError as exc:
            logger.warning("DOCKER_SANDBOX_EXTRA_MOUNTS 项 %r 已忽略: %s", entry, exc)
    if mounts:
        logger.info("沙箱额外挂载: %s", " ".join(f"{m.host}:{m.target}" for m in mounts))
    return mounts


def migrate_legacy_dirs(thread_id: str, repo: dict | None = None) -> None:
    """best-effort 迁移旧布局目录到新 thread_root(源存在且目标不存在才移动)。

    - local 旧根 ``<root>/local/<tid12>`` → 新 thread_root;
    - docker 旧仓库 ``<root>/repos/<name>`` → ``<thread_root>/<name>``。

    失败仅告警不阻断(旧数据保留在原处,新目录从零开始)。
    """
    local_path = _repo_local_path(repo)
    if local_path:
        return  # 路径仓库:宿主原路径,无迁移

    root = _local_root()
    tid12 = str(thread_id)[:12]
    # 先按**路径**计算目标(不建目录):建了目录"目标不存在"检查就恒假
    troot = root / _THREADS_SUBDIR / tid12

    # local 旧根 → 新 thread_root
    legacy_local = root / "local" / tid12
    if legacy_local.is_dir() and not troot.exists():
        try:
            troot.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy_local), str(troot))
            logger.info("已迁移 local 沙箱目录 %s → %s", legacy_local, troot)
        except OSError as exc:
            logger.warning("迁移 local 沙箱目录失败(%s → %s): %s", legacy_local, troot, exc)
        return  # 整根已迁移,不再单迁仓库

    # docker 旧仓库目录 → 新 layout 的 repo_host
    name = _repo_name(repo)
    if not name:
        return
    troot.mkdir(parents=True, exist_ok=True)
    layout = resolve_thread_layout(thread_id, repo)
    legacy_repo = root / "repos" / name
    target = Path(layout.repo_host)
    if legacy_repo.is_dir() and not target.exists() and str(legacy_repo) != str(target):
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy_repo), str(target))
            logger.info("已迁移仓库目录 %s → %s", legacy_repo, target)
        except OSError as exc:
            logger.warning("迁移仓库目录失败(%s → %s): %s", legacy_repo, target, exc)
