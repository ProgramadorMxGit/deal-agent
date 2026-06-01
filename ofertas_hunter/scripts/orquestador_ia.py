#!/usr/bin/env python3
"""
orquestador_ia.py — Orquestador autónomo con salida visual Rich.

Loops independientes en paralelo:
  - Amazon:     discover + hunt continuo
  - ML:         discover + hunt continuo
  - Telegram:   backfill continuo de canales configurados
  - Dispatcher: dispatch_outbox continuo (respeta cooldown 5 min)

Supervisión IA: kiro-cli analiza el rendimiento cada 10 ciclos de Amazon.

Uso:
    .\.venv\Scripts\python.exe scripts\orquestador_ia.py
    .\.venv\Scripts\python.exe scripts\orquestador_ia.py --once
"""
import asyncio
import json
import os
import subprocess
import sys
import time
import argparse
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from rich import box

def _resolve_kiro_cli() -> str:
    """Resuelve la ruta a kiro-cli según OS y entorno.

    1. Var de entorno ``KIRO_CLI`` (override explícito).
    2. Windows: ``%LOCALAPPDATA%\\Kiro-Cli\\kiro-cli.exe``.
    3. Linux/Mac: ``~/.local/bin/kiro-cli`` o ``shutil.which("kiro-cli")``.
    """
    import os as _os
    import shutil as _shutil

    override = _os.environ.get("KIRO_CLI")
    if override and Path(override).exists():
        return override
    if _sys.platform == "win32":
        candidates = [
            Path(_os.environ.get("LOCALAPPDATA", "")) / "Kiro-Cli" / "kiro-cli.exe",
            Path.home() / "AppData" / "Local" / "Kiro-Cli" / "kiro-cli.exe",
        ]
    else:
        candidates = [
            Path.home() / ".local" / "bin" / "kiro-cli",
            Path("/usr/local/bin/kiro-cli"),
            Path("/usr/bin/kiro-cli"),
        ]
    for cand in candidates:
        if cand.exists():
            return str(cand)
    on_path = _shutil.which("kiro-cli")
    if on_path:
        return on_path
    # Fallback: nombre simple para que el subprocess falle limpio
    return "kiro-cli"


# En Windows la consola legacy no soporta emojis — usamos ASCII puro
import sys as _sys
_IS_WIN_LEGACY = _sys.platform == "win32"

KIRO_CLI = _resolve_kiro_cli()
BOT_DIR  = Path(__file__).resolve().parents[1]

MCP_TIMEOUTS = {
    "discover_seeds": 60,
    "hunt_amazon": 180,
    "hunt_mercadolibre": 180,
    "process_telegram": 45,
    "dispatch_outbox": 45,
    "enrich_amazon_affiliates": 240,
    "get_frontier_stats": 30,
    "pause_marketplace": 15,
}

# Cada cuántos ciclos del dispatcher se corre el enriquecimiento de
# afiliados Amazon (los items Amazon sin afiliado válido quedan bloqueados
# por el publisher hasta ser enriquecidos).
AMAZON_AFFILIATE_ENRICH_EVERY = 1
AMAZON_AFFILIATE_ENRICH_LIMIT = 5


def _env_float(name: str, default: float) -> float:
    """Lee un float de entorno con fallback seguro (clamp a >= 0)."""
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        val = float(raw)
        return val if val >= 0 else default
    except (TypeError, ValueError):
        return default


# Throttle incondicional del loop de Mercado Libre. Sin esto, cuando el
# frontier ML está lleno (~19k URLs) y muchas se saltan rápido, el loop gira
# a ~20 ciclos/s, quemando CPU e inflando runtime_events. Con 5s el máximo
# es ~720 ciclos/h; con 10s, ~360 ciclos/h. Configurable vía .env.
ML_LOOP_SLEEP_SECONDS = _env_float("ML_LOOP_SLEEP_SECONDS", 5.0)

console = Console(highlight=False)

# Iconos: emoji en terminales modernas, ASCII en legacy Windows
_ICON_AMAZON   = "[AMZ]" if _IS_WIN_LEGACY else "🛒"
_ICON_ML       = "[ML] " if _IS_WIN_LEGACY else "🏪"
_ICON_DISPATCH = "[DSP]" if _IS_WIN_LEGACY else "📤"
_ICON_IA       = "[IA] " if _IS_WIN_LEGACY else "🤖"
_ICON_WARN     = "[!]  " if _IS_WIN_LEGACY else "⚠️ "
_ICON_STATUS   = "[ST] " if _IS_WIN_LEGACY else "📊"
_ICON_ROCKET   = "[>>]" if _IS_WIN_LEGACY else "🚀"
_ICON_PUBLISH  = "[OK]" if _IS_WIN_LEGACY else "✅"

