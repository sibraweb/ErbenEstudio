# -*- coding: utf-8 -*-
"""ARCA — Mis Comprobantes, para un CLIENTE DEL ESTUDIO.

Por qué existe si el ERP ya tiene uno
-------------------------------------
El job de nuestro sistema (`Vinculacion bancos/tools/mis_comprobantes.py`) hace
tres cosas que acá NO queremos:

  1. lee los contribuyentes de `contribuyentes.py`, un dict con NUESTRAS
     empresas — un cliente del estudio no está ahí, y agregarlo sería
     mezclar los dos mundos;
  2. copia el export a ``H:\\My Drive\\SIBRA_ERP\\FACTURAS`` — la carpeta de
     nuestro ERP (Juan, 2026-09-02: *"carga la base en la carpeta de drive de
     este sistema, no en la que usamos de sibra erp"*);
  3. cierra la cadena corriendo el parser del ERP y la carga a **nuestra**
     Supabase.

Acá el destino es uno solo y sale de `rutas.py`:

    H:\\My Drive\\ERBEN\\clientes\\<CUIT> - <NOMBRE>\\comprobantes\\

Lo que SÍ se reusa
------------------
El login de ARCA es un campo minado que ya está desminado en
`tools/sesion_arca.py`: los dos pasos del formulario, la sesión vencida que no
vuelve a `auth.afip.gob.ar`, y sobre todo que **cada servicio guarda su propia
cookie** (Mis Comprobantes vive en `fes.afip.gob.ar` y su sesión solo queda
capturada si se ENTRA al servicio durante el login). Reescribir eso sería
volver a pisar las mismas minas, así que se importa y se le cambian dos cosas:

  · dónde guarda la sesión  → ``C:\\SIBRA\\estudio\\sesiones\\``
  · de dónde saca la clave  → el Credential Manager del estudio (`EST/arca`)

⚠ Sin lo segundo, un alias del estudio que se llame igual que uno nuestro
autocompletaría NUESTRA clave fiscal en el login de un cliente.

⚠⚠ CONTROL DE IDENTIDAD. Antes de bajar nada verifica que el contribuyente
elegido en ARCA sea el del `--alias`. Bajar los comprobantes del CUIT
equivocado dentro de la carpeta de un cliente es peor que no bajarlos: entra
como suyo y nadie lo mira de nuevo.

Uso:
    py arca_comprobantes.py --alias DEMO
    py arca_comprobantes.py --alias DEMO --desde 01/01/2026 --hasta 02/09/2026
    py arca_comprobantes.py --alias DEMO --solo emitidos
    py arca_comprobantes.py --alias DEMO --login     # forzar login a mano

Salida: 0 bajó · 1 sesión vencida / login no completado · 2 no bajó nada ·
3 el contribuyente de la sesión no es el del alias
"""
import argparse
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI))
sys.path.insert(0, str(AQUI.parent))

import clientes  # noqa: E402
import credenciales  # noqa: E402
import rutas  # noqa: E402

# Los jobs prestados viven en el otro repo — la MISMA constante que usa
# `suite.py`, para que el día que el estudio corra en otra máquina se cambie
# en un solo lugar.
TOOLS_SIBRA = Path(os.environ.get(
    "SIBRA_TOOLS",
    Path(__file__).resolve().parents[2] / "Vinculacion bancos" / "tools"))
sys.path.insert(0, str(TOOLS_SIBRA))

try:
    import sesion_arca as SA
except ImportError as e:
    print(f"No encuentro el login de ARCA en {TOOLS_SIBRA} ({e}).\n"
          "Es un job heredado: si el estudio corre en otra máquina hay que "
          "traerlo o reescribirlo (ver parsers/LEEME.md).")
    sys.exit(1)

from playwright.sync_api import sync_playwright  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ── el préstamo, redirigido al estudio ──────────────────────────────────────
SESIONES = rutas.RUNTIME / "sesiones"
SA.RUNTIME_DIR = SESIONES
SA.obtener_credencial = lambda fuente, alias: credenciales.obtener(fuente, alias)


