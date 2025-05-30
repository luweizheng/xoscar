# Copyright 2022-2023 XProbe Inc.
# derived from copyright 1999-2021 Alibaba Group Holding Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures as futures
import configparser
import contextlib
import itertools
import logging.config
import multiprocessing
import os
import random
import signal
import sys
import threading
import uuid
from dataclasses import dataclass
from multiprocessing import util
from types import TracebackType
from typing import List, Optional

from ..._utils import reset_id_random_seed
from ...utils import dataslots, ensure_coverage
from ..config import ActorPoolConfig
from ..message import (
    ControlMessage,
    ControlMessageType,
    CreateActorMessage,
    new_message_id,
)
from ..pool import MainActorPoolBase, SubActorPoolBase, _register_message_handler

_is_windows: bool = sys.platform.startswith("win")

if sys.version_info[:2] == (3, 9):
    # fix for Python 3.9, see https://bugs.python.org/issue43517
    if sys.platform == "win32":
        from multiprocessing import popen_spawn_win32 as popen_spawn

        popen_forkserver = popen_fork = synchronize = None
    else:
        from multiprocessing import popen_fork, popen_forkserver
        from multiprocessing import popen_spawn_posix as popen_spawn
        from multiprocessing import synchronize
    _ = popen_spawn, popen_forkserver, popen_fork, synchronize
    del _
