"""Normalización e inferencia de metadata para diversidad.

Este módulo NO toca gates ni publica nada. Solo deriva, a partir del título y
el payload de una oferta, una metadata *normalizada* y confiable que el
selector de diversidad usa para evitar repetición de categoría / marca /
familia de producto.

Motivación (ver diagnóstico):
- `payload.category` de Mercado Libre viene contaminado (`"vender uno igual"`,
  `"ver más"`, etc.). Si el scorer la usa cruda, no detecta repetición.
- Variantes del mismo producto (mismo whey, distinto sabor) tienen `item_id`
  distinto y por eso el dedupe exacto no las detecta.

Todo aquí es determinístico y sin dependencias externas (solo stdlib), para
que sea testeable y no agregue superficie de fallo en producción.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Optional


# ---------------------------------------------------------------------------
# Normalización de texto
# ---------------------------------------------------------------------------

def strip_accents(text: str) -> str:
    """Quita acentos/diacríticos (á→a, ñ→n) preservando ASCII base."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def normalize_title(title: Optional[str]) -> str:
    """lower + sin acentos + sin signos + espacios colapsados.

    Mantiene dígitos (sirven para distinguir tamaños/modelos), pero
    el `%` y la puntuación se eliminan.
    """
    t = strip_accents((title or "").lower())
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


# ---------------------------------------------------------------------------
# Categoría
# ---------------------------------------------------------------------------

# Categorías canónicas (la lista mínima exigida por el spec).
CATEGORIES = [
    "proteina/suplementos",
    "bebe",
    "tecnologia",
    "hogar",
    "belleza",
    "salud",
    "ropa",
    "calzado",
    "despensa",
    "herramientas",
    "juguetes",
    "mascotas",
    "auto",
    "oficina",
    "deportes",
    "industrial",
    "otro",
]

# Categorías "basura" o demasiado genéricas que NUNCA deben usarse como señal.
# Se comparan sobre la versión normalizada (sin acentos, lower).
# Incluye textos de botones / CTA de Mercado Libre y placeholders de "sin
# categoría" para que jamás se cuenten como una categoría real.
GARBAGE_CATEGORIES = {
    "",
    "vender uno igual",
    "vender uno igualito",
    "comprar ahora",
    "comprar",
    "agregar al carrito",
    "anadir al carrito",
    "preguntar",
    "preguntas",
    "compartir",
    "seguir",
    "mas info",
    "mas informacion",
    "ver mas",
    "ver todos",
    "ver todas",
    "ver detalle",
    "ver detalles",
    "otros",
    "otro",
    "varios",
    "general",
    "generales",
    "mas vendidos",
    "ofertas",
    "oferta",
    "ofertas del dia",
    "promociones",
    "promocion",
    "destacados",
    "recomendados",
    "categoria",
    "categorias",
    "sin categoria",
    "sin clasificar",
    "productos",
    "producto",
    "articulo",
    "articulos",
    "inicio",
    "home",
    "tienda",
    "todo",
    "todos",
    "none",
    "null",
    "na",
}

