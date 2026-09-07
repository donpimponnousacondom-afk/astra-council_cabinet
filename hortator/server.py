from __future__ import annotations

import asyncio

import uvicorn

from .app import create_app


class CouncilServer(uvicorn.Server):
    """End dashboard streams before Uvicorn waits for HTTP connections to drain."""

    def __init__(self, directory, host, port):
        self.stopping = asyncio.Event()
        super().__init__(
            uvicorn.Config(
                create_app(directory, stopping=self.stopping),
                host=host,
                port=port,
                workers=1,
                proxy_headers=False,
                timeout_graceful_shutdown=10,
            )
        )

    async def shutdown(self, sockets=None):
        self.stopping.set()
        await super().shutdown(sockets)


def serve(directory, host, port):
    try:
        CouncilServer(directory, host, port).run()
    except KeyboardInterrupt:
        pass  # Uvicorn restores/re-raises SIGINT after its orderly shutdown.
