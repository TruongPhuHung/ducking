from __future__ import annotations

import os
import select
import signal
import sys
import time


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    control_fd = int(sys.argv[1])
    process_group = int(sys.argv[2])
    try:
        while True:
            readable, _, _ = select.select([control_fd], [], [], 1.0)
            if not readable:
                continue
            if os.read(control_fd, 1):
                continue
            try:
                os.killpg(process_group, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                return 0
            time.sleep(5)
            try:
                os.killpg(process_group, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            return 0
    finally:
        os.close(control_fd)


if __name__ == "__main__":
    raise SystemExit(main())
