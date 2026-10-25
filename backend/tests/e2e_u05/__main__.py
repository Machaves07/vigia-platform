"""Canal de vida del mundo: listo por stdout, parada por EOF en stdin o señal."""

from __future__ import annotations

import signal
import sys

from tests.e2e_u05.world import world


def main() -> int:
    def stop(_signal: int, _frame: object) -> None:
        raise SystemExit(130)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with world():
        print("VIGIA_WORLD_READY", flush=True)
        sys.stdin.read()
    print("VIGIA_WORLD_STOPPED", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
