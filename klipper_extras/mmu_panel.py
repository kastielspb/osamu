"""
OSAMU MMU Panel - Moonraker Component

Serves the MMU web panel at /server/mmu/
No nginx configuration required.

Installation:
1. Copy or symlink this file to ~/moonraker/moonraker/components/mmu_panel.py
2. Add to moonraker.conf:
   [mmu_panel]
   path: /path/to/osamu/klipper_extras/web_panel

3. Restart Moonraker: sudo systemctl restart moonraker
4. Access panel at: http://<printer-ip>/server/mmu/
"""

from __future__ import annotations
import os
import pathlib
import logging
from typing import TYPE_CHECKING, Dict, Any

if TYPE_CHECKING:
    from moonraker.confighelper import ConfigHelper
    from moonraker.common import WebRequest

COMPONENT_NAME = "mmu_panel"
PANEL_ROUTE = "/server/mmu"

# MIME types for static files
MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class MmuPanel:
    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()
        self.name = config.get_name()

        # Get panel path from config
        self.panel_path = pathlib.Path(
            config.get("path", "~/osamu/klipper_extras/web_panel")
        ).expanduser().resolve()

        if not self.panel_path.is_dir():
            raise config.error(
                f"[{self.name}] Panel path does not exist: {self.panel_path}"
            )

        logging.info(f"MMU Panel: Serving from {self.panel_path}")

        # Register HTTP endpoints
        self.server.register_endpoint(
            f"{PANEL_ROUTE}/", ["GET"], self._handle_root,
            transports=["http"], wrap_result=False
        )
        self.server.register_endpoint(
            f"{PANEL_ROUTE}/{{path:path}}", ["GET"], self._handle_static,
            transports=["http"], wrap_result=False
        )

    async def _handle_root(self, web_request: WebRequest) -> Any:
        """Serve index.html for root path"""
        return await self._serve_file("index.html")

    async def _handle_static(self, web_request: WebRequest) -> Any:
        """Serve static files"""
        path = web_request.get_str("path", "index.html")
        return await self._serve_file(path)

    async def _serve_file(self, filename: str) -> Any:
        """Read and return file with appropriate MIME type"""
        # Security: prevent path traversal
        safe_path = pathlib.Path(filename).name
        if "/" in filename:
            parts = pathlib.Path(filename).parts
            # Only allow single level subdirectory
            if len(parts) <= 2 and ".." not in parts:
                safe_path = str(pathlib.Path(*parts))
            else:
                return self._not_found(filename)

        file_path = self.panel_path / safe_path

        if not file_path.is_file():
            # Try index.html for directory-like paths
            if (self.panel_path / safe_path / "index.html").is_file():
                file_path = self.panel_path / safe_path / "index.html"
            else:
                return self._not_found(filename)

        # Verify path is within panel directory (prevent traversal)
        try:
            file_path.resolve().relative_to(self.panel_path)
        except ValueError:
            return self._not_found(filename)

        suffix = file_path.suffix.lower()
        content_type = MIME_TYPES.get(suffix, "application/octet-stream")

        content = file_path.read_bytes()

        return {
            "status_code": 200,
            "headers": {"Content-Type": content_type},
            "body": content,
        }

    def _not_found(self, filename: str) -> Dict[str, Any]:
        return {
            "status_code": 404,
            "headers": {"Content-Type": "text/plain"},
            "body": f"Not found: {filename}".encode(),
        }


def load_component(config: ConfigHelper) -> MmuPanel:
    return MmuPanel(config)
