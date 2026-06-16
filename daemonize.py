#!/usr/bin/env python3
"""Daemonize helper — double-fork to fully escape parent's process group.

Usage:
    python3 daemonize.py <command> [args...]

Writes child PID to /tmp/daemonize_<sanitized_cmd>.pid.
"""
import os
import sys
import time
from pathlib import Path

def pidfile_path(cmd):
    safe = "_".join(c for c in " ".join(cmd) if c.isalnum() or c in "-_")[:80]
    return Path(f"/tmp/daemonize_{safe}.pid")

def main():
    if len(sys.argv) < 2:
        print("Usage: daemonize.py <cmd> [args...]", file=sys.stderr)
        sys.exit(1)

    cmd = sys.argv[1:]
    pf = pidfile_path(cmd)

    # First fork
    pid = os.fork()
    if pid > 0:
        # Parent waits for first child to write PID
        for _ in range(50):
            if pf.exists():
                time.sleep(0.1)
                print(f"Daemonized: PID {pf.read_text().strip()}, log: {cmd[-2] if len(cmd) >= 2 else 'inherit'}")
                sys.exit(0)
            time.sleep(0.1)
        print("Daemonization timeout", file=sys.stderr)
        sys.exit(1)

    # First child: setsid + second fork
    os.setsid()
    pid = os.fork()
    if pid > 0:
        # First child exits, leaving grandchild orphaned and reparented to init
        sys.exit(0)

    # Grandchild: actual daemon
    os.chdir("/")
    os.umask(0)

    # Write PID file
    pf.write_text(str(os.getpid()))

    # Redirect stdio
    log_path = Path("/tmp/daemonize.log")
    log_f = open(log_path, "a")
    os.dup2(log_f.fileno(), 1)
    os.dup2(log_f.fileno(), 2)
    devnull = open("/dev/null", "r")
    os.dup2(devnull.fileno(), 0)

    os.execvp(cmd[0], cmd)

if __name__ == "__main__":
    main()