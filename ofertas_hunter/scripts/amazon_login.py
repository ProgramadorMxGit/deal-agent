"""Login interactivo Amazon con flush garantizado del perfil persistente.

Versión simplificada y robusta del comando `python -m ofertas_hunter
login --marketplace amazon`. Usa Playwright **sync** para evitar
problemas de coroutine cancellation que pueden cortar el flush a disco.

Uso:

    .\.venv\Scripts\python.exe scripts\amazon_login.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROFILE_DIR = Path("secrets/browser_profiles/amazon").resolve()
INITIAL_URL = (
    "https://www.amazon.com.mx/ap/signin"
    "?openid.pape.max_auth_age=0"
    "&openid.return_to=https%3A%2F%2Fwww.amazon.com.mx%2F"
    "&openid.identity=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select"
    "&openid.assoc_handle=mxflex"
    "&openid.mode=checkid_setup"
    "&openid.claimed_id=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select"
    "&openid.ns=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0"
)


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: playwright no está instalado en este venv.")
        return 2

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    print()
    print("=" * 60)
    print(" Login Amazon — sesión persistente robusta")
    print("=" * 60)
    print(f" Perfil: {PROFILE_DIR}")
    print()
    print(" Se abrirá Chromium NO headless apuntando a la página de")
    print(" login de Amazon directamente. Haz tu login normal:")
    print()
    print("   1. Email + contraseña")
    print("   2. 2FA si te lo pide")
    print("   3. Marca 'Mantenerme conectado'")
    print("   4. Espera a ver tu nombre arriba en la homepage")
    print()
    print(" Cuando hayas terminado, vuelve a esta terminal y presiona")
    print(" ENTER. NO cierres la ventana del navegador con la X — déjala")
    print(" abierta para que este script la cierre con flush limpio.")
    print()

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-extensions",
                "--no-first-run",
                "--disable-default-apps",
                "--disable-infobars",
                "--start-maximized",
            ],
            ignore_default_args=["--enable-automation"],
            viewport={"width": 1366, "height": 768},
            locale="es-MX",
            timezone_id="America/Mexico_City",
        )
        # Stealth básico
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = ctx.new_page()
        try:
            page.goto(INITIAL_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            print(f"WARN: goto falló ({exc}); el navegador igual está abierto.")

        print()
        print(" >>> Navegador abierto. Inicia sesión y presiona ENTER aquí <<<")
        try:
            input(" > ")
        except (EOFError, KeyboardInterrupt):
            pass

        # Mostrar URL final
        try:
            print(f"\n Última URL visitada: {page.url}")
        except Exception:
            pass

        # Flush ordenado: cerrar pages → context → playwright sale del with.
        try:
            for p in ctx.pages:
                try:
                    p.close()
                except Exception:
                    pass
        except Exception:
            pass
        ctx.close()

    # Verificar que el perfil quedó persistido en disco.
    files = list(PROFILE_DIR.rglob("*"))
    file_count = sum(1 for f in files if f.is_file())
    total_bytes = sum(f.stat().st_size for f in files if f.is_file())
    print()
    print(f" Perfil guardado: {file_count} archivos, {total_bytes / 1024:.1f} KB")
    if file_count == 0:
        print(" ⚠ El perfil quedó vacío. Repite el proceso.")
        return 3
    print(" ✓ Sesión Amazon persistida correctamente.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
