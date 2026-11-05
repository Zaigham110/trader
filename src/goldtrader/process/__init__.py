"""Process plumbing: PID locks, child supervisor with backoff, exit log (ARCHITECTURE.md §4, §5)."""

from goldtrader.process.exitlog import ExitLog
from goldtrader.process.lock import CONFLICTS, Lock, LockHeld, acquire, pid_alive
from goldtrader.process.supervisor import ChildExit, ChildSpec, Supervisor, db_exit_recorder

__all__ = [
    "CONFLICTS",
    "ChildExit",
    "ChildSpec",
    "ExitLog",
    "Lock",
    "LockHeld",
    "Supervisor",
    "acquire",
    "db_exit_recorder",
    "pid_alive",
]
