# -*- coding: utf-8 -*-
"""Credenciales del estudio — Credential Manager de Windows, namespace propio.

Mismo mecanismo que `Vinculacion bancos/tools/credenciales.py` (la clave queda
cifrada por Windows y atada al usuario, nunca en un archivo), pero bajo el
servicio **`erben-estudio`** en vez de `sibra-tesoreria`.

Por qué el namespace aparte (ARQUITECTURA.md §3): un alias del estudio y uno
nuestro pueden llamarse igual. Si compartieran namespace, cargar la clave de
"RODRIGUEZ" del estudio pisaría la de un "RODRIGUEZ" nuestro — y el síntoma
sería un job fallando con "usuario o contraseña incorrectos", que se lee como
un bug del scraper y no como lo que es.

Uso:
    from credenciales import obtener, guardar
    cred = obtener("DGR-Fsa", "RODRIGUEZ")   # {"usuario","clave","guardada"} o None

    py credenciales.py --set --fuente DGR-Fsa --alias RODRIGUEZ
    py credenciales.py --status
"""
import argparse
import datetime as dt
import getpass
import json
import sys

import keyring

SERVICIO = "erben-estudio"

# Cada cuántos días conviene rotar. No es una preferencia: los portales la
# vencen solos y el job empieza a fallar con un error que no dice que la clave
# venció.
DIAS_ROTACION = {
    "banco": 90,
    "arca": None,       # no vence por tiempo (sí se bloquea por intentos)
    "DGR-Fsa": None,
    "DGR-Ctes": None,
}


def _service(fuente):
    return f"{SERVICIO}:{fuente}"


def guardar(fuente, alias, usuario, clave):
    keyring.set_password(_service(fuente), alias, json.dumps({
        "usuario": usuario, "clave": clave, "guardada": dt.date.today().isoformat()}))


def obtener(fuente, alias):
    raw = keyring.get_password(_service(fuente), alias)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def estado_rotacion(fuente, alias):
    cred = obtener(fuente, alias)
    if not cred:
        return None
    limite = DIAS_ROTACION.get(fuente)
    guardada = cred.get("guardada")
    if not guardada:
        return {"dias": None, "limite": limite, "vencida": None}
    try:
        dias = (dt.date.today() - dt.date.fromisoformat(guardada)).days
    except ValueError:
        return {"dias": None, "limite": limite, "vencida": None}
    return {"dias": dias, "limite": limite,
            "vencida": (limite is not None and dias >= limite)}


def borrar(fuente, alias):
    try:
        keyring.delete_password(_service(fuente), alias)
        return True
    except keyring.errors.PasswordDeleteError:
        return False


def pedir_y_guardar(fuente, alias):
    print(f"Credencial de {alias} para '{fuente}' → Credential Manager de Windows.")
    usuario = input("  Usuario/CUIT: ").strip()
    clave = getpass.getpass("  Clave (no se muestra): ").strip()
    if not usuario or not clave:
        print("  Vacío — no se guardó nada.")
        return None
    guardar(fuente, alias, usuario, clave)
    print("  ✓ guardada y fechada hoy")
    return {"usuario": usuario, "clave": clave}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Credenciales del estudio")
    ap.add_argument("--set", action="store_true")
    ap.add_argument("--get", action="store_true")
    ap.add_argument("--delete", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--fuente")
    ap.add_argument("--alias")
    a = ap.parse_args()

    if a.status or not (a.set or a.get or a.delete):
        import clientes
        print(f"\n  Servicio: {SERVICIO}\n")
        for f in clientes.FUENTES:
            for c in clientes.de_fuente(f):
                est = estado_rotacion(f, c["alias"])
                if est is None:
                    marca = "FALTA"
                elif est.get("vencida"):
                    marca = f"ROTAR ({est['dias']}d)"
                elif est.get("dias") is None:
                    marca = "ok (s/fecha)"
                else:
                    marca = f"ok ({est['dias']}d)"
                print(f"  {f:<10} {c['alias']:<12} {marca}")
        return 0
    if not (a.fuente and a.alias):
        print("Hacen falta --fuente y --alias")
        return 2
    if a.set:
        pedir_y_guardar(a.fuente, a.alias)
    elif a.get:
        c = obtener(a.fuente, a.alias)
        print(f"  {'existe, usuario ' + c['usuario'] if c else 'no hay credencial guardada'}")
    elif a.delete:
        print("  borrada" if borrar(a.fuente, a.alias) else "  no había nada")
    return 0


if __name__ == "__main__":
    sys.exit(main())