# Reglas de inferencia por título. ORDEN IMPORTA: lo más específico primero.
# Cada entrada: (categoria_canonica, regex sobre título normalizado).
_CATEGORY_RULES: list[tuple[str, str]] = [
    # belleza / skincare PRIMERO: "protector solar", "vitamina c" en crema, etc.
    # se evalúa antes que suplementos para que un protector solar con
    # "vitamina c" no se clasifique como suplemento.
    ("belleza",
     r"\b(protector\s?solar|bloqueador\s?solar|filtro\s?solar|fotoprotector|"
     r"maquillaje|labial|lapiz\s?labial|rimel|mascara\s?de\s?pesta|delineador|"
     r"rubor|base\s?de\s?maquillaje|corrector|sombras|paleta\s?de\s?sombras|"
     r"shampoo|champu|"
     r"acondicionador|serum|suero\s?facial|crema\s?facial|crema\s?corporal|"
     r"crema\s?hidratante|locion\s?corporal|"
     r"perfume|fragancia|colonia|eau\s?de\s?parfum|eau\s?de\s?toilette|"
     r"\bedp\b|\bedt\b|\bedc\b|parfum|"
     r"skincare|cuidado\s?facial|antiarrugas|antiedad|anti\s?edad|"
     r"facial|toallitas?\s?desmaquillant|desmaquillant|micelar|"
     r"nivea|eucerin|loreal|l\s?oreal|maybelline|cerave|avene|isdin|vichy|"
     r"garnier|neutrogena|cetaphil|la\s?roche|roche\s?posay|revlon|"
     r"bath\s?body|cabello|"
     r"secadora\s?de\s?pelo|plancha\s?de\s?pelo|rizadora|esmalte|"
     r"acido\s?hialuronico|contorno\s?de\s?ojos|exfoliante|mascarilla\s?facial|"
     r"afeitadora|rasuradora|recortadora|oneblade|one\s?blade|"
     r"cepillo\s?de\s?dientes|oral\s?b|pasta\s?dental|enjuague\s?bucal|"
     r"depiladora|gel\s?limpiador|limpiador\s?facial)\b"),
    # proteína / suplementos (después de belleza para no robar "vitamina c"
    # de un cosmético). "vitamina" exige contexto de suplemento.
    ("proteina/suplementos",
     r"\b(whey|proteina|protein|creatina|creatine|suplement|bcaa|aminoacid|"
     r"glutamina|pre\s?workout|preentreno|gainer|mass\s?gainer|caseina|"
     r"isolate|iso\s?protein|colageno\s?hidrolizado|"
     r"l\s?carnitina|carnitina|hidrolizada|hidrolizado|nitro\s?tech|"
     r"multivitaminico|multivitaminas|complejo\s?b|vitamina\s?c\s?en\s?capsulas|"
     r"vitaminas\s?gomitas|magnesio\s?en\s?polvo|omega\s?3|centrum)\b"),
    # mascotas ANTES que bebé: una "transportadora para perro/gato" es mascota,
    # no autoasiento de bebé. Las transportadoras/jaulas/bolsas con contexto de
    # mascota se enrutan aquí; las de bebé caen luego en `bebe`.
    ("mascotas",
     r"\b(croqueta|croquetas|alimento\s?para\s?(perro|gato)|"
     r"arena\s?para\s?gato|rascador|"
     r"(transportadora|jaula|bolsa\s?transportadora|mochila\s?transportadora|"
     r"comedero|bebedero|collar|correa|arnes|cama)\s?"
     r"(para\s?)?(perro|gato|mascota|mascotas|cachorro|felino|canino)|"
     r"(perro|gato|mascota|cachorro)\b.*\b(transportadora|transporte|jaula|arnes|"
     r"comedero|bebedero|rascador|croqueta)|"
     r"para\s?(perros?|gatos?|mascotas?)\b)"),
    # organización / lavandería del hogar ANTES de ropa: un "cesto para ropa"
    # es hogar, no ropa. Captura cestos/canastos/organizadores/lavandería.
    ("hogar",
     r"\b(cesto|canasto|canasta\s?(de\s?ropa|organizadora)|"
     r"lavanderia|ropa\s?sucia|organizador\s?(de\s?ropa|plegable)|"
     r"perchero|burro\s?de\s?planchar|tendedero)\b"),
    # bebé
    ("bebe",
     r"\b(carriola|andadera|andaderas|correpasillo|pa[n]al|pa[n]ales|biberon|"
     r"mamila|mamilas|tetina|chupon|cuna|moises|"
     r"autoasiento|auto\s?asiento|sillas?\s?para\s?(bebe|comer|periquera)|"
     r"periquera|bebe|beb[e]s|maternidad|"
     r"portabebe|porta\s?bebe|mordedera|sonaja|"
     r"tina\s?(de\s?bano|para\s?bebe)|banera|toallitas\s?humedas)\b"),
    # calzado (antes que ropa)
    ("calzado",
     r"\b(tenis|zapato|zapatos|zapatilla|sandalia|bota|botas|crocs|"
     r"calzado|sneaker|huarache|mocasin|pantufla)\b"),
    # ropa / accesorios de vestir
    ("ropa",
     r"\b(playera|camisa|camiseta|pantalon|jeans|sudadera|sueter|sueteres|"
     r"hoodie|chamarra|chaqueta|vestido|short|falda|blusa|abrigo|leggings|"
     r"calcetin|calceti|boxer|ropa|gorra|cachucha|"
     r"bolso|bolsa\s?de\s?mano|cartera|monedero|mochila|maleta|"
     r"lentes\s?de\s?sol|gafas\s?de\s?sol|reloj\s?de\s?pulsera|"
     r"michael\s?kors|guess|tommy\s?hilfiger)\b"),
    # salud
    ("salud",
     r"\b(termometro|oximetro|mascarilla|cubrebocas|botiquin|"
     r"baumanometro|nebulizador|"
     r"silla\s?de\s?ruedas|rollator|ortopedic|ortopedica|pastillero|venda|"
     r"muletas|baston|faja\s?ortopedica|rodillera|tobillera|"
     r"glucometro|tensiometro)\b"),
    # herramientas
    ("herramientas",
     r"\b(taladro|destornillad|desarmador|martillo|llave\s?de|herramient|"
     r"sierra|esmeril|pinza|atornillad|broca|rotomartillo|caladora|"
     r"soldadora|pulidora|esmeriladora|kit\s?de\s?herramient|"
     r"juego\s?de\s?(herramient|desarmador))\b"),
    # industrial
    ("industrial",
     r"\b(guante|guantes|nitrilo|latex|casco\s?de\s?seguridad|"
     r"seguridad\s?industrial|overol|botas\s?industrial|"
     r"epp|googles\s?de\s?seguridad|tapon\s?auditivo|faja\s?industrial|"
     r"lona|lonas)\b"),
    # tecnología
    ("tecnologia",
     r"\b(laptop|notebook|macbook|ultrabook|computadora|pc\s?gamer|ssd|"
     r"disco\s?duro|teclado|mouse|monitor|pantalla|audifon|audifono|headset|"
     r"diadema|tv|smart\s?tv|television|celular|smartphone|iphone|galaxy|"
     r"xiaomi|tablet|ipad|router|modem|usb|hdmi|webcam|impresora|consola|"
     r"xbox|playstation|nintendo|switch|gpu|tarjeta\s?grafica|procesador|"
     r"ryzen|intel|memoria\s?ram|powerbank|cargador|bocina|parlante|alexa|"
     r"echo\s?dot|robot\s?aspirador|aspiradora\s?robot|drone|dron|"
     r"fuente\s?atx|fuente\s?de\s?poder|gabinete\s?gamer|disipador|"
     r"camara|smartwatch|reloj\s?intelig|audi\s?fonos|videojuego)\b"),
    # hogar (después de tech para no robar "aspiradora robot")
    ("hogar",
     r"\b(sarten|olla|licuadora|aspiradora|cafetera|colchon|almohada|"
     r"sabana|sabanas|cortina|mueble|silla|mesa|organizador|cocina|"
     r"vajilla|microondas|refrigerador|lavadora|ventilador|abanico|freidora|"
     r"air\s?fryer|airfryer|plancha\s?de\s?ropa|toalla|edredon|"
     r"juego\s?de\s?sabanas|batidora|tostador|hervidor|escoba|trapeador|"
     r"set\s?de\s?cocina|bateria\s?de\s?cocina|termo|vaso|botella|cantimplora|"
     r"cesto|canasto|canasta|cubeta|contenedor|jabonera|"
     r"pila|pilas|bateria\s?aa|bateria\s?aaa|duracell|"
     r"humidificador|difusor|vela|veladora|maceta)\b"),
    # despensa / bebidas
    ("despensa",
     r"\b(cafe|azucar|arroz|aceite\s?de\s?(oliva|cocina)|cereal|galleta|"
     r"galletas|snack|botana|"
     r"refresco|despensa|atun|leche|harina|pasta|salsa|mayonesa|"
     r"chocolate\s?en\s?polvo|nescafe|nutella|"
     r"cerveza|vino|whisky|whiskey|\bron\b|tequila|vodka|ginebra|licor|"
     r"bebida\s?alcoholica|coronita|corona\s?extra)\b"),
    # juguetes
    ("juguetes",
     r"\b(juguete|lego|muneca|munecas|peluche|figura\s?de\s?accion|hot\s?wheels|"
     r"barbie|nerf|rompecabezas|didactico|pista\s?de\s?carros|"
     r"juego\s?de\s?mesa|funko|mega\s?bloks|bloques\s?de\s?construccion)\b"),
    # mascotas (genérico, sin contexto de cría) — captura términos sueltos.
    ("mascotas",
     r"\b(croqueta|croquetas|arena\s?para\s?gato|rascador|"
     r"comedero|bebedero)\b"),
    # auto
    ("auto",
     r"\b(llanta|neumatico|aceite\s?de\s?motor|bateria\s?de\s?auto|"
     r"limpiaparabrisas|autoestereo|amplificador\s?de\s?auto|"
     r"funda\s?de\s?asiento|escaner\s?obd|anticongelante|balata)\b"),
    # oficina
    ("oficina",
     r"\b(papeleria|cuaderno|boligrafo|pluma|engrapadora|archivero|"
     r"silla\s?de\s?oficina|escritorio|tinta\s?para\s?impresora|"
     r"toner|folder|carpeta)\b"),
    # deportes
    ("deportes",
     r"\b(bicicleta|mancuerna|mancuernas|pesa|pesas|caminadora|eliptica|yoga|"
     r"balon|raqueta|patines|casco\s?de\s?ciclismo|tienda\s?de\s?campana|"
     r"banda\s?elastica|barra\s?olimpica|guantes\s?de\s?box)\b"),
]


