# -*- coding: utf-8 -*-
"""Respaldo de la base — en caliente, con fecha y con retención.

Hasta hoy no había ninguno: si ese disco moría, se perdía la contabilidad de
todos los clientes del estudio. Es un riesgo mucho más grande que cualquier
discusión sobre el motor de base.

Cómo copia
----------
Con `sqlite3.Connection.backup()`, que es la API de respaldo EN CALIENTE de
SQLite. No es `shutil.copy`: copiar el archivo a mano mientras alguien escribe
puede llevarse un estado inconsistente, y el `.sqlite3-wal` que queda afuera se
lleva las últimas transacciones. Esta API coordina con el motor y la copia sale
íntegra aunque el sistema esté en uso.

Después de copiar, **verifica la copia** (`PRAGMA integrity_check` + que tenga
las tablas y filas esperadas). Un respaldo que nadie probó no es un respaldo:
es un archivo.

Dónde
-----
`<Drive del estudio>/respaldo/estudio_<fecha>.sqlite3` — ver `rutas.py`. La base
en sí NUNCA vive en el Drive (un SQLite dentro de una carpeta que sincroniza se
corrompe): solo suben las copias.

Retención
---------
Se guardan las últimas `COPIAS` y se borran las más viejas. Tener una sola es
casi lo mismo que no tener ninguna: si el problema es corrupción, la copia de
hoy ya está corrupta y hace falta la de anteayer.
"""
import re
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rutas  # noqa: E402

COPIAS = 10          # cuántas se conservan
DIAS_AVISO = 3       # a partir de acá el panel avisa que está viejo


def _verificar(destino):
    """Que la copia sirva de verdad: íntegra y con datos adentro."""
    con = sqlite3.connect(destino)
    try:
        estado = con.execute("PRAGMA integrity_check").fetchone()[0]
        if estado != "ok":
            return False, f"integrity_check dice: {estado}"
        n = con.execute("SELECT COUNT(*) FROM clientes").fetchone()[0]
        tablas = con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        return True, f"{tablas} tablas · {n} cliente(s)"
    except sqlite3.Error as e:
        return False, str(e)
    finally:
        con.close()


def copiar(origen=None, carpeta=None, motivo="manual"):
    """Hace la copia y limpia las viejas. Devuelve un dict con lo que pasó —
    nunca lanza: que falle el respaldo no puede impedir que el sistema arranque."""
    origen = Path(origen or rutas.DB_PATH)
    carpeta = Path(carpeta or rutas.RESPALDO)
    if not origen.exists():
        return {"ok": False, "error": "todavía no hay base que respaldar"}
    if not rutas.hay_drive() and carpeta == rutas.RESPALDO:
        return {"ok": False, "error": f"el Drive no está montado ({rutas.DRIVE.parent})"}

    rutas.asegurar(carpeta)
    # Con SEGUNDOS: con resolución de minuto, dos respaldos seguidos escriben
    # el mismo archivo y el segundo pisa al primero — se cree tener dos copias
    # y hay una. Lo encontró la prueba de retención.
    sello = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    destino = carpeta / f"estudio_{sello}.sqlite3"
    try:
        con = sqlite3.connect(origen)
        dst = sqlite3.connect(destino)
        with dst:
            con.backup(dst)          # respaldo EN CALIENTE
        dst.close()
        con.close()
    except Exception as e:
        return {"ok": False, "error": f"no se pudo copiar: {e}"}

    bien, detalle = _verificar(destino)
    if not bien:
        # una copia rota es peor que ninguna: engaña
        try:
            destino.unlink()
        except OSError:
            pass
        return {"ok": False, "error": f"la copia salió mal y se descartó — {detalle}"}

    borradas = _limpiar(carpeta)
    return {"ok": True, "archivo": str(destino), "motivo": motivo,
            "mb": round(destino.stat().st_size / 1048576, 2),
            "detalle": detalle, "borradas": borradas}


def _limpiar(carpeta, cuantas=COPIAS):
    viejas = sorted(Path(carpeta).glob("estudio_*.sqlite3"),
                    key=lambda p: p.name, reverse=True)[cuantas:]
    n = 0
    for v in viejas:
        try:
            v.unlink()
            n += 1
        except OSError:
            pass
    return n


def estado(carpeta=None):
    """Para el panel: cuándo fue la última y si conviene avisar."""
    carpeta = Path(carpeta or rutas.RESPALDO)
    # La forma de la respuesta es SIEMPRE la misma. Antes, sin copias, faltaban
    # `ultima`, `dias` y `mb`, y el que leía esos campos se rompía justo en el
    # caso raro — que es cuando más importa que la pantalla funcione.
    vacio = {"hay": False, "copias": 0, "ultima": None, "dias": None,
             "mb": None, "carpeta": str(carpeta), "avisar": True}

    # ⚠ NO ES LO MISMO «nunca se respaldó» QUE «no puedo ver el Drive» (03/09).
    # Decir lo primero cuando pasa lo segundo es una acusación falsa: las
    # copias pueden estar todas ahí y lo que se cayó es el montaje de Google
    # Drive. Y el arreglo es distinto: una pide respaldar, la otra prender
    # Drive.
    if not rutas.hay_drive():
        return {**vacio, "mensaje": f"El Drive del estudio no está montado "
                                    f"({rutas.DRIVE}). No puedo ver las copias "
                                    f"— pueden estar todas ahí.",
                "sin_drive": True}
    try:
        copias = sorted(carpeta.glob("estudio_*.sqlite3"), key=lambda p: p.name, reverse=True)
    except OSError:
        copias = []
    if not copias:
        return {**vacio, "mensaje": "Nunca se respaldó la base."}
    ultima = copias[0]
    # Por regex y no por posición: un cambio de una letra en el prefijo movía
    # todos los índices y la fecha salía mal sin que nadie se entere.
    m = re.search(r"(\d{4}-\d{2}-\d{2})_(\d{4})(\d{2})?", ultima.name)
    try:
        cuando = datetime.strptime(m.group(1) + "_" + m.group(2), "%Y-%m-%d_%H%M")
    except (AttributeError, ValueError):
        cuando = datetime.fromtimestamp(ultima.stat().st_mtime)
    dias = (date.today() - cuando.date()).days
    return {
        "hay": True, "copias": len(copias),
        "ultima": cuando.isoformat(timespec="minutes"),
        "dias": dias,
        "mb": round(ultima.stat().st_size / 1048576, 2),
        "carpeta": str(carpeta),
        "avisar": dias >= DIAS_AVISO,
        "mensaje": (f"Hace {dias} día(s) que no se respalda." if dias >= DIAS_AVISO
                    else f"Última copia: {cuando.strftime('%d/%m/%Y %H:%M')}"),
    }


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    import argparse
    ap = argparse.ArgumentParser(description="Respalda la base del estudio")
    ap.add_argument("--estado", action="store_true", help="solo informar")
    a = ap.parse_args()

    if a.estado:
        e = estado()
        print(f"\n  {e['mensaje']}")
        if e["hay"]:
            print(f"  {e['copias']} copia(s) en {e['carpeta']}")
        return 0

    print(f"\n  Base:     {rutas.DB_PATH}")
    print(f"  Destino:  {rutas.RESPALDO}")
    r = copiar(motivo="a mano")
    if not r["ok"]:
        print(f"\n  ⛔ {r['error']}")
        return 1
    print(f"\n  ✓ {Path(r['archivo']).name} — {r['mb']} MB · {r['detalle']}")
    if r["borradas"]:
        print(f"    ({r['borradas']} copia(s) vieja(s) borradas, se guardan las últimas {COPIAS})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
