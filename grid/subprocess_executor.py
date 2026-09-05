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
import subprocess
from typing import Any

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


class SubprocessAgentExecutor(AgentExecutor):
    BIN = ""                 # 子类声明：可执行文件路径
    ARGS_PREFIX: list[str] = []   # 子类声明：命令前缀参数
    USE_SHELL = True         # .cmd shim 需要 True；真 exe 设 False 更安全
    QUERY_VIA_STDIN = False  # True=query 走 stdin（pi/codex）；False=位置参数（dsh 需要 task 参数）
    TIMEOUT = 600
    KILL_GRACE_SECONDS = 30  # 进程树终止后等待回收的上限
    WORKING_TEXT = "处理中..."

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

    def _popen(self, q: str, extra_args: list[str] | None = None) -> "subprocess.Popen[bytes]":
        """按子类声明的转义策略启动子进程。

        ``extra_args`` 由执行器层的元数据（如请求指定模型/provider）透传而来，
        追加到 ``ARGS_PREFIX`` 之后、用户查询之前；默认为空，行为不变。
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
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                )
            # dsh 这类必须 task 作为位置参数的：显式加双引号，
            # 否则 list2cmdline 对无空格中文参数不加引号会丢 task
            safe_q = q.replace('"', "'")
            cmd = subprocess.list2cmdline([self.BIN, *prefix, safe_q])
            return subprocess.Popen(
                cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        # 真 exe（Windows）或任意 POSIX 平台：参数列表直接 exec，免 shell 注入。
        # Linux 上 npm shim 是带 shebang 的脚本，无需 shell，USE_SHELL 被忽略。
        # start_new_session 让 POSIX 子进程自成进程组，_kill_process_tree 才能
        # killpg 整树终止。
        popen_kwargs: dict[str, Any] = dict(
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            start_new_session=not IS_WINDOWS,
        )
        if self.QUERY_VIA_STDIN:
            return subprocess.Popen(
                [self.BIN, *prefix], stdin=subprocess.PIPE, **popen_kwargs
            )
        return subprocess.Popen([self.BIN, *prefix, q], **popen_kwargs)

    def _run(self, query: str, extra_args: list[str] | None = None) -> str:
        """同步执行子进程，返回文本结果（在 asyncio.to_thread 里跑）。

        失败一律抛 ``ExecutorFailure``，由 ``execute`` 落成 FAILED 终态。
        """
        q = self._sanitize(query)
        # stdin 传参在两个平台统一生效（Windows shell 分支与 POSIX exec 分支
        # 都为 QUERY_VIA_STDIN 打开了 stdin=PIPE）。
        stdin_payload = q.encode("utf-8") if self.QUERY_VIA_STDIN else None
        try:
            proc = self._popen(q, extra_args)
        except Exception as e:  # noqa: BLE001
            raise ExecutorFailure(f"(调用失败: {type(e).__name__}: {e})") from e

        try:
            stdout, stderr = proc.communicate(
                input=stdin_payload, timeout=self.TIMEOUT
            )
        except subprocess.TimeoutExpired:
            self._kill_process_tree(proc)
            self._reap_after_kill(proc)
            raise ExecutorTimeout(f"(调用超时 >{self.TIMEOUT}s)") from None
        except Exception as e:  # noqa: BLE001
            self._kill_process_tree(proc)
            self._reap_after_kill(proc)
            raise ExecutorFailure(f"(调用失败: {type(e).__name__}: {e})") from e

        out = self._clip_output(stdout.decode("utf-8", errors="replace").strip())
        if out:
            return out
        err = stderr.decode("utf-8", errors="replace").strip()
        return f"(无输出) stderr: {err[:500]}"

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if context.current_task:
            task = context.current_task
        else:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)

        updater = TaskUpdater(event_queue=event_queue, task_id=task.id, context_id=task.context_id)
        await updater.update_status(
            state=TaskState.TASK_STATE_WORKING,
            message=new_text_message(self.WORKING_TEXT),
        )

        query = get_message_text(context.message)
        # 执行器层透传：请求元数据（如 model/provider）→ 追加到命令参数。
        extra_args = self._executor_args_from_metadata(context.metadata)
        try:
            result = await asyncio.to_thread(self._run, query, extra_args)
        except ExecutorFailure as failure:
            # 终态可取回：原因既写成 artifact，也写进 status message，
            # 状态是诚实的 FAILED。
            await updater.add_artifact(parts=[new_text_part(text=failure.text)])
            await updater.update_status(
                state=TaskState.TASK_STATE_FAILED,
                message=new_text_message(failure.text),
            )
            return
        except Exception as exc:  # noqa: BLE001
            # 任何未预期异常也必须落终态：否则任务永远停在 WORKING，
            # 调用方既拿不到结果也不知道该不该重试（执行器被无限占用）。
            text = f"(内部错误: {type(exc).__name__}: {exc})"
            await updater.add_artifact(parts=[new_text_part(text=text)])
            await updater.update_status(
                state=TaskState.TASK_STATE_FAILED,
                message=new_text_message(text),
            )
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
        raise NotImplementedError("cancel not supported")