# Mapa marca -> categoría canónica. Se usa SOLO cuando el título no dio match
# por reglas (p.ej. títulos de marketing como "EN SU PRECIO MÁS BAJO 🔥") pero
# sí se reconoce la marca. Confianza media-baja.
_BRAND_CATEGORY_HINT: dict[str, str] = {
    # belleza
    "nivea": "belleza", "eucerin": "belleza", "loreal": "belleza",
    "maybelline": "belleza", "cerave": "belleza", "avene": "belleza",
    "isdin": "belleza", "vichy": "belleza", "garnier": "belleza",
    "neutrogena": "belleza", "cetaphil": "belleza", "la roche posay": "belleza",
    "revlon": "belleza",
    # hogar
    "stanley": "hogar", "owala": "hogar", "oster": "hogar", "tfal": "hogar",
    "hamilton": "hogar", "cuisinart": "hogar", "vasconia": "hogar",
    "lysol": "hogar", "kleenex": "hogar", "persil": "hogar",
    # tecnología
    "samsung": "tecnologia", "apple": "tecnologia", "iphone": "tecnologia",
    "xiaomi": "tecnologia", "hp": "tecnologia", "dell": "tecnologia",
    "lenovo": "tecnologia", "asus": "tecnologia", "acer": "tecnologia",
    "lg": "tecnologia", "sony": "tecnologia", "jbl": "tecnologia",
    "bose": "tecnologia", "logitech": "tecnologia", "redragon": "tecnologia",
    "hyperx": "tecnologia", "corsair": "tecnologia", "kingston": "tecnologia",
    "seagate": "tecnologia", "anker": "tecnologia", "baseus": "tecnologia",
    "ugreen": "tecnologia", "motorola": "tecnologia", "huawei": "tecnologia",
    "honor": "tecnologia", "tcl": "tecnologia", "hisense": "tecnologia",
    "razer": "tecnologia", "noco": "tecnologia",
    # juguetes
    "lego": "juguetes", "barbie": "juguetes",
    # herramientas
    "bosch": "herramientas", "dewalt": "herramientas", "truper": "herramientas",
    "makita": "herramientas", "black decker": "herramientas",
    # proteína / suplementos
    "bhp": "proteina/suplementos", "optimum": "proteina/suplementos",
    "birdman": "proteina/suplementos", "scitec": "proteina/suplementos",
    "muscletech": "proteina/suplementos", "bpi": "proteina/suplementos",
    "prozis": "proteina/suplementos", "43 supplements": "proteina/suplementos",
    "evolution": "proteina/suplementos", "gwynne": "proteina/suplementos",
    "fsb": "proteina/suplementos",
    # calzado / ropa
    "nike": "calzado", "adidas": "calzado", "puma": "calzado",
}


