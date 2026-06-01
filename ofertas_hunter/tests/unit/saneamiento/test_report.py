"""Tests unitarios para ``ofertas_hunter.saneamiento.report``.

Estos tests definen el contrato de la API descrito en el design.md del
spec ``bot-saneamiento-vps`` (sección ``Data Models`` → ``TaskResult`` y
``SaneamientoReport``). Se escriben antes de la implementación
(TDD-RED): la tarea 8.2 hará que estos tests pasen.

Contrato resumido:

- ``TaskResult`` — frozen dataclass con
  ``task``, ``mode`` (``"dry-run" | "apply"``), ``marketplace_filter``,
  ``inspected``, ``affected``, ``ids_sample`` (lista de ``int``,
  longitud ≤30), ``error: str | None = None``,
  ``skipped_reason: str | None = None``.
- ``SaneamientoReport`` — frozen dataclass con
  ``started_at``, ``finished_at``, ``mode``, ``marketplace_filter``,
  ``mcp_serve_active``, ``tasks: list[TaskResult]`` y propiedades
  agregadas ``total_inspected``, ``total_affected``, ``has_errors``.
- ``render_human(report) -> str`` — bloque legible con una línea por
  task que incluye los literales ``inspected=`` y ``affected=`` y, si
  ``ids_sample`` no es vacío, una representación de la lista.
- ``render_json_line(report) -> str`` — una sola línea prefijada
  exactamente por ``"JSON: "`` seguida de un objeto JSON parseable que
  contiene todos los campos del design.
- ``print_to_stdout(report, *, file=sys.stdout)`` — imprime el bloque
  legible y la línea JSON; en Dry_Run la última línea no vacía debe
  ser exactamente ``"DRY-RUN: usa --apply para escribir cambios"``
  (Requirement 3.3).

Tests cubren Requirements 3.2, 3.3, 6.1.
"""

from __future__ import annotations

import io
import json
from dataclasses import FrozenInstanceError

import pytest

from ofertas_hunter.saneamiento.report import (
    SaneamientoReport,
    TaskResult,
    print_to_stdout,
    render_human,
    render_json_line,
)


# ---------------------------------------------------------------------------
# Constantes compartidas — timestamps deterministas
# ---------------------------------------------------------------------------


_STARTED_AT = "2026-05-29T12:34:56.789Z"
_FINISHED_AT = "2026-05-29T12:34:57.456Z"
_DRY_RUN_LITERAL = "DRY-RUN: usa --apply para escribir cambios"


# ---------------------------------------------------------------------------
# Helpers — builders mínimos para no repetir kwargs en cada test
# ---------------------------------------------------------------------------


def _make_task_result(
    *,
    task: str = "outbox-duplicates",
    mode: str = "apply",
    marketplace_filter: str = "all",
    inspected: int = 12,
    affected: int = 10,
    ids_sample: list[int] | None = None,
    error: str | None = None,
    skipped_reason: str | None = None,
) -> TaskResult:
    return TaskResult(
        task=task,
        mode=mode,
        marketplace_filter=marketplace_filter,
        inspected=inspected,
        affected=affected,
        ids_sample=list(ids_sample) if ids_sample is not None else [1, 2, 3],
        error=error,
        skipped_reason=skipped_reason,
    )


def _make_report(
    *,
    mode: str = "apply",
    marketplace_filter: str = "all",
    mcp_serve_active: bool = False,
    tasks: list[TaskResult] | None = None,
) -> SaneamientoReport:
    return SaneamientoReport(
        started_at=_STARTED_AT,
        finished_at=_FINISHED_AT,
        mode=mode,
        marketplace_filter=marketplace_filter,
        mcp_serve_active=mcp_serve_active,
        tasks=list(tasks) if tasks is not None else [_make_task_result()],
    )


# ---------------------------------------------------------------------------
# A. ``TaskResult`` — shape básica (frozen, defaults, validations)
# (Requirements 3.2, 6.1)
# ---------------------------------------------------------------------------


class TestTaskResultShape:
    """Cubre la forma del dataclass declarado en el design."""

    def test_construct_with_all_fields(self) -> None:
        tr = _make_task_result(
            task="outbox-stale",
            mode="apply",
            marketplace_filter="amazon",
            inspected=5,
            affected=3,
            ids_sample=[10, 11, 12],
            error=None,
            skipped_reason=None,
        )

        assert tr.task == "outbox-stale"
        assert tr.mode == "apply"
        assert tr.marketplace_filter == "amazon"
        assert tr.inspected == 5
        assert tr.affected == 3
        assert tr.ids_sample == [10, 11, 12]
        assert tr.error is None
        assert tr.skipped_reason is None

    def test_default_error_and_skipped_reason_are_none(self) -> None:
        tr = TaskResult(
            task="outbox-stale",
            mode="dry-run",
            marketplace_filter="all",
            inspected=0,
            affected=0,
            ids_sample=[],
        )

        assert tr.error is None
        assert tr.skipped_reason is None

    def test_is_frozen_assignment_raises(self) -> None:
        tr = _make_task_result()

        with pytest.raises(FrozenInstanceError):
            tr.affected = 99  # type: ignore[misc]


class TestTaskResultValidations:
    """Validaciones invariantes del Requirement 3.2 / 6.1.

    El design garantiza:

    - ``affected <= inspected``;
    - ``len(ids_sample) <= 30``;
    - ``affected == 0`` cuando ``mode == "dry-run"``.

    La implementación debe rechazar construcciones que las violen
    (idiomático ``__post_init__`` que levanta ``ValueError``).
    """

    def test_affected_greater_than_inspected_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            TaskResult(
                task="outbox-duplicates",
                mode="apply",
                marketplace_filter="all",
                inspected=5,
                affected=6,
                ids_sample=[1, 2, 3, 4, 5],
            )

    def test_ids_sample_longer_than_30_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            TaskResult(
                task="outbox-duplicates",
                mode="apply",
                marketplace_filter="all",
                inspected=31,
                affected=31,
                ids_sample=list(range(1, 32)),  # len == 31
            )

    def test_dry_run_with_nonzero_affected_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            TaskResult(
                task="outbox-duplicates",
                mode="dry-run",
                marketplace_filter="all",
                inspected=5,
                affected=3,
                ids_sample=[1, 2, 3],
            )

    def test_ids_sample_with_exactly_30_is_accepted(self) -> None:
        # Boundary: exactamente 30 ids es válido (≤30).
        tr = TaskResult(
            task="outbox-duplicates",
            mode="apply",
            marketplace_filter="all",
            inspected=30,
            affected=30,
            ids_sample=list(range(1, 31)),
        )
        assert len(tr.ids_sample) == 30


# ---------------------------------------------------------------------------
# B. ``SaneamientoReport`` — propiedades agregadas
# ---------------------------------------------------------------------------


class TestSaneamientoReportAggregates:
    """``total_inspected``, ``total_affected`` y ``has_errors``."""

    def test_totals_sum_across_tasks(self) -> None:
        tasks = [
            _make_task_result(
                task="outbox-duplicates",
                inspected=5,
                affected=2,
                ids_sample=[1, 2],
            ),
            _make_task_result(
                task="outbox-stale",
                inspected=3,
                affected=1,
                ids_sample=[7],
            ),
        ]
        report = _make_report(tasks=tasks)

        assert report.total_inspected == 8
        assert report.total_affected == 3

    def test_empty_tasks_list_yields_zero_totals_and_no_errors(self) -> None:
        report = _make_report(tasks=[])

        assert report.total_inspected == 0
        assert report.total_affected == 0
        assert report.has_errors is False

    def test_has_errors_false_when_all_tasks_have_error_none(self) -> None:
        tasks = [
            _make_task_result(error=None),
            _make_task_result(task="outbox-stale", error=None),
        ]
        report = _make_report(tasks=tasks)

        assert report.has_errors is False

    def test_has_errors_true_when_any_task_has_error(self) -> None:
        tasks = [
            _make_task_result(error=None),
            _make_task_result(
                task="outbox-stale",
                inspected=0,
                affected=0,
                ids_sample=[],
                error="ValueError: bad",
            ),
        ]
        report = _make_report(tasks=tasks)

        assert report.has_errors is True

    def test_is_frozen_assignment_raises(self) -> None:
        report = _make_report()

        with pytest.raises(FrozenInstanceError):
            report.mode = "dry-run"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# C. ``render_human`` — bloque legible con línea por task
# (Requirement 3.2)
# ---------------------------------------------------------------------------


class TestRenderHuman:
    """El render legible debe incluir, por task, ``inspected=``,
    ``affected=`` y la representación de ``ids_sample`` cuando no
    está vacío.
    """

    def test_human_render_contains_per_task_inspected_and_affected(
        self,
    ) -> None:
        tasks = [
            _make_task_result(
                task="outbox-duplicates",
                inspected=12,
                affected=10,
                ids_sample=[1, 2, 3],
            ),
        ]
        report = _make_report(mode="apply", tasks=tasks)

        rendered = render_human(report)

        assert "outbox-duplicates" in rendered
        assert "inspected=12" in rendered
        assert "affected=10" in rendered

    def test_human_render_contains_ids_sample_list(self) -> None:
        tasks = [
            _make_task_result(
                task="outbox-duplicates",
                inspected=3,
                affected=3,
                ids_sample=[101, 202, 303],
            ),
        ]
        report = _make_report(mode="apply", tasks=tasks)

        rendered = render_human(report)

        # Aceptamos cualquiera de las representaciones idiomáticas
        # (con o sin espacios). Lo importante es que los tres ids
        # aparezcan dentro de la lista renderizada.
        assert "101" in rendered
        assert "202" in rendered
        assert "303" in rendered
        assert "ids_sample=" in rendered

    def test_human_render_does_not_contain_json_prefix(self) -> None:
        """``render_human`` y ``render_json_line`` son rendererings
        independientes; el legible no debe llevar el prefijo ``JSON:``.
        """

        report = _make_report()
        rendered = render_human(report)

        # No debe aparecer el prefijo del render JSON-line.
        assert "JSON: " not in rendered

    def test_human_render_does_not_contain_dry_run_literal(self) -> None:
        """El literal de Dry_Run sólo lo añade ``print_to_stdout``,
        no ``render_human``.
        """

        report = _make_report(mode="dry-run", tasks=[
            _make_task_result(
                task="outbox-duplicates",
                mode="dry-run",
                inspected=5,
                affected=0,
                ids_sample=[1, 2, 3],
            ),
        ])

        rendered = render_human(report)

        assert _DRY_RUN_LITERAL not in rendered


# ---------------------------------------------------------------------------
# D. ``render_json_line`` — una sola línea prefijada por ``JSON: ``
# (Requirement 6.1 — forma JSON del report)
# ---------------------------------------------------------------------------


class TestRenderJsonLine:
    """Verifica forma, prefijo y completitud del render JSON-line."""

    def test_json_line_starts_with_prefix(self) -> None:
        report = _make_report()

        line = render_json_line(report)

        assert line.startswith("JSON: ")

    def test_json_line_is_single_line(self) -> None:
        """Sin saltos de línea embebidos: stdout puede separar líneas
        por ``\\n`` y los consumidores scriptables hacen ``grep ^JSON:``.
        """

        report = _make_report()

        line = render_json_line(report)

        # No debe contener saltos de línea internos.
        assert "\n" not in line
        assert "\r" not in line

    def test_json_line_payload_is_parseable_to_dict_with_all_fields(
        self,
    ) -> None:
        tasks = [
            _make_task_result(
                task="outbox-duplicates",
                mode="apply",
                marketplace_filter="all",
                inspected=12,
                affected=10,
                ids_sample=[1, 2, 3],
                error=None,
            ),
        ]
        report = _make_report(
            mode="apply",
            marketplace_filter="all",
            mcp_serve_active=False,
            tasks=tasks,
        )

        line = render_json_line(report)
        payload_text = line[len("JSON: "):]
        payload = json.loads(payload_text)

        # Top-level: todos los campos del design.
        assert isinstance(payload, dict)
        for key in (
            "started_at",
            "finished_at",
            "mode",
            "marketplace_filter",
            "mcp_serve_active",
            "tasks",
        ):
            assert key in payload, f"clave top-level faltante: {key}"

        assert payload["started_at"] == _STARTED_AT
        assert payload["finished_at"] == _FINISHED_AT
        assert payload["mode"] == "apply"
        assert payload["marketplace_filter"] == "all"
        assert payload["mcp_serve_active"] is False

        # Tasks: una entrada por TaskResult con todos los campos.
        assert isinstance(payload["tasks"], list)
        assert len(payload["tasks"]) == 1
        task_payload = payload["tasks"][0]
        for key in (
            "task",
            "mode",
            "marketplace_filter",
            "inspected",
            "affected",
            "ids_sample",
            "error",
        ):
            assert key in task_payload, f"clave de task faltante: {key}"

        assert task_payload["task"] == "outbox-duplicates"
        assert task_payload["mode"] == "apply"
        assert task_payload["marketplace_filter"] == "all"
        assert task_payload["inspected"] == 12
        assert task_payload["affected"] == 10
        assert task_payload["ids_sample"] == [1, 2, 3]
        assert task_payload["error"] is None