def redirigir_relevamientos(carpeta):
    """El login prestado NO solo loguea: aprovecha la ventana abierta para
    relevar Cuentas Tributarias, MIS FACILIDADES (planes de pago) y los VEP
    pagados — un login, tres datos (idea de Juan, 21/08). Eso esta bien y se
    queda; el problema es DONDE los escribe.

    Paso de verdad el 02/09: la primera corrida para un cliente del estudio
    dejo 8 archivos con sus planes de pago y sus VEP en
    la carpeta ARCA de NUESTRO sistema (web_sibra/tesoreria/arca).
    Los datos fiscales de un cliente del estudio no van ahi.

    Los modulos calculan su ruta al importarse, asi que hay que pisar tambien
    lo que ya derivo de ella (MANIFIESTO), no solo SALIDA."""
    for nombre in ("cct_vencimientos", "mis_facilidades"):
        try:
            mod = __import__(nombre)
        except Exception as e:
            print(f"  (relevamiento {nombre}: no esta - {str(e)[:50]})")
            continue
        mod.SALIDA = carpeta
        if hasattr(mod, "MANIFIESTO"):
            mod.MANIFIESTO = carpeta / "relevado.json"

URL_MCMP = "https://fes.afip.gob.ar/mcmp/jsp/index.do"

SECCIONES = {
    # sección: (selector del menú, prefijo del archivo)
    "recibidos": ("a[href*=comprobantesRecibidos]", "Recibidos"),
    "emitidos": ("a[href*=comprobantesEmitidos]", "Emitidos"),
}

# ⚠ ARCA RECORTA EL RANGO Y NO AVISA (relevado en el ERP el 2026-08-31: se
# pidieron 20 meses y volvieron 12, con el nombre de archivo prometiendo el
# rango entero). Se pide de a tramos; 6 meses deja margen contra el límite y
# para el uso de todos los días (35 días) sigue siendo un solo pedido.
MESES_POR_TRAMO = 6


def tramos(desde, hasta, meses=MESES_POR_TRAMO):
    """Parte [desde, hasta] en tramos de `meses`, en dd/mm/aaaa."""
    d0 = datetime.strptime(desde, "%d/%m/%Y").date()
    d1 = datetime.strptime(hasta, "%d/%m/%Y").date()
    if d1 < d0:
        return []
    salida, ini = [], d0
    while ini <= d1:
        y, m = ini.year, ini.month + meses
        y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
        try:
            fin = date(y, m, ini.day) - timedelta(days=1)
        except ValueError:                      # 31 de un mes que no lo tiene
            fin = date(y, m, 1) - timedelta(days=1)
        fin = min(fin, d1)
        salida.append((ini.strftime("%d/%m/%Y"), fin.strftime("%d/%m/%Y")))
        ini = fin + timedelta(days=1)
    return salida


def _solo_digitos(s):
    return re.sub(r"\D", "", s or "")


