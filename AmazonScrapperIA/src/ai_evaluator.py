"""
ai_evaluator.py — Evalúa ofertas con kiro-cli (Claude Sonnet 4.6).

Modos de IA (en orden de prioridad):
1. kiro-cli autenticado  →  kiro-cli.exe chat --no-interactive "prompt"
2. ANTHROPIC_API_KEY     →  SDK de Anthropic directo
3. Sin IA                →  Evaluación por reglas simples
"""
import json
import time
import logging
import subprocess
import re
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ai_evaluator")

# ── Localizar kiro-cli.exe ────────────────────────────────────────────────────
_KIRO_CANDIDATES = [
    r"C:\Users\Programador Mx\AppData\Local\kiro-cli\kiro-cli.exe",
    r"C:\Users\Programador Mx\AppData\Local\Kiro-Cli\kiro-cli.exe",
]

def _find_kiro_cli() -> Optional[str]:
    for p in _KIRO_CANDIDATES:
        if Path(p).exists():
            return p
    appdata = os.environ.get("LOCALAPPDATA", "")
    for sub in ["kiro-cli", "Kiro-Cli", "Kiro-CLI"]:
        p = os.path.join(appdata, sub, "kiro-cli.exe")
        if os.path.exists(p):
            return p
    return None

def _kiro_is_authed(kiro_path: str) -> bool:
    try:
        r = subprocess.run([kiro_path, "whoami"],
                           capture_output=True, text=True, timeout=8)
        return r.returncode == 0 and r.stdout.strip() and "Not logged in" not in r.stdout
    except Exception:
        return False

KIRO_CLI   = _find_kiro_cli()
KIRO_AUTH  = _kiro_is_authed(KIRO_CLI) if KIRO_CLI else False
HAS_ANTHROPIC = bool(os.environ.get("ANTHROPIC_API_KEY"))

if KIRO_AUTH:
    _AI_MODE = "kiro"
    logger.info(f"✅ IA: kiro-cli autenticado → {KIRO_CLI}")
elif HAS_ANTHROPIC:
    _AI_MODE = "anthropic"
    logger.info("✅ IA: ANTHROPIC_API_KEY disponible")
else:
    _AI_MODE = "rules"
    logger.warning("⚠️  IA: Sin autenticación — evaluación por reglas (ejecuta setup_kiro_login.py)")

# ── Prompts ───────────────────────────────────────────────────────────────────

_EVAL_PROMPT = """\
Eres un experto cazador de ofertas en Amazon México.
Evalúa esta oferta y determina si es genuina.

PRODUCTO:
- Título: {title}
- ASIN: {asin}
- Precio actual: ${price_current} MXN
- Precio original: ${price_original} MXN
- Descuento: {discount_percent}%
- Prime: {has_prime} | Rating: {rating}/5 ({reviews} reseñas)
- Categoría: {category} | Deal badge: {has_deal} | Cupón: {has_coupon}

RESPONDE SOLO con JSON (sin markdown):
{{"verdict":"EXCELENTE|BUENA|REGULAR|DESCARTAR","score":0-100,"genuine_discount":true|false,"reasoning":"max 100 chars","emoji":"emoji","short_description":"max 120 chars","tags":[]}}"""

_BATCH_PROMPT = """\
Eres un experto cazador de ofertas en Amazon México.
Evalúa estas {count} ofertas. RESPONDE SOLO con JSON array (sin markdown):

{offers_json}

Formato por item:
{{"asin":"...","verdict":"EXCELENTE|BUENA|REGULAR|DESCARTAR","score":0-100,"genuine_discount":true|false,"reasoning":"breve","emoji":"emoji","short_description":"max 120 chars"}}"""

_DOM_PROMPT = """\
Analiza este HTML de Amazon.com.mx y extrae precios/descuentos.
HTML: {html}

RESPONDE SOLO con JSON (sin markdown):
{{"price_current":null,"price_original":null,"discount_percent":null,"discount_source":"badge|calculated|coupon","has_coupon":false,"is_lightning_deal":false,"confidence":"alta|media|baja","notes":""}}"""

