"""Linux parent-death guardian for one owned process group, never a system-wide kill."""

import ctypes
import os
import signal
import resource
import subprocess
import sys


def terminate(_signum=None, _frame=None):
    os.killpg(os.getpgrp(), signal.SIGKILL)


def main():
    if sys.platform != "linux" or len(sys.argv) < 3 or os.getpgrp() != os.getpid():
        return 2
    parent = int(sys.argv[1])
    signal.signal(signal.SIGTERM, terminate)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    if os.getppid() != parent or libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0 or os.getppid() != parent:
        return 2
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024 * 1024, 32 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_AS, (1024 * 1024 * 1024, 1024 * 1024 * 1024))
    child = subprocess.Popen(sys.argv[2:], close_fds=True)
    while True:
        try:
            return child.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            if os.getppid() != parent:
                terminate()


if __name__ == "__main__":
    raise SystemExit(main())
