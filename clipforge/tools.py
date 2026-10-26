"""Locate external tools (ffmpeg, ffprobe).

On Windows, winget puts ffmpeg on PATH only for processes started after the user signs out and
back in, so a worker started from Explorer right after setup can't see it. Fall back to
winget's known install locations instead of failing jobs.
"""

import glob
import os
import shutil
from functools import cache


class ToolMissing(RuntimeError):
    pass


def _windows_candidates(name: str) -> list[str]:
    local = os.environ.get("LOCALAPPDATA", "")
    exe = f"{name}.exe"
    return [
        os.path.join(local, "Microsoft", "WinGet", "Links", exe),
        *sorted(glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages", "Gyan.FFmpeg*",
                                       "*", "bin", exe)), reverse=True),
        os.path.join("C:\\", "ProgramData", "chocolatey", "bin", exe),
        os.path.join("C:\\", "ffmpeg", "bin", exe),
    ]


@cache
def tool(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    if os.name == "nt":
        for cand in _windows_candidates(name):
            if os.path.isfile(cand):
                return cand
    raise ToolMissing(f"{name} not found. Install ffmpeg (Windows: winget install Gyan.FFmpeg), "
                      "then sign out and back in.")


def ffmpeg() -> str:
    return tool("ffmpeg")


def ffprobe() -> str:
    return tool("ffprobe")
