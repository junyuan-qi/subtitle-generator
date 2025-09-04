import os
from typing import List

SUPPORTED_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm"}


def ensure_dirs(*dirs: str) -> None:
    for d in dirs:
        if d:
            os.makedirs(d, exist_ok=True)


def find_videos(src_dir: str) -> List[str]:
    paths: List[str] = []
    if not os.path.isdir(src_dir):
        return paths
    for entry in sorted(os.listdir(src_dir)):
        p = os.path.join(src_dir, entry)
        if not os.path.isfile(p):
            continue
        ext = os.path.splitext(p)[1].lower()
        if ext in SUPPORTED_VIDEO_EXTS:
            paths.append(p)
    return paths
