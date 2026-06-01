"""
memory_store.py — Memoria persistente del scraper.
Aprende qué categorías tienen más ofertas, qué selectores funcionan,
qué productos ya fueron vistos, y mejora la estrategia con el tiempo.
"""
import json
import time
import logging
from pathlib import Path
from typing import Optional
from collections import defaultdict

logger = logging.getLogger("memory_store")


class MemoryStore:
    def __init__(self, memory_path: str):
        self.path = Path(memory_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"Error cargando memoria: {e} — iniciando nueva")
        return self._default_structure()

    def _default_structure(self) -> dict:
        return {
            "version": 2,
            "created_at": time.time(),
            "updated_at": time.time(),
            "stats": {
                "total_pages_visited": 0,
                "total_products_evaluated": 0,
                "total_offers_found": 0,
                "total_ai_calls": 0,
                "total_dom_heals": 0,
                "sessions": 0,
            },
            "category_performance": {},
            "selector_failures": {},
            "selector_successes": {},
            "best_seeds": [],
            "worst_seeds": [],
            "seen_asins": [],
            "seen_urls": [],
            "ai_evaluations": [],
            "dom_heal_history": [],
            "learned_patterns": {
                "high_discount_keywords": [],
                "low_quality_patterns": [],
                "best_categories": [],
                "best_time_windows": [],
            },
            "session_history": [],
        }

    def save(self):
        self._data["updated_at"] = time.time()
        try:
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception as e:
            logger.error(f"Error guardando memoria: {e}")

    # ── Visitas ──────────────────────────────────────────────────────────────

    def mark_url_visited(self, url: str):
        seen = self._data["seen_urls"]
        if url not in seen:
            seen.append(url)
            # Mantener solo los últimos 5000
            if len(seen) > 5000:
                self._data["seen_urls"] = seen[-5000:]
        self._data["stats"]["total_pages_visited"] += 1

    def is_url_visited(self, url: str) -> bool:
        return url in self._data["seen_urls"]

    def mark_asin_seen(self, asin: str):
        seen = self._data["seen_asins"]
        if asin not in seen:
            seen.append(asin)
            if len(seen) > 10000:
                self._data["seen_asins"] = seen[-10000:]

    def is_asin_seen(self, asin: str) -> bool:
        return asin in self._data["seen_asins"]

    # ── Estadísticas ─────────────────────────────────────────────────────────

    def increment_stat(self, key: str, amount: int = 1):
        if key in self._data["stats"]:
            self._data["stats"][key] += amount

    def get_stat(self, key: str) -> int:
        return self._data["stats"].get(key, 0)

    # ── Rendimiento por categoría ─────────────────────────────────────────────

    def record_category_result(self, category: str, found_offer: bool, discount: Optional[int] = None):
        if category not in self._data["category_performance"]:
            self._data["category_performance"][category] = {
                "visits": 0,
                "offers_found": 0,
                "total_discount": 0,
                "avg_discount": 0,
                "score": 0.0,
            }
        cat = self._data["category_performance"][category]
        cat["visits"] += 1
        if found_offer:
            cat["offers_found"] += 1
            if discount:
                cat["total_discount"] += discount
        if cat["visits"] > 0:
            cat["avg_discount"] = cat["total_discount"] / max(cat["offers_found"], 1)
            # Score = tasa de éxito * descuento promedio normalizado
            success_rate = cat["offers_found"] / cat["visits"]
            cat["score"] = success_rate * (cat["avg_discount"] / 100)

    def get_best_categories(self, top_n: int = 10) -> list:
        cats = self._data["category_performance"]
        sorted_cats = sorted(cats.items(), key=lambda x: x[1]["score"], reverse=True)
        return [c[0] for c in sorted_cats[:top_n]]

    # ── Rendimiento de seeds ──────────────────────────────────────────────────

    def record_seed_result(self, seed_url: str, offers_found: int, pages_visited: int):
        seeds = self._data.get("seed_performance", {})
        if seed_url not in seeds:
            seeds[seed_url] = {"total_offers": 0, "total_pages": 0, "runs": 0, "efficiency": 0.0}
        s = seeds[seed_url]
        s["total_offers"] += offers_found
        s["total_pages"] += pages_visited
        s["runs"] += 1
        s["efficiency"] = s["total_offers"] / max(s["total_pages"], 1)
        self._data["seed_performance"] = seeds

    def get_best_seeds(self, top_n: int = 15) -> list:
        seeds = self._data.get("seed_performance", {})
        sorted_seeds = sorted(seeds.items(), key=lambda x: x[1]["efficiency"], reverse=True)
        return [s[0] for s in sorted_seeds[:top_n]]

    # ── Selectores ───────────────────────────────────────────────────────────

    def record_selector_result(self, selector: str, success: bool):
        key = "selector_successes" if success else "selector_failures"
        if selector not in self._data[key]:
            self._data[key][selector] = 0
        self._data[key][selector] += 1

    def get_failing_selectors(self, min_failures: int = 5) -> list:
        return [
            sel for sel, count in self._data["selector_failures"].items()
            if count >= min_failures
        ]

    # ── Evaluaciones IA ──────────────────────────────────────────────────────

    def record_ai_evaluation(self, asin: str, title: str, discount: int,
                              ai_verdict: str, ai_reasoning: str):
        self._data["ai_evaluations"].append({
            "ts": time.time(),
            "asin": asin,
            "title": title[:80],
            "discount": discount,
            "verdict": ai_verdict,
            "reasoning": ai_reasoning[:200],
        })
        # Mantener solo los últimos 500
        if len(self._data["ai_evaluations"]) > 500:
            self._data["ai_evaluations"] = self._data["ai_evaluations"][-500:]
        self._data["stats"]["total_ai_calls"] += 1

    def get_recent_evaluations(self, n: int = 20) -> list:
        return self._data["ai_evaluations"][-n:]

    # ── DOM Healing ──────────────────────────────────────────────────────────

    def record_dom_heal(self, old_selectors: dict, new_selectors: dict, reason: str):
        self._data["dom_heal_history"].append({
            "ts": time.time(),
            "reason": reason,
            "old": old_selectors,
            "new": new_selectors,
        })
        self._data["stats"]["total_dom_heals"] += 1

    # ── Patrones aprendidos ───────────────────────────────────────────────────

    def learn_keyword(self, keyword: str, category: str = "high_discount"):
        key = "high_discount_keywords" if category == "high_discount" else "low_quality_patterns"
        if keyword not in self._data["learned_patterns"][key]:
            self._data["learned_patterns"][key].append(keyword)

    def get_learned_keywords(self) -> list:
        return self._data["learned_patterns"]["high_discount_keywords"]

    # ── Sesiones ─────────────────────────────────────────────────────────────

    def start_session(self) -> str:
        session_id = f"session_{int(time.time())}"
        self._data["stats"]["sessions"] += 1
        self._data["session_history"].append({
            "id": session_id,
            "started_at": time.time(),
            "ended_at": None,
            "offers_found": 0,
            "pages_visited": 0,
        })
        if len(self._data["session_history"]) > 50:
            self._data["session_history"] = self._data["session_history"][-50:]
        return session_id

    def end_session(self, session_id: str, offers_found: int, pages_visited: int):
        for s in reversed(self._data["session_history"]):
            if s["id"] == session_id:
                s["ended_at"] = time.time()
                s["offers_found"] = offers_found
                s["pages_visited"] = pages_visited
                break

    def get_summary(self) -> dict:
        stats = self._data["stats"]
        best_cats = self.get_best_categories(5)
        recent_evals = self.get_recent_evaluations(5)
        return {
            "stats": stats,
            "best_categories": best_cats,
            "recent_ai_verdicts": [e["verdict"] for e in recent_evals],
            "dom_heals": stats["total_dom_heals"],
            "seen_asins": len(self._data["seen_asins"]),
        }
