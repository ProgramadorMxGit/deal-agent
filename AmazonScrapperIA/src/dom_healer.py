"""
dom_healer.py — Detecta cuando los selectores CSS de Amazon fallan
y usa kiro-cli (Claude Sonnet 4.6) para proponer nuevos selectores automáticamente.
"""
import json
import time
import logging
import subprocess
import re
import os
from pathlib import Path
from collections import deque
from typing import Optional

logger = logging.getLogger("dom_healer")

SELECTORS_PATH = Path(__file__).parent.parent / "config" / "selectors.json"
HEAL_SAMPLES_DIR = Path(__file__).parent.parent / "data" / "heal_samples"

# ── Localizar kiro-cli ────────────────────────────────────────────────────────
def _find_kiro_cli() -> str:
    candidates = [
        r"C:\Users\Programador Mx\AppData\Local\kiro-cli\kiro-cli.exe",
        r"C:\Users\Programador Mx\AppData\Local\Kiro-Cli\kiro-cli.exe",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    appdata = os.environ.get("LOCALAPPDATA", "")
    for sub in ["kiro-cli", "Kiro-Cli"]:
        p = os.path.join(appdata, sub, "kiro-cli.exe")
        if os.path.exists(p):
            return p
    return ""

KIRO_CLI = _find_kiro_cli()


class DegradationMonitor:
    """
    Monitorea la tasa de fallos de extracción de precios.
    Si supera el umbral, activa DomHealer.
    """
    def __init__(self, threshold: float = 0.4, window: int = 20):
        self.threshold = threshold
        self.window = window
        self._results: deque = deque(maxlen=window)

    def record(self, success: bool):
        self._results.append(1 if success else 0)

    def is_degraded(self) -> bool:
        if len(self._results) < self.window // 2:
            return False
        failure_rate = 1 - (sum(self._results) / len(self._results))
        if failure_rate >= self.threshold:
            logger.warning(f"⚠️  Degradación: {failure_rate:.0%} fallos en {len(self._results)} páginas")
            return True
        return False

    def failure_rate(self) -> float:
        if not self._results:
            return 0.0
        return 1 - (sum(self._results) / len(self._results))

    def reset(self):
        self._results.clear()


class DomHealer:
    """
    Cuando detecta degradación de selectores, captura HTML de muestra
    y llama a kiro-cli para que Claude Sonnet 4.6 proponga nuevos selectores.
    Actualiza selectors.json automáticamente.
    """
    def __init__(self, memory_store=None):
        self.memory = memory_store
        HEAL_SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
        self._last_heal_time = 0
        self._min_heal_interval = 300  # 5 minutos entre heals

    def can_heal(self) -> bool:
        return (time.time() - self._last_heal_time) > self._min_heal_interval

    async def heal(self, page, context: str = "search") -> bool:
        """
        Captura HTML de la página actual y pide a Claude que proponga
        nuevos selectores CSS para extraer precios y descuentos.
        """
        if not self.can_heal():
            return False

        if not KIRO_CLI:
            logger.warning("DomHealer: kiro-cli no disponible — saltando heal")
            return False

        logger.info("🔧 DomHealer activado — capturando HTML de muestra...")

        try:
            html_snippet = await self._capture_relevant_html(page, context)
            if not html_snippet:
                return False

            # Guardar muestra
            sample_path = HEAL_SAMPLES_DIR / f"sample_{int(time.time())}.html"
            sample_path.write_text(html_snippet, encoding="utf-8")

            current_selectors = json.loads(SELECTORS_PATH.read_text(encoding="utf-8"))
            new_selectors = self._ask_claude_for_selectors(
                html_snippet, current_selectors, context
            )

            if new_selectors:
                old_selectors = current_selectors.copy()
                self._apply_new_selectors(current_selectors, new_selectors, context)
                SELECTORS_PATH.write_text(
                    json.dumps(current_selectors, ensure_ascii=False, indent=2),
                    encoding="utf-8"
                )
                logger.info(f"✅ DomHealer: selectores actualizados para '{context}'")

                if self.memory:
                    self.memory.record_dom_heal(
                        old_selectors.get(context, {}),
                        new_selectors,
                        f"Degradación en contexto: {context}"
                    )
                    self.memory.save()

                self._last_heal_time = time.time()
                return True

        except Exception as e:
            logger.error(f"DomHealer error: {e}")

        return False

    async def _capture_relevant_html(self, page, context: str) -> Optional[str]:
        """Captura zonas relevantes del HTML."""
        try:
            url = page.url
            snippets = []

            if context == "search":
                containers = await page.query_selector_all(
                    "[data-component-type='s-search-result'], .s-result-item"
                )
                for container in containers[:3]:
                    try:
                        html = await container.inner_html()
                        snippets.append(f"<!-- RESULTADO -->\n{html[:3000]}")
                    except Exception:
                        pass
            else:
                for zone_sel in ["#apex_offerDisplay_desktop", "#corePrice_desktop", "#price", "#centerCol"]:
                    try:
                        el = await page.query_selector(zone_sel)
                        if el:
                            html = await el.inner_html()
                            snippets.append(f"<!-- {zone_sel} -->\n{html[:4000]}")
                            break
                    except Exception:
                        pass

            if not snippets:
                body = await page.query_selector("body")
                if body:
                    snippets.append((await body.inner_html())[:8000])

            return f"<!-- URL: {url} -->\n<!-- CONTEXTO: {context} -->\n\n" + "\n\n".join(snippets)

        except Exception as e:
            logger.error(f"Error capturando HTML: {e}")
            return None

    def _ask_claude_for_selectors(
        self, html_snippet: str, current_selectors: dict, context: str
    ) -> Optional[dict]:
        """Llama a kiro-cli chat para obtener nuevos selectores CSS."""
        current_ctx = current_selectors.get(context, {})

        prompt = (
            f"Eres experto en web scraping de Amazon.com.mx. "
            f"Analiza este HTML y propón selectores CSS para: precio actual, precio original, "
            f"badge de descuento, título, link. "
            f"Contexto: {context}. "
            f"Selectores actuales fallando: {json.dumps(current_ctx)}. "
            f"HTML: {html_snippet[:4000]}. "
            f"RESPONDE SOLO con JSON (sin markdown): "
            f'{{\"price_current\":\"sel\",\"price_original\":\"sel\",\"discount_badge\":\"sel\",\"product_title\":\"sel\",\"product_link\":\"sel\"}}'
        )

        try:
            result = subprocess.run(
                [KIRO_CLI, "chat", "--no-interactive", "--model", "claude-sonnet-4-5", prompt],
                capture_output=True, text=True, timeout=60,
                encoding="utf-8", errors="replace"
            )
            if result.returncode == 0 and result.stdout:
                return self._parse_json(result.stdout.strip())
        except subprocess.TimeoutExpired:
            logger.warning("DomHealer: timeout en kiro-cli")
        except Exception as e:
            logger.error(f"DomHealer kiro-cli error: {e}")
        return None

    def _parse_json(self, text: str) -> Optional[dict]:
        if not text:
            return None
        clean = re.sub(r"```(?:json)?\n?", "", text).strip().rstrip("`").strip()
        m = re.search(r"\{.*\}", clean, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
        try:
            return json.loads(clean)
        except json.JSONDecodeError:
            return None

    def _apply_new_selectors(self, current: dict, new_selectors: dict, context: str):
        if context not in current:
            current[context] = {}
        for key, value in new_selectors.items():
            if value and isinstance(value, str):
                current[context][key] = value
        current["_last_healed"] = time.time()
        current["_heal_count"] = current.get("_heal_count", 0) + 1
