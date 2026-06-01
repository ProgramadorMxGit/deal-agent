"""Bot_Saneamiento — housekeeping oneshot del outbox.

Este paquete consolida los scripts ad-hoc de limpieza
(`scripts/_vps_cleanup_*.py` y
`scripts/cleanup_outbox_missing_prev_price.py`) en un único
subcomando testeable: ``python -m ofertas_hunter saneamiento``.

API pública (a re-exportar conforme se implementen los módulos en
tareas posteriores):

- ``SaneamientoRunner`` — orquestador del run.
- ``BaseSaneamientoTask`` — clase base de cada task.
- ``TaskResult``, ``SaneamientoReport`` — modelo de reporte.
- ``SaneamientoLock``, ``SaneamientoLockBusy`` — lockfile dedicado.
- ``SaneamientoRunArgs`` — args parseados del subcomando.
- ``cmd_saneamiento`` — entrypoint del subcomando.

En esta tarea (1.1) sólo se materializa el esqueleto del paquete.
Las re-exportaciones se añadirán en tareas posteriores a medida
que cada módulo se implemente, para evitar fallos de import sobre
símbolos aún no definidos.
"""

from __future__ import annotations
