"""本地 Docker 沙箱 provider(设计书 §4.4 / 2024 设计书 §4.1.1)。

替代全部 5 种云 provider(langsmith/daytona/modal/runloop/e2b),
唯一 provider = ``docker``:每线程一个容器,跑在宿主 Docker daemon 上。

要点:
- 容器名 ``oswe-sbx-<thread_id 前 12 位>``;已存在且存活 → 复用;
  已删除 → ``SandboxGoneError``(调用方重建);不存在 → ``docker create``。
- ``-v <宿主仓库绝对路径>:/workspace/<repo>``(bind mount,git 对象库共享宿主);
  ``-v <线程 .gitconfig-sandbox>:/workspace/.gitconfig-sandbox:ro`` +
  ``GIT_CONFIG_GLOBAL=/workspace/.gitconfig-sandbox`` → commit 作者 = 线程 owner 的 git 身份。
- 命令执行 = ``docker exec -i -w /workspace/<repo> <容器> sh -lc <cmd>``;
- 文件/搜索工具(继承自 ``BaseSandbox``)在**容器路径空间**工作
  (``/workspace/<repo>/...``),``upload_files``/``download_files`` 把容器路径
  映射到 bind mount 宿主路径落盘(零拷贝),命令仍走 docker exec。
  超时 = SIGTERM/SIGKILL 该 exec 进程(不杀容器)。
- **不挂** Docker socket、**不注入**任何 API key / GitHub token。
- run 结束**不销毁**(线程复用);删线程时 best-effort ``docker rm -f``。
"""

import asyncio
import json
import logging
import os
import shlex
import subprocess
import time
from pathlib import Path

from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox

logger = logging.getLogger(__name__)

# 容器名前缀(容器名 = 前缀 + thread_id 前 12 位)
_CONTAINER_PREFIX = "oswe-sbx-"
# 容器内工作目录根(仓库 bind 到 /workspace/<repo>)
_WORKSPACE_ROOT = "/workspace"
# 容器内 git 配置文件路径(宿主线程序列化文件以只读方式挂入)
_GITCONFIG_PATH = "/workspace/.gitconfig-sandbox"
# 线程级 git 配置文件的宿主目录
_GITCONFIG_ROOT = Path(os.environ.get("DOCKER_SANDBOX_GITCONFIG_DIR", "/tmp/tailorbird-gitconfig"))
# 默认命令超时(秒)
_DEFAULT_COMMAND_TIMEOUT = 600


class DockerSandboxError(RuntimeError):
    """Docker daemon 不可用或容器操作失败。"""


def _container_name(sandbox_id: str) -> str:
    """sandbox_id(= thread_id)→ 容器名(前 12 位)。"""
    return f"{_CONTAINER_PREFIX}{str(sandbox_id)[:12]}"


def _docker(*args: str, timeout: int = 60) -> str:
    """执行 docker CLI 命令,返回 stdout;失败抛 DockerSandboxError。

    参数:
        *args: docker 子命令参数(如 ``("info",)``)。
        timeout: 命令超时(秒)。
    """
    cmd = ["docker", *args]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError as exc:
        raise DockerSandboxError("docker CLI 不存在,无法使用 docker 沙箱") from exc
    except subprocess.TimeoutExpired as exc:
        raise DockerSandboxError(f"docker {' '.join(args[:2])} 超时") from exc
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()[:500]
        raise DockerSandboxError(
            f"docker {' '.join(args[:2])} 失败(rc={proc.returncode}): {stderr}"
        )
    return proc.stdout


def _container_state(container: str) -> str | None:
    """查容器状态;不存在返回 None,存在返回 State(如 running/exited)。

    ``docker inspect`` 对不存在的对象 rc=1(stderr 含 "no such object"),
    这是**存在性探测**的正常路径,返回 None 而非抛错。
    """
    try:
        out = _docker(
            "inspect",
            "-f",
            "{{.State.Status}}",
            container,
        )
    except DockerSandboxError as exc:
        message = str(exc).lower()
        if "no such object" in message or "not found" in message:
            return None
        raise
    state = out.strip()
    return state or None


def write_thread_gitconfig(
    thread_id: str,
    *,
    git_name: str | None = None,
    git_email: str | None = None,
) -> Path:
    """生成/刷新该线程的 ``.gitconfig-sandbox`` 文件,返回宿主路径。

    内容 = 已解析的 git 身份(由 ``_resolve_git_identity`` 在事件循环里解析好
    后传入):优先本地用户注册表 git_name/git_email,否则宿主 ``~/.gitconfig``
    的 user.name/user.email(序列化写入,不做 [include] 宿主文件)。
    本函数只做纯文件写,可在线程池安全调用。
    """
    name, email = git_name, git_email
    _GITCONFIG_ROOT.mkdir(parents=True, exist_ok=True)
    path = _GITCONFIG_ROOT / f"oswe-sbx-{str(thread_id)[:12]}.gitconfig"
    lines = ["[user]"]
    if name:
        lines.append(f"    name = {name}")
    if email:
        lines.append(f"    email = {email}")
    # safe.directory 防止容器内 uid 不匹配时 git 拒认仓库
    lines.append("[safe]")
    lines.append("    directory = *")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


async def _resolve_git_identity(owner_username: str | None) -> tuple[str | None, str | None]:
    """解析 commit 作者身份:注册表 git_name/git_email 优先,回退宿主 gitconfig。

    注册表读取是 async(SDK store),必须在事件循环里 await,
    不能在 worker 线程同步调用。
    """
    # 1) 用户注册表(延迟导入,避免循环依赖)
    if owner_username:
        try:
            from ..dashboard.user_registry import read_user_record

            record = await read_user_record(owner_username)
            if isinstance(record, dict):
                name = record.get("git_name")
                email = record.get("git_email")
                if name and email:
                    return str(name), str(email)
        except Exception:  # 注册表不可用不阻断沙箱创建
            logger.debug("用户注册表读取失败,回退宿主 gitconfig", exc_info=True)
    # 2) 宿主 ~/.gitconfig 的 user.name / user.email(文件 I/O → 线程池)
    try:
        return await asyncio.to_thread(_read_host_git_identity)
    except Exception:
        logger.debug("宿主 gitconfig 解析失败", exc_info=True)
    return None, None


