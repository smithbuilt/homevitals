"""Which installer put homevitals here, and how to ask it for more.

uv tool venvs carry no pip, and pipx would create a second copy next to a uv
install, so every command that reinstalls or extends the package has to go
through the installer that owns it. Three callers need this: the updater, the
uninstall hint, and the browser fallback when its optional extra is missing.
"""
from __future__ import annotations

import importlib.util
import shlex
import shutil
import sys

# This build's package name. It is not on PyPI: "eufy-sync" there is the original one-person tool.
DIST_NAME = "homevitals"
BROWSER_EXTRA = f"{DIST_NAME}[browser]"


def installer() -> str:
    """Return "uv", "pipx", or "pip"."""
    # Normalize backslashes so the marker matches on Windows uv installs
    # (C:\...\uv\tools\...) as well as POSIX ones.
    if "/uv/tools/" in sys.executable.replace("\\", "/") and shutil.which("uv"):
        return "uv"
    if shutil.which("pipx"):
        return "pipx"
    return "pip"


def install_argv(spec: str) -> list[str]:
    """The argv that installs `spec` (a requirement like "homevitals[browser]")
    through the installer that owns this copy."""
    which = installer()
    if which == "uv":
        return ["uv", "tool", "install", "--force", spec]
    if which == "pipx":
        return ["pipx", "install", "--force", spec]
    return [sys.executable, "-m", "pip", "install", "--upgrade", spec]


def install_command(spec: str) -> str:
    """install_argv as one line a person can paste. Square brackets are shell
    globs, so a spec with an extra is quoted; pip is spelled as the plain
    command rather than the interpreter path, which is what a reader expects."""
    which = installer()
    if which == "uv":
        head = "uv tool install --force"
    elif which == "pipx":
        head = "pipx install --force"
    else:
        head = "pip install"
    return f"{head} {shlex.quote(spec)}"


def uninstall_command() -> str:
    which = installer()
    if which == "uv":
        return f"uv tool uninstall {DIST_NAME}"
    if which == "pipx":
        return f"pipx uninstall {DIST_NAME}"
    return f"pip uninstall {DIST_NAME}"


def browser_install_command() -> str:
    """How to add the optional browser login (Playwright). This build comes from a
    wheel file, not PyPI, so the extra is added to the copy already installed."""
    which = installer()
    if which == "uv":
        return 'uv tool install --force --reinstall --with playwright "<the .whl file you installed>"'
    if which == "pipx":
        return f"pipx inject {DIST_NAME} playwright"
    return "pip install playwright"


def has_browser_extra() -> bool:
    """Whether the optional Playwright dependency is installed."""
    return importlib.util.find_spec("playwright") is not None
