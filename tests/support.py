"""Run real local HTTP services on reserved ephemeral loopback sockets."""

import asyncio
import os
import socket
import sys
import tempfile
from contextlib import asynccontextmanager

import httpx


@asynccontextmanager
async def running_server(module, environment=None):
    """Reserve the listener before spawning and always terminate the child."""
    folder = tempfile.TemporaryDirectory()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "uvicorn",
        module,
        "--fd",
        str(listener.fileno()),
        "--log-level",
        "warning",
        "--no-access-log",
        pass_fds=(listener.fileno(),),
        env={
            **os.environ,
            "GATEWAY_DATABASE": folder.name + "/quota.sqlite3",
            **(environment or {}),
        },
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    listener.close()
    url = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            for _ in range(100):
                if process.returncode is not None:
                    raise RuntimeError("Test server exited before readiness")
                try:
                    await client.get(url, timeout=0.2)
                    break
                except httpx.TransportError:
                    await asyncio.sleep(0.05)
            else:
                raise TimeoutError("Local test server did not become ready")
        yield url
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill()
                await process.wait()
        await process.communicate()
        folder.cleanup()
