from __future__ import annotations

import subprocess
import sys

def install(**kwargs):
    package = "tree-sitter-language-pack"
    print(f"Installing {package}...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", package],
        check=False,
    )
    if result.returncode == 0:
        print("Tree-sitter runtime installed successfully.")
    elif result.returncode != 0:
        raise RuntimeError(f"Failed to install {package} (exit code {result.returncode})")