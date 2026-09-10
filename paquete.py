# -*- coding: utf-8 -*-
"""Los parsers se bajan firmados, y solo con el permiso vigente.

El chequeo de licencia (`licencia.py`) es un candado de vidrio: esto es Python
en la máquina del otro y quien sepa lo borra. Lo que retiene de verdad es esto
— la pieza que el estudio **no puede reproducir**: los jobs que saben entrar a
ARCA y a los portales de rentas.

Los portales cambian seguido. Un sistema que se quedó con la última versión que
alcanzó a bajar deja de entrar en unos meses, y no hay nada que hackear para
arreglarlo: hay que volver a escribir el scraper. Ese es el amarre, y es sano
— obliga a seguir dando valor en vez de a vigilar.

Qué viaja
---------
**Baja código, no sube nada.** El pedido lleva el número de instalación y la
versión que ya tiene; nada de los datos del estudio ni de sus clientes. Esa es
la línea y no se cruza: el argumento de venta es que la contabilidad no sale de
la máquina, y un solo campo de más lo convierte en mentira.

La firma protege al ESTUDIO, no a nosotros
------------------------------------------
Verificar la firma no impide que alguien se quede con el paquete que ya bajó —
una vez que corrió, está en su disco. Lo que impide es que un tercero le sirva
al estudio un paquete propio: acá adentro van los jobs que se loguean en ARCA
**con las claves del contribuyente**. Un parser adulterado es una fuga de
credenciales fiscales, así que un paquete sin firma válida no se ejecuta, ni
siquiera si eso deja al estudio sin poder correr los jobs hoy.

Y si no hay internet: se usa el último paquete verificado que haya en disco,
mientras la licencia esté vigente.
"""
import hashlib
import json
import os
import shutil
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import licencia
import rutas

# De dónde se bajan. Vacío = no hay entrega remota y se usan los parsers que
# vengan con la instalación (que es como corre hoy en el estudio que lo hace).
URL = os.environ.get("ERBEN_URL_PAQUETE", "")

CARPETA = rutas.RUNTIME / "parsers"        # ⚠ fuera del Drive, como la base
ACTUAL = CARPETA / "instalado.json"
ESPERA = 20                                 # segundos


def _sha256(b):
    return hashlib.sha256(b).hexdigest()


def instalado():
    """Qué paquete hay puesto, o None."""
    try:
        return json.loads(ACTUAL.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _bajar(url, espera=ESPERA):
    req = urllib.request.Request(url, headers={"User-Agent": "ERBEN-ESTUDIO"})
    with urllib.request.urlopen(req, timeout=espera) as r:
        return r.read()


def _verificar(manifiesto, zip_bytes):
    """La firma del manifiesto y que el zip sea EL del manifiesto."""
    if not licencia.firma_valida(manifiesto):
        return "la firma del paquete no cierra"
    if _sha256(zip_bytes) != manifiesto.get("sha256"):
        return "el archivo bajado no es el que dice el manifiesto"
    return None


def _instalar(manifiesto, zip_bytes):
    """Descomprime y verifica archivo por archivo.

    El hash del zip entero ya se controló, pero se vuelve a mirar cada archivo
    después de extraer: un zip puede traer entradas de más, o rutas con `..`
    que escriban fuera de la carpeta."""
    destino = CARPETA / str(manifiesto["version"])
    tmp = CARPETA / f".tmp-{manifiesto['version']}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    crudo = CARPETA / f".tmp-{manifiesto['version']}.zip"
    crudo.write_bytes(zip_bytes)
    with zipfile.ZipFile(crudo) as z:
        for nombre in z.namelist():
            if nombre.endswith("/"):
                continue
            if ".." in nombre or Path(nombre).is_absolute():
                shutil.rmtree(tmp, ignore_errors=True)
                return f"el paquete trae una ruta que se sale de la carpeta: {nombre}"
            z.extract(nombre, tmp)
    crudo.unlink(missing_ok=True)

    for nombre, sha in (manifiesto.get("archivos") or {}).items():
        p = tmp / nombre
        if not p.exists() or _sha256(p.read_bytes()) != sha:
            shutil.rmtree(tmp, ignore_errors=True)
            return f"«{nombre}» no coincide con el manifiesto"

    shutil.rmtree(destino, ignore_errors=True)
    tmp.replace(destino)
    ACTUAL.write_text(json.dumps(
        {k: v for k, v in manifiesto.items() if k != "archivos"},
        ensure_ascii=False, indent=2), encoding="utf-8")
    return None


def asegurar(url=None, forzar=False):
    """Deja el paquete listo y devuelve (carpeta, novedad).

    `carpeta` es None cuando no hay nada usable — y ahí los jobs no corren.
    `novedad` es la línea para mostrar o loguear."""
    est = licencia.estado()
    if not est["puede_escribir"]:
        # Sin permiso no se baja ni se corre: es exactamente el punto.
        puesto = instalado()
        return None, f"{est['titulo']} — los jobs no corren."

    url = url if url is not None else URL
    puesto = instalado()
    if not url:
        return (CARPETA / str(puesto["version"]) if puesto else None,
                "sin entrega remota configurada")

    sep = "&" if "?" in url else "?"
    lic = licencia.leer() or {}
    pedido = (f"{url}{sep}instalacion={lic.get('instalacion', '')}"
              f"&tengo={(puesto or {}).get('version', '')}")
    try:
        manifiesto = json.loads(_bajar(pedido))
    except (urllib.error.URLError, ValueError, OSError) as e:
        # Sin internet se sigue con lo que hay: cortarle el día al estudio
        # porque se cayó la conexión sería un bug nuestro, no un candado.
        if puesto:
            return CARPETA / str(puesto["version"]), f"sin conexión: sigo con {puesto['version']}"
        return None, f"no pude bajar los parsers y no hay ninguno instalado ({e})"

    if not forzar and puesto and str(puesto.get("version")) == str(manifiesto.get("version")):
        return CARPETA / str(puesto["version"]), f"al día ({puesto['version']})"

    try:
        zip_bytes = _bajar(manifiesto["zip"] if manifiesto.get("zip", "").startswith("http")
                           else url.rsplit("/", 1)[0] + "/" + manifiesto["zip"])
    except (urllib.error.URLError, KeyError, OSError) as e:
        if puesto:
            return CARPETA / str(puesto["version"]), f"no pude bajar el paquete: {e}"
        return None, f"no pude bajar el paquete: {e}"

    mal = _verificar(manifiesto, zip_bytes) or _instalar(manifiesto, zip_bytes)
    if mal:
        # ⚠ NO se corre. Acá adentro van los jobs que se loguean en ARCA con
        # las claves del contribuyente.
        if puesto:
            return CARPETA / str(puesto["version"]), f"⚠ paquete rechazado ({mal}); sigo con {puesto['version']}"
        return None, f"⚠ paquete rechazado: {mal}"
    return CARPETA / str(manifiesto["version"]), f"actualizado a {manifiesto['version']}"


def carpeta_de_jobs(local):
    """De dónde salen los jobs: el paquete bajado, o los de la instalación.

    `local` es la carpeta `parsers/` que viene con el código. Se usa cuando no
    hay entrega remota configurada — el modo en que corre hoy el estudio que lo
    desarrolla."""
    p, _ = asegurar()
    return p if p and p.exists() else Path(local)


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    p, novedad = asegurar(forzar="--forzar" in sys.argv)
    print(f"\n  URL:      {URL or '— sin entrega remota —'}")
    print(f"  Carpeta:  {p or '—'}")
    print(f"  Estado:   {novedad}\n")
