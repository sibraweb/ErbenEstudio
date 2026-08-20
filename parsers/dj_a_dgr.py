# -*- coding: utf-8 -*-
"""Lleva la DJ de IIBB liquidada en ERBEN al portal de la provincia — y ahí para.

    "un job que encuentra una sesión de DGR abierta y busque la DJ guardada y
     liquidada de ese CUIT y la cargue para que el usuario presente"
                                                        (Juan, 2026-08-18)

Lo que hace, en orden:
  1. le pide al sistema la DJ del período (bases por actividad, ya controladas)
  2. usa la sesión de ATP del cliente; si está vencida, abre la ventana y
     espera a que la persona entre (el login tiene reCAPTCHA)
  3. **verifica que el CUIT logueado sea el del cliente** — sin esto se podría
     cargar la DJ de un cliente en el portal de otro, que es lo peor que puede
     pasar en un sistema donde cada cliente es un compartimiento estanco
  4. abre la grilla del período y escribe cada base imponible en la fila que
     corresponde al par (código de actividad, alícuota)
  5. **DEJA LA VENTANA ABIERTA Y NO PRESENTA NADA**

⚠⚠ ESTE JOB NO PRESENTA. Nunca aprieta Aceptar ni Guardar.
Presentar una DJ es un acto fiscal irreversible con nombre y apellido: lo hace
la persona, mirando la pantalla. El job prepara, el humano ejecuta — la misma
regla del preparador de pagos (TESORERIA__DEFINICION.md §11).

Uso:
    py dj_a_dgr.py --alias RODRIGUEZ --periodo 07/2026
    py dj_a_dgr.py --alias RODRIGUEZ --periodo 07/2026 --revisar   # no escribe, solo compara
"""
import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import clientes  # noqa: E402

API = "http://localhost:8310"
BASE = "https://www.atpformosa.gob.ar/consultas"
RUNTIME_DIR = Path(r"C:\SIBRA\estudio")
SALIDA = Path(r"H:\My Drive\web_sibra\estudio\dj")


def storage_de(alias):
    return RUNTIME_DIR / f"atp_storage_{alias}.json"


