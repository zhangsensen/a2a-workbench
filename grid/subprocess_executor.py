"""共享的子进程 agent 执行器基类：输入净化 + 命令转义 + 统一任务流程。

四个 agent（pi/claude/codex/dsh）的 executor 都继承它，只需声明 BIN / ARGS_PREFIX / USE_SHELL。
安全要点（A2A 官方警告：外部 agent 输入不可信，须净化防 prompt injection）：
- sanitize：去控制字符 + 限长（codex/dsh 子类改为超限拒绝，不截断）
- 转义：真 exe 用参数列表（免 shell 注入）；.cmd shim 用 list2cmdline 正确转义

失败语义（SENY-162 Stage 1 #4）：
超时、输入超限和内部异常都必须落到 **可取回的终态**，而且必须是 `FAILED`，
不能像旧实现那样把 `(调用超时 >600s)` 当成 artifact 再标 `COMPLETED` —— 那让
调用方只能靠匹配中文字符串才能区分“成功”和“超时”，任何自动化判定都会把超时
当成功。因此这里定义 ``ExecutorFailure``：``_run`` 抛出它，``execute`` 把文本
写成 artifact（终态原因可取回）并把任务标成 ``TASK_STATE_FAILED``。

执行器占用（同上）：`subprocess.run(shell=True, timeout=...)` 在 Windows 上只
杀掉 `cmd.exe` shim，`node.exe` / 真实 CLI 子孙进程会活下来继续吃 CPU 和
API 配额，并且继承的管道句柄还能让后续 `communicate()` 永久阻塞。DSH 早先已
单独修过，这里把 `taskkill /T /F` 进程树终止上提到基类，让 pi/claude/codex
共享同一保证。
"""
import asyncio
import os
import re
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from a2a.helpers import (
    get_message_text,
    new_task_from_user_message,
    new_text_message,
    new_text_part,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import TaskState

MAX_INPUT = 20000
MAX_OUTPUT = 50000

# 同一份代码要跑在两个运行环境上：Windows（开发/备用）与 WSL Linux（systemd
# 单一 owner，见 deploy-to-wsl.sh）。所有平台差异集中在本文件的三个点：
# shell 使用（仅 Windows 的 .cmd shim 需要）、进程组创建、进程树终止。
IS_WINDOWS = os.name == "nt"

# 输出被裁剪时必须显式说明，否则调用方拿到的是一段看起来完整的答案。
OUTPUT_TRUNCATED_NOTICE = (
    "\n\n[输出已截断: 仅返回前 {kept} 个字符，共 {total} 个字符]"
)


class ExecutorFailure(Exception):
    """A2A 任务失败的终态原因（文本可直接回给调用方）。"""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


class ExecutorTimeout(ExecutorFailure):
    """子进程超时，且进程树已被终止。"""


class SessionNotFound(ExecutorFailure):
    """CLI 明确报告待恢复的原生会话不存在。"""


@dataclass
class _ProcessEntry:
    """一次 execute 调用与其子进程之间共享的取消状态。"""

    process: "subprocess.Popen[bytes] | None" = None
    cancelled: bool = False


class SubprocessAgentExecutor(AgentExecutor):
    BIN = ""                 # 子类声明：可执行文件路径
    ARGS_PREFIX: list[str] = []   # 子类声明：命令前缀参数
    USE_SHELL = True         # .cmd shim 需要 True；真 exe 设 False 更安全
    QUERY_VIA_STDIN = False  # True=query 走 stdin（pi/codex）；False=位置参数（dsh 需要 task 参数）
    TIMEOUT = 600
    KILL_GRACE_SECONDS = 30  # 进程树终止后等待回收的上限
    WORKING_TEXT = "处理中..."

    # 只有同时声明这两个开关的 CLI 才启用会话连续性。默认 None 保证不支持
    # 原生会话的执行器完全沿用原来的调用方式。
    SESSION_ID_FLAG: str | None = None
    RESUME_FLAG: str | None = None

    SESSION_DB_DIR: ClassVar[Path] = Path(__file__).resolve().parent / "data"
    _sessions_db_lock: ClassVar[threading.Lock] = threading.Lock()

    # 同一 context 的原生会话不能被并发 resume。锁表由外层锁保护，实际
    # context 锁在 asyncio.to_thread 的工作线程里持有，不阻塞事件循环。
    _context_locks: ClassVar[dict[str, threading.Lock]] = {}
    _context_locks_lock: ClassVar[threading.Lock] = threading.Lock()

    # 所有子类（尤其是有独立 _run 的 DSH）共享同一张运行表。entry 也由
    # execute 持有，因此 cancel 从表中取走它之后，execute 仍能看到
    # cancelled=True，避免再写 FAILED/COMPLETED 终态。
    _running_processes: ClassVar[dict[str, _ProcessEntry]] = {}
    _running_processes_lock: ClassVar[threading.Lock] = threading.Lock()

    # context 不是安全边界，只是会话路由键：服务仅监听回环，执行手本身拥有
    # 全权限。这里的白名单用于拒绝歧义/失控的存储键，而不是权限隔离。
    _CONTEXT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
    _SESSION_NOT_FOUND_MARKERS = (
        "session not found",
        "no conversation found",
        "thread not found",
        "no such session",
    )

    @classmethod
    def _validated_context(cls, value: Any) -> str | None:
        """校验会话路由键；缺省为 None，显式非法值响亮拒绝。"""
        if value is None:
            return None
        if not isinstance(value, str) or not cls._CONTEXT_RE.fullmatch(value):
            raise ExecutorFailure(
                "(context 无效: 仅允许 1-64 位字母、数字、下划线或连字符)"
            )
        return value

    @classmethod
    def _context_lock(cls, context_id: str) -> threading.Lock:
        with SubprocessAgentExecutor._context_locks_lock:
            return SubprocessAgentExecutor._context_locks.setdefault(
                context_id, threading.Lock()
            )

    @classmethod
    def _session_db_path(cls) -> Path:
        return cls.SESSION_DB_DIR / f"{cls.__name__.lower()}-sessions.db"

    @classmethod
    def _session_native_id(cls, context_id: str) -> str | None:
        """读取 context 对应的 CLI 原生会话 id。"""
        with SubprocessAgentExecutor._sessions_db_lock:
            cls.SESSION_DB_DIR.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(cls._session_db_path()) as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS sessions "
                    "(context TEXT PRIMARY KEY, native_id TEXT, updated REAL)"
                )
                row = db.execute(
                    "SELECT native_id FROM sessions WHERE context = ?",
                    (context_id,),
                ).fetchone()
        return row[0] if row is not None else None

    @classmethod
    def _store_session(cls, context_id: str, native_id: str) -> None:
        """子进程成功后写入（或刷新）context 的原生会话 id。"""
        with SubprocessAgentExecutor._sessions_db_lock:
            cls.SESSION_DB_DIR.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(cls._session_db_path()) as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS sessions "
                    "(context TEXT PRIMARY KEY, native_id TEXT, updated REAL)"
                )
                db.execute(
                    "INSERT INTO sessions(context, native_id, updated) VALUES (?, ?, ?) "
                    "ON CONFLICT(context) DO UPDATE SET "
                    "native_id = excluded.native_id, updated = excluded.updated",
                    (context_id, native_id, time.time()),
                )

    @classmethod
    def _register_process(
        cls,
        task_id: str | None,
        process: "subprocess.Popen[bytes]",
        entry: _ProcessEntry,
    ) -> bool:
        """原子发布已启动进程，并返回此前是否已经收到取消。"""
        # entry 已由 execute 在 spawn 前按 task_id 注册；保留参数以让所有
        # 子类沿用同一发布接口。
        with SubprocessAgentExecutor._running_processes_lock:
            entry.process = process
            return entry.cancelled

    @classmethod
    def _register_entry(
        cls, task_id: str | None, entry: _ProcessEntry
    ) -> None:
        """在 spawn 前发布运行条目，使 cancel 不会漏掉启动窗口。"""
        if task_id is None:
            return
        with SubprocessAgentExecutor._running_processes_lock:
            SubprocessAgentExecutor._running_processes[task_id] = entry

    @classmethod
    def _clear_process(
        cls, process: "subprocess.Popen[bytes]", entry: _ProcessEntry
    ) -> None:
        """进程回收后清空引用，但保留 execute 级运行条目。"""
        with SubprocessAgentExecutor._running_processes_lock:
            if entry.process is process:
                entry.process = None

    @classmethod
    def _take_process(
        cls,
        task_id: str | None,
        *,
        expected: _ProcessEntry | None = None,
        cancelled: bool = False,
    ) -> _ProcessEntry | None:
        """原子取走运行条目；自然结束和 cancel 只会有一方成功。"""
        if task_id is None:
            return None
        with SubprocessAgentExecutor._running_processes_lock:
            entry = SubprocessAgentExecutor._running_processes.get(task_id)
            if entry is None or (expected is not None and entry is not expected):
                return None
            entry = SubprocessAgentExecutor._running_processes.pop(task_id)
            if cancelled:
                entry.cancelled = True
            return entry

    def _sanitize(self, text: str) -> str:
        """去控制字符 + 限长，防异常输入。"""
        if not isinstance(text, str):
            return ""
        cleaned = "".join(ch for ch in text if ch >= " " or ch in "\n\t")
        return cleaned[:MAX_INPUT]

    @classmethod
    def _kill_process_tree(cls, proc: "subprocess.Popen[bytes]") -> None:
        """终止 shim 及其全部子孙进程。

        Windows: taskkill /T /F 按进程树杀。
        POSIX: ``_popen`` 用 ``start_new_session=True`` 起进程，进程组 id 即
        proc.pid，``os.killpg`` 覆盖整棵树 —— 曾经的实现在 Linux 上调
        taskkill.exe（不存在），静默回落成只杀直接子进程，node 子孙存活
        继续吃配额。
        """
        if IS_WINDOWS:
            try:
                subprocess.run(
                    ["taskkill.exe", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                    check=False,
                )
                return
            except Exception:  # noqa: BLE001
                pass
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
                return
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            proc.kill()
        except OSError:
            pass

    @classmethod
    def _reap_after_kill(cls, proc: "subprocess.Popen[bytes]") -> None:
        """回收已被终止的进程；绝不无超时地 communicate()。

        shim 死了但 node 子孙还活着时，继承的管道句柄会让无超时的
        ``communicate()`` 永久挂住 —— 那正是“无限占用执行器”的形态。
        """
        for stream in (proc.stdout, proc.stderr, proc.stdin):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        try:
            proc.wait(timeout=cls.KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass

    @classmethod
    def _clip_output(cls, text: str) -> str:
        """裁剪超长输出，但显式标注，不静默截断。"""
        if len(text) <= MAX_OUTPUT:
            return text
        return text[:MAX_OUTPUT] + OUTPUT_TRUNCATED_NOTICE.format(
            kept=MAX_OUTPUT, total=len(text)
        )

    def _popen(self, q: str, extra_args: list[str] | None = None,
               cwd: str | None = None) -> "subprocess.Popen[bytes]":
        """按子类声明的转义策略启动子进程。

        ``extra_args`` 由执行器层的元数据（如请求指定模型/provider）透传而来，
        追加到 ``ARGS_PREFIX`` 之后、用户查询之前；默认为空，行为不变。
        ``cwd`` 把子进程钉到指定工作目录（如某个 git worktree），并行任务
        各占一个 worktree 时互不踩踏；None 保持服务默认目录。
        """
        prefix = [*self.ARGS_PREFIX, *(extra_args or [])]
        if self.USE_SHELL and IS_WINDOWS:
            if self.QUERY_VIA_STDIN:
                # cmd.exe /c "<string>" 按行解析：字符串里只要有一个换行，
                # 从第一个换行起的内容会被当成第二条命令，query 里的换行会被
                # 整段截断。引号转义（list2cmdline）保护的是 argv 切分，
                # 保护不了外层 cmd.exe 自己的按行解析。
                # 修法：query 走 stdin（codex exec / pi --print 读 stdin）；
                # 命令行只保留固定 BIN/ARGS_PREFIX，不含用户可控多行文本。
                cmd = subprocess.list2cmdline([self.BIN, *prefix])
                return subprocess.Popen(
                    cmd, shell=True, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd,
                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                )
            # dsh 这类必须 task 作为位置参数的：显式加双引号，
            # 否则 list2cmdline 对无空格中文参数不加引号会丢 task
            safe_q = q.replace('"', "'")
            cmd = subprocess.list2cmdline([self.BIN, *prefix, safe_q])
            return subprocess.Popen(
                cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=cwd,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        # 真 exe（Windows）或任意 POSIX 平台：参数列表直接 exec，免 shell 注入。
        # Linux 上 npm shim 是带 shebang 的脚本，无需 shell，USE_SHELL 被忽略。
        # start_new_session 让 POSIX 子进程自成进程组，_kill_process_tree 才能
        # killpg 整树终止。
        popen_kwargs: dict[str, Any] = dict(
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            start_new_session=not IS_WINDOWS,
        )
        if self.QUERY_VIA_STDIN:
            return subprocess.Popen(
                [self.BIN, *prefix], stdin=subprocess.PIPE, **popen_kwargs
            )
        return subprocess.Popen([self.BIN, *prefix, q], **popen_kwargs)

    # cwd 只做输入卫生校验，不是权限边界：四个 CLI 本身全权限、可访问任意
    # 路径；服务仅监听回环。校验目的：拒绝控制字符/相对路径/不存在的目录，
    # 让"钉错工作区"在提交时响亮失败，而不是默默跑在服务目录里。
    MAX_CWD_LEN = 500

    @classmethod
    def _validated_cwd(cls, value: Any) -> str | None:
        """校验元数据里的 cwd：缺省返回 None；给了但非法必须响亮拒绝。

        一个"在 worktree X 写代码"的任务如果静默落在默认目录执行，等于在
        错误的地方动手 —— 比拒绝危险得多，所以非法值抛 ``ExecutorFailure``。
        """
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ExecutorFailure("(cwd 无效: 必须是非空字符串)")
        if any(ch < " " for ch in value):
            raise ExecutorFailure("(cwd 无效: 含控制字符/以-开头/超长)")
        v = value.strip()
        if len(v) > cls.MAX_CWD_LEN or v.startswith("-"):
            raise ExecutorFailure("(cwd 无效: 含控制字符/以-开头/超长)")
        path = Path(v)
        if not path.is_absolute():
            raise ExecutorFailure(f"(cwd 无效: 必须是绝对路径: {v})")
        if not path.is_dir():
            raise ExecutorFailure(f"(cwd 无效: 目录不存在: {v})")
        return str(path)

    def _run(self, query: str, extra_args: list[str] | None = None,
             cwd_request: Any = None, task_id: str | None = None,
             entry: _ProcessEntry | None = None) -> str:
        """同步执行子进程，返回文本结果（在 asyncio.to_thread 里跑）。

        失败一律抛 ``ExecutorFailure``，由 ``execute`` 落成 FAILED 终态。
        """
        q = self._sanitize(query)
        cwd = self._validated_cwd(cwd_request)
        # stdin 传参在两个平台统一生效（Windows shell 分支与 POSIX exec 分支
        # 都为 QUERY_VIA_STDIN 打开了 stdin=PIPE）。
        stdin_payload = q.encode("utf-8") if self.QUERY_VIA_STDIN else None
        run_entry = entry if entry is not None else _ProcessEntry()
        if run_entry.cancelled:
            raise ExecutorFailure("(任务已取消，未启动子进程)")
        try:
            proc = self._popen(q, extra_args, cwd)
        except Exception as e:  # noqa: BLE001
            raise ExecutorFailure(f"(调用失败: {type(e).__name__}: {e})") from e

        if self._register_process(task_id, proc, run_entry):
            try:
                self._kill_process_tree(proc)
                self._reap_after_kill(proc)
            finally:
                self._clear_process(proc, run_entry)
            raise ExecutorFailure("(任务已取消，子进程已终止)")

        try:
            try:
                stdout, stderr = proc.communicate(
                    input=stdin_payload, timeout=self.TIMEOUT
                )
            except subprocess.TimeoutExpired:
                if not run_entry.cancelled:
                    self._kill_process_tree(proc)
                    self._reap_after_kill(proc)
                raise ExecutorTimeout(f"(调用超时 >{self.TIMEOUT}s)") from None
            except Exception as e:  # noqa: BLE001
                if not run_entry.cancelled:
                    self._kill_process_tree(proc)
                    self._reap_after_kill(proc)
                raise ExecutorFailure(f"(调用失败: {type(e).__name__}: {e})") from e
        finally:
            self._clear_process(proc, run_entry)

        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        out = self._clip_output(stdout_text)
        err = stderr_text
        # 非零退出码是失败，即使 stdout 有内容——CLI 报错时常带部分输出，
        # 把它当成功答案返回会让调用方拿着半截结果继续走（旧行为，真 bug）。
        if proc.returncode:
            failure_type = ExecutorFailure
            combined_output = f"{stdout_text}\n{stderr_text}".lower()
            is_resume = bool(
                self.RESUME_FLAG
                and extra_args
                and self.RESUME_FLAG in extra_args
            )
            if is_resume and any(
                marker in combined_output
                for marker in self._SESSION_NOT_FOUND_MARKERS
            ):
                failure_type = SessionNotFound
            raise failure_type(
                f"(退出码 {proc.returncode}) stderr: {err[:1000]}\n"
                f"stdout: {out[:2000]}"
            )
        if out:
            return out
        return f"(无输出) stderr: {err[:500]}"

    def _run_with_session(
        self,
        query: str,
        extra_args: list[str],
        cwd_request: Any,
        context_id: str | None,
        context_reset: bool,
        task_id: str | None,
        entry: _ProcessEntry,
    ) -> str:
        """在同步工作线程中完成会话选路、串行执行与成功后持久化。"""
        if context_id is None or not (self.SESSION_ID_FLAG and self.RESUME_FLAG):
            return self._run(
                query, extra_args, cwd_request, task_id, entry
            )

        with self._context_lock(context_id):
            native_id = (
                None if context_reset else self._session_native_id(context_id)
            )
            is_resume = native_id is not None
            if native_id is None:
                native_id = str(uuid.uuid4())
                session_args = [self.SESSION_ID_FLAG, native_id]
            else:
                session_args = [self.RESUME_FLAG, native_id]

            try:
                result = self._run(
                    query,
                    [*extra_args, *session_args],
                    cwd_request,
                    task_id,
                    entry,
                )
            except SessionNotFound:
                # 只有 CLI 明确报告会话不存在时才 fresh retry。超时、普通
                # 非零退出、权限或网络失败均直接上抛，避免重复副作用。
                if not is_resume or entry.cancelled:
                    raise
                native_id = str(uuid.uuid4())
                result = self._run(
                    query,
                    [*extra_args, self.SESSION_ID_FLAG, native_id],
                    cwd_request,
                    task_id,
                    entry,
                )

            self._store_session(context_id, native_id)
            return result

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if context.current_task:
            task = context.current_task
        else:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)

        updater = TaskUpdater(event_queue=event_queue, task_id=task.id, context_id=task.context_id)
        entry = _ProcessEntry()
        self._register_entry(task.id, entry)
        try:
            await updater.update_status(
                state=TaskState.TASK_STATE_WORKING,
                message=new_text_message(self.WORKING_TEXT),
            )
            query = get_message_text(context.message)
            metadata = context.metadata if isinstance(context.metadata, dict) else {}
            # 执行器层透传：请求元数据（如 model/provider）→ 追加到命令参数；
            # cwd 原样传入 _run，由 _validated_cwd 决定接受或响亮拒绝。
            extra_args = self._executor_args_from_metadata(metadata)
            cwd_request = metadata.get("cwd")
            context_id = self._validated_context(metadata.get("context"))
            context_reset = metadata.get("contextReset") == "1"
            result = await asyncio.to_thread(
                self._run_with_session,
                query,
                extra_args,
                cwd_request,
                context_id,
                context_reset,
                task.id,
                entry,
            )
        except ExecutorFailure as failure:
            if self._take_process(task.id, expected=entry) is None:
                return
            # 终态可取回：原因既写成 artifact，也写进 status message，
            # 状态是诚实的 FAILED。
            await updater.add_artifact(parts=[new_text_part(text=failure.text)])
            await updater.update_status(
                state=TaskState.TASK_STATE_FAILED,
                message=new_text_message(failure.text),
            )
            return
        except Exception as exc:  # noqa: BLE001
            if self._take_process(task.id, expected=entry) is None:
                return
            # 任何未预期异常也必须落终态：否则任务永远停在 WORKING，
            # 调用方既拿不到结果也不知道该不该重试（执行器被无限占用）。
            text = f"(内部错误: {type(exc).__name__}: {exc})"
            await updater.add_artifact(parts=[new_text_part(text=text)])
            await updater.update_status(
                state=TaskState.TASK_STATE_FAILED,
                message=new_text_message(text),
            )
            return

        if self._take_process(task.id, expected=entry) is None:
            return
        await updater.add_artifact(parts=[new_text_part(text=result)])
        await updater.update_status(
            state=TaskState.TASK_STATE_COMPLETED,
            message=new_text_message("完成"),
        )

    # 子类可声明各自的换模型 CLI 参数。None 表示该 CLI 不支持该维度，
    # 对应的元数据会被静默忽略（不会拼进命令行）。
    MODEL_FLAG: str | None = None         # 如 "--model"；claude/codex 均支持
    PROVIDER_FLAG: str | None = None      # 如 "--provider"；目前仅 pi 支持

    # 模型/provider 值只允许这些字符：字母/数字/下划线/点/斜杠/冒号/空格/
    # 括号/连字符(末尾)+中文。明确禁掉 shell 元字符(& | ; < > ` ' " $ 换行等)、
    # 以 "-" 开头的值、内嵌 "--" 的值 —— 防止 A2A 外部输入注入任意 CLI 选项。
    _SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9_.\/: ()\u4e00-\u9fff-]{1,120}$")
    _LEADING_DASH_RE = re.compile(r"^-")

    @classmethod
    def _safe_meta_value(cls, value: Any) -> str | None:
        """把元数据里的 model/provider 值洗净为可安全进命令行参数串。"""
        if not isinstance(value, str):
            return None
        v = value.strip()
        if not v:
            return None
        if len(v.encode("utf-8")) > 128:
            return None
        if cls._LEADING_DASH_RE.match(v):
            return None
        if "--" in v:  # 内嵌双横杠会被当成 CLI 开关，拒绝（合法模型名不含 --）
            return None
        if not cls._SAFE_VALUE_RE.fullmatch(v):
            return None
        return v

    def _executor_args_from_metadata(self, metadata: Any) -> list[str]:
        """把请求元数据翻译成追加到命令行的参数。

        通用实现：基于子类声明的 ``MODEL_FLAG`` / ``PROVIDER_FLAG``，把
        ``metadata["model"]`` / ``metadata["provider"]`` 洗净后拼成对应开关。
        未声明该维度的 agent 自动忽略对应元数据（不拼进命令行，行为不变）。
        元数据来自 A2A ``SendMessageRequest.metadata``，不可信，一律白名单校验。
        """
        if not isinstance(metadata, dict):
            return []
        args: list[str] = []
        if self.MODEL_FLAG and (model := self._safe_meta_value(metadata.get("model"))):
            args += [self.MODEL_FLAG, model]
        if self.PROVIDER_FLAG and (provider := self._safe_meta_value(metadata.get("provider"))):
            args += [self.PROVIDER_FLAG, provider]
        return args

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        entry = self._take_process(context.task_id, cancelled=True)
        if entry is None:
            # execute 已取得终态写入权，或任务从未在本进程运行；不双写终态。
            return

        process = entry.process
        if process is not None:
            await asyncio.to_thread(self._kill_process_tree, process)
            await asyncio.to_thread(self._reap_after_kill, process)

        updater = TaskUpdater(
            event_queue=event_queue,
            task_id=context.task_id,
            context_id=context.context_id,
        )
        await updater.update_status(
            state=TaskState.TASK_STATE_CANCELED,
            message=new_text_message("已取消"),
        )
