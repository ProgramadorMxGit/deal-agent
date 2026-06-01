"""Verifica en el VPS que el fix detecta audio como accesorio."""
from ofertas_hunter.intelligence.accessory_detector import is_generic_accessory
from ofertas_hunter.intelligence.price_error_scorer import _fingerprint_smartphone

cases = [
    ("XIAOMI Audifonos Redmi Buds 6 Play Negro", True, False),
    ("Sony Audifonos inalambricos on-Ear WH-CH520", True, False),
    ("JBL Flip 7 Bocina Portatil Bluetooth", True, False),
    ("Galaxy Buds 2 Pro Audifonos", True, False),
    ("iPhone 15 Pro Max 256GB", False, True),
    ("Samsung Galaxy S24 Ultra", False, True),
    # Premium audio que debe seguir como NO accesorio:
    ("Audifonos Sony WF-1000XM5 Bluetooth", False, False),
    ("AirPods Pro 2 Apple", False, False),
]

print(f"  {'expect_acc':>12}  {'actual_acc':>11}  {'expect_phn':>11}  {'actual_phn':>11}  title")
print(f"  {'-'*12}  {'-'*11}  {'-'*11}  {'-'*11}  {'-'*60}")
ok_count = 0
for title, expect_acc, expect_phone in cases:
    actual_acc = is_generic_accessory(title)
    actual_phone = _fingerprint_smartphone(title)
    ok = (actual_acc == expect_acc) and (actual_phone == expect_phone)
    if ok:
        ok_count += 1
    mark = "OK" if ok else "FAIL"
    print(f"  {str(expect_acc):>12}  {str(actual_acc):>11}  {str(expect_phone):>11}  {str(actual_phone):>11}  {title}  [{mark}]")

print()
print(f"  {ok_count}/{len(cases)} casos correctos")
