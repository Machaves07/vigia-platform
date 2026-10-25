"""Canal de vida del mundo: listo por stdout, parada por EOF en stdin o señal."""

from __future__ import annotations

import signal
import sys
import traceback

from tests.e2e_u05.world import world


def main() -> int:
    def stop(_signal: int, _frame: object) -> None:
        raise SystemExit(130)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        with world():
            print("VIGIA_WORLD_READY", flush=True)
            sys.stdin.read()
    except Exception as error:
        # Solo tipos y ubicaciones: ni mensajes de librerías ni cuerpos o credenciales.
        frames = traceback.extract_tb(error.__traceback__)
        locations = ", ".join(f"{frame.name}:{frame.lineno}" for frame in frames[-4:])
        print(f"VIGIA_WORLD_FAILURE {type(error).__name__} ({locations})", flush=True)
        return 1
    print("VIGIA_WORLD_STOPPED", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