_STRATEGY_PROMPT = """\
Eres experto en ofertas de Amazon México.
Estadísticas del scraper: {stats}

Sugiere estrategia mejorada. RESPONDE SOLO con JSON (sin markdown):
{{"priority_categories":[],"new_search_urls":[],"new_keywords":[],"best_hours":[],"strategy_notes":""}}"""


# ── Clase principal ───────────────────────────────────────────────────────────

class AIEvaluator:
    def __init__(self, memory_store=None, model: str = "claude-sonnet-4-5"):
        self.memory   = memory_store
        self.model    = model
        self._calls   = 0
        self._last_t  = 0.0
        self._min_gap = 1.0   # segundos entre llamadas

    # ── Throttle ─────────────────────────────────────────────────────────────

    def _throttle(self):
        gap = time.time() - self._last_t
        if gap < self._min_gap:
            time.sleep(self._min_gap - gap)
        self._last_t = time.time()

    # ── Backend de llamada ────────────────────────────────────────────────────

    def _call_ai(self, prompt: str, timeout: int = 60) -> Optional[str]:
        """Llama al backend de IA disponible y retorna texto de respuesta."""
        self._throttle()
        self._calls += 1

        if _AI_MODE == "kiro":
            return self._call_kiro_cli(prompt, timeout)
        elif _AI_MODE == "anthropic":
            return self._call_anthropic(prompt, timeout)
        else:
            return None   # modo reglas — no hay llamada

    def _call_kiro_cli(self, prompt: str, timeout: int) -> Optional[str]:
        """Usa kiro-cli.exe chat --no-interactive."""
        try:
            result = subprocess.run(
                [KIRO_CLI, "chat", "--no-interactive",
                 "--model", self.model, prompt],
                capture_output=True, text=True,
                timeout=timeout, encoding="utf-8", errors="replace"
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
            if result.stderr:
                logger.debug(f"kiro-cli stderr: {result.stderr[:300]}")
            return None
        except subprocess.TimeoutExpired:
            logger.warning(f"Timeout kiro-cli (>{timeout}s)")
            return None
        except Exception as e:
            logger.error(f"Error kiro-cli: {e}")
            return None

    def _call_anthropic(self, prompt: str, timeout: int) -> Optional[str]:
        """Usa el SDK de Anthropic directamente."""
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
            msg = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}]
            )
            return msg.content[0].text if msg.content else None
        except Exception as e:
            logger.error(f"Error Anthropic SDK: {e}")
            return None

    # ── Parseo de respuestas ──────────────────────────────────────────────────

    def _parse_obj(self, text: str) -> Optional[dict]:
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

    def _parse_arr(self, text: str) -> Optional[list]:
        if not text:
            return None
        clean = re.sub(r"```(?:json)?\n?", "", text).strip().rstrip("`").strip()
        m = re.search(r"\[.*\]", clean, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
        try:
            return json.loads(clean)
        except json.JSONDecodeError:
            return None

    # ── Evaluación por reglas (sin IA) ────────────────────────────────────────

    def _rules_evaluate(self, product: dict) -> dict:
        """Evaluación básica sin IA basada en reglas simples."""
        discount = product.get("discount_percent", 0) or 0
        rating   = product.get("rating", 0) or 0
        reviews  = product.get("reviews", 0) or 0
        title    = product.get("title", "") or ""

        score = 50
        if discount >= 70: score += 30
        elif discount >= 60: score += 20
        elif discount >= 50: score += 10

        if rating >= 4.0: score += 10
        if reviews >= 100: score += 10
        if product.get("has_prime"): score += 5
        if product.get("has_deal"): score += 5

        # Penalizar títulos sospechosos
        suspicious = ["genérico", "sin marca", "noname", "compatible"]
        if any(s in title.lower() for s in suspicious):
            score -= 20

        if score >= 80:   verdict = "EXCELENTE"
        elif score >= 65: verdict = "BUENA"
        elif score >= 50: verdict = "REGULAR"
        else:             verdict = "DESCARTAR"

        # Emoji por categoría
        cat = (product.get("category") or "").lower()
        emoji = "🛍️"
        if any(k in cat for k in ["electr", "comput", "celular", "tablet"]): emoji = "📱"
        elif any(k in cat for k in ["audio", "sonido", "headphone"]): emoji = "🎧"
        elif any(k in cat for k in ["cocina", "hogar", "kitchen"]): emoji = "🏠"
        elif any(k in cat for k in ["ropa", "zapato", "moda"]): emoji = "👗"
        elif any(k in cat for k in ["juguete", "toy", "game"]): emoji = "🎮"
        elif any(k in cat for k in ["deporte", "sport"]): emoji = "⚽"

        return {
            "verdict": verdict,
            "score": min(100, max(0, score)),
            "genuine_discount": discount >= 50,
            "reasoning": f"Reglas: {discount}% OFF, rating {rating}, {reviews} reseñas",
            "emoji": emoji,
            "short_description": f"{emoji} {title[:80]} — {discount}% OFF",
            "tags": [],
        }

    # ── API pública ───────────────────────────────────────────────────────────

    def evaluate_offer(self, product: dict) -> dict:
        """Evalúa una oferta. Usa IA si está disponible, reglas si no."""
        default = self._rules_evaluate(product)

        if _AI_MODE == "rules":
            return default

        prompt = _EVAL_PROMPT.format(
            title=str(product.get("title", ""))[:150],
            asin=product.get("asin", "N/A"),
            price_current=product.get("price_current", "N/A"),
            price_original=product.get("price_original", "N/A"),
            discount_percent=product.get("discount_percent", "N/A"),
            has_prime=product.get("has_prime", False),
            rating=product.get("rating", "N/A"),
            reviews=product.get("reviews", 0),
            category=product.get("category", "N/A"),
            has_deal=product.get("has_deal", False),
            has_coupon=product.get("has_coupon", False),
        )

        raw = self._call_ai(prompt)
        parsed = self._parse_obj(raw)
        if not parsed:
            logger.debug(f"IA no parseó respuesta para {product.get('asin')} — usando reglas")
            return default

        if self.memory:
            self.memory.record_ai_evaluation(
                asin=product.get("asin", ""),
                title=str(product.get("title", ""))[:80],
                discount=product.get("discount_percent", 0),
                ai_verdict=parsed.get("verdict", "REGULAR"),
                ai_reasoning=parsed.get("reasoning", ""),
            )

        return {**default, **parsed}

    def evaluate_batch(self, products: list) -> list:
        """Evalúa múltiples ofertas en una sola llamada."""
        if not products:
            return []

        if _AI_MODE == "rules":
            return [{"asin": p.get("asin", ""), **self._rules_evaluate(p)} for p in products]

        offers_data = [
            {
                "asin": p.get("asin", ""),
                "title": str(p.get("title", ""))[:100],
                "price_current": p.get("price_current"),
                "price_original": p.get("price_original"),
                "discount_percent": p.get("discount_percent"),
                "rating": p.get("rating"),
                "reviews": p.get("reviews", 0),
                "category": p.get("category", ""),
            }
            for p in products
        ]

        prompt = _BATCH_PROMPT.format(
            count=len(offers_data),
            offers_json=json.dumps(offers_data, ensure_ascii=False, indent=2)
        )

        raw = self._call_ai(prompt, timeout=90)
        parsed = self._parse_arr(raw)
        if not parsed:
            # Fallback individual con reglas
            return [{"asin": p.get("asin", ""), **self._rules_evaluate(p)} for p in products]
        return parsed

    def analyze_html_for_prices(self, html: str) -> Optional[dict]:
        """Analiza HTML crudo para extraer precios cuando los selectores fallan."""
        if _AI_MODE == "rules":
            return None
        raw = self._call_ai(_DOM_PROMPT.format(html=html[:5000]), timeout=30)
        return self._parse_obj(raw)

    def generate_search_strategy(self, memory_summary: dict) -> dict:
        """Genera estrategia de búsqueda mejorada basada en historial."""
        if _AI_MODE == "rules":
            return {}
        raw = self._call_ai(
            _STRATEGY_PROMPT.format(
                stats=json.dumps(memory_summary, ensure_ascii=False)[:1000]
            ),
            timeout=45
        )
        return self._parse_obj(raw) or {}

    def get_stats(self) -> dict:
        return {"total_calls": self._calls, "model": self.model, "mode": _AI_MODE}
