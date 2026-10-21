"""Puerto opcional para la telemetría de las unidades sobre los bucles compartidos."""

from typing import Protocol


class WorkerTelemetry(Protocol):
    """Observaciones, nunca decisiones ni cambios del estado del worker."""

    def dead_letter(self, consumer: str) -> None: ...

    def task_duration(self, task: str, result: str, elapsed_ms: float) -> None: ...

    def task_age(self, task: str, age: float) -> None: ...

    async def report_once(self) -> None: ...
