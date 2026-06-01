#!/usr/bin/env python3
"""Mergea (dedup) las nuevas seeds category-aware en los JSON de seeds.
Hace backup. Idempotente. NO toca otras seeds; solo agrega las nuevas que falten.
"""
import json, sys
from pathlib import Path
from datetime import datetime

ROOT = Path("/opt/deal-agent/ofertas_hunter")

NEW_ML = [
    "https://www.mercadolibre.com.mx/ofertas/cocina",
    "https://www.mercadolibre.com.mx/ofertas/organizacion",
    "https://www.mercadolibre.com.mx/ofertas/electrodomesticos",
    "https://www.mercadolibre.com.mx/ofertas/herramientas",
    "https://www.mercadolibre.com.mx/ofertas/deportes",
    "https://www.mercadolibre.com.mx/ofertas/bebes",
    "https://www.mercadolibre.com.mx/ofertas/computacion",
    "https://www.mercadolibre.com.mx/ofertas/electronica",
    "https://www.mercadolibre.com.mx/ofertas/muebles",
    "https://www.mercadolibre.com.mx/ofertas/jardin",
    "https://listado.mercadolibre.com.mx/cesto-ropa-sucia_Descuento_50-100",
    "https://listado.mercadolibre.com.mx/organizador-ropa_Descuento_50-100",
    "https://listado.mercadolibre.com.mx/olla-cocina_Descuento_50-100",
    "https://listado.mercadolibre.com.mx/bateria-de-cocina_Descuento_50-100",
    "https://listado.mercadolibre.com.mx/sarten_Descuento_50-100",
    "https://listado.mercadolibre.com.mx/termo-acero-inoxidable_Descuento_50-100",
    "https://listado.mercadolibre.com.mx/fuente-de-poder-pc_Descuento_50-100",
    "https://listado.mercadolibre.com.mx/teclado-mecanico_Descuento_50-100",
    "https://listado.mercadolibre.com.mx/monitor-gamer_Descuento_50-100",
    "https://listado.mercadolibre.com.mx/taladro-inalambrico_Descuento_50-100",
    "https://listado.mercadolibre.com.mx/juego-de-herramientas_Descuento_50-100",
    "https://listado.mercadolibre.com.mx/aspiradora_Descuento_50-100",
    "https://listado.mercadolibre.com.mx/freidora-de-aire_Descuento_50-100",
    "https://listado.mercadolibre.com.mx/licuadora_Descuento_50-100",
    "https://listado.mercadolibre.com.mx/cafetera_Descuento_50-100",
    "https://listado.mercadolibre.com.mx/mochila_Descuento_50-100",
    "https://listado.mercadolibre.com.mx/tenis-deportivos_Descuento_50-100",
    "https://listado.mercadolibre.com.mx/carriola_Descuento_50-100",
    "https://listado.mercadolibre.com.mx/juguetes-ninos_Descuento_50-100",
]

NEW_AMZ = [
    "https://www.amazon.com.mx/deals?bubble-id=deals-collection-home-kitchen",
    "https://www.amazon.com.mx/deals?bubble-id=deals-collection-electronics",
    "https://www.amazon.com.mx/deals?bubble-id=deals-collection-computers",
    "https://www.amazon.com.mx/deals?bubble-id=deals-collection-home-improvement",
    "https://www.amazon.com.mx/deals?bubble-id=deals-collection-sports-outdoors",
    "https://www.amazon.com.mx/s?k=ollas+cocina&rh=p_n_pct-off-with-tax%3A50-",
    "https://www.amazon.com.mx/s?k=bateria+de+cocina&rh=p_n_pct-off-with-tax%3A50-",
    "https://www.amazon.com.mx/s?k=sarten+antiadherente&rh=p_n_pct-off-with-tax%3A50-",
    "https://www.amazon.com.mx/s?k=termo+acero+inoxidable&rh=p_n_pct-off-with-tax%3A50-",
    "https://www.amazon.com.mx/s?k=vaso+termico&rh=p_n_pct-off-with-tax%3A50-",
    "https://www.amazon.com.mx/s?k=organizador+ropa&rh=p_n_pct-off-with-tax%3A50-",
    "https://www.amazon.com.mx/s?k=cesto+ropa+sucia&rh=p_n_pct-off-with-tax%3A50-",
    "https://www.amazon.com.mx/s?k=organizador+closet&rh=p_n_pct-off-with-tax%3A50-",
    "https://www.amazon.com.mx/s?k=fuente+de+poder+pc&rh=p_n_pct-off-with-tax%3A50-",
    "https://www.amazon.com.mx/s?k=tarjeta+grafica&rh=p_n_pct-off-with-tax%3A50-",
    "https://www.amazon.com.mx/s?k=memoria+ram+ddr4&rh=p_n_pct-off-with-tax%3A50-",
    "https://www.amazon.com.mx/s?k=ssd+nvme+1tb&rh=p_n_pct-off-with-tax%3A50-",
    "https://www.amazon.com.mx/s?k=teclado+mecanico&rh=p_n_pct-off-with-tax%3A50-",
    "https://www.amazon.com.mx/s?k=monitor+gamer&rh=p_n_pct-off-with-tax%3A50-",
    "https://www.amazon.com.mx/s?k=taladro+inalambrico&rh=p_n_pct-off-with-tax%3A50-",
    "https://www.amazon.com.mx/s?k=juego+de+herramientas&rh=p_n_pct-off-with-tax%3A50-",
    "https://www.amazon.com.mx/s?k=freidora+de+aire&rh=p_n_pct-off-with-tax%3A50-",
    "https://www.amazon.com.mx/s?k=licuadora&rh=p_n_pct-off-with-tax%3A50-",
    "https://www.amazon.com.mx/s?k=cafetera&rh=p_n_pct-off-with-tax%3A50-",
    "https://www.amazon.com.mx/s?k=organizador+cocina&rh=p_n_pct-off-with-tax%3A50-",
    "https://www.amazon.com.mx/s?k=mochila&rh=p_n_pct-off-with-tax%3A50-",
    "https://www.amazon.com.mx/s?k=tenis+deportivos&rh=p_n_pct-off-with-tax%3A50-",
    "https://www.amazon.com.mx/s?k=juguetes+ninos&rh=p_n_pct-off-with-tax%3A50-",
    "https://www.amazon.com.mx/s?k=carriola+bebe&rh=p_n_pct-off-with-tax%3A50-",
    "https://www.amazon.com.mx/s?k=sabanas+matrimonial&rh=p_n_pct-off-with-tax%3A50-",
]


def merge(path: Path, new_urls):
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, list), f"{path} no es lista"
    existing = set(data)
    added = [u for u in new_urls if u not in existing]
    if not added:
        print(f"{path.name}: nada nuevo (ya tiene {len(data)})")
        return 0
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    bak = path.with_name(path.name + f".bak_seeds_{ts}")
    bak.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    data.extend(added)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{path.name}: +{len(added)} seeds (total {len(data)}); backup {bak.name}")
    return len(added)


merge(ROOT / "config/seeds/mercadolibre.json", NEW_ML)
merge(ROOT / "config/seeds/amazon.json", NEW_AMZ)
print("DONE")
