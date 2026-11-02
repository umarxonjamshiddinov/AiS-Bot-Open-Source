from __future__ import annotations

import os
from pathlib import Path


class RuntimeLease:
    """OS releases this lock after a crash; the empty lock file may remain.

    Polling and destructive maintenance take this lease. Read-only tools and
    backups can coexist; a second bot must not recover the first bot's requests.
    """

    def __init__(self, database_path: Path) -> None:
        self.path = Path(str(database_path) + ".runtime.lock")
        self._file = None

    def acquire(self) -> None:
        if self._file is not None:
            raise RuntimeError("Runtime lease is already held")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        file = self.path.open("a+b")
        try:
            if self.path.stat().st_size == 0:
                file.write(b"\0")
                file.flush()
            file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            file.close()
            raise RuntimeError("Another bot instance already owns this database") from error
        self._file = file

    def release(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