def elegir_contribuyente(ctx, cli):
    """Entra a Mis Comprobantes y confirma DE QUIÉN son los comprobantes.

    Bajar el export del CUIT equivocado dentro de la carpeta de un cliente es
    peor que no bajarlo: entra como suyo y nadie lo mira de nuevo. Por eso acá
    siempre se verifica, y si no cierra, se corta."""
    page = ctx.new_page()
    page.goto(URL_MCMP, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    cuerpo = page.inner_text("body")
    if "auth.afip.gob.ar" in page.url:
        print("Sesión de Mis Comprobantes vencida.\n"
              f"  Corré:  py arca_comprobantes.py --alias {cli['alias']} --login")
        return None, 1

    cuit = _solo_digitos(cli["cuit"])
    formateado = f"{cuit[:2]}-{cuit[2:10]}-{cuit[10:]}" if len(cuit) == 11 else cuit

    # ⚠ NO SIEMPRE HAY QUE ELEGIR (02/09). Si el login ya entró al servicio, el
    # contribuyente queda seteado (`setearContribuyente.do`) y la URL directa
    # cae en el menú, sin pantalla de «Elegí una persona». Exigirla siempre
    # hacía fallar la corrida justo después de un login exitoso.
    if "una persona" in cuerpo:
        for texto in (formateado, cuit, cli["razon_social"]):
            enlace = page.locator("a", has_text=texto)
            if enlace.count():
                enlace.first.click()
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(3000)
                print(f"  contribuyente: {cli['razon_social']} ({formateado})")
                return page, 0
        print(f"  {cli['razon_social']} ({formateado}) NO esta en la lista de ARCA.\n"
              "  La sesion abierta es de otro contribuyente, o falta la relacion\n"
              "  de representacion en AFIP. No se baja nada.\n"
              f"  En pantalla: {cuerpo[:300].strip()}")
        return None, 3

    # Ya estamos adentro: hay que confirmar de quién. La pantalla muestra el
    # CUIT del contribuyente activo.
    visibles = set(re.findall(r"\d{2}-?\d{8}-?\d", cuerpo))
    normalizados = {_solo_digitos(v) for v in visibles}
    if normalizados and cuit not in normalizados:
        print(f"  Mis Comprobantes esta abierto con OTRO contribuyente.\n"
              f"  esperaba {formateado} - en pantalla: {', '.join(sorted(visibles))}\n"
              "  No se baja nada.")
        return None, 3
    if not normalizados:
        print("  no pude leer el CUIT en pantalla - sigo, pero revisa que el\n"
              f"    export sea de {formateado}.")
    print(f"  contribuyente: {cli['razon_social']} ({formateado}) - ya seteado")
    return page, 0


def descargar(app, seccion, desde, hasta, carpeta):
    """Baja el xlsx de una sección para un tramo. Devuelve la ruta o None."""
    selector_menu, prefijo = SECCIONES[seccion]
    app.locator("a[href*=menuPrincipal]").first.click()
    app.wait_for_load_state("domcontentloaded")
    app.wait_for_timeout(2000)
    app.locator(selector_menu).first.click()
    app.wait_for_load_state("domcontentloaded")
    app.wait_for_timeout(3000)
    app.fill("#fechaEmision", f"{desde} - {hasta}")
    app.keyboard.press("Escape")
    app.click("#buscarComprobantes")
    app.wait_for_timeout(8000)

    cuerpo = app.inner_text("body")
    if "Mostrando registros" not in cuerpo and "No se encontraron" in cuerpo:
        print(f"    {seccion} {desde}–{hasta}: sin comprobantes.")
        return None

    # La grilla dice «Mostrando registros 1 a N de M». Si N < M el export sale
    # cortado y el nombre del archivo igual promete el rango entero: hay que
    # verlo acá, no meses después cuando falta una factura en el libro.
    marca = re.search(r"Mostrando registros?\s+(\d[\d.]*)\s+a\s+(\d[\d.]*)\s+de\s+(\d[\d.]*)",
                      cuerpo)
    if marca:
        hasta_n = int(marca.group(2).replace(".", ""))
        total_n = int(marca.group(3).replace(".", ""))
        if hasta_n < total_n:
            print(f"    ⚠ {seccion}: la grilla muestra {hasta_n} de {total_n} — "
                  "el export puede salir cortado. Pedí un tramo más corto.")

    boton = app.locator("button.buttons-excel, a.buttons-excel")
    if not boton.count():
        print(f"    {seccion} {desde}–{hasta}: no apareció el botón Excel.")
        return None
    with app.expect_download(timeout=30000) as dinfo:
        boton.first.click()
    periodo = f"{desde.replace('/', '-')}_a_{hasta.replace('/', '-')}"
    carpeta.mkdir(parents=True, exist_ok=True)
    destino = carpeta / f"MisComprobantes_{prefijo}_{periodo}.xlsx"
    dinfo.value.save_as(str(destino))
    print(f"    {seccion}: {destino.name}")
    return destino


def main():
    ap = argparse.ArgumentParser(description="ARCA Mis Comprobantes — cliente del estudio")
    ap.add_argument("--alias", help="cliente del estudio (ver: py clientes.py)")
    ap.add_argument("--desde", help="dd/mm/aaaa (default: hace 35 días)")
    ap.add_argument("--hasta", help="dd/mm/aaaa (default: hoy)")
    ap.add_argument("--solo", choices=list(SECCIONES), help="una sola sección")
    ap.add_argument("--login", action="store_true", help="forzar el login a mano")
    args = ap.parse_args()

    todos = clientes.de_fuente("arca")
    if not todos:
        print("No hay clientes en la base. Se dan de alta en la pantalla "
              "«Clientes del estudio».")
        return 2
    if args.alias:
        cli = clientes.por_alias(args.alias)
        if not cli:
            print(f"No existe el cliente «{args.alias}». Hay: "
                  + ", ".join(c["alias"] for c in todos))
            return 2
    elif len(todos) == 1:
        cli = todos[0]
    else:
        print("Hay más de un cliente — decí cuál con --alias: "
              + ", ".join(c["alias"] for c in todos))
        return 2

    hasta = args.hasta or date.today().strftime("%d/%m/%Y")
    desde = args.desde or (date.today() - timedelta(days=35)).strftime("%d/%m/%Y")
    secciones = [args.solo] if args.solo else list(SECCIONES)
    carpeta = rutas.carpeta_cliente(cli["cuit"], cli["razon_social"], "comprobantes")

    print(f"\n  ARCA · Mis Comprobantes")
    print(f"  cliente:  {cli['alias']} — {cli['razon_social']} ({cli['cuit']})")
    print(f"  rango:    {desde} a {hasta}")
    print(f"  destino:  {carpeta}")
    if not rutas.hay_drive():
        print(f"  ⚠ el Drive del estudio NO está montado ({rutas.DRIVE}) — "
              "se va a crear la ruta igual, pero no sincroniza.")

    SESIONES.mkdir(parents=True, exist_ok=True)
    # Lo que el login releve de paso (CCT, facilidades, VEP) tambien va a la
    # carpeta del cliente en el Drive del estudio, no a la nuestra.
    redirigir_relevamientos(rutas.asegurar(
        rutas.carpeta_cliente(cli["cuit"], cli["razon_social"], "arca")))
    storage = SA.storage_de(cli["alias"])
    if args.login or not storage.exists():
        print("\n  Abriendo ARCA para que te loguees…")
        print(f"  >>> Entrá con la clave fiscal de {cli['razon_social']}.")
        print("  >>> Cuando el portal cargue, la sesión se guarda sola.\n")
        if not SA.login_manual(cli["alias"]):
            return 1
    elif not SA.check(cli["alias"], headless=True):
        print("\n  La sesión guardada venció — abro el login.\n")
        if not SA.login_manual(cli["alias"]):
            return 1

    bajados = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(storage_state=str(storage), accept_downloads=True)
        app, err = elegir_contribuyente(ctx, cli)
        if app is None:
            browser.close()
            return err
        for seccion in secciones:
            print(f"  {seccion}:")
            for t_desde, t_hasta in tramos(desde, hasta):
                try:
                    d = descargar(app, seccion, t_desde, t_hasta, carpeta)
                    if d:
                        bajados.append(d)
                except Exception as e:
                    # Un tramo que falla no puede tirar abajo el resto: el
                    # login cuesta una persona y hay que aprovecharlo.
                    print(f"    ⚠ {seccion} {t_desde}–{t_hasta}: {str(e)[:90]}")
        ctx.storage_state(path=str(storage))    # renovar lo que ARCA haya rotado
        browser.close()

    if not bajados:
        print("\n  No se bajó ningún archivo.\n")
        return 2
    print(f"\n  {len(bajados)} archivo(s) en {carpeta}")
    print("  Siguiente paso:  py cargar_comprobantes.py --alias "
          f"{cli['alias']}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