# Contadores globales
_stats = {
    "amazon_cycles": 0,
    "ml_cycles": 0,
    "telegram_cycles": 0,
    "dispatch_cycles": 0,
    "amazon_enqueued": 0,
    "ml_enqueued": 0,
    "telegram_enqueued": 0,
    "dispatched": 0,
    "amazon_captchas": 0,
    "start_time": time.time(),
}


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log_amazon(msg: str, style: str = "green") -> None:
    console.print(f"  [bold {style}]{_ICON_AMAZON} [Amazon][/bold {style}] {msg}")


def log_ml(msg: str, style: str = "yellow") -> None:
    console.print(f"  [bold {style}]{_ICON_ML} [ML][/bold {style}]     {msg}")


def log_dispatch(msg: str, style: str = "cyan") -> None:
    console.print(f"  [bold {style}]{_ICON_DISPATCH} [Dispatch][/bold {style}] {msg}")


def log_ia(msg: str) -> None:
    console.print(f"\n  [bold magenta]{_ICON_IA} [IA][/bold magenta] {msg}\n")


def log_warn(msg: str) -> None:
    console.print(f"  [bold red]{_ICON_WARN} [WARN][/bold red] {msg}")


async def dispatch_mcp(server, tool: str, args: dict) -> dict:
    """Ejecuta una tool MCP sin dejar que bloquee indefinidamente un loop."""
    timeout = MCP_TIMEOUTS.get(tool, 60)
    try:
        return await asyncio.wait_for(server.dispatch(tool, args), timeout=timeout)
    except asyncio.TimeoutError:
        log_warn(f"{tool}: timeout tras {timeout}s; se reintentara en el proximo ciclo")
        return {"error": "timeout", "tool": tool, "timeout_seconds": timeout}


