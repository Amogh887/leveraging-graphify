from __future__ import annotations

import os


def safe_join(root: str, rel: str) -> str | None:
    root_real = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root_real, rel))
    if target == root_real or target.startswith(root_real + os.sep):
        return target
    return None