def _api(ruta):
    try:
        with urllib.request.urlopen(API + ruta, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.URLError:
        raise SystemExit(
            "No pude hablar con el sistema en " + API + ".\n"
            "  Arrancá ERBEN ESTUDIO primero (el ícono del escritorio) y volvé a correr esto.")
    except urllib.error.HTTPError as e:
        raise SystemExit("El sistema contestó con error: " + e.read().decode("utf-8", "replace")[:200])


def traer_dj(alias, periodo):
    """La DJ del período, ya controlada. Si el control no cierra, no se carga:
    presentar una DJ con ventas sin imputar es subdeclarar."""
    d = _api(f"/api/c/dj/base?cliente={alias}&periodo={periodo.replace('/', '%2F')}")
    if not d.get("bases"):
        raise SystemExit(f"No hay ventas cargadas en {periodo} para {alias}. No hay nada que llevar.")
    if not d.get("control_ok"):
        raise SystemExit(
            f"⛔ La DJ de {periodo} NO cierra: las bases suman {d['suma_bases']:,.2f} y las ventas "
            f"{d['total_ventas']:,.2f} (diferencia {d['diferencia']:,.2f}).\n"
            "  Hay ventas sin actividad asignada — arreglalas en el módulo Facturas.\n"
            "  No se carga una DJ que no cierra.")
    return d


def _cuit_en_pantalla(page):
    """El CUIT que el portal muestra en su cabecera del logueado."""
    try:
        texto = page.locator("body").inner_text()
    except Exception:
        return None
    m = re.search(r"\b(\d{11})\b", texto)
    return m.group(1) if m else None


def _asegurar_sesion(ctx, page, cliente, minutos=8):
    """Sesión viva y del cliente correcto, o se pide el login a la persona."""
    page.goto(f"{BASE}/datos.php", wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    if page.locator('a[href*="logout.php"]').count() == 0:
        print("")
        print("=" * 68)
        print(f"  Entrá a ATP como {cliente['alias']} — CUIT {cliente['cuit']}")
        print(f"  {BASE.rsplit('/', 1)[0]}")
        print("")
        print("  El login tiene reCAPTCHA, así que lo hacés vos.")
        print(f"  Tenés {minutos} minutos. Cuando estés adentro, el job sigue solo.")
        print("=" * 68)
        for _ in range(minutos * 60 // 5):
            page.wait_for_timeout(5000)
            if page.locator('a[href*="logout.php"]').count() > 0:
                break
        else:
            raise SystemExit("No se completó el login. No se cargó nada.")
        page.goto(f"{BASE}/datos.php", wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

    # El control que no se puede saltear: el portal tiene que ser el DEL cliente
    cuit_portal = _cuit_en_pantalla(page)
    if cuit_portal and cuit_portal != cliente["cuit"]:
        raise SystemExit(
            f"⛔ La sesión abierta es del CUIT {cuit_portal} y la DJ es de "
            f"{cliente['cuit']} ({cliente['alias']}).\n"
            "  No se carga nada: sería meter la DJ de un cliente en el portal de otro.")
    return cuit_portal


def _filas_de_la_grilla(page):
    """Las filas de `actividad_carga`, leídas de los campos ocultos que el
    portal ya resolvió: [{i, codigo, alicuota, input}]."""
    return page.evaluate("""() => {
        const out = [];
        document.querySelectorAll('input[name^="base_imponible_"]').forEach(inp => {
            const i = inp.name.split('_').pop();
            const g = n => (document.querySelector(`input[name="${n}_${i}"]`) || {}).value;
            out.push({i, codigo: g('DGRCONT16_COD'),
                      alicuota: parseFloat(g('DGRCONT28_ALICUOTA')),
                      actual: inp.value});
        });
        return out;
    }""")


def cargar(alias, periodo, revisar=False, minutos=8):
    from playwright.sync_api import sync_playwright

    cliente = clientes.por_alias(alias)
    if not cliente:
        raise SystemExit(f"'{alias}' no es un cliente del estudio. Ver la pantalla Clientes.")
    dj = traer_dj(alias, periodo)
    mm, aaaa = periodo.split("/")

    print(f"\n  {cliente['razon_social']} · CUIT {cliente['cuit']} · período {periodo}")
    print(f"  {len(dj['bases'])} actividad(es) con base · impuesto determinado "
          f"${dj['impuesto_determinado']:,.2f}")
    print(f"  control OK: las bases cierran con las ventas (${dj['total_ventas']:,.2f})\n")

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    storage = storage_de(alias)
    with sync_playwright() as p:
        b = p.chromium.launch(headless=False)
        ctx = b.new_context(viewport={"width": 1500, "height": 950},
                            storage_state=str(storage) if storage.exists() else None)
        page = ctx.new_page()
        _asegurar_sesion(ctx, page, cliente, minutos)
        try:
            ctx.storage_state(path=str(storage))
        except Exception:
            pass

        page.goto(f"{BASE}/iibb_ddjj.php?caseid=actividad_carga&n_mes={mm}&n_anio={aaaa}",
                  wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        grilla = _filas_de_la_grilla(page)
        if not grilla:
            raise SystemExit(
                "El portal no mostró la grilla de actividades del período.\n"
                "  Puede que ya esté presentado (ahí solo deja RECTIFICAR) o que el período no exista.")

        # Emparejar por el PAR (código, alícuota): en Formosa la misma actividad
        # vive con varias alícuotas y son filas distintas de verdad.
        pendientes = {(b_["codigo"], round(float(b_["alicuota"]), 2)): b_ for b_ in dj["bases"]}
        escritas, sin_fila = [], []
        for fila in grilla:
            clave = (fila["codigo"], round(fila["alicuota"], 2))
            base = pendientes.pop(clave, None)
            if base is None:
                continue
            valor = f"{base['base']:.2f}"        # el portal pide PUNTO decimal
            if not revisar:
                page.fill(f'input[name="base_imponible_{fila["i"]}"]', valor)
            escritas.append({"codigo": fila["codigo"], "alicuota": fila["alicuota"],
                             "base": base["base"], "antes": fila["actual"]})
        sin_fila = list(pendientes.values())

        print("  " + ("REVISIÓN (no se escribió nada)" if revisar else "CARGADO en la grilla") + ":")
        for e in escritas:
            print(f"    {e['codigo']} al {e['alicuota']}%  ->  ${e['base']:,.2f}"
                  + (f"   (el portal tenía {e['antes']})" if e["antes"] not in ("0.00", "", None) else ""))
        if sin_fila:
            print("\n  ⚠ Estas actividades NO tienen fila en el portal y quedaron afuera:")
            for s in sin_fila:
                print(f"    {s['codigo']} al {s['alicuota']}%  ${s['base']:,.2f}")
            print("    (el padrón del portal manda: si falta una actividad, se da de alta en ATP)")

        SALIDA.mkdir(parents=True, exist_ok=True)
        sello = f"{alias}_{aaaa}{mm}_{date.today().isoformat()}"
        try:
            page.screenshot(path=str(SALIDA / f"DJ_{sello}.png"), full_page=True)
        except Exception:
            pass
        (SALIDA / f"DJ_{sello}.json").write_text(json.dumps(
            {"cliente": cliente, "periodo": periodo, "cargado": escritas,
             "sin_fila": sin_fila, "dj": dj, "solo_revision": revisar},
            indent=2, ensure_ascii=False), encoding="utf-8")

        print("")
        print("=" * 68)
        if revisar:
            print("  Revisión terminada — no se tocó nada en el portal.")
        else:
            print("  LISTO: las bases están cargadas en la pantalla.")
            print("")
            print("  Ahora te toca a vos:")
            print("   1. revisá los importes contra la pantalla")
            print("   2. apretá Aceptar / Presentar vos mismo")
            print("")
            print("  ⚠ El job NO presenta nada: presentar es un acto fiscal y lo firma")
            print("     una persona, no un robot.")
        print(f"\n  Constancia: {SALIDA / ('DJ_' + sello + '.json')}")
        print("=" * 68)
        try:
            input("\n  >>> ENTER acá cuando termines para cerrar la ventana: ")
        except (EOFError, OSError):
            page.wait_for_timeout(120000)
        b.close()
    return 0


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Carga la DJ de IIBB en el portal (no presenta)")
    ap.add_argument("--alias", required=True)
    ap.add_argument("--periodo", required=True, help="MM/AAAA")
    ap.add_argument("--revisar", action="store_true", help="compara sin escribir")
    ap.add_argument("--minutos", type=int, default=8)
    a = ap.parse_args()
    if not re.fullmatch(r"\d{2}/\d{4}", a.periodo):
        raise SystemExit("El período va como MM/AAAA (ej. 07/2026)")
    return cargar(a.alias, a.periodo, a.revisar, a.minutos)


if __name__ == "__main__":
    sys.exit(main())
