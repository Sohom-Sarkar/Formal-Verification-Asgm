"""Run a callable on a thread with a large stack.

Elaboration recurses down the logic cones, which get deep on wide ripple-carry
adders, and CPython's default stack is not enough for 64+ bits. Windows rejects
some stack sizes outright, so fall back until one is accepted.
"""

import sys
import threading

SIZES = (128 * 1024 * 1024, 64 * 1024 * 1024, 32 * 1024 * 1024)


def run(fn, *args, recursion_limit=100000, **kwargs):
    """Call fn(*args, **kwargs) on a big-stack thread and return its result.

    Exceptions propagate to the caller rather than being swallowed by the
    thread.
    """
    sys.setrecursionlimit(recursion_limit)

    for size in SIZES:
        try:
            threading.stack_size(size)
            break
        except (ValueError, RuntimeError):
            continue

    box = {}

    def target():
        try:
            box["value"] = fn(*args, **kwargs)
        except BaseException as exc:            # noqa: BLE001 - re-raised below
            box["error"] = exc

    thread = threading.Thread(target=target)
    thread.start()
    thread.join()

    if "error" in box:
        raise box["error"]
    return box.get("value")