def _read_host_git_identity() -> tuple[str | None, str | None]:
    """读宿主 ``~/.gitconfig`` / ``/etc/gitconfig`` 的 user.name/user.email(阻塞,线程池调用)。"""
    from configparser import ConfigParser

    config = ConfigParser(strict=False)  # 宿主 gitconfig 可能有重复键(如多 [safe] directory)
    candidates = [
        Path.home() / ".gitconfig",
        Path("/etc/gitconfig"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            config.read(candidate)
            if config.has_section("user"):
                name = config.get("user", "name", fallback=None)
                email = config.get("user", "email", fallback=None)
                if name or email:
                    return name, email
    return None, None


def _repo_mount_point(repo: dict | None) -> tuple[str, str | None]:
    """解析仓库 → (容器内挂载点, 宿主绝对路径)。

    本变体仓库是**本地路径**概念:``repo = {"name": ..., "owner"?: ...,
    "local_path"?: ...}``。宿主路径解析规则(与 utils/repo.py 一致):
    - ``local_path``(UI 按路径添加的仓库)→ 直达该宿主路径;
    - REPO_LOCAL_PATHS 环境变量(owner/name 或 name → 宿主绝对路径)显式映射;
    - 否则 ``/workspace/<repo>`` 下自动建目录(git init 由 agent 完成)。
    无仓库时回退 ``/workspace``(宿主 ``LOCAL_SANDBOX_ROOT_DIR/workspace``)。
    """
    name = ""
    if isinstance(repo, dict):
        name = str(repo.get("name") or "").strip()
    if not name:
        root = Path(os.environ.get("LOCAL_SANDBOX_ROOT_DIR", "/tmp/tailorbird-sandboxes"))
        host = root / "workspace"
        return _WORKSPACE_ROOT, str(host)
    # 路径添加的仓库:local_path 直达宿主路径(优先级高于 env 映射)
    local_path = str((repo or {}).get("local_path") or "").strip()
    if local_path:
        return f"{_WORKSPACE_ROOT}/{name}", str(
            Path(local_path).expanduser().resolve()
        )
    # 显式映射表:owner/name 或 name → 宿主路径
    mapping_raw = os.environ.get("REPO_LOCAL_PATHS", "")
    owner = str((repo or {}).get("owner") or "").strip()
    key = f"{owner}/{name}" if owner else name
    for entry in mapping_raw.split(","):
        if not entry.strip():
            continue
        map_key, sep, map_value = entry.partition(":")
        if sep and map_key.strip() == key:
            return f"{_WORKSPACE_ROOT}/{name}", map_value.strip()
    # 默认:本地根目录下按仓库名建目录
    root = Path(os.environ.get("LOCAL_SANDBOX_ROOT_DIR", "/tmp/tailorbird-sandboxes"))
    host = root / "repos" / name
    return f"{_WORKSPACE_ROOT}/{name}", str(host)


class DockerSandbox(BaseSandbox):
    """本地 Docker 容器沙箱后端(实现 SandboxBackendProtocol)。

    文件/搜索操作直接走 bind mount 的宿主路径(零拷贝、快),
    命令执行走 ``docker exec``(容器内环境)。
    """

    # execute 输出大时是否允许 capture-at-source offload(容器内 coreutils 齐全)
    enable_capture_offload: bool = False

    def __init__(
        self,
        sandbox_id: str,
        repo: dict | None = None,
        owner_username: str | None = None,
    ) -> None:
        # 线程 id(= sandbox_id,容器名据此生成)
        self._sandbox_id: str = str(sandbox_id)
        # 容器名
        self._container: str = _container_name(self._sandbox_id)
        # 仓库元信息(解析挂载点用)
        self._repo: dict | None = repo
        # 线程 owner(解析 git 身份用)
        self._owner_username: str | None = owner_username
        # 容器内工作目录 / 宿主仓库路径(纯计算,无 I/O)
        self._workdir, host_path = _repo_mount_point(repo)
        # 宿主仓库目录(bind mount 源);实际 mkdir 在 _prepare_fs 里(线程池)
        self._host_repo: str = host_path or ""
        # 容器内 git 配置文件的宿主路径;实际写盘在 _prepare_fs 里(线程池)
        self._gitconfig_host: Path = _GITCONFIG_ROOT / f"{self._container}.gitconfig"
        # 命令默认超时
        self._default_timeout: int = _DEFAULT_COMMAND_TIMEOUT

    def _prepare_fs(self, git_name: str | None, git_email: str | None) -> None:
        """落盘宿主侧前置:仓库目录 mkdir + gitconfig 写入(阻塞 I/O,须在线程池调用)。

        身份由工厂在事件循环里解析好(注册表是 async store)后传入。
        """
        if self._host_repo:
            Path(self._host_repo).mkdir(parents=True, exist_ok=True)
        self._gitconfig_host = write_thread_gitconfig(
            self._sandbox_id, git_name=git_name, git_email=git_email
        )

    @property
    def id(self) -> str:
        """沙箱后端唯一标识(= thread_id)。"""
        return self._sandbox_id

    @property
    def host_repo_path(self) -> str:
        """宿主仓库目录(bind mount 源),供 dashboard 的 git diff 端点使用。"""
        return self._host_repo

    @property
    def workdir(self) -> str:
        """容器内工作目录(文件相对路径的根)。"""
        return self._workdir

    def _ensure_container(self) -> None:
        """确保容器存在且运行;不存在则创建,死了则 start,删除则报错。"""
        try:
            state = _container_state(self._container)
        except DockerSandboxError as exc:
            # docker inspect 失败可能是容器不存在(stderr 含 "No such object")
            if "No such object" in str(exc) or "not found" in str(exc).lower():
                state = None
            else:
                raise
        if state is None:
            self._create_container()
        elif state == "exited" or state == "created":
            _docker("start", self._container)
        # running/created→started 后无需额外操作

    def _create_container(self) -> None:
        """docker create:bind mount 仓库 + 只读 gitconfig + 环境,不挂 socket、不注入 token。"""
        image = os.environ.get("DOCKER_SANDBOX_IMAGE", "python:3.12-slim")
        network = os.environ.get("DOCKER_SANDBOX_NETWORK", "bridge")
        args = [
            "create",
            "--name",
            self._container,
            # 容器内 git 全局配置 = 挂载的线程身份文件
            "-e",
            f"GIT_CONFIG_GLOBAL={_GITCONFIG_PATH}",
            "-e",
            f"HOME=/workspace",
            "-e",
            f"SANDBOX_THREAD_ID={self._sandbox_id}",
            # 线程级 git 身份(只读挂载)
            "-v",
            f"{self._gitconfig_host}:{_GITCONFIG_PATH}:ro",
            # 工作目录
            "-w",
            self._workdir,
        ]
        # 仓库 bind mount(共享 git 对象库);宿主目录必须存在
        if self._host_repo:
            args += [
                "-v",
                f"{self._host_repo}:{self._workdir}",
            ]
        # 网络:bridge(默认)或 none(断网)
        if network == "none":
            args += ["--network", "none"]
        # 资源上限(可选)
        mem = os.environ.get("DOCKER_SANDBOX_MEM", "")
        if mem:
            args += ["-m", mem]
        cpus = os.environ.get("DOCKER_SANDBOX_CPUS", "")
        if cpus:
            args += ["--cpus", cpus]
        # 默认命令:保持容器存活(idle);真正命令走 docker exec
        args += [image, "sleep", "infinity"]
        _docker(*args, timeout=180)
        _docker("start", self._container)
        logger.info("已创建沙箱容器 %s(镜像=%s,仓库=%s)", self._container, image, self._workdir)

    def _setup_container_tools(self) -> None:
        """新容器一次性初始化:装 git(镜像 python:3.12-slim 不带)。

        已装则跳过(复用/重建幂等);阻塞 I/O,由工厂在 to_thread 里调用。
        """
        try:
            _docker("exec", self._container, "sh", "-c", "command -v git >/dev/null 2>&1", timeout=30)
            return  # 已有 git
        except DockerSandboxError:
            pass
        logger.info("沙箱容器 %s 初始化:安装 git", self._container)
        _docker(
            "exec",
            self._container,
            "sh",
            "-c",
            "apt-get update -qq && apt-get install -y -qq git >/dev/null",
            timeout=600,
        )

    # ---- 命令执行 -------------------------------------------------------

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        """在容器内执行 shell 命令:``docker exec -i -w <workdir> <容器> sh -lc <cmd>``。

        超时 = 杀掉 exec 进程(不杀容器);输出合并 stdout+stderr,超限截断。
        """
        self._ensure_container()
        # 非负校验(timeout=0 表示不限时,deepagents 约定)
        if timeout is not None and timeout < 0:
            raise ValueError("timeout 必须为非负整数")
        effective_timeout = timeout if timeout and timeout > 0 else self._default_timeout
        cmd = [
            "docker",
            "exec",
            "-i",
            "-w",
            self._workdir,
            self._container,
            "sh",
            "-lc",
            command,
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
            )
        except subprocess.TimeoutExpired:
            # 超时:docker exec 子进程被 python 杀掉(容器不受影响)
            return ExecuteResponse(
                output=f"[超时] 命令执行超过 {effective_timeout}s,已终止",
                exit_code=124,
                truncated=False,
            )
        output = proc.stdout or ""
        if proc.stderr:
            output += f"\n[stderr]\n{proc.stderr}"
        # 输出截断(与 LocalShellBackend 一致的 100KB 上限)
        truncated = False
        max_chars = 100_000
        if len(output) > max_chars:
            output = output[:max_chars] + f"\n...[输出已截断,共 {len(output)} 字符以上]"
            truncated = True
        return ExecuteResponse(
            output=output,
            exit_code=proc.returncode,
            truncated=truncated,
        )

    # ---- 文件操作(走 bind mount 的宿主路径直读,零拷贝)------------------

    def _host_path_for(self, path: str) -> Path:
        """容器路径 → 宿主路径(upload_files/download_files/delete 用)。

        ``BaseSandbox`` 的文件方法传入的是**容器绝对路径**(如
        ``/workspace/<repo>/x.py``),而仓库是 bind mount:容器里
        ``<workdir>/...`` 对应宿主的 ``self._host_repo/...``。因此:
        - 无仓库回退(容器根 = /workspace):宿主根 =
          ``LOCAL_SANDBOX_ROOT_DIR/workspace``(与 ``_repo_mount_point`` 一致);
        - 有仓库:``workdir`` 前缀替换为宿主仓库路径。
        相对路径与容器根内其它路径(如 ``/tmp``)按原样挂到对应宿主根下。
        """
        cleaned = str(path).lstrip("/")
        if not self._host_repo:
            root = Path(
                os.environ.get("LOCAL_SANDBOX_ROOT_DIR", "/tmp/tailorbird-sandboxes")
            ) / "workspace"
            return root / cleaned
        wd = self._workdir.lstrip("/")
        if not cleaned:
            return Path(self._host_repo)
        if cleaned == wd:
            return Path(self._host_repo)
        if cleaned.startswith(wd + "/"):
            return Path(self._host_repo) / cleaned[len(wd) + 1 :]
        # 容器根内、仓库挂载之外的路径(如 /tmp):挂到宿主仓库根下尽力映射
        return Path(self._host_repo) / cleaned

    def upload_files(
        self, files: list[tuple[str, bytes]]
    ) -> list[FileUploadResponse]:
        """把文件写入沙箱(宿主 bind 路径直写)。"""
        from deepagents.backends.protocol import FileData

        results: list[FileUploadResponse] = []
        for path, content in files:
            target = self._host_path_for(path)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                results.append(FileUploadResponse(path=path, error=None))
            except Exception as exc:  # 单文件失败不阻断其它文件
                results.append(FileUploadResponse(path=path, error=str(exc)))
        _ = FileData
        return results

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """从沙箱读文件(宿主 bind 路径直读)。"""
        results: list[FileDownloadResponse] = []
        for path in paths:
            target = self._host_path_for(path)
            try:
                if not target.is_file():
                    results.append(
                        FileDownloadResponse(path=path, content=None, error="not found")
                    )
                    continue
                results.append(
                    FileDownloadResponse(
                        path=path, content=target.read_bytes(), error=None
                    )
                )
            except Exception as exc:
                results.append(
                    FileDownloadResponse(path=path, content=None, error=str(exc))
                )
        return results

    def delete(self, file_path: str) -> "DeleteResult":
        """删除沙箱内路径(宿主 bind 路径直删;目录递归、符号链接只删链接)。"""
        import shutil

        from deepagents.backends.protocol import DeleteResult

        target = self._host_path_for(file_path)
        try:
            if not target.exists() and not target.is_symlink():
                return DeleteResult(error=f"Error: '{file_path}' not found")
            if target.is_symlink():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            return DeleteResult(path=file_path)
        except Exception as exc:
            return DeleteResult(error=f"Error deleting '{file_path}': {exc}")

    async def adelete(self, file_path: str) -> "DeleteResult":
        """异步删除(宿主文件 I/O → 线程池)。"""
        return await asyncio.to_thread(self.delete, file_path)

    # ---- 生命周期 -------------------------------------------------------

    def destroy(self) -> None:
        """best-effort 删除容器(删线程时调用;失败只告警不抛)。"""
        try:
            _docker("rm", "-f", self._container, timeout=30)
            logger.info("已删除沙箱容器 %s", self._container)
        except DockerSandboxError as exc:
            logger.warning("删除沙箱容器 %s 失败(best-effort): %s", self._container, exc)


def docker_daemon_available() -> bool:
    """Docker daemon 是否可达(启动校验用)。"""
    try:
        _docker("info", timeout=15)
        return True
    except DockerSandboxError:
        return False


def validate_docker_startup_config() -> None:
    """启动校验:daemon 可达 + 镜像名非空(失败拒启)。"""
    if not docker_daemon_available():
        raise RuntimeError(
            "SANDBOX_TYPE=docker 但 Docker daemon 不可达(docker info 失败)。"
            "请确认宿主 Docker 已启动,或改用 SANDBOX_TYPE=local 裸开发。"
        )
    image = os.environ.get("DOCKER_SANDBOX_IMAGE", "python:3.12-slim")
    if not image:
        raise RuntimeError("DOCKER_SANDBOX_IMAGE 不能为空")


async def create_docker_sandbox(
    sandbox_id: str | None = None,
    *,
    repo: dict | None = None,
    owner_username: str | None = None,
) -> DockerSandbox:
    """工厂:创建或复用线程的 Docker 沙箱(async,在事件循环里解析 git 身份)。

    参数:
        sandbox_id: 线程 id(容器名据此生成);None 时报错(本变体必须线程级)。
        repo: 仓库元信息(解析宿主挂载路径)。
        owner_username: 线程 owner(解析 commit 作者 git 身份)。

    已存在且存活 → 复用;已删除 → ``SandboxGoneError``;不存在 → 创建。
    docker CLI 调用走线程池(不阻塞单事件循环);身份解析(await 注册表)
    在事件循环完成。
    """
    if not sandbox_id:
        raise ValueError("docker 沙箱必须指定 sandbox_id(= thread_id)")
    # 身份解析(事件循环内 await 注册表 store;gitconfig 宿主文件读取在内部 to_thread)
    git_name, git_email = await _resolve_git_identity(owner_username)
    backend = DockerSandbox(sandbox_id, repo=repo, owner_username=owner_username)
    try:
        # 宿主侧前置(mkdir + gitconfig 写盘,阻塞 I/O → 线程池)
        await asyncio.to_thread(
            backend._prepare_fs, git_name, git_email
        )
        # docker 命令走线程池
        await asyncio.to_thread(backend._ensure_container)
        # 容器内工具初始化(git 等;复用容器幂等跳过)
        await asyncio.to_thread(backend._setup_container_tools)
    except DockerSandboxError as exc:
        message = str(exc).lower()
        if "no such object" in message or "not found" in message:
            from ..utils.sandbox import SandboxGoneError

            raise SandboxGoneError(
                f"沙箱容器 {backend._container} 已不存在,请重建"
            ) from exc
        raise
    return backend
