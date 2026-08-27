# -*- coding: utf-8 -*-
"""Dónde vive cada cosa. UN solo lugar.

Juan (2026-08-26): *"hoy puedo hacer una carpeta en nuestro Drive que sea ERBEN
y ahí usa todo, y luego ponemos la cuenta que va a tener"*.

Por eso todo cuelga de **una constante** (`DRIVE`, pisable con la variable de
entorno `ERBEN_DRIVE`): el día que el estudio tenga su propia cuenta de Google,
se mueve la carpeta, se cambia una línea, y no hay que salir a cazar rutas por
cinco archivos. Antes de esto había rutas absolutas repartidas en los jobs y en
el server, cada una con su propio criterio.

    H:\\My Drive\\ERBEN\\
        respaldo\\                     copias de la base, con fecha
        estado\\                       lo que dejan los jobs (atp_estado.json…)
        clientes\\<CUIT> - <nombre>\\
            atp\\                      relevamientos del portal
            djs\\                      constancias de DJ presentada
            extractos\\                lo que baja del banco
            comprobantes\\             Mis Comprobantes de ARCA

⚠⚠ LA BASE NO VA AL DRIVE. Vive en `C:\\SIBRA\\estudio\\estudio.sqlite3` y solo
sus COPIAS suben. Un SQLite adentro de una carpeta que sincroniza es una receta
para corromperlo: Drive puede agarrar el archivo a mitad de una escritura y
subir un estado inconsistente — y peor, bajarlo después encima del bueno.

⚠ Y una copia en Drive **no es un respaldo completo**: si el archivo se corrompe,
la corrupción se sincroniza. Lo que salva es tener VARIAS copias con fecha (por
eso la retención) más el historial de versiones de Drive.
"""
import os
from pathlib import Path

# ── la carpeta del estudio en el Drive ───────────────────────────────────────
# Hoy cuelga del Drive nuestro. Cuando ERBEN tenga su cuenta, esto es lo único
# que cambia (o se setea ERBEN_DRIVE y ni eso).
DRIVE = Path(os.environ.get("ERBEN_DRIVE", r"H:\My Drive\ERBEN"))

# La carpeta en Drive (web): https://drive.google.com/drive/folders/<ID>
# Se anota para cuando el job hable con la API de Google; hoy se usa el disco.
DRIVE_ID = "11mAJsY5oZcRgZLrYnDJX3WmckVHwb7NP"

RESPALDO = DRIVE / "respaldo"
ESTADO = DRIVE / "estado"          # los .json que dejan los jobs
CLIENTES = DRIVE / "clientes"

# ⚠ LOS DATOS NO VAN EN EL CÓDIGO (Juan, 2026-08-27: *"esos datos van en la base
# de datos, el repo solo es para ver las cosas y operar"*).
#
# Hasta hoy el alta del primer cliente estaba HARDCODEADA en `server.py`: el
# CUIT de una persona real, sus actividades y sus agentes de retención vivían
# adentro del repo. Un repo público habría publicado el perfil fiscal de un
# contribuyente.
#
# Ahora eso es DATO: vive acá, en el Drive del estudio. Si el archivo no está,
# la base arranca vacía y los clientes se dan de alta por pantalla — que es
# como tiene que ser.
SEMILLA = DRIVE / "semilla.json"

# ── lo que NO va al Drive ────────────────────────────────────────────────────
RUNTIME = Path(os.environ.get("ERBEN_RUNTIME", r"C:\SIBRA\estudio"))
DB_PATH = Path(os.environ.get("ESTUDIO_DB", str(RUNTIME / "estudio.sqlite3")))
CREDENCIALES_GOOGLE = RUNTIME / "credentials.json"


def hay_drive():
    """¿Está montado el Drive? Si no, los jobs guardan igual en local y avisan
    — quedarse sin escribir porque no hay Drive sería peor."""
    try:
        return DRIVE.parent.exists()
    except OSError:
        return False


def carpeta_cliente(cuit, razon_social=None, sub=None):
    """La carpeta de un cliente, creada si no está.

    El nombre lleva CUIT **y** razón social: el CUIT es la identidad (dos
    clientes pueden escribir su nombre distinto) y el nombre es para que un
    humano encuentre la carpeta sin tener que memorizar números."""
    nombre = str(cuit)
    if razon_social:
        limpio = "".join(c for c in razon_social if c not in r'\/:*?"<>|').strip()
        nombre = f"{cuit} - {limpio[:40]}"
    p = CLIENTES / nombre
    if sub:
        p = p / sub
    return p


def asegurar(p):
    """Crea la carpeta y la devuelve. Si el Drive no está, no explota: devuelve
    la ruta igual y que el que llama decida."""
    try:
        Path(p).mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return Path(p)


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(f"\n  Drive:    {DRIVE}      {'✓ montado' if hay_drive() else '✗ NO está'}")
    print(f"  Respaldo: {RESPALDO}")
    print(f"  Estado:   {ESTADO}")
    print(f"  Clientes: {CLIENTES}")
    print(f"\n  Base:     {DB_PATH}      "
          f"{'✓' if DB_PATH.exists() else '✗ todavía no existe'}")
    print(f"  Google:   {CREDENCIALES_GOOGLE}   "
          f"{'✓' if CREDENCIALES_GOOGLE.exists() else '✗ falta'}")
    print(f"\n  Ejemplo:  {carpeta_cliente('30123456789', 'EJEMPLO SRL', 'djs')}\n")
