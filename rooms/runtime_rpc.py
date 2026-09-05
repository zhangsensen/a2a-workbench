"""Persistent, private stdio transports for the installed official CLIs."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal


class RuntimeFailure(RuntimeError):
    pass


class Process:
    def __init__(self, args, cwd, env=None):
        self.args, self.cwd, self.env = args, cwd, env
        self.process = None
        self.reader = None
        self.events = asyncio.Queue()
        self.pending = {}
        self.serial = 0
        self.generation = 0

    @property
    def alive(self):
        return self.process is not None and self.process.returncode is None

    @property
    def pid(self):
        return self.process.pid if self.alive else None

    async def start(self):
        if self.alive:
            return
        self.events = asyncio.Queue()
        self.process = await asyncio.create_subprocess_exec(
            *self.args, cwd=self.cwd, env=self.env, start_new_session=True,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            # Protocols may log credentials or full prompts on stderr. Never persist it.
            stderr=asyncio.subprocess.DEVNULL, limit=16 * 1024 * 1024,
        )
        self.generation += 1
        self.reader = asyncio.create_task(self._read())

    async def write(self, data):
        if not self.alive:
            raise RuntimeFailure("Model process is not running")
        self.process.stdin.write((json.dumps(data) + "\n").encode())
        await self.process.stdin.drain()

    async def _read(self):
        try:
            while line := await self.process.stdout.readline():
                try:
                    data = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if "id" in data and "method" in data:
                    await self._server_request(data)
                elif data.get("id") in self.pending:
                    future = self.pending[data["id"]]
                    if not future.done():
                        if "error" in data:
                            # Do not expose arbitrary provider details/stack/arguments.
                            err = data["error"]
                            future.set_exception(RuntimeFailure(
                                f"Runtime RPC failed (code {err.get('code', 'unknown')})"
                            ))
                        else:
                            future.set_result(data.get("result"))
                else:
                    await self.events.put(data)
        finally:
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(RuntimeFailure("Model process disconnected"))
            await self.events.put({"_disconnected": True})

    async def _server_request(self, data):
        # These are the documented runtime-preference requests, not tool approvals.
        if data["method"] == "session/requestRuntimePreferences":
            response = {"nativeSearchEnhancementsEnabled": False,
                        "memoryEnabled": False, "askUserQuestionAutoResolutionEnabled": False,
                        "modelContextBudgetStrategy": "preflight-v1"}
            await self.write({"id": data["id"], "result": response})
        else:
            await self.write({"id": data["id"], "error": {
                "code": -32601, "message": "Roundtable discusses supplied context; interactive tools are unavailable."
            }})

    async def request(self, method, params, timeout=120):
        self.serial += 1
        key = self.serial
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = future
        try:
            await self.write({"id": key, "method": method, "params": params})
            try:
                return await asyncio.wait_for(future, timeout)
            except RuntimeFailure as exc:
                raise RuntimeFailure(f'{method}: {exc}') from None
        finally:
            self.pending.pop(key, None)

    async def event(self):
        event = await self.events.get()
        if event.get("_disconnected"):
            raise RuntimeFailure("Model process disconnected")
        return event

    def drain(self):
        while not self.events.empty():
            self.events.get_nowait()

    async def close(self):
        if self.alive:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.process.wait(), 8)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                await self.process.wait()
        if self.reader:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader
