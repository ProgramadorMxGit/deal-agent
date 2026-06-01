"""Tests unitarios para ``ofertas_hunter.saneamiento.cli.cmd_saneamiento``.

Estos tests definen el contrato del entrypoint del subcomando
``saneamiento`` descrito en el design del spec
``bot-saneamiento-vps`` (sección
``Components and Interfaces`` → ``cli.cmd_saneamiento`` y la matriz
de ``Error Handling`` → exit codes ``0`` / ``1`` / ``2``).

Se escriben antes de la implementación (TDD-RED): la tarea 10.2 hará
que estos tests pasen poblando ``src/ofertas_hunter/saneamiento/cli.py``
con la función ``cmd_saneamiento``.

Contrato resumido (ver design.md):

1. Parsear ``args`` (``argparse.Namespace``) a ``SaneamientoRunArgs``
   con ``apply_mode = args.apply``.
2. Abrir DB vía ``ofertas_hunter.db.connect``. Si ``connect()`` levanta
   excepción → exit ``1`` SIN haber adquirido el lock (Requirement 1.9).
3. Adquirir ``SaneamientoLock(data/saneamiento.lock,
   mcp_lock_path=data/mcp_serve.lock)``.
   * Si ``acquire()`` levanta ``SaneamientoLockBusy`` → imprime ``pid``
     y ``started_at`` del holder en stdout, NO toca DB, exit ``2``
     (Requirement 2.7).
4. Construir ``SaneamientoRunner(conn, run_args)`` y llamar ``.run()``.
   * Si la run levanta excepción no controlada → exit ``1``. El runner
     se encarga de emitir ``saneamiento_run_failed`` antes de
     re-lanzar (cubierto por los tests del runner, 9.1).
5. Imprimir el ``SaneamientoReport`` por stdout vía
   ``print_to_stdout``. El CLI inyecta el ``mcp_serve_active`` real
   del ``LockAcquisition`` en el report final (el runner no conoce
   ese flag y devuelve ``False``) — Requirement 2.5.
6. Liberar el lock, cerrar conn, exit ``0``.

En Dry_Run el render incluye la línea literal
``DRY-RUN: usa --apply para escribir cambios`` (Requirement 3.3).

Cobertura: Requirements 1.8, 1.9, 2.5, 2.7, 3.3.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from typing import Any

import pytest

from ofertas_hunter.saneamiento import cli as cli_mod
from ofertas_hunter.saneamiento.cli import cmd_saneamiento
from ofertas_hunter.saneamiento.lockfile import (
    LockAcquisition,
    SaneamientoLockBusy,
)
from ofertas_hunter.saneamiento.report import (
    DRY_RUN_LITERAL,
    SaneamientoReport,
    TaskResult,
)


# ---------------------------------------------------------------------------
# Constantes y helpers comunes.
# ---------------------------------------------------------------------------


_HOLDER_PID = 424242  # cualquier pid distinto al del proceso de tests
_HOLDER_STARTED_AT = "2026-05-29T11:00:00.000Z"


def _make_args(
    *,
    task: str = "all",
    apply: bool = False,
    marketplace: str = "all",
) -> argparse.Namespace:
    """Construye el ``argparse.Namespace`` que recibiría
    ``cmd_saneamiento`` desde ``__main__.py``.

    Atributos esperados según el wiring del subcomando:
    ``task``, ``apply`` (bool), ``marketplace``.
    """

    ns = argparse.Namespace()
    ns.task = task
    ns.apply = apply
    ns.marketplace = marketplace
    return ns


def _make_report(
    *,
    mode: str,
    mcp_serve_active: bool = False,
    marketplace_filter: str = "all",
) -> SaneamientoReport:
    """Construye un ``SaneamientoReport`` mínimo pero válido.

    Respeta los invariantes de ``TaskResult`` (``affected <= inspected``;
    ``affected == 0`` en Dry_Run).
    """

    inspected = 5
    affected = 0 if mode == "dry-run" else 5
    return SaneamientoReport(
        started_at="2026-05-29T12:00:00.000Z",
        finished_at="2026-05-29T12:00:01.000Z",
        mode=mode,  # type: ignore[arg-type]
        marketplace_filter=marketplace_filter,
        mcp_serve_active=mcp_serve_active,
        tasks=[
            TaskResult(
                task="outbox-duplicates",
                mode=mode,  # type: ignore[arg-type]
                marketplace_filter=marketplace_filter,
                inspected=inspected,
                affected=affected,
                ids_sample=[101, 102, 103, 104, 105],
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Fakes — instalables vía monkeypatch sobre el módulo ``cli``.
# ---------------------------------------------------------------------------


class _FakeLock:
    """Stand-in de ``SaneamientoLock`` con ``acquire`` / ``release``
    programables. Se construye sin args; el factory es quien maneja
    los argumentos del CLI.
    """

    def __init__(self) -> None:
        self.acquire_called: int = 0
        self.release_called: int = 0
        self._raises_in_acquire: BaseException | None = None
        self._mcp_serve_active: bool = False
        self._holder: dict[str, Any] = {
            "pid": os.getpid(),
            "started_at": "2026-05-29T12:00:00.000Z",
        }
        self.constructed_with_args: tuple[Any, ...] = ()
        self.constructed_with_kwargs: dict[str, Any] = {}

    def configure(
        self,
        *,
        raises: BaseException | None = None,
        mcp_serve_active: bool = False,
    ) -> "_FakeLock":
        self._raises_in_acquire = raises
        self._mcp_serve_active = mcp_serve_active
        return self

    def acquire(self) -> LockAcquisition:
        self.acquire_called += 1
        if self._raises_in_acquire is not None:
            raise self._raises_in_acquire
        return LockAcquisition(
            mcp_serve_active=self._mcp_serve_active,
            holder=self._holder,
        )

    def release(self) -> None:
        self.release_called += 1


def _install_fake_lock(
    monkeypatch: pytest.MonkeyPatch,
    *,
    raises: BaseException | None = None,
    mcp_serve_active: bool = False,
) -> _FakeLock:
    """Reemplaza ``cli_mod.SaneamientoLock`` por una factory que
    devuelve siempre la misma instancia ``_FakeLock`` configurada.
    """

    fake = _FakeLock().configure(
        raises=raises,
        mcp_serve_active=mcp_serve_active,
    )

    def _factory(*args: Any, **kwargs: Any) -> _FakeLock:
        fake.constructed_with_args = args
        fake.constructed_with_kwargs = kwargs
        return fake

    monkeypatch.setattr(cli_mod, "SaneamientoLock", _factory, raising=False)
    return fake


class _FakeRunner:
    """Stand-in de ``SaneamientoRunner`` cuyo ``run()`` devuelve un
    ``SaneamientoReport`` programado o levanta una excepción
    inyectada.
    """

    def __init__(self) -> None:
        self.run_called: int = 0
        self._report: SaneamientoReport | None = None
        self._raises: BaseException | None = None
        self.constructed_with_args: tuple[Any, ...] = ()
        self.constructed_with_kwargs: dict[str, Any] = {}

    def configure(
        self,
        *,
        report: SaneamientoReport | None = None,
        raises: BaseException | None = None,
    ) -> "_FakeRunner":
        self._report = report
        self._raises = raises
        return self

    def run(self) -> SaneamientoReport:
        self.run_called += 1
        if self._raises is not None:
            raise self._raises
        assert self._report is not None, (
            "_FakeRunner.run() invocado sin haber configurado report"
        )
        return self._report


def _install_fake_runner(
    monkeypatch: pytest.MonkeyPatch,
    *,
    report: SaneamientoReport | None = None,
    raises: BaseException | None = None,
) -> _FakeRunner:
    """Reemplaza ``cli_mod.SaneamientoRunner`` por una factory que
    devuelve la misma instancia ``_FakeRunner`` configurada.
    """

    fake = _FakeRunner().configure(report=report, raises=raises)

    def _factory(*args: Any, **kwargs: Any) -> _FakeRunner:
        fake.constructed_with_args = args
        fake.constructed_with_kwargs = kwargs
        return fake

    monkeypatch.setattr(cli_mod, "SaneamientoRunner", _factory, raising=False)
    return fake


def _install_fake_connect(
    monkeypatch: pytest.MonkeyPatch,
    *,
    raises: BaseException | None = None,
    conn: sqlite3.Connection | None = None,
) -> sqlite3.Connection | None:
    """Reemplaza ``cli_mod.connect`` por un stub.

    - Si ``raises`` no es ``None``: cualquier llamada a ``connect()``
      levanta esa excepción.
    - Si ``conn`` es ``None``: crea una conexión ``:memory:`` nueva y
      la devuelve siempre.
    - Si se pasa ``conn``: la devuelve siempre (útil para snapshots
      pre/post run).
    """

    if raises is not None:
        def _raising_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            raise raises  # type: ignore[misc]

        monkeypatch.setattr(
            cli_mod, "connect", _raising_connect, raising=False
        )
        return None

    real_conn = conn if conn is not None else sqlite3.connect(":memory:")

    def _ok_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return real_conn

    monkeypatch.setattr(cli_mod, "connect", _ok_connect, raising=False)
    return real_conn


def _extract_json_line(stdout: str) -> dict[str, Any]:
    """Devuelve el dict parseado de la única línea ``JSON: {...}``
    del stdout. Lanza ``AssertionError`` si hay 0 o >1 líneas JSON.
    """

    lines = [
        line for line in stdout.splitlines() if line.startswith("JSON: ")
    ]
    assert len(lines) == 1, (
        f"Esperaba exactamente 1 línea JSON en stdout, encontré {len(lines)}"
    )
    return json.loads(lines[0][len("JSON: "):])


# ===========================================================================
# 1) Dry_Run happy path → exit 0, render legible + literal Dry_Run.
#    (Requirements 1.8, 3.3)
# ===========================================================================


class TestDryRunHappyPath:
    """Validates: Requirements 1.8, 3.3"""

    def test_dry_run_returns_0_and_prints_dry_run_literal(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _install_fake_connect(monkeypatch)
        lock = _install_fake_lock(monkeypatch, mcp_serve_active=False)
        runner = _install_fake_runner(
            monkeypatch,
            report=_make_report(mode="dry-run", mcp_serve_active=False),
        )

        rc = cmd_saneamiento(_make_args(apply=False))

        captured = capsys.readouterr()

        assert rc == 0
        # El render legible incluye el header del Saneamiento_Run.
        assert "Saneamiento_Run" in captured.out
        # El render JSON aparece como una sola línea ``JSON: ...``.
        assert "JSON: " in captured.out
        # La línea literal exigida por el Requirement 3.3 debe aparecer
        # tal cual (sin variaciones tipográficas) en stdout.
        assert DRY_RUN_LITERAL in captured.out
        # El lock se adquirió y se liberó en el happy path.
        assert lock.acquire_called == 1
        assert lock.release_called == 1
        # El runner corrió.
        assert runner.run_called == 1


# ===========================================================================
# 2) Apply_Mode happy path → exit 0, sin literal de Dry_Run.
#    (Requirement 1.8)
# ===========================================================================


class TestApplyModeHappyPath:
    """Validates: Requirement 1.8"""

    def test_apply_mode_returns_0(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _install_fake_connect(monkeypatch)
        lock = _install_fake_lock(monkeypatch, mcp_serve_active=False)
        runner = _install_fake_runner(
            monkeypatch,
            report=_make_report(mode="apply", mcp_serve_active=False),
        )

        rc = cmd_saneamiento(_make_args(apply=True))

        captured = capsys.readouterr()

        assert rc == 0
        # El header del render legible se imprime también en Apply_Mode.
        assert "Saneamiento_Run" in captured.out
        # Y la línea literal de Dry_Run NO debe aparecer en Apply_Mode.
        assert DRY_RUN_LITERAL not in captured.out
        # Se adquirió y liberó el lock.
        assert lock.acquire_called == 1
        assert lock.release_called == 1
        # El runner corrió.
        assert runner.run_called == 1


# ===========================================================================
# 3) Lock vivo de otro pid → exit 2, imprime holder, no toca DB.
#    (Requirement 2.7)
# ===========================================================================


class TestLockBusyAbortsWithExit2:
    """Validates: Requirement 2.7"""

    def test_lock_busy_returns_2_prints_holder_and_does_not_touch_db(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Conexión real con una tabla mínima — sirve para snapshotear
        # el contenido antes y después de la invocación.
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE outbox (id INTEGER PRIMARY KEY, state TEXT)"
        )
        conn.execute("INSERT INTO outbox(state) VALUES ('pending')")
        conn.execute("INSERT INTO outbox(state) VALUES ('pending')")
        conn.commit()
        before_rows = list(conn.execute("SELECT id, state FROM outbox"))

        _install_fake_connect(monkeypatch, conn=conn)

        # El lock levanta busy con un holder de otro pid.
        holder = {"pid": _HOLDER_PID, "started_at": _HOLDER_STARTED_AT}
        lock = _install_fake_lock(
            monkeypatch,
            raises=SaneamientoLockBusy(holder=holder),
        )

        # El runner NO debe ser invocado.
        runner = _install_fake_runner(
            monkeypatch,
            report=_make_report(mode="apply"),  # nunca se usará
        )

        rc = cmd_saneamiento(_make_args(apply=True))

        captured = capsys.readouterr()

        # Exit code 2 (Requirement 2.7).
        assert rc == 2

        # stdout incluye pid y started_at del holder (Requirement 2.7).
        assert str(_HOLDER_PID) in captured.out
        assert _HOLDER_STARTED_AT in captured.out

        # El runner nunca se invocó (no se entró a la fase de run).
        assert runner.run_called == 0

        # Y la DB queda intacta (no hay INSERT/UPDATE/DELETE de la run).
        assert lock.acquire_called == 1
        after_rows = list(conn.execute("SELECT id, state FROM outbox"))
        assert before_rows == after_rows


# ===========================================================================
# 4) Excepción inyectada en el runner → exit 1.
#    (Requirement 1.9)
# ===========================================================================


class TestRunnerExceptionReturns1:
    """Validates: Requirement 1.9"""

    def test_runner_exception_returns_1(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _install_fake_connect(monkeypatch)
        lock = _install_fake_lock(monkeypatch, mcp_serve_active=False)
        runner = _install_fake_runner(
            monkeypatch,
            raises=RuntimeError("boom"),
        )

        rc = cmd_saneamiento(_make_args(apply=True))

        # Exit 1 cuando el runner re-lanza una excepción no controlada.
        # La emisión real del runtime_event ``saneamiento_run_failed``
        # corre por cuenta del runner (cubierto por test_runner.py
        # → TestUnhandledRunnerExceptionEmitsRunFailed). El CLI
        # solamente debe traducir el re-raise a exit code 1 sin
        # propagar la excepción al caller.
        assert rc == 1
        # Llegamos a invocar al runner (lock fue adquirido antes).
        assert lock.acquire_called == 1
        assert runner.run_called == 1


# ===========================================================================
# 5) ``connect()`` falla → exit 1, no se intenta adquirir el lock.
#    (Requirement 1.9)
# ===========================================================================


class TestConnectFailureReturns1WithoutWritingLock:
    """Validates: Requirement 1.9"""

    def test_connect_failure_returns_1_and_does_not_acquire_lock(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _install_fake_connect(
            monkeypatch,
            raises=sqlite3.OperationalError("database is locked"),
        )
        # El lock está disponible pero no debe ser tocado.
        lock = _install_fake_lock(monkeypatch)
        runner = _install_fake_runner(
            monkeypatch,
            report=_make_report(mode="apply"),  # nunca se usará
        )

        rc = cmd_saneamiento(_make_args())

        # Exit 1 antes de cualquier escritura de lock o ejecución.
        assert rc == 1
        # NO se adquirió ni se liberó el lock — el ``connect()`` falla
        # antes de la fase de lock.
        assert lock.acquire_called == 0
        assert lock.release_called == 0
        # NO se invocó al runner.
        assert runner.run_called == 0


# ===========================================================================
# 6) Convivencia con ``mcp_serve.lock`` vivo → run completa,
#    ``report.mcp_serve_active is True`` reflejado en stdout.
#    (Requirement 2.5)
# ===========================================================================


class TestMcpServeActiveIsPropagatedToReport:
    """Validates: Requirement 2.5"""

    def test_mcp_serve_active_true_propagates_into_printed_report(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _install_fake_connect(monkeypatch)

        # El lock detectó ``mcp_serve.lock`` activo, pero NO aborta
        # la run (Requirement 2.5).
        lock = _install_fake_lock(monkeypatch, mcp_serve_active=True)

        # El runner devuelve un report con ``mcp_serve_active=False``
        # (default — el runner por sí solo no conoce el estado del
        # lock). El CLI debe reescribir el campo con el valor real
        # del ``LockAcquisition`` antes de imprimir.
        _install_fake_runner(
            monkeypatch,
            report=_make_report(mode="apply", mcp_serve_active=False),
        )

        rc = cmd_saneamiento(_make_args(apply=True))

        captured = capsys.readouterr()

        # La run completó exitosamente — la concurrencia con
        # ``mcp_serve.lock`` no es motivo de aborto.
        assert rc == 0
        assert lock.acquire_called == 1
        assert lock.release_called == 1

        # El render legible muestra ``mcp_serve_active=true`` (lower
        # case por el formato definido en ``report.render_human``).
        assert "mcp_serve_active=true" in captured.out

        # Y la línea JSON parseada también reporta ``true``.
        payload = _extract_json_line(captured.out)
        assert payload["mcp_serve_active"] is True