# ---------------------------------------------------------------------------
# E. ``print_to_stdout`` — Dry_Run termina con literal exacto
# (Requirement 3.3)
# ---------------------------------------------------------------------------


def _last_non_empty_line(text: str) -> str:
    """Devuelve la última línea no vacía del texto (rstripping)."""

    lines = [ln for ln in text.splitlines() if ln.strip() != ""]
    assert lines, "se esperaba al menos una línea no vacía en el output"
    return lines[-1]


class TestPrintToStdoutDryRun:
    """En Dry_Run la última línea es exactamente la literal del Req. 3.3."""

    def test_dry_run_last_line_is_exact_literal(self) -> None:
        tasks = [
            _make_task_result(
                task="outbox-duplicates",
                mode="dry-run",
                inspected=5,
                affected=0,
                ids_sample=[1, 2, 3],
            ),
        ]
        report = _make_report(mode="dry-run", tasks=tasks)

        buf = io.StringIO()
        print_to_stdout(report, file=buf)
        output = buf.getvalue()

        assert _last_non_empty_line(output) == _DRY_RUN_LITERAL

    def test_dry_run_includes_human_render_and_json_line(self) -> None:
        """Antes del literal Dry_Run, el render legible y la línea
        ``JSON: ...`` deben estar presentes en el stdout.
        """

        tasks = [
            _make_task_result(
                task="outbox-duplicates",
                mode="dry-run",
                inspected=5,
                affected=0,
                ids_sample=[1, 2, 3],
            ),
        ]
        report = _make_report(mode="dry-run", tasks=tasks)

        buf = io.StringIO()
        print_to_stdout(report, file=buf)
        output = buf.getvalue()

        # Render legible: marca de la task.
        assert "outbox-duplicates" in output
        assert "inspected=5" in output

        # Render JSON-line: una línea con el prefijo.
        json_lines = [
            ln for ln in output.splitlines() if ln.startswith("JSON: ")
        ]
        assert len(json_lines) == 1, (
            "se esperaba exactamente una línea con prefijo 'JSON: '"
        )

        # El literal Dry_Run sigue al final.
        assert _DRY_RUN_LITERAL in output


# ---------------------------------------------------------------------------
# F. ``print_to_stdout`` — Apply_Mode NO incluye el literal Dry_Run
# ---------------------------------------------------------------------------


class TestPrintToStdoutApply:
    """El literal Dry_Run sólo aparece en Dry_Run, nunca en Apply_Mode."""

    def test_apply_mode_does_not_contain_dry_run_literal(self) -> None:
        tasks = [
            _make_task_result(
                task="outbox-duplicates",
                mode="apply",
                inspected=12,
                affected=10,
                ids_sample=[1, 2, 3],
            ),
        ]
        report = _make_report(mode="apply", tasks=tasks)

        buf = io.StringIO()
        print_to_stdout(report, file=buf)
        output = buf.getvalue()

        assert _DRY_RUN_LITERAL not in output

    def test_apply_mode_still_emits_human_render_and_json_line(self) -> None:
        """En Apply_Mode el contrato del stdout es: render legible +
        línea JSON, sin la línea final de Dry_Run.
        """

        tasks = [
            _make_task_result(
                task="outbox-duplicates",
                mode="apply",
                inspected=12,
                affected=10,
                ids_sample=[1, 2, 3],
            ),
        ]
        report = _make_report(mode="apply", tasks=tasks)

        buf = io.StringIO()
        print_to_stdout(report, file=buf)
        output = buf.getvalue()

        assert "outbox-duplicates" in output
        json_lines = [
            ln for ln in output.splitlines() if ln.startswith("JSON: ")
        ]
        assert len(json_lines) == 1
