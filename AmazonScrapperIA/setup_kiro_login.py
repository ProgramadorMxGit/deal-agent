#!/usr/bin/env python3
"""
setup_kiro_login.py — Autentica kiro-cli para que el scraper pueda usar la IA.

Ejecutar UNA SOLA VEZ antes de usar el scraper:
    py setup_kiro_login.py
"""
import subprocess
import sys
import os
import time
import webbrowser
from pathlib import Path

KIRO_CLI = r"C:\Users\Programador Mx\AppData\Local\kiro-cli\kiro-cli.exe"

def check_logged_in() -> bool:
    try:
        result = subprocess.run(
            [KIRO_CLI, "whoami"],
            capture_output=True, text=True, timeout=10
        )
        return result.returncode == 0 and result.stdout.strip() and "Not logged in" not in result.stdout
    except Exception:
        return False

def main():
    print("=" * 62)
    print("   KIRO-CLI LOGIN -- Amazon Scraper IA")
    print("=" * 62)
    print()

    if not Path(KIRO_CLI).exists():
        print(f"❌ kiro-cli no encontrado en: {KIRO_CLI}")
        print("   Verifica que Kiro-CLI esté instalado.")
        sys.exit(1)

    # Verificar si ya está logueado
    if check_logged_in():
        result = subprocess.run([KIRO_CLI, "whoami"], capture_output=True, text=True)
        print(f"✅ Ya estás logueado: {result.stdout.strip()}")
        print("\n🚀 Puedes ejecutar el scraper:")
        print("   py run_with_kiro.py")
        return

    print("📋 Iniciando proceso de login con Builder ID (cuenta gratuita de AWS)...")
    print("   Se abrirá tu browser para confirmar el código.\n")

    # Iniciar login con device flow
    proc = subprocess.Popen(
        [KIRO_CLI, "login", "--license", "free", "--use-device-flow"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding='utf-8',
        errors='replace'
    )

    # Leer output para mostrar el código
    code_shown = False
    url_shown = False
    start = time.time()

    print("⏳ Esperando código de verificación...")

    for line in proc.stdout:
        line = line.strip()
        if line:
            print(f"   {line}")

        # Detectar código
        if "Code:" in line and not code_shown:
            code = line.split("Code:")[-1].strip()
            print(f"\n{'='*50}")
            print(f"🔑 CÓDIGO DE VERIFICACIÓN: {code}")
            print(f"{'='*50}")
            print("\n1. Abre: https://view.awsapps.com/start/#/")
            print(f"2. Ingresa el código: {code}")
            print("3. Aprueba el acceso")
            print("\nEsperando confirmación...\n")
            # Abrir browser automáticamente
            try:
                webbrowser.open("https://view.awsapps.com/start/#/")
            except Exception:
                pass
            code_shown = True

        # Timeout de 3 minutos
        if time.time() - start > 180:
            print("⏰ Timeout — intenta de nuevo")
            proc.terminate()
            break

    proc.wait()

    # Verificar resultado
    if check_logged_in():
        result = subprocess.run([KIRO_CLI, "whoami"], capture_output=True, text=True)
        print(f"\n✅ Login exitoso: {result.stdout.strip()}")
        print("\n🚀 Ahora puedes ejecutar el scraper:")
        print("   py run_with_kiro.py")
        print("   py run_with_kiro.py --continuous")
    else:
        print("\n⚠️  Login no completado.")
        print("\nAlternativa: usa ANTHROPIC_API_KEY")
        print("   1. Obtén tu key en: https://console.anthropic.com/")
        print("   2. Ejecuta: $env:ANTHROPIC_API_KEY='sk-ant-...'")
        print("   3. Luego: py run_with_kiro.py")
        print("\nO ejecuta el scraper sin IA (evaluación por reglas):")
        print("   py run_with_kiro.py --no-ai")

if __name__ == "__main__":
    main()