@dataclass(frozen=True)
class CategoryResult:
    raw: Optional[str]
    normalized: str
    source: str           # "payload" | "title" | "brand" | "fallback"
    confidence: float     # 0.0 - 1.0


def _is_garbage_category(raw_norm: str) -> bool:
    if raw_norm in GARBAGE_CATEGORIES:
        return True
    # demasiado corta o numérica
    if len(raw_norm) <= 2:
        return True
    # puramente numérica (precios, %)
    if re.fullmatch(r"[0-9 ]+", raw_norm):
        return True
    return False


def _category_from_rules(text: str) -> Optional[str]:
    """Devuelve la categoría canónica si `text` matchea alguna regla, o None."""
    if not text:
        return None
    for cat, pattern in _CATEGORY_RULES:
        if re.search(pattern, text):
            return cat
    return None


def infer_offer_category(
    title: Optional[str],
    payload: Optional[dict] = None,
    marketplace: Optional[str] = None,
    source: Optional[str] = None,
) -> CategoryResult:
    """Infiera una categoría canónica confiable.

    Estrategia (orden de prioridad):
    1. Reglas por título (alta confianza).
    2. `payload.category` (breadcrumb) SOLO si no es basura: mapeado a canónica
       por reglas (conf 0.7) o usado tal cual si es un breadcrumb real
       sin mapeo (conf 0.5).
    3. Hint por marca normalizada (para títulos de marketing sin keyword) (0.4).
    4. Fallback a "otro" (confianza baja).

    Se prioriza el título sobre `payload.category` porque en ML la categoría
    cruda está contaminada (`"vender uno igual"`, botones, etc.) y el título es
    la señal más fiable. NUNCA se usa una categoría de `GARBAGE_CATEGORIES`.
    """
    payload = payload or {}
    raw = payload.get("category")
    norm_title = normalize_title(title)

    # 1. Reglas por título
    cat = _category_from_rules(norm_title)
    if cat is not None:
        return CategoryResult(raw=raw, normalized=cat, source="title", confidence=0.9)

    # 2. payload.category (breadcrumb) si NO es basura
    if raw:
        raw_norm = normalize_title(raw)
        if not _is_garbage_category(raw_norm):
            mapped = _category_from_rules(raw_norm)
            if mapped is not None:
                return CategoryResult(raw=raw, normalized=mapped, source="payload", confidence=0.7)
            # breadcrumb válido pero sin mapeo canónico → usar tal cual
            return CategoryResult(raw=raw, normalized=raw_norm, source="payload", confidence=0.5)

    # 3. Hint por marca normalizada (títulos de marketing sin keyword de producto)
    brand_n = normalize_brand(payload.get("brand"), title)
    if brand_n:
        hinted = _BRAND_CATEGORY_HINT.get(brand_n)
        if hinted:
            return CategoryResult(raw=raw, normalized=hinted, source="brand", confidence=0.4)

    # 4. fallback
    return CategoryResult(raw=raw, normalized="otro", source="fallback", confidence=0.1)


