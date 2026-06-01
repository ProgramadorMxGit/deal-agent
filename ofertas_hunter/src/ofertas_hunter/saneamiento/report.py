"""Modelo y renderizado del ``SaneamientoReport``.

Define las dataclasses inmutables que el ``SaneamientoRunner`` produce
al final de cada run y los renderers que las traducen a stdout.

API pública:

- ``TaskResult``: resultado de una sola ``Saneamiento_Task``.
- ``SaneamientoReport``: agregado del run completo (header + tasks).
- ``render_human(report) -> str``: bloque legible (multi-línea).
- ``render_json_line(report) -> str``: una sola línea ``"JSON: {...}"``
  parseable por consumidores scriptables.
- ``print_to_stdout(report, *, file=sys.stdout)``: imprime ambos
  renders y, en Dry_Run, añade al final la línea literal del
  Requirement 3.3.
- ``DRY_RUN_LITERAL``: la cadena exacta requerida por 3.3.

Los invariantes del Requirement 6.1 se validan en
``TaskResult.__post_init__`` (``ValueError``):

- ``affected <= inspected``
- ``len(ids_sample) <= 30``
- ``mode == "dry-run"`` ⇒ ``affected == 0``

Cubre Requirements 3.2, 3.3, 6.1.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import IO, Literal, Optional

__all__ = [
    "TaskResult",
    "SaneamientoReport",
    "render_human",
    "render_json_line",
    "print_to_stdout",
    "DRY_RUN_LITERAL",
]


DRY_RUN_LITERAL = "DRY-RUN: usa --apply para escribir cambios"
"""Línea literal final exigida por Requirement 3.3 en Dry_Run."""


_MAX_IDS_SAMPLE = 30


@dataclass(frozen=True)
class TaskResult:
    """Resultado inmutable de una ``Saneamiento_Task``.

    Campos derivados del design (sección Data Models → ``TaskResult``).
    Los invariantes se validan en ``__post_init__`` y, si se violan,
    se levanta ``ValueError`` con un mensaje descriptivo.
    """

    task: str
    mode: Literal["dry-run", "apply"]
    marketplace_filter: str
    inspected: int
    affected: int
    ids_sample: list[int]
    error: Optional[str] = None
    skipped_reason: Optional[str] = None

    def __post_init__(self) -> None:
        # affected <= inspected (Requirement 6.1).
        if self.affected > self.inspected:
            raise ValueError(
                "TaskResult invariant: affected "
                f"({self.affected}) > inspected ({self.inspected})"
            )

        # ids_sample acotada a 30 (Requirement 6.1).
        if len(self.ids_sample) > _MAX_IDS_SAMPLE:
            raise ValueError(
                "TaskResult invariant: len(ids_sample)="
                f"{len(self.ids_sample)} > {_MAX_IDS_SAMPLE}"
            )

        # En Dry_Run no se aplica nada (Requirement 3.1 / 6.1).
        if self.mode == "dry-run" and self.affected != 0:
            raise ValueError(
                "TaskResult invariant: affected must be 0 in dry-run "
                f"mode (got {self.affected})"
            )


@dataclass(frozen=True)
class SaneamientoReport:
    """Agregado inmutable del ``Saneamiento_Run`` completo.

    Sigue la forma del design (sección Data Models →
    ``SaneamientoReport``). Las propiedades agregadas
    ``total_inspected``, ``total_affected`` y ``has_errors`` se
    calculan on-demand para evitar inconsistencias con ``tasks``.
    """

    started_at: str
    finished_at: str
    mode: Literal["dry-run", "apply"]
    marketplace_filter: str
    mcp_serve_active: bool
    tasks: list[TaskResult]

    @property
    def total_inspected(self) -> int:
        return sum(t.inspected for t in self.tasks)

    @property
    def total_affected(self) -> int:
        return sum(t.affected for t in self.tasks)

    @property
    def has_errors(self) -> bool:
        return any(t.error is not None for t in self.tasks)


def render_human(report: SaneamientoReport) -> str:
    """Bloque legible multi-línea, una línea por task.

    El formato sigue el ejemplo del design (sección Data Models →
    Forma legible). El literal Dry_Run NO se incluye aquí; lo añade
    ``print_to_stdout`` cuando ``mode == "dry-run"`` (Requirement 3.3).
    """

    lines: list[str] = [
        "=== Saneamiento_Run "
        f"mode={report.mode} "
        f"marketplace={report.marketplace_filter} "
        f"started={report.started_at} ==="
    ]

    for t in report.tasks:
        ids_repr = f"ids_sample={list(t.ids_sample)!r}"
        line = (
            f"[{t.task}] "
            f"inspected={t.inspected} "
            f"affected={t.affected} "
            f"{ids_repr}"
        )
        if t.error is not None:
            line += f" error={t.error!r}"
        if t.skipped_reason is not None:
            line += f" skipped_reason={t.skipped_reason!r}"
        lines.append(line)

    lines.append(f"mcp_serve_active={str(report.mcp_serve_active).lower()}")
    return "\n".join(lines)


def render_json_line(report: SaneamientoReport) -> str:
    """Una sola línea ``"JSON: {...}"`` con todos los campos del design.

    Usa separadores compactos para garantizar que el payload no
    contiene saltos de línea internos (consumidores hacen
    ``grep ^JSON:`` sobre stdout).
    """

    payload: dict[str, object] = {
        "started_at": report.started_at,
        "finished_at": report.finished_at,
        "mode": report.mode,
        "marketplace_filter": report.marketplace_filter,
        "mcp_serve_active": report.mcp_serve_active,
        "tasks": [
            {
                "task": t.task,
                "mode": t.mode,
                "marketplace_filter": t.marketplace_filter,
                "inspected": t.inspected,
                "affected": t.affected,
                "ids_sample": list(t.ids_sample),
                "error": t.error,
            }
            for t in report.tasks
        ],
    }
    return "JSON: " + json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    )


def print_to_stdout(
    report: SaneamientoReport,
    *,
    file: IO[str] | None = None,
) -> None:
    """Imprime el render legible y la línea ``JSON: ...``.

    Si ``report.mode == "dry-run"``, añade como última línea el
    literal exacto ``DRY-RUN: usa --apply para escribir cambios``
    (Requirement 3.3).

    El parámetro ``file`` permite redirigir la salida en tests
    (``io.StringIO``); por defecto escribe a ``sys.stdout``.
    """

    out = file if file is not None else sys.stdout
    print(render_human(report), file=out)
    print(render_json_line(report), file=out)
    if report.mode == "dry-run":
        print(DRY_RUN_LITERAL, file=out)