def log_status() -> None:
    elapsed = int(time.time() - _stats["start_time"])
    h, m = divmod(elapsed // 60, 60)
    s = elapsed % 60
    uptime = f"{h:02d}:{m:02d}:{s:02d}"

    table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    table.add_column(style="dim")
    table.add_column(style="bold white")
    table.add_column(style="dim")
    table.add_column(style="bold white")

    table.add_row("Uptime", uptime, "Publicadas", str(_stats["dispatched"]))
    table.add_row(
        "Amazon ciclos", str(_stats["amazon_cycles"]),
        "Amazon encoladas", str(_stats["amazon_enqueued"]),
    )
    table.add_row(
        "ML ciclos", str(_stats["ml_cycles"]),
        "ML encoladas", str(_stats["ml_enqueued"]),
    )
    table.add_row(
        "Telegram ciclos", str(_stats["telegram_cycles"]),
        "Telegram encoladas", str(_stats["telegram_enqueued"]),
    )
    if _stats["amazon_captchas"]:
        table.add_row("CAPTCHAs Amazon", str(_stats["amazon_captchas"]), "", "")

    console.print(Panel(table, title=f"[bold cyan]{_ICON_STATUS} STATUS {_ts()}[/bold cyan]", border_style="cyan"))


def _kiro_is_authed() -> bool:
    try:
        r = subprocess.run([KIRO_CLI, "whoami"], capture_output=True, text=True, timeout=8)
        return r.returncode == 0 and r.stdout.strip() and "Not logged in" not in r.stdout
    except Exception:
        return False


def ask_kiro_agent(prompt: str, agent: str = "ofertas-orquestador", timeout: int = 90) -> str:
    """Llama a kiro-cli con agente + trust-all-tools + no-interactive.
    
    Replica exactamente el patrón del legacy run_bot.ps1:
        kiro-cli chat --agent amazon-hunter --trust-all-tools --no-interactive $prompt
    
    Con este patrón el agente tiene acceso a las tools MCP y las usa directamente.
    """
    try:
        result = subprocess.run(
            [KIRO_CLI, "--classic", "chat", "--agent", agent, "--trust-all-tools", "--no-interactive", prompt],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace", cwd=str(BOT_DIR),
        )
        if result.returncode == 0 and result.stdout.strip():
            # Filtrar líneas de ruido (igual que run_bot.ps1)
            lines = []
            for line in result.stdout.splitlines():
                if any(x in line for x in [
                    "RemoteException", "CategoryInfo", "FullyQualified",
                    "trusted", "risks", "kiro.dev", "Agents can",
                    "Welcome to", "Prefer the classic",
                ]):
                    continue
                if line.strip():
                    lines.append(line)
            return "\n".join(lines).strip()
        return ""
    except subprocess.TimeoutExpired:
        return "[Timeout kiro-cli]"
    except Exception as e:
        return f"[Error: {e}]"


def ask_kiro(prompt: str, timeout: int = 30) -> str:
    """Llamada simple sin agente — para análisis rápido."""
    try:
        result = subprocess.run(
            [KIRO_CLI, "--classic", "chat", "--no-interactive", prompt],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace", cwd=str(BOT_DIR),
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Loop Amazon
# ---------------------------------------------------------------------------

async def loop_amazon(server, once: bool) -> None:
    log_amazon("Loop arrancado", "green")
    while True:
        try:
            disc = await dispatch_mcp(server, "discover_seeds", {"marketplace": "amazon", "limit": 4})
            persisted = disc.get("persisted", 0)
            if persisted:
                log_amazon(f"+{persisted} URLs al frontier", "green")

            hunt = await dispatch_mcp(server, "hunt_amazon", {"limit": 5})
            enqueued = hunt.get("enqueued", 0)
            processed = hunt.get("processed", 0)
            _stats["amazon_cycles"] += 1
            _stats["amazon_enqueued"] += enqueued

            if processed > 0:
                enq_str = f"[bold green]+{enqueued} encoladas[/bold green]" if enqueued > 0 else f"encoladas={enqueued}"
                console.print(
                    f"  [bold green]{_ICON_AMAZON} [Amazon][/bold green] ciclo [cyan]#{_stats['amazon_cycles']}[/cyan]"
                    f"  procesados=[white]{processed}[/white]  {enq_str}"
                )

            # Auto-pausa SOLO con captcha real high-confidence.
            #
            # Antes contábamos cualquier `discarded_reason == "captcha_detected"`,
            # lo que provocaba pausa de 10min cada ciclo cuando el detector
            # marcaba 5 URLs en una ronda — incluso si eran captchas
            # reales que el bot debería seguir intentando con backoff
            # corto en vez de detenerse 10min.
            #
            # Nueva regla (alineada con AmazonCaptchaDetector central):
            # - sólo cuentan los outcomes con
            #   `captcha_should_pause_marketplace=True` (high confidence
            #   estructural + visible).
            # - umbral mínimo: 2 captchas REALES en la misma ronda.
            outcomes = hunt.get("outcomes", [])
            real_high_captchas = [
                o for o in outcomes
                if o.get("captcha_should_pause_marketplace") is True
                and (o.get("captcha_confidence") or "") == "high"
            ]
            suspected_captchas = [
                o for o in outcomes
                if o.get("discarded_reason") == "captcha_detected"
                and o not in real_high_captchas
            ]
            if suspected_captchas:
                # Sólo log, no pausa.
                log_warn(
                    f"Amazon: {len(suspected_captchas)} captcha(s) sospechoso(s) "
                    "— sin pausar (confidence != high o sin should_pause)"
                )

            if len(real_high_captchas) >= 2:
                _stats["amazon_captchas"] += len(real_high_captchas)
                log_warn(
                    f"Amazon: {len(real_high_captchas)} CAPTCHAs reales "
                    "high-confidence — pausando 10 min"
                )
                await dispatch_mcp(server, "pause_marketplace", {
                    "marketplace": "amazon",
                    "reason": "captcha_burst",
                    "ttl_seconds": 600,
                })
                await asyncio.sleep(600)
            elif len(real_high_captchas) == 1:
                # 1 captcha real: backoff corto sin pausa global. Da margen
                # a que la sesión persistente del operador siga válida en
                # los siguientes ciclos.
                _stats["amazon_captchas"] += 1
                log_warn("Amazon: 1 captcha real — backoff corto 60s (sin pausa global)")
                await asyncio.sleep(60)

            frontier = await dispatch_mcp(server, "get_frontier_stats", {"marketplace": "amazon"})
            if frontier.get("total", 0) == 0:
                await asyncio.sleep(30)

        except asyncio.CancelledError:
            break
        except Exception as e:
            log_warn(f"Amazon ERROR: {e}")
            await asyncio.sleep(5)

        if once:
            break


# ---------------------------------------------------------------------------
# Loop ML
# ---------------------------------------------------------------------------

async def loop_ml(server, once: bool) -> None:
    log_ml("Loop arrancado", "yellow")
    while True:
        try:
            disc = await dispatch_mcp(server, "discover_seeds", {"marketplace": "mercadolibre", "limit": 4})
            persisted = disc.get("persisted", 0)
            if persisted:
                log_ml(f"+{persisted} URLs al frontier", "yellow")

            hunt = await dispatch_mcp(server, "hunt_mercadolibre", {"limit": 5})

            if hunt.get("skipped") and hunt.get("reason") == "ml_paused_for_login":
                log_warn("ML: cookies expiradas — pausando 30 min")
                await asyncio.sleep(1800)
                continue

            enqueued = hunt.get("enqueued", 0)
            processed = hunt.get("processed", 0)
            _stats["ml_cycles"] += 1
            _stats["ml_enqueued"] += enqueued

            if processed > 0:
                enq_str = f"[bold green]+{enqueued} encoladas[/bold green]" if enqueued > 0 else f"encoladas={enqueued}"
                console.print(
                    f"  [bold yellow]{_ICON_ML} [ML][/bold yellow]     ciclo [cyan]#{_stats['ml_cycles']}[/cyan]"
                    f"  procesados=[white]{processed}[/white]  {enq_str}"
                )

            frontier = await dispatch_mcp(server, "get_frontier_stats", {"marketplace": "mercadolibre"})
            if frontier.get("total", 0) == 0:
                await asyncio.sleep(30)

        except asyncio.CancelledError:
            break
        except Exception as e:
            log_warn(f"ML ERROR: {e}")
            await asyncio.sleep(5)

        if once:
            break

        # Throttle incondicional: evita el runaway del loop ML cuando el
        # frontier no está vacío (ver ML_LOOP_SLEEP_SECONDS). No afecta
        # Amazon ni Telegram. Si es 0, no duerme (no recomendado).
        if ML_LOOP_SLEEP_SECONDS > 0:
            await asyncio.sleep(ML_LOOP_SLEEP_SECONDS)


# ---------------------------------------------------------------------------
# Loop Telegram
# ---------------------------------------------------------------------------

async def loop_telegram(server, once: bool) -> None:
    log_dispatch("Telegram loop arrancado", "magenta")
    while True:
        try:
            result = await dispatch_mcp(server, "process_telegram", {"limit": 80, "budget": 40})
            if result.get("skipped"):
                log_warn(f"Telegram: skipped={result.get('reason')}")
                return
            if result.get("error"):
                log_warn(f"Telegram ERROR: {result.get('error')} {result.get('detail', '')}")
                await asyncio.sleep(60)
            else:
                enqueued = int(result.get("enqueued", 0) or 0)
                _stats["telegram_cycles"] += 1
                _stats["telegram_enqueued"] += enqueued
                if result.get("processed", 0):
                    console.print(
                        f"  [bold magenta][TG]  [Telegram][/bold magenta] "
                        f"ciclo [cyan]#{_stats['telegram_cycles']}[/cyan] "
                        f"procesados=[white]{result.get('processed', 0)}[/white] "
                        f"actionable={result.get('actionable', 0)} "
                        f"duplicates={result.get('duplicates', 0)} "
                        f"[bold green]+{enqueued} encoladas[/bold green]"
                    )

        except asyncio.CancelledError:
            break
        except Exception as e:
            log_warn(f"Telegram ERROR: {e}")
            await asyncio.sleep(30)

        if once:
            break
        await asyncio.sleep(30)


# ---------------------------------------------------------------------------
# Loop Dispatcher
# ---------------------------------------------------------------------------

async def loop_dispatcher(server, once: bool) -> None:
    log_dispatch("Loop arrancado", "cyan")
    while True:
        try:
            # Fase de afiliados: antes de despachar, enriquecer items Amazon
            # `pending` sin affiliate_url. El publisher BLOQUEA Amazon sin
            # afiliado válido, así que sin esta fase esos items nunca salen.
            if _stats["dispatch_cycles"] % AMAZON_AFFILIATE_ENRICH_EVERY == 0:
                try:
                    enr = await dispatch_mcp(
                        server,
                        "enrich_amazon_affiliates",
                        {"limit": AMAZON_AFFILIATE_ENRICH_LIMIT},
                    )
                    if enr.get("success") and enr.get("enriched"):
                        console.print(
                            f"  [bold cyan]{_ICON_DISPATCH} [Afiliados][/bold cyan] "
                            f"Amazon enriquecidos=[bold green]{enr.get('enriched')}[/bold green] "
                            f"fallidos={enr.get('failed', 0)} "
                            f"candidatos={enr.get('total_candidates', 0)}"
                        )
                except Exception as e:  # noqa: BLE001 - nunca congelar el loop
                    log_warn(f"Afiliados Amazon ERROR: {e}")

            result = await dispatch_mcp(server, "dispatch_outbox", {"limit": 3})
            ticks = result.get("ticks", 0)
            if ticks > 0:
                _stats["dispatched"] += ticks
                console.print(
                    f"  [bold cyan]{_ICON_DISPATCH} [Dispatch][/bold cyan] "
                    f"[bold green]{ticks} oferta(s) publicada(s)[/bold green] "
                    f"- total=[bold white]{_stats['dispatched']}[/bold white]"
                )
            else:
                if _stats["dispatch_cycles"] < 3 or _stats["dispatch_cycles"] % 6 == 0:
                    log_dispatch(
                        f"ciclo #{_stats['dispatch_cycles'] + 1}: sin publicacion "
                        "(cooldown, sin elegibles o selector sin candidato)",
                        "dim",
                    )
                await asyncio.sleep(10)
            _stats["dispatch_cycles"] += 1

        except asyncio.CancelledError:
            break
        except Exception as e:
            log_warn(f"Dispatcher ERROR: {e}")
            await asyncio.sleep(5)

        if once:
            break


# ---------------------------------------------------------------------------
# Loop supervisión IA
# ---------------------------------------------------------------------------

async def loop_ia_supervision(server, once: bool) -> None:
    """Supervisión IA cada 10 ciclos.
    
    kiro-cli en modo --no-interactive NO tiene acceso a tools MCP.
    Usamos ask_kiro (sin agente) para análisis de texto simple,
    y ejecutamos las acciones directamente via el server MCP de Python.
    """
    last_amazon_cycles = 0
    while True:
        await asyncio.sleep(60)
        if once:
            break
        try:
            current = _stats["amazon_cycles"]
            if current - last_amazon_cycles >= 10:
                last_amazon_cycles = current

                # Ejecutar dispatch directamente (no necesita IA)
                try:
                    dispatch = await dispatch_mcp(server, "dispatch_outbox", {"limit": 3})
                    ticks = dispatch.get("ticks", 0)
                    if ticks > 0:
                        _stats["dispatched"] += ticks
                        log_dispatch(f"[IA-ciclo] {ticks} oferta(s) publicada(s) - total={_stats['dispatched']}")
                except Exception:
                    pass

                # Análisis de texto con kiro-cli (sin MCP, solo texto)
                summary = json.dumps({
                    "amazon_cycles": _stats["amazon_cycles"],
                    "ml_cycles": _stats["ml_cycles"],
                    "amazon_enqueued": _stats["amazon_enqueued"],
                    "ml_enqueued": _stats["ml_enqueued"],
                    "dispatched": _stats["dispatched"],
                    "amazon_captchas": _stats["amazon_captchas"],
                }, ensure_ascii=False)
                prompt = (
                    f"Supervisor del bot ofertas_hunter. Stats: {summary}. "
                    f"En 1 oracion: algo inusual o accion recomendada? Solo texto plano."
                )
                resp = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: ask_kiro(prompt, timeout=20)
                )
                if resp and not resp.startswith("["):
                    log_ia(resp[:200])
        except asyncio.CancelledError:
            break
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Loop status periódico
# ---------------------------------------------------------------------------

async def loop_status(server, once: bool) -> None:
    while True:
        await asyncio.sleep(300)
        if once:
            break
        try:
            log_status()
        except asyncio.CancelledError:
            break
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main_async(once: bool) -> None:
    from ofertas_hunter.config import get_settings
    from ofertas_hunter.db import connect, init_db
    from ofertas_hunter.mcp.context import ServerContext
    from ofertas_hunter.mcp.server import MCPServer
    from ofertas_hunter.session.ml_session_runtime import MLSessionRecoveryRuntime

    s = get_settings()
    init_db()
    conn = connect()

    # Aplica el throttle ML desde config/.env (pydantic lee .env aunque no
    # esté exportado al entorno). El default del módulo es 5s; aquí se
    # respeta `ml_loop_sleep_seconds` si está definido en config/.env.
    global ML_LOOP_SLEEP_SECONDS
    try:
        cfg_sleep = float(getattr(s, "ml_loop_sleep_seconds", ML_LOOP_SLEEP_SECONDS))
        if cfg_sleep >= 0:
            ML_LOOP_SLEEP_SECONDS = cfg_sleep
    except (TypeError, ValueError):
        pass
    ctx = ServerContext.build(db=conn, settings=s)
    server = MCPServer(ctx)

    # ── ML Session Recovery: monitor + webhook entrante ──────────────────
    # Detecta cookie_expiry → avisa al admin via WhatsApp → recibe JSON
    # de cookies → hot-reload sin reiniciar el bot.
    async def _evolution_send(number: str, text: str) -> bool:
        """Wrapper para mandar WhatsApp al admin via Evolution API."""
        try:
            client = ctx.get_evolution_client()
            resp = await client.send_text(number, text)
            return bool(resp.success)
        except Exception as exc:
            log_warn(f"ML Recovery: send_text falló: {exc}")
            return False

    ml_recovery = MLSessionRecoveryRuntime.build(
        settings=s,
        db=conn,
        evolution_send=_evolution_send,
        ctx=ctx,
    )
    await ml_recovery.start()

    # Header de inicio
    console.print()
    console.print(Panel(
        Text.assemble(
            (f"{_ICON_ROCKET} Arrancando orquestador IA...\n", "bold white"),
            ("Cuenta: ", "dim"), (f"{s.evolution_api_key[:8]}...\n" if s.evolution_api_key else "N/A\n", "dim"),
            ("MCP tools: ", "dim"), (f"{len(server.registry)}\n", "bold cyan"),
            ("Publishing: ", "dim"), (
                "enabled\n" if s.publishing_enabled else "disabled (dry-run)\n",
                "bold green" if s.publishing_enabled else "bold yellow"
            ),
            ("Scheduler: ", "dim"), (ctx.scheduler.decide().mode.value, "bold green"),
            ("\nML throttle: ", "dim"), (f"{ML_LOOP_SLEEP_SECONDS:g}s/ciclo", "bold cyan"),
        ),
        title="[bold cyan]OFERTAS HUNTER - Orquestador IA[/bold cyan]",
        subtitle="[dim]Amazon + ML + Telegram + Dispatch[/dim]",
        border_style="cyan",
        padding=(0, 2),
    ))
    console.print()

    async def loop_ml_recovery(once: bool) -> None:
        """Loop que llama a monitor.tick() cada 30s para detectar
        cookie_expiry y avisar al admin via WhatsApp."""
        while True:
            try:
                result = await ml_recovery.monitor_tick()
                if result == "ml_session_admin_alerted":
                    log_warn("ML Recovery: cookies expiradas — aviso enviado al admin via WhatsApp")
            except Exception as exc:
                log_warn(f"ML Recovery monitor error: {exc}")
            if once:
                break
            await asyncio.sleep(30)

    try:
        if once:
            await asyncio.gather(
                loop_amazon(server, once=True),
                loop_ml(server, once=True),
                loop_telegram(server, once=True),
                loop_dispatcher(server, once=True),
            )
        else:
            await asyncio.gather(
                loop_amazon(server, once=False),
                loop_ml(server, once=False),
                loop_telegram(server, once=False),
                loop_dispatcher(server, once=False),
                loop_ia_supervision(server, once=False),
                loop_status(server, once=False),
                loop_ml_recovery(once=False),
            )
    finally:
        await ml_recovery.stop()
        await ctx.aclose()
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Orquestador IA del bot ofertas_hunter")
    parser.add_argument("--once", action="store_true", help="Un ciclo y terminar")
    args = parser.parse_args()

    # Verificar kiro-cli (patrón del legacy)
    kiro_authed = _kiro_is_authed()
    if kiro_authed:
        r = subprocess.run([KIRO_CLI, "whoami"], capture_output=True, text=True, timeout=5)
        console.print(f"[dim]kiro-cli: OK - {r.stdout.strip()[:60]}[/dim]")
        console.print(f"[dim]Supervision IA: activa (agente ofertas-orquestador cada 10 ciclos)[/dim]")
    else:
        console.print("[dim]kiro-cli: no autenticado - ejecuta 'kiro-cli login' primero[/dim]")
        console.print("[dim]Supervision IA: desactivada (bot corre igual sin ella)[/dim]")

    try:
        asyncio.run(main_async(once=args.once))
    except KeyboardInterrupt:
        console.print("\n[bold red]⏹  Detenido por usuario.[/bold red]")


if __name__ == "__main__":
    main()