def compute_diversity_metadata(
    payload: Optional[dict] = None,
    *,
    title: Optional[str] = None,
) -> dict:
    """Calcula los 6 campos normalizados de diversidad para PERSISTIR en el
    payload del outbox. Fuente única de verdad usada por todos los enqueue
    sites y por el backfill.

    Devuelve un dict con:
      category_raw, category_normalized, category_source, category_confidence,
      brand_normalized, product_family
    """
    payload = payload or {}
    if title is None:
        title = payload.get("title") or ""
    cat = infer_offer_category(
        title, payload, payload.get("marketplace"), payload.get("source")
    )
    brand_n = normalize_brand(payload.get("brand"), title)
    family = product_family(title, payload.get("brand"))
    return {
        "category_raw": cat.raw,
        "category_normalized": cat.normalized,
        "category_source": cat.source,
        "category_confidence": cat.confidence,
        "brand_normalized": brand_n,
        "product_family": family,
    }


# ---------------------------------------------------------------------------
# Marca
# ---------------------------------------------------------------------------

# Marcas conocidas (normalizadas). Si el título o brand crudo contiene el
# alias, se mapea a la forma canónica.
_KNOWN_BRANDS = [
    "samsung", "apple", "iphone", "xiaomi", "hp", "dell", "lenovo", "asus",
    "acer", "lg", "sony", "jbl", "bose", "logitech", "nike", "adidas", "puma",
    "bosch", "dewalt", "truper", "makita", "stanley", "nestle", "optimum",
    "birdman", "pioneer", "oster", "hamilton", "cuisinart", "tfal", "philips",
    "redragon", "hyperx", "corsair", "kingston", "seagate", "anker", "baseus",
    "ugreen", "motorola", "huawei", "honor", "tcl", "hisense", "nintendo",
    "microsoft", "razer", "steren", "vasconia", "bhp", "empower", "gwynne",
    "fsb", "rca", "bluelander", "roborock", "nivea", "eucerin", "loreal",
    "maybelline", "cerave", "lysol", "kleenex", "persil", "owala", "noco",
    "amazon basics", "amazonbasics", "ksport", "scitec", "muscletech",
    "bpi", "evolution", "prozis", "isdin", "vichy", "garnier", "neutrogena",
    "la roche posay", "cetaphil", "hawaiian tropic", "coppertone",
]

