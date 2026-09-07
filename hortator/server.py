from __future__ import annotations

import asyncio

import uvicorn

from .app import create_app
from .console import OperationalConsole


class CouncilServer(uvicorn.Server):
    """End dashboard streams before Uvicorn waits for HTTP connections to drain."""

    def __init__(self, directory, host, port, console=None):
        self.stopping = asyncio.Event()
        self.console = console
        super().__init__(
            uvicorn.Config(
                create_app(directory, stopping=self.stopping, console=console),
                host=host,
                port=port,
                workers=1,
                proxy_headers=False,
                timeout_graceful_shutdown=10,
                log_config=None if console else uvicorn.config.LOGGING_CONFIG,
            )
        )

    async def shutdown(self, sockets=None):
        self.stopping.set()
        if self.console:
            self.console.stop_keys()
        await super().shutdown(sockets)


def serve(directory, host, port, **console_options):
    with OperationalConsole(**console_options) as console:
        console.notice(f"Dashboard: http://{host}:{port}")
        console.notice(
            f"First-run password file: {directory / 'initial-password'} (unless HORTATOR_ADMIN_PASSWORD is set)"
        )
        try:
            CouncilServer(directory, host, port, console).run()
        except KeyboardInterrupt:
            pass  # Uvicorn restores/re-raises SIGINT after its orderly shutdown.