elif sys.version_info[:2] == (3, 6):  # pragma: no cover
    from multiprocessing.process import BaseProcess

    # define kill method for multiprocessing
    def _mp_kill(self):
        if not _is_windows:
            try:
                os.kill(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                if self.wait(timeout=0.1) is None:
                    raise
        else:
            self.terminate()

    BaseProcess.kill = _mp_kill

logger = logging.getLogger(__name__)
_init_main_suspended_local = threading.local()


def _terminate_children():
    for c in multiprocessing.active_children():
        try:
            c.terminate()
        except Exception:
            pass


if util:
    # Import multiprocessing.util to register _exit_function at exit.
    atexit.register(_terminate_children)


def _patch_spawn_get_preparation_data():
    try:
        from multiprocessing import spawn as mp_spawn

        _raw_get_preparation_data = mp_spawn.get_preparation_data

        def _patched_get_preparation_data(*args, **kw):
            ret = _raw_get_preparation_data(*args, **kw)
            if getattr(_init_main_suspended_local, "value", False):
                # make sure user module is not imported when start cluster
                ret.pop("init_main_from_name", None)
                ret.pop("init_main_from_path", None)
            return ret

        _patched_get_preparation_data._indigen_patched = True
        if not getattr(mp_spawn.get_preparation_data, "_indigen_patched", False):
            mp_spawn.get_preparation_data = _patched_get_preparation_data
    except (ImportError, AttributeError):  # pragma: no cover
        pass


@contextlib.contextmanager
def _suspend_init_main():
    try:
        _init_main_suspended_local.value = True
        yield
    finally:
        _init_main_suspended_local.value = False


@dataslots
@dataclass
class SubpoolStatus:
    # for status, 0 is succeeded, 1 is failed
    status: int | None = None
    external_addresses: List[str] | None = None
    error: BaseException | None = None
    traceback: TracebackType | None = None


_PRE_SET_ENV_LOCK = asyncio.Lock()


@contextlib.asynccontextmanager
async def _pre_set_env_in_main(env: dict[str, str]):
    # Normally, `env` is set in sub pool,
    # but something may have happened during initialization,
    # e.g. CUDA_VISIBLE_DEVICES is too late to set when some actions may have polluted cuda
    # we have to set environ before new process started
    # enable this only when XOSCAR_PRE_SET_ENV=1
    enable_pre_set_env = bool(int(os.getenv("XOSCAR_PRE_SET_ENV", 0)))
    if not enable_pre_set_env or not env:
        yield
        return

    global_environ = os.environ.copy()
    async with _PRE_SET_ENV_LOCK:
        try:
            logger.debug("Updating environment variables in main: %s", env)
            os.environ.update(env)
            yield
        finally:
            os.environ = global_environ  # type: ignore


@_register_message_handler
class MainActorPool(MainActorPoolBase):
    @classmethod
    def get_external_addresses(
        cls,
        address: str,
        n_process: int | None = None,
        ports: list[int] | None = None,
        schemes: list[Optional[str]] | None = None,
    ):
        """Get external address for every process"""
        assert n_process is not None
        if ":" in address:
            host, port_str = address.rsplit(":", 1)
            port = int(port_str)
            if ports:
                if len(ports) != n_process:
                    raise ValueError(
                        f"`ports` specified, but its count "
                        f"is not equal to `n_process`, "
                        f"number of ports: {len(ports)}, "
                        f"n_process: {n_process}"
                    )
                sub_ports = ports
            else:
                sub_ports = [0] * n_process
        else:
            host = address
            if ports and len(ports) != n_process + 1:
                # ports specified, the first of which should be main port
                raise ValueError(
                    f"`ports` specified, but its count "
                    f"is not equal to `n_process` + 1, "
                    f"number of ports: {len(ports)}, "
                    f"n_process + 1: {n_process + 1}"
                )
            elif not ports:
                ports = [0] * (n_process + 1)
            port = ports[0]
            sub_ports = ports[1:]
        if not schemes:
            prefix_iter = itertools.repeat("")
        else:
            prefix_iter = [f"{scheme}://" if scheme else "" for scheme in schemes]  # type: ignore
        return [
            f"{prefix}{host}:{port}"
            for port, prefix in zip([port] + sub_ports, prefix_iter)
        ]

    @classmethod
    def gen_internal_address(
        cls, process_index: int, external_address: str | None = None
    ) -> str | None:
        if hasattr(asyncio, "start_unix_server"):
            return f"unixsocket:///{process_index}"
        else:
            return external_address

    @classmethod
    async def start_sub_pool(
        cls,
        actor_pool_config: ActorPoolConfig,
        process_index: int,
        start_method: str | None = None,
    ):
        def start_pool_in_process():
            ctx = multiprocessing.get_context(method=start_method)
            status_queue = ctx.Queue()
            main_pool_pid = os.getpid()

            with _suspend_init_main():
                process = ctx.Process(
                    target=cls._start_sub_pool,
                    args=(
                        actor_pool_config,
                        process_index,
                        status_queue,
                        main_pool_pid,
                    ),
                    name=f"IndigenActorPool{process_index}",
                )
                process.start()

            # wait for sub actor pool to finish starting
            process_status = status_queue.get()
            return process, process_status

        _patch_spawn_get_preparation_data()
        loop = asyncio.get_running_loop()
        async with _pre_set_env_in_main(
            actor_pool_config.get_pool_config(process_index)["env"]
        ):
            with futures.ThreadPoolExecutor(1) as executor:
                create_pool_task = loop.run_in_executor(executor, start_pool_in_process)
                return await create_pool_task

    @classmethod
    async def wait_sub_pools_ready(cls, create_pool_tasks: List[asyncio.Task]):
        processes: list[multiprocessing.Process] = []
        ext_addresses = []
        error = None
        for task in create_pool_tasks:
            process, status = await task
            processes.append(process)
            if status.status == 1:
                # start sub pool failed
                error = status.error.with_traceback(status.traceback)
            else:
                ext_addresses.append(status.external_addresses)
        if error:
            for p in processes:
                # error happens, kill all subprocesses
                p.kill()
            raise error
        return processes, ext_addresses

    @classmethod
    def _start_sub_pool(
        cls,
        actor_config: ActorPoolConfig,
        process_index: int,
        status_queue: multiprocessing.Queue,
        main_pool_pid: int,
    ):
        ensure_coverage()

        # make sure enough randomness for every sub pool
        random.seed(uuid.uuid1().bytes)
        reset_id_random_seed()

        conf = actor_config.get_pool_config(process_index)
        suspend_sigint = conf["suspend_sigint"]
        if suspend_sigint:
            signal.signal(signal.SIGINT, lambda *_: None)

        logging_conf = conf["logging_conf"] or {}
        if isinstance(logging_conf, configparser.RawConfigParser):
            logging.config.fileConfig(logging_conf)
        elif logging_conf.get("dict"):
            logging.config.dictConfig(logging_conf["dict"])
        elif logging_conf.get("file"):
            logging.config.fileConfig(logging_conf["file"])
        elif logging_conf.get("level"):
            logging.getLogger("__main__").setLevel(logging_conf["level"])
            logging.getLogger("xoscar").setLevel(logging_conf["level"])
            if logging_conf.get("format"):
                logging.basicConfig(format=logging_conf["format"])

        use_uvloop = conf["use_uvloop"]
        if use_uvloop:
            import uvloop

            asyncio.set_event_loop(uvloop.new_event_loop())
        else:
            asyncio.set_event_loop(asyncio.new_event_loop())

        coro = cls._create_sub_pool(
            actor_config, process_index, status_queue, main_pool_pid
        )
        asyncio.run(coro)

    @classmethod
    async def _create_sub_pool(
        cls,
        actor_config: ActorPoolConfig,
        process_index: int,
        status_queue: multiprocessing.Queue,
        main_pool_pid: int,
    ):
        process_status = None
        try:
            cur_pool_config = actor_config.get_pool_config(process_index)
            env = cur_pool_config["env"]
            if env:
                os.environ.update(env)
            pool = await SubActorPool.create(
                {
                    "actor_pool_config": actor_config,
                    "process_index": process_index,
                    "main_pool_pid": main_pool_pid,
                }
            )
            external_addresses = cur_pool_config["external_address"]
            process_status = SubpoolStatus(
                status=0, external_addresses=external_addresses
            )
            await pool.start()
        except:  # noqa: E722  # nosec  # pylint: disable=bare-except
            _, error, tb = sys.exc_info()
            process_status = SubpoolStatus(status=1, error=error, traceback=tb)
            raise
        finally:
            status_queue.put(process_status)
            status_queue.cancel_join_thread()
        await pool.join()

    async def append_sub_pool(
        self,
        label: str | None = None,
        internal_address: str | None = None,
        external_address: str | None = None,
        env: dict | None = None,
        modules: list[str] | None = None,
        suspend_sigint: bool | None = None,
        use_uvloop: bool | None = None,
        logging_conf: dict | None = None,
        start_method: str | None = None,
        kwargs: dict | None = None,
    ):
        # external_address has port 0, subprocess will bind random port.
        external_address = (
            external_address
            or MainActorPool.get_external_addresses(self.external_address, n_process=1)[
                -1
            ]
        )

        # use last process index's logging_conf and use_uv_loop config if not provide
        actor_pool_config = self._config.as_dict()
        last_process_index = self._config.get_process_indexes()[-1]
        last_logging_conf = actor_pool_config["pools"][last_process_index][
            "logging_conf"
        ]
        last_use_uv_loop = actor_pool_config["pools"][last_process_index]["use_uvloop"]
        _logging_conf = logging_conf or last_logging_conf
        _use_uv_loop = use_uvloop if use_uvloop is not None else last_use_uv_loop

        process_index = next(MainActorPool.process_index_gen(external_address))
        internal_address = internal_address or MainActorPool.gen_internal_address(
            process_index, external_address
        )

        self._config.add_pool_conf(
            process_index,
            label,
            internal_address,
            external_address,
            env,
            modules,
            suspend_sigint,
            _use_uv_loop,
            _logging_conf,
            kwargs,
        )

        def start_pool_in_process():
            ctx = multiprocessing.get_context(method=start_method)
            status_queue = ctx.Queue()
            main_pool_pid = os.getpid()

            with _suspend_init_main():
                process = ctx.Process(
                    target=self._start_sub_pool,
                    args=(self._config, process_index, status_queue, main_pool_pid),
                    name=f"IndigenActorPool{process_index}",
                )
                process.start()

            # wait for sub actor pool to finish starting
            process_status = status_queue.get()
            return process, process_status

        loop = asyncio.get_running_loop()
        async with _pre_set_env_in_main(env):  # type: ignore
            with futures.ThreadPoolExecutor(1) as executor:
                create_pool_task = loop.run_in_executor(executor, start_pool_in_process)
                process, process_status = await create_pool_task

        self._config.reset_pool_external_address(
            process_index, process_status.external_addresses[0]
        )
        self.attach_sub_process(process_status.external_addresses[0], process)

        control_message = ControlMessage(
            message_id=new_message_id(),
            address=self.external_address,
            control_message_type=ControlMessageType.sync_config,
            content=self._config,
        )
        await self.handle_control_command(control_message)
        # The actual port will return in process_status.
        return process_status.external_addresses[0]

    async def remove_sub_pool(
        self, external_address: str, timeout: float | None = None, force: bool = False
    ):
        process = self.sub_processes[external_address]
        process_index = self._config.get_process_index(external_address)
        await self.stop_sub_pool(external_address, process, timeout, force)
        del self.sub_processes[external_address]
        self._config.remove_pool_config(process_index)

        control_message = ControlMessage(
            message_id=new_message_id(),
            address=self.external_address,
            control_message_type=ControlMessageType.sync_config,
            content=self._config,
        )
        await self.handle_control_command(control_message)

    async def kill_sub_pool(
        self, process: multiprocessing.Process, force: bool = False
    ):
        if not force:  # pragma: no cover
            # must shutdown gracefully, or subprocess created by model will not exit
            if not _is_windows:
                try:
                    os.kill(process.pid, signal.SIGINT)  # type: ignore
                except OSError:  # pragma: no cover
                    pass
            process.terminate()  # SIGTERM
            wait_pool = futures.ThreadPoolExecutor(1)
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(wait_pool, process.join, 3)
            finally:
                wait_pool.shutdown(False)
        process.kill()  # SIGKILL
        await asyncio.to_thread(process.join, 5)

    async def is_sub_pool_alive(self, process: multiprocessing.Process):
        try:
            return await asyncio.to_thread(process.is_alive)
        except RuntimeError as ex:  # pragma: no cover
            if "cannot schedule new futures" not in str(ex):
                # when atexit is triggered, the default pool might be shutdown
                # and to_thread will fail
                raise
            return process.is_alive()

    async def recover_sub_pool(self, address: str):
        process_index = self._config.get_process_index(address)
        # process dead, restart it
        # remember always use spawn to recover sub pool
        task = asyncio.create_task(
            self.start_sub_pool(self._config, process_index, "spawn")
        )
        self.sub_processes[address] = (await self.wait_sub_pools_ready([task]))[0][0]

        if self._auto_recover == "actor":
            # need to recover all created actors
            for _, message in self._allocated_actors[address].values():
                create_actor_message: CreateActorMessage = message  # type: ignore
                await self.call(address, create_actor_message)

    async def start(self):
        await super().start()
        await self.start_monitor()


@_register_message_handler
class SubActorPool(SubActorPoolBase):
    pass