# "43 supplements" es una marca cuyo nombre empieza con número.
_BRAND_REGEX_OVERRIDES = [
    (r"\b43\s?supplements\b", "43 supplements"),
    (r"\b43\s?proteina\b", "43 supplements"),
    (r"^43\b", "43 supplements"),
    (r"\bblack\s?\+?\s?decker\b", "black decker"),
    (r"\bt\s?-?\s?fal\b", "tfal"),
]

# Palabras que NO son marca aunque aparezcan primero en el título.
_NON_BRAND_LEADING = {
    "proteina", "protein", "suplemento", "kit", "set", "pack", "combo",
    "oferta", "nueva", "nuevo", "the", "el", "la", "los", "las", "para",
    "whey", "robot", "aspiradora", "aspirador", "cargador", "audifonos",
    "audifono", "bocina", "laptop", "tablet", "celular", "smart",
    "de", "del", "por", "con", "protector", "filtro", "bloqueador",
}


def normalize_brand(brand: Optional[str], title: Optional[str] = None) -> Optional[str]:
    """Devuelve una marca normalizada o None.

    Prioridad:
    1. Overrides por regex (marcas con número/símbolo).
    2. `brand` crudo si mapea a una marca conocida.
    3. Marca conocida encontrada en el título.
    4. `brand` crudo normalizado (si parece marca real).
    5. None.
    """
    norm_brand = normalize_title(brand) if brand else ""
    norm_title = normalize_title(title)

    # Quitar prefijos de ruido comunes que el scraper deja en el campo brand
    # ("de ISDIN", "marca Nivea", "por Bosch"...) para no contar "de isdin"
    # como una marca distinta de "isdin".
    norm_brand = re.sub(r"^(de|del|marca|por|by|the)\s+", "", norm_brand).strip()

    # 1. overrides (sobre brand y título)
    for pattern, canonical in _BRAND_REGEX_OVERRIDES:
        if (norm_brand and re.search(pattern, norm_brand)) or re.search(pattern, norm_title):
            return canonical

    # 2. brand crudo mapea a conocida
    if norm_brand:
        for kb in _KNOWN_BRANDS:
            if re.search(r"\b" + re.escape(kb) + r"\b", norm_brand):
                return kb

    # 3. marca conocida en título
    for kb in _KNOWN_BRANDS:
        if re.search(r"\b" + re.escape(kb) + r"\b", norm_title):
            return kb

    # 4. brand crudo que parece marca (una o dos palabras, no genérica)
    if norm_brand:
        tokens = norm_brand.split()
        if 1 <= len(tokens) <= 3 and tokens[0] not in _NON_BRAND_LEADING:
            return norm_brand

    return None


# ---------------------------------------------------------------------------
# Familia de producto / fingerprint
# ---------------------------------------------------------------------------

