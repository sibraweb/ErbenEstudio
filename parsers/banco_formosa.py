# -*- coding: utf-8 -*-
"""Banco de Formosa: entrar, bajar los resúmenes y cargarlos — en UNA ventana.

Juan (18/09): *«ponerlo ya en el job de Banco de Formosa: tenemos el que entra
a la cuenta y el que trae los resúmenes»*. Son dos scripts de SIBRA
(`sesion_formosa.py` y `formosa_resumenes.py`) y los dos andan. Lo que faltaba
es lo del medio y lo del final: que lo bajado caiga en la casa del ESTUDIO y
que entre al sistema sin que nadie se acuerde de correr otro paso.

Qué hace, en orden
------------------
1. Abre el home banking con ventana visible y ESPERA a que la persona entre.
   El login es a mano —el banco lo pide así— y la clave la escribe ella: el
   job no la ve ni la guarda.
2. Adentro de ESA ventana baja los resúmenes PDF que haya, uno por mes.
3. Los guarda en la carpeta del cliente en el Drive del estudio.
4. Los carga, cada uno a la cuenta que dice su propio encabezado.

⚠ TODO EN LA MISMA VENTANA, y no es un gusto: verificado el 07/08 en SIBRA, la
sesión de este banco **no sobrevive fuera de su ventana**. Se guardó la sesión
recién creada, se la reabrió en otro proceso a los minutos, y rebotó al login.
No hay job sin persona posible para este banco: hay un login atendido y todo
lo demás automático adentro.

⚠ Y A LA CASA DEL ESTUDIO. El script prestado escribe en NUESTRO Drive
(`web_sibra\\tesoreria\\formosa`) y guarda la sesión en `C:\\SIBRA\\tesoreria`:
los extractos de un cliente del estudio no van ahí, y la cuenta de un
contribuyente no puede quedar logueada en una carpeta nuestra. Se redirigen
las dos cosas antes de usarlo — el mismo arreglo que `arca_comprobantes.py`,
que pasó a hacer falta el día que ocho archivos de un cliente aparecieron en
la carpeta de SIBRA.

La cuenta sale del PDF, no se adivina
-------------------------------------
Cada resumen trae impreso su número de cuenta («N° 270154/1»). Se carga en la
cuenta del cliente que tiene ESE número. Si el cliente no la tiene dada de
alta, no se carga en otra parecida: se avisa y se sigue.

Uso
---
    py banco_formosa.py --alias DAECOS                  # entrar, bajar y cargar
    py banco_formosa.py --alias DAECOS --meses 2026-09  # solo esos meses
    py banco_formosa.py --alias DAECOS --solo-cargar    # sin banco: carga lo que ya hay
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI))
sys.path.insert(0, str(AQUI.parent))

import clientes  # noqa: E402
import rutas  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

API = "http://localhost:8310"
TOOLS_SIBRA = Path(os.environ.get(
    "SIBRA_TOOLS",
    Path(__file__).resolve().parents[2] / "Vinculacion bancos" / "tools"))

# Lo que imprime el banco en el encabezado del resumen: «N° 270154/1». Los
# dígitos del número y de la sub-cuenta, sin el texto alrededor.
NUMERO_CUENTA = re.compile(r"N[°º]\s*(\d{3,}/\d+)")


def _api(ruta):
    with urllib.request.urlopen(API + ruta, timeout=30) as r:
        return json.loads(r.read())


def numero_de_cuenta(pdf):
    """El número de cuenta que dice el PDF, o None si no se lee."""
    try:
        import pdfplumber
        with pdfplumber.open(pdf) as doc:
            texto = doc.pages[0].extract_text() or ""
    except Exception:
        return None
    m = NUMERO_CUENTA.search(texto.replace("\n", " "))
    return m.group(1) if m else None


def cuenta_del_cliente(alias, numero):
    """La cuenta de Banco de Formosa (BCRA 315) del cliente con ese número."""
    if not numero:
        return None
    for c in _api(f"/api/c/cuentas?cliente={alias}"):
        if (c.get("codigo_bcra") == "315"
                and re.sub(r"\s", "", c.get("numero") or "") == numero):
            return c
    return None


def cargar(alias, pdf):
    """Un PDF al sistema. Devuelve (ok, líneas para mostrar)."""
    numero = numero_de_cuenta(pdf)
    cta = cuenta_del_cliente(alias, numero)
    if not cta:
        return False, [f"   {pdf.name}: ⚠ el resumen es de la cuenta "
                       f"{numero or '(no se lee el número)'}, y {alias} no tiene "
                       "esa cuenta dada de alta. No lo cargo en otra: dala de alta "
                       "en Bancos → Cuentas y volvé a correr con --solo-cargar."]
    p = subprocess.run([sys.executable, str(AQUI / "cargar_extracto.py"),
                        "--alias", alias, "--cuenta", str(cta["id"]), "--archivo", str(pdf)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    salida = (p.stdout or "") + (p.stderr or "")
    utiles = [l.strip() for l in salida.splitlines()
              if any(k in l for k in ("CARGADO", "Cadena", "cadena", "arranque", "No ", "⚠"))]
    return p.returncode == 0, [f"   {pdf.name} → cuenta {numero}"] + [f"      {l}" for l in utiles]


def bajar(alias, carpeta, meses):
    """Entra al banco (a mano) y baja los resúmenes a `carpeta`. Devuelve cuántos."""
    sys.path.insert(0, str(TOOLS_SIBRA))
    try:
        import sesion_formosa as SF
        import formosa_resumenes as FR
    except ImportError as e:
        print(f"No encuentro los scripts de Banco de Formosa en {TOOLS_SIBRA} ({e}).\n"
              "Son heredados de SIBRA: si el estudio corre en otra máquina hay que "
              "traerlos (ver parsers/LEEME.md).")
        return None
    from playwright.sync_api import sync_playwright

    # ── el préstamo, redirigido al estudio ──
    SF.RUNTIME_DIR = rutas.RUNTIME / "sesiones"
    FR.SALIDA = carpeta
    SF.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    st = SF.storage_de(alias)
    with sync_playwright() as p:
        b = p.chromium.launch(headless=False)
        # accept_downloads: sin esto el PDF se descarta sin dejar rastro.
        ctx = b.new_context(storage_state=str(st) if st.exists() else None,
                            accept_downloads=True, viewport={"width": 1500, "height": 950})
        page = ctx.new_page()
        page.goto(SF.URL_HOME, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        if page.locator('input[type="password"]:visible').count():
            print("=" * 64)
            print(f"  TE TOCA ENTRAR: logueate con la cuenta de {alias}.")
            print("  Tenés 5 minutos. Cuando entres, bajo los resúmenes solo.")
            print("=" * 64)
            for _ in range(30):
                page.wait_for_timeout(10000)
                if page.locator('input[type="password"]:visible').count() == 0:
                    break
            else:
                print(">>> No se completó el login en 5 minutos. No bajé nada.")
                b.close()
                return None
        n = FR.correr(page, alias, meses)
        try:
            ctx.storage_state(path=str(st))
        except Exception:
            pass
        b.close()
    return n


def main():
    ap = argparse.ArgumentParser(description="Banco de Formosa: bajar y cargar resúmenes")
    ap.add_argument("--alias", required=True, help="cliente del estudio")
    ap.add_argument("--meses", default=None, help="ej: 2026-08,2026-09 (vacío = todos)")
    ap.add_argument("--solo-cargar", action="store_true",
                    help="no entra al banco: carga los PDF que ya estén en la carpeta")
    a = ap.parse_args()

    cli = clientes.por_alias(a.alias)
    if not cli:
        print(f"No existe «{a.alias}».")
        return 2
    carpeta = rutas.asegurar(rutas.carpeta_cliente(cli["cuit"], cli.get("razon_social"),
                                                   "extractos"))
    print(f"\n  Banco de Formosa · {a.alias}")
    print(f"  Carpeta: {carpeta}\n")

    if not a.solo_cargar:
        n = bajar(a.alias, carpeta, a.meses)
        if n is None:
            return 1
        print(f"\n  {n} resumen(es) nuevo(s) bajado(s).\n")

    # Se cargan TODOS los PDF de la carpeta, no solo los de recién: el
    # cargador no duplica (huella + ordinal), así que un resumen que ya estaba
    # entra en cero, y uno que se bajó en una corrida que falló antes de
    # cargar se recupera solo.
    pdfs = sorted(carpeta.glob("*.pdf"))
    if not pdfs:
        print("  No hay resúmenes en la carpeta.")
        return 0
    print(f"  Cargando {len(pdfs)} resumen(es):")
    mal = 0
    for pdf in pdfs:
        ok, lineas = cargar(a.alias, pdf)
        mal += not ok
        print("\n".join(lineas))
    print(f"\n  Listo{'' if not mal else f' — {mal} con problemas, mirá arriba'}.\n")
    return 0 if not mal else 1


if __name__ == "__main__":
    sys.exit(main())
