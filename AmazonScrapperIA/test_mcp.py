"""Prueba rápida del servidor MCP."""
import sys
sys.path.insert(0, ".")

try:
    from mcp_server import server, _price, _disc_text, _offers
    print("✅ mcp_server importado correctamente")

    assert _price("$1,299.00") == 1299.0
    assert _price("$15,999") == 15999.0
    assert _disc_text("-65%") == 65
    assert _disc_text("50% de descuento") == 50
    print("✅ Helpers de precio funcionando")

    offers = _offers()
    print(f"✅ Ofertas guardadas: {len(offers)}")

    print("\n✅ Servidor MCP listo")
    print("\nEjecutar el agente autónomo:")
    print('  kiro-cli chat --agent amazon-hunter --trust-all-tools "Inicia una sesion de caza de ofertas autonoma en Amazon MX. Busca al menos 10 ofertas con descuento mayor o igual a 50%."')
    print("\nVer reporte:")
    print('  kiro-cli chat --agent amazon-hunter --no-interactive "Muestra el reporte de inteligencia con todas las ofertas encontradas"')

except Exception as e:
    print(f"❌ Error: {e}")
    import traceback
    traceback.print_exc()