# Sabores, colores y palabras de presentación que distinguen VARIANTES del
# mismo producto base y por tanto deben eliminarse al construir la familia.
_VARIANT_TOKENS = {
    # sabores
    "vainilla", "chocolate", "cookies", "cream", "fresa", "platano", "banana",
    "limon", "limonada", "ponche", "mazapan", "cafe", "capuchino", "caramelo",
    "coco", "mango", "durazno", "naranja", "uva", "cereza", "menta",
    "galleta", "galletas", "natural", "sabor", "sabores", "frutos", "rojos",
    "horchata", "matcha", "te", "verde", "neutro", "sin",
    # colores
    "azul", "negro", "negra", "rojo", "roja", "blanco", "blanca", "verde",
    "amarillo", "rosa", "rosado", "gris", "morado", "naranja", "dorado",
    "plata", "plateado", "cafe", "beige", "color", "multicolor",
    # presentación
    "bote", "bolsa", "pack", "paquete", "caja", "kit", "set", "combo",
    "unidad", "unidades", "pza", "pzas", "piezas", "pieza", "x",
    "aroma", "edicion", "presentacion",
}

# Conectores irrelevantes
_STOPWORDS = {"de", "con", "y", "el", "la", "los", "las", "para", "en", "a",
              "the", "and", "of", "ultra", "premium", "original", "100"}


def _family_tokens(title: Optional[str], brand: Optional[str]) -> list[str]:
    norm = normalize_title(title)
    tokens = norm.split()
    out: list[str] = []
    for tok in tokens:
        if tok in _VARIANT_TOKENS:
            continue
        if tok in _STOPWORDS:
            continue
        # tokens de unidad puros tipo "2", "27", "kg", "lbs", "ml", "g", "gr"
        if re.fullmatch(r"\d+", tok):
            # conservar números (tamaños distinguen modelos) salvo que sean
            # muy cortos y aislados; los conservamos para no sobre-colapsar
            out.append(tok)
            continue
        out.append(tok)
    return out


def product_family(title: Optional[str], brand: Optional[str] = None) -> str:
    """Clave de familia de producto base (colapsa variantes de sabor/color).

    Heurística: marca normalizada + primeros tokens de contenido del título
    tras quitar sabores/colores/presentación/stopwords. No pretende ser
    perfecta; busca colapsar variantes obvias del mismo producto.
    """
    nb = normalize_brand(brand, title)
    tokens = _family_tokens(title, nb)
    # tomar los primeros 5 tokens de contenido como núcleo de la familia
    core = tokens[:5]
    parts: list[str] = []
    if nb:
        parts.append(nb.replace(" ", "_"))
    parts.extend(core)
    slug = "_".join(parts)
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug or normalize_title(title).replace(" ", "_")[:40]


def title_fingerprint(title: Optional[str]) -> str:
    """Fingerprint estable del título (normalizado, sin variantes)."""
    norm = normalize_title(title)
    tokens = [t for t in norm.split() if t not in _VARIANT_TOKENS]
    return " ".join(tokens)


# ---------------------------------------------------------------------------
# Similitud difusa (sin dependencias externas)
# ---------------------------------------------------------------------------

def _token_jaccard(a: str, b: str) -> float:
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def title_similarity(a: Optional[str], b: Optional[str]) -> float:
    """Similitud 0..1 entre dos títulos.

    Combina ratio de secuencia (difflib) y Jaccard de tokens, tomando el
    máximo. Opera sobre fingerprints (sin sabores/colores) para que
    variantes del mismo producto den similitud alta.
    """
    fa, fb = title_fingerprint(a), title_fingerprint(b)
    if not fa or not fb:
        return 0.0
    seq = SequenceMatcher(None, fa, fb).ratio()
    jac = _token_jaccard(fa, fb)
    return max(seq, jac)


__all__ = [
    "strip_accents",
    "normalize_title",
    "CATEGORIES",
    "GARBAGE_CATEGORIES",
    "CategoryResult",
    "infer_offer_category",
    "compute_diversity_metadata",
    "normalize_brand",
    "product_family",
    "title_fingerprint",
    "title_similarity",
]
