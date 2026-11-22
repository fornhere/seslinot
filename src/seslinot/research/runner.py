"""İzole alt süreç (asyncio + opsiyonel systemd-run) çalıştırıcısı."""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path

from seslinot.platform.os_compat import (
    current_platform,
    popen_isolated_kwargs,
    terminate_process_tree_async,
)
from seslinot.research.adapters.base import WorkerAdapter, WorkerRequest, WorkerResult

_UNIT_SAFE_RE = re.compile(r"[^A-Za-z0-9:_.-]+")


def sanitize_unit_name(name: str) -> str:
    """Systemd transient unit adını geçerli karakter setine indirger."""
    cleaned = _UNIT_SAFE_RE.sub("-", name.strip()).strip("-")
    if not cleaned:
        cleaned = "seslinot-worker"
    return cleaned[:120]


async def cancel_unit(unit_name: str) -> bool:
    """Çalışan bir user systemd transient unit'ini durdurur (yalnız Linux)."""
    if current_platform() != "linux" or not unit_name or shutil.which("systemctl") is None:
        return False
    safe_unit = sanitize_unit_name(unit_name)
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl",
            "--user",
            "stop",
            safe_unit,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        rc = await asyncio.wait_for(proc.wait(), timeout=10.0)
        return rc == 0
    except (OSError, TimeoutError):
        return False


async def _drain_stream(stream: asyncio.StreamReader | None, target_path: Path) -> None:
    if stream is None:
        return
    with target_path.open("wb") as fh:
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                break
            fh.write(chunk)
            fh.flush()


async def _feed_stdin(stream: asyncio.StreamWriter | None, payload: bytes | None) -> None:
    if stream is None:
        return
    try:
        if payload is not None:
            stream.write(payload)
            await stream.drain()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        try:
            stream.close()
            await stream.wait_closed()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


async def _terminate_process_group(proc: asyncio.subprocess.Process) -> None:
    await terminate_process_tree_async(proc)


class ProcessRunner:
    """Worker CLI komutlarını izole alt süreç veya systemd-run içinde yürütür."""

    def __init__(self, use_systemd_run: bool = True) -> None:
        self.use_systemd_run = use_systemd_run

    def build_wrapped_command(
        self,
        argv: list[str],
        *,
        unit_name: str | None,
        timeout_secs: int,
    ) -> tuple[list[str], str | None]:
        """Linux'ta uygun olduğunda komutu systemd-run --user kaynak kısıtlamalarıyla sarar."""
        if (
            current_platform() == "linux"
            and self.use_systemd_run
            and unit_name
            and shutil.which("systemd-run") is not None
        ):
            safe_unit = sanitize_unit_name(unit_name)
            wrapped = [
                "systemd-run",
                "--user",
                "--collect",
                "--wait",
                f"--unit={safe_unit}",
                "-p",
                "Nice=15",
                "-p",
                "IOSchedulingClass=idle",
                "-p",
                f"RuntimeMaxSec={max(1, int(timeout_secs))}",
                "--",
                *argv,
            ]
            return wrapped, safe_unit
        return list(argv), None

    async def run(self, adapter: WorkerAdapter, req: WorkerRequest) -> WorkerResult:
        """Adaptör komutunu çalıştırır, stdout/stderr/exit.code kaydeder ve sonucu ayrıştırır."""
        req.work_dir.mkdir(parents=True, exist_ok=True)
        # Direct API gibi süreç-dışı asenkron yürütme desteği
        direct_exec = getattr(adapter, "execute_direct", None)
        if callable(direct_exec):
            return await direct_exec(req)

        argv, stdin_bytes, output_file_path = adapter.build_command(req)

        raw_stream_path = req.work_dir / "raw_stream.jsonl"
        stderr_path = req.work_dir / "worker.log"
        exit_code_path = req.work_dir / "exit.code"

        cmd, active_unit = self.build_wrapped_command(
            argv,
            unit_name=req.unit_name,
            timeout_secs=req.timeout_secs,
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(req.work_dir),
                **popen_isolated_kwargs(),
            )
        except FileNotFoundError as exc:
            exit_code_path.write_text("127\n", encoding="utf-8")
            stderr_path.write_text(f"Komut bulunamadı: {cmd[0]} ({exc})\n", encoding="utf-8")
            raw_stream_path.touch(exist_ok=True)
            return WorkerResult(
                ok=False,
                exit_code=127,
                raw_stream_path=raw_stream_path,
                stderr_path=stderr_path,
                backend=adapter.backend_name,
                error=f"CLI ikili dosyası bulunamadı: {cmd[0]}",
            )
        except OSError as exc:
            exit_code_path.write_text("126\n", encoding="utf-8")
            stderr_path.write_text(f"Süreç başlatma hatası: {exc}\n", encoding="utf-8")
            raw_stream_path.touch(exist_ok=True)
            return WorkerResult(
                ok=False,
                exit_code=126,
                raw_stream_path=raw_stream_path,
                stderr_path=stderr_path,
                backend=adapter.backend_name,
                error=f"Süreç başlatılamadı: {exc}",
            )

        stdout_task = asyncio.create_task(_drain_stream(proc.stdout, raw_stream_path))
        stderr_task = asyncio.create_task(_drain_stream(proc.stderr, stderr_path))
        stdin_task = (
            asyncio.create_task(_feed_stdin(proc.stdin, stdin_bytes))
            if stdin_bytes is not None
            else None
        )

        async def _wait_all() -> int:
            if stdin_task is not None:
                await stdin_task
            rc = await proc.wait()
            await asyncio.gather(stdout_task, stderr_task)
            return rc

        try:
            exit_code = await asyncio.wait_for(_wait_all(), timeout=float(req.timeout_secs))
        except TimeoutError:
            if active_unit:
                await cancel_unit(active_unit)
            await _terminate_process_group(proc)
            for t in (stdin_task, stdout_task, stderr_task):
                if t is not None and not t.done():
                    t.cancel()
            await asyncio.gather(
                *[t for t in (stdin_task, stdout_task, stderr_task) if t is not None],
                return_exceptions=True,
            )
            exit_code_path.write_text("124\n", encoding="utf-8")
            return WorkerResult(
                ok=False,
                exit_code=124,
                raw_stream_path=raw_stream_path,
                stderr_path=stderr_path,
                backend=adapter.backend_name,
                error=f"Worker zaman aşımına uğradı ({req.timeout_secs}s)",
            )
        except asyncio.CancelledError:
            if active_unit:
                await cancel_unit(active_unit)
            await _terminate_process_group(proc)
            for t in (stdin_task, stdout_task, stderr_task):
                if t is not None and not t.done():
                    t.cancel()
            await asyncio.gather(
                *[t for t in (stdin_task, stdout_task, stderr_task) if t is not None],
                return_exceptions=True,
            )
            exit_code_path.write_text("130\n", encoding="utf-8")
            raise

        exit_code_path.write_text(f"{exit_code}\n", encoding="utf-8")
        result = adapter.parse_output(req, raw_stream_path, output_file_path)
        result.exit_code = exit_code
        result.raw_stream_path = raw_stream_path
        result.stderr_path = stderr_path
        result.backend = adapter.backend_name

        if exit_code != 0:
            result.ok = False
            if not result.error and stderr_path.is_file():
                tail = stderr_path.read_text(encoding="utf-8", errors="replace").strip()[-500:]
                result.error = tail or f"Worker sıfır olmayan çıkış kodu döndürdü: {exit_code}"

        return result
