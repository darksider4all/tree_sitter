from __future__ import annotations

import subprocess
import sys

from helpers.api import ApiHandler, Request, Response


class InstallDeps(ApiHandler):
    async def process(self, input: dict, request: Request) -> dict | Response:
        package = "tree-sitter-language-pack"
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", package],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return Response(
                f"Failed to install {package}: {result.stderr.strip()}",
                500,
            )
        return {"ok": True, "message": f"{package} installed successfully."}
