# Frontier deals-focus + diversidad — diseño

Fecha: 2026-06-03
Objetivo del usuario: que el bot vuelva a encontrar más ofertas (≥50% desc.),
**manteniendo la diversidad de categorías** (no obligatorio sacrificarla).

## Problema (causa raíz confirmada)

El frontier ML se llenó de **27,120 listados de categoría genérica**
(`listado.mercadolibre.com.mx/computacion/...`, `/electrodomesticos/...`) con
CERO URLs de `/ofertas`, `Descuento` o `mas-vendidos`. Esos listados traen
productos a precio normal (descuento real mediana 23.5%), así que el bot gasta
su presupuesto de hunt en productos que no llegan al 50% y la tasa de
publicación cayó a la mitad (154/día → 76/día).

Mecanismo: el discovery extrae links de navegación de las páginas y los mete al
frontier como `listing` (score 3.0). Las páginas de ofertas enlazan a
categorías genéricas, que se acumulan y desplazan a las páginas de ofertas.

## Los 4 puntos a implementar

### Punto 1 — Limpiar el frontier (script de mantenimiento)
`scripts/clean_frontier_non_deals.py`: borra del frontier ML los `listing`/
`category` que NO sean páginas de ofertas/descuento/best-seller. Idempotente,
seguro en DB viva. Conserva `product` y `deals`.

### Punto 2 — Re-priorizar en el clasificador de URLs
`url_classifier.py`: distinguir listados de OFERTA de listados genéricos.
- Listado ML con `/ofertas`, `_Descuento`, `Deal`, `tier=deal`, `promociones`
  → `deals` con score alto (8.0).
- Listado genérico (`listado.mercadolibre.com.mx/categoria/...` sin descuento)
  → score bajo (1.0) para que NO desplace a las ofertas.
- Mantiene `product` (10.0) y `category` (2.0).

### Punto 3 — Planner apunta a sub-ofertas
`category_deficit_planner` recomienda categorías deficitarias. Añadir un mapeo
categoría → URL de ofertas (`/ofertas/<slug>`) y un helper que el discovery use
para sembrar páginas de OFERTAS por categoría deficitaria, en vez de listados
genéricos. Así la diversidad se logra **sobre ofertas reales**.

### Punto 4 — Combinar 1+3 manteniendo diversidad
El diversity curator + quota de outbox **siguen activos** (eso da la variedad).
Solo cambia el *origen* de URLs: páginas de ofertas por categoría. El planner
de déficit sigue mandando, pero ahora produce `/ofertas/<categoria>`.

## No-objetivos
- No tocar los gates de publicación (precio, afiliado, etc.).
- No desactivar el diversity curator (el usuario quiere mantener diversidad).
- No bajar el umbral de 50%.

## Verificación
- Tests unit del clasificador (deals vs genérico, scores).
- Tests del mapeo categoría→ofertas.
- Script de limpieza: dry-run + conteo.
- Deploy: limpiar frontier en VPS, sembrar ofertas por categoría, reiniciar,
  observar que `disc50+` y enqueued ML suben en las siguientes horas.
