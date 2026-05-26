"""Runtime watchdog para los agentes del bot.

Filosofía:

- Cada **task supervisado** envuelve una factory `() -> Awaitable[None]` que
  representa el loop del agente (hunter, listener, dispatcher, ...). El task
  hace `await registry.heartbeat(handle)` periódicamente.
- El watchdog corre en su propio task: cada `poll_interval_seconds` revisa
  el `last_heartbeat` de cada supervisado.
- Si `now - last_heartbeat > stale_after_seconds`, el watchdog cancela el
  task actual, espera un backoff y lo reinicia con la factory.
- Si el agente cae o reinicia más de `max_restarts_in_window` veces en
  `restart_window_seconds`, el watchdog **degrada** ese agente:
  - llama a `agent_runs(status="error")`,
  - emite `runtime_event(severity=critical)`,
  - deja el task en estado `degraded` y NO lo reinicia.

El watchdog es testeable: clock inyectable + asyncio. Los tests pueden
forzar fallos en la factory para validar el comportamiento.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Deque, Optional

from .events import emit_runtime_event
from .heartbeat import AgentRunHandle, AgentRunRegistry


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tipo de factory
# ---------------------------------------------------------------------------


# La factory recibe el handle de la corrida actual y el registry (con el
# clock inyectado por el watchdog), para que el agente pueda llamar a
# `registry.heartbeat(handle)` sin construir su propio registry.
AgentFactory = Callable[[AgentRunHandle, AgentRunRegistry], Awaitable[None]]


# ---------------------------------------------------------------------------
# Estado por agente supervisado
# ---------------------------------------------------------------------------


@dataclass
class SupervisedAgent:
    name: str
    factory: AgentFactory
    handle: Optional[AgentRunHandle] = None
    task: Optional[asyncio.Task] = None
    restart_count: int = 0
    restart_timestamps: Deque[float] = field(default_factory=lambda: deque(maxlen=10))
    degraded: bool = False
    last_failure_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------


@dataclass
class WatchdogConfig:
    poll_interval_seconds: float = 30.0
    stale_after_seconds: float = 60.0
    backoff_seconds: tuple[float, ...] = (5.0, 15.0, 60.0)
    max_restarts_in_window: int = 5
    restart_window_seconds: float = 600.0


class RuntimeWatchdog:
    """Supervisor mínimo de agentes."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        config: Optional[WatchdogConfig] = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.db = conn
        self.config = config or WatchdogConfig()
        self._clock = clock
        self.registry = AgentRunRegistry(conn, clock=clock)
        self._agents: dict[str, SupervisedAgent] = {}
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------
    # Registro de agentes
    # ------------------------------------------------------------------

    def register(self, name: str, factory: AgentFactory) -> SupervisedAgent:
        if name in self._agents:
            raise ValueError(f"agent ya registrado: {name}")
        agent = SupervisedAgent(name=name, factory=factory)
        self._agents[name] = agent
        return agent

    def get(self, name: str) -> SupervisedAgent:
        return self._agents[name]

    @property
    def agents(self) -> dict[str, SupervisedAgent]:
        return dict(self._agents)

    # ------------------------------------------------------------------
    # Lifecycle de tasks
    # ------------------------------------------------------------------

    async def start_all(self) -> None:
        for name in list(self._agents):
            await self._spawn(name)

    async def _spawn(self, name: str) -> None:
        agent = self._agents[name]
        if agent.degraded:
            return
        handle = self.registry.start(name)
        agent.handle = handle
        agent.task = asyncio.create_task(self._wrap(agent), name=f"agent:{name}")

    async def _wrap(self, agent: SupervisedAgent) -> None:
        """Ejecuta la factory y captura excepciones para no matar el watchdog."""
        assert agent.handle is not None
        try:
            await agent.factory(agent.handle, self.registry)
            self.registry.finish(agent.handle, status="ok")
        except asyncio.CancelledError:
            self.registry.finish(agent.handle, status="killed")
            raise
        except Exception as exc:
            agent.last_failure_reason = f"{type(exc).__name__}: {exc}"
            logger.warning("agent %s falló: %s", agent.name, exc)
            self.registry.finish(
                agent.handle,
                status="error",
                summary={"error": agent.last_failure_reason},
            )

    # ------------------------------------------------------------------
    # Tick principal
    # ------------------------------------------------------------------

    async def tick(self) -> None:
        """Una pasada de supervisión. Reinicia tasks caídos / con heartbeat
        viejo, degrada los que excedan el límite.
        """
        now = self._clock()
        for name, agent in self._agents.items():
            if agent.degraded:
                continue
            await self._supervise_one(name, agent, now)

    async def _supervise_one(
        self, name: str, agent: SupervisedAgent, now: datetime
    ) -> None:
        # 1) ¿El task murió?
        task = agent.task
        if task is None or task.done():
            await self._restart_agent(agent, reason="task_done", now=now)
            return

        # 2) ¿Heartbeat viejo?
        last = self.registry.last_heartbeat(name)
        if last is None:
            return  # aún no se registró, dejar pasar este ciclo
        elapsed = (now - last).total_seconds()
        if elapsed > self.config.stale_after_seconds:
            await self._restart_agent(
                agent, reason=f"heartbeat_stale_{int(elapsed)}s", now=now
            )

    # ------------------------------------------------------------------
    # Restart / degrade
    # ------------------------------------------------------------------

    async def _restart_agent(
        self, agent: SupervisedAgent, *, reason: str, now: datetime
    ) -> None:
        # Cancela el task previo si sigue vivo.
        if agent.task is not None and not agent.task.done():
            agent.task.cancel()
            try:
                await asyncio.wait_for(agent.task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass

        # Registra restart
        agent.restart_timestamps.append(time.time())
        agent.restart_count += 1

        # Cuenta restarts dentro de la ventana
        window_start = time.time() - self.config.restart_window_seconds
        recent = sum(1 for ts in agent.restart_timestamps if ts >= window_start)
        if recent > self.config.max_restarts_in_window:
            agent.degraded = True
            agent.last_failure_reason = (
                f"too_many_restarts ({recent} in {int(self.config.restart_window_seconds)}s)"
            )
            emit_runtime_event(
                self.db,
                kind="agent_degraded",
                severity="critical",
                payload={
                    "agent": agent.name,
                    "restart_count": agent.restart_count,
                    "reason": reason,
                    "window_seconds": self.config.restart_window_seconds,
                    "max_restarts": self.config.max_restarts_in_window,
                },
            )
            logger.error(
                "agent %s degradado tras %d restarts en %ds",
                agent.name,
                recent,
                int(self.config.restart_window_seconds),
            )
            return

        # Backoff exponencial acotado
        backoff_idx = min(agent.restart_count - 1, len(self.config.backoff_seconds) - 1)
        backoff = self.config.backoff_seconds[max(0, backoff_idx)]
        emit_runtime_event(
            self.db,
            kind="agent_restart",
            severity="warning",
            payload={
                "agent": agent.name,
                "reason": reason,
                "restart_count": agent.restart_count,
                "backoff_seconds": backoff,
            },
        )
        logger.info(
            "agent %s reinicio (motivo=%s, restart_count=%d, backoff=%.1fs)",
            agent.name,
            reason,
            agent.restart_count,
            backoff,
        )
        # En tests, evitamos el sleep usando el flag (clock = test clock no avanza).
        # En producción, el sleep da tiempo a que cese una causa transitoria.
        if backoff > 0 and not self._is_test_mode():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass

        await self._spawn(agent.name)

    def _is_test_mode(self) -> bool:
        # Si el clock fue inyectado y no es el wall clock real, asumimos test.
        return self._clock is not (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------
    # Loop público + shutdown
    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        logger.info(
            "RuntimeWatchdog arrancando (poll=%.1fs, stale=%.1fs)",
            self.config.poll_interval_seconds,
            self.config.stale_after_seconds,
        )
        try:
            while not self._stop.is_set():
                try:
                    await self.tick()
                except Exception as exc:
                    logger.exception("watchdog tick falló: %s", exc)
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self.config.poll_interval_seconds
                    )
                except asyncio.TimeoutError:
                    pass
        finally:
            await self.shutdown_all()

    async def stop(self) -> None:
        self._stop.set()

    async def shutdown_all(self) -> None:
        for agent in self._agents.values():
            if agent.task and not agent.task.done():
                agent.task.cancel()
                try:
                    await asyncio.wait_for(agent.task, timeout=5.0)
                except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                    pass


__all__ = [
    "AgentFactory",
    "RuntimeWatchdog",
    "SupervisedAgent",
    "WatchdogConfig",
]
