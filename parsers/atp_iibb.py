# -*- coding: utf-8 -*-
"""Job Tesorería — ATP Formosa (DGR-Fsa): TODOS los relevamientos de IIBB en
una pasada. Espejo de dgr_deuda.py (Corrientes), pero el portal de Formosa da
más, así que el job trae más:

  · datos.php                  → datos del contribuyente + inscripciones
  · datos.php?caseid=actividades → PADRÓN de actividades (código, inicio,
                                   principal, exento) — lo que faltaba para
                                   actividades.py bajo "DGR-Fsa"
  · iibb_ddjj.php?caseid=actividad_lista   → bases imponibles por período
  · iibb_ddjj.php?caseid=deducciones_lista → deducciones por período
  · iibb_ddjj.php?caseid=ddjj_lista        → DJ presentadas (impuesto, bonif,
                                             saldo, estado) + detección del
                                             próximo período SIN presentar
  · consul_ret_per.php         → detalle de ret/perc/bancarias/SIRTAC/SIRCUPA
                                 por agente (mes actual y anterior)
  · estado_deuda.php           → IIBB (174) e IPS (179)
  · consulta_pagos_efectuados.php → pagos del mes actual y anterior
  · buzon_fiscal_electronico.php?caseid=notificaciones_lista → notificaciones
    (⚠ solo LISTA: no clickea "Leer" — leer una notificación del domicilio
    fiscal electrónico dispara el plazo legal)

100% automático con las sesiones de sesion_atp.py — una por contribuyente
(ATP es login propio, sin representación). Recorre los contribuyentes con
"DGR-Fsa" en tools/contribuyentes.py; si a alguno le falta la sesión, lo
salta con aviso y sigue (no corta todo el job por uno).

El relevamiento del 2026-08-17 (H:\\...\\tesoreria\\atp_formosa\\2026-08-17\\)
validó al centavo la matemática del portal contra liquidacion_iibb_formosa.py:
IMPUESTO = Σ base×alícuota; BONIF = 20% (y $0 cuando rige el mínimo);
DEDUCCIONES = ret+perc+banc+SIRCUPA+saldo a favor anterior; el mínimo de la
R.G. 05/2026 actúa de piso duro.

Uso:
    py atp_iibb.py                     # todos los contribuyentes DGR-Fsa
    py atp_iibb.py --alias RODRIGUEZ   # uno solo
Salida: H:\\My Drive\\web_sibra\\tesoreria\\atp\\<alias>\\ATP_<fecha>.xlsx (+ .txt + .png)
        H:\\My Drive\\web_sibra\\tesoreria\\atp\\atp_estado.json (todos los alias)
Códigos: 0 ok (al menos uno) · 1 ninguna sesión viva · 2 error
"""
import argparse
import json
import math
import re
import sys
from datetime import date
from pathlib import Path

import pandas as pd
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rutas  # noqa: E402
from clientes import de_fuente
from sesion_atp import storage_de, SELECTOR_LOGUEADO

SALIDA = rutas.ESTADO          # el atp_estado.json que consume el sistema
BASE = "https://www.atpformosa.gob.ar/consultas"
FUENTE = "DGR-Fsa"


# ── parsers ──────────────────────────────────────────────────────────────────
def _monto(s):
    """'$ 1.115.702,48' → 1115702.48 — formato AR normal (a diferencia de
    Corrientes, ATP separa los centavos con coma, sin trampas)."""
    t = re.sub(r"[^\d.,-]", "", str(s or ""))
    if not t:
        return None
    # dots = miles, coma = decimal; si no hay coma, el punto puede ser decimal
    # (los inputs del form usan "8603.00") — se decide por la posición.
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    elif t.count(".") == 1 and len(t.split(".")[1]) == 2:
        pass  # ya viene con punto decimal estilo form
    else:
        t = t.replace(".", "")
    try:
        return round(float(t), 2)
    except ValueError:
        return None


def _sin_nan(obj):
    """NaN no es JSON válido y el navegador falla en silencio (visto
    2026-07-30 con cct_estado.json) — misma limpieza que dgr_deuda.py."""
    if isinstance(obj, dict):
        return {k: _sin_nan(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sin_nan(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    if obj is not None and type(obj).__name__ in ("NaTType", "Timestamp"):
        return str(obj) if type(obj).__name__ == "Timestamp" else None
    return obj


def parse_datos(texto):
    """datos.php — denominación, CUIT, tipo e inscripciones vigentes."""
    out = {}
    m = re.search(r"DENOMINACION\s*:\s*(.+)", texto)
    out["denominacion"] = m.group(1).strip() if m else None
    m = re.search(r"N.\s*CUIT\s*:\s*([\d\-]+)", texto)
    out["cuit_portal"] = m.group(1).replace("-", "") if m else None
    m = re.search(r"TIPO DE PERSONA\s*:\s*(.+)", texto)
    out["tipo_persona"] = m.group(1).strip() if m else None
    out["inscripciones"] = [
        {"tributo": t.strip(), "numero": n, "fecha": f}
        for t, n, f in re.findall(
            r"([A-ZÑÁÉÍÓÚ][^\n]+?)\s*\nN° INSCRIPCIÓN\s*:\s*(\d+)\s*\nFECHA INSCRIPCIÓN\s*:\s*([\d/]+)", texto)
    ]
    return out


def parse_padron(texto):
    """datos.php?caseid=actividades — una línea por actividad vigente:
    '731009 - Servicios de publicidad n.c.p. - Inicio:   01/04/2011 -
     Principal:   NO - Exento:   NO'"""
    return [
        {"codigo": c, "nombre": d.strip(), "inicio": i,
         "principal": p == "SI", "exento": e == "SI"}
        for c, d, i, p, e in re.findall(
            r"(\d{6}) - (.+?) - Inicio:\s+([\d/]+) - Principal:\s+(SI|NO) - Exento:\s+(SI|NO)",
            texto)
    ]


def parse_actividad_lista(texto):
    """caseid=actividad_lista — bases por período. El body viene como
    'PERIODO: 06/2026: RECTIFICAR' seguido de filas tab-separadas
    'cod - descr\\tSI/NO\\tSI/NO\\t3%\\t $ base\\t $ impuesto'."""
    periodos, actual = [], None
    for linea in texto.splitlines():
        linea = linea.rstrip()
        m = re.match(r"PERIODO:\s*(\d{2}/\d{4})", linea.strip())
        if m:
            actual = {"periodo": m.group(1), "filas": []}
            periodos.append(actual)
            continue
        m = re.match(
            r"(\d{6}) - (.*?)\t(SI|NO)\t(SI|NO)\t([\d.]+)\s*%\t\s*\$\s*([\d.,]+)\t\s*\$\s*([\d.,]+)",
            linea)
        if m and actual is not None:
            actual["filas"].append({
                "codigo": m.group(1), "actividad": m.group(2).strip(),
                "principal": m.group(3) == "SI", "exento": m.group(4) == "SI",
                "alicuota": float(m.group(5)),
                "base": _monto(m.group(6)), "impuesto": _monto(m.group(7)),
            })
    for p in periodos:
        p["base_total"] = round(sum(f["base"] or 0 for f in p["filas"]), 2)
        p["impuesto_total"] = round(sum(f["impuesto"] or 0 for f in p["filas"]), 2)
    return periodos


_CLAVES_DEDUCCION = {
    "RETENCIONES": "retenciones",
    "PERCEPCIONES": "percepciones",
    "RET. BANCARIAS": "ret_bancarias",
    "OTRAS RETENCIONES": "otras_retenciones",
    "RET. TARJETAS - SIRTAC": "sirtac",
    "RET. BILLETERAS DIGITALES - SIRCUPA": "sircupa",
    "PAGOS A CUENTA": "pagos_a_cuenta",
    "OTROS PAGOS A CUENTA": "otros_pagos_a_cuenta",
    "OTROS CREDITOS": "otros_creditos",
    "SALDO A FAVOR": "saldo_a_favor",
}


def parse_deducciones_lista(texto):
    """caseid=deducciones_lista — bloques por período; cada línea trae 1-2
    pares 'ETIQUETA\\t$ monto' y la primera línea del bloque el período."""
    periodos, actual = [], None
    for linea in texto.splitlines():
        m_per = re.search(r"\t(\d{2}/\d{4})\t", linea)
        if m_per and "RETENCIONES" in linea:
            actual = {"periodo": m_per.group(1)}
            periodos.append(actual)
        if actual is None:
            continue
        for etiqueta, monto in re.findall(r"([A-ZÑÁÉÍÓÚÜ][A-ZÑÁÉÍÓÚÜ \.\-]*?)\t\$\s*([\d.,]+)", linea):
            clave = _CLAVES_DEDUCCION.get(etiqueta.strip())
            if clave:
                actual[clave] = _monto(monto)
    return periodos


def parse_ddjj_lista(texto):
    """caseid=ddjj_lista — DJ presentadas, bloque de 6 líneas por DJ."""
    djs, dj = [], None
    for linea in texto.splitlines():
        m = re.match(r"FECHA VENC\.\t([\d/]+)\t+FECHA PRES\.\t([\d/: ]+?)\t+(\d{2}/\d{4})", linea)
        if m:
            dj = {"vencimiento": m.group(1), "presentada": m.group(2).strip(),
                  "periodo": m.group(3)}
            djs.append(dj)
            continue
        if dj is None:
            continue
        m = re.match(r"SECUENCIA\t(.+?)\t+TIPO\t(.+?)\t*$", linea)
        if m:
            dj["secuencia"], dj["tipo"] = m.group(1).strip(), m.group(2).strip()
        m = re.match(r"IMPUESTO\t\$\s*([\d.,]+)\t+DEDUCCIONES\t\$\s*([\d.,]+)", linea)
        if m:
            dj["impuesto"], dj["deducciones"] = _monto(m.group(1)), _monto(m.group(2))
        m = re.match(r"BONIFICACION\t\$\s*([\d.,]+)\t+INTERESES\t\$\s*([\d.,]+)", linea)
        if m:
            dj["bonificacion"], dj["intereses"] = _monto(m.group(1)), _monto(m.group(2))
        m = re.match(r"A FAVOR DGR\t\$\s*([\d.,]+)\t+A FAVOR CONTRIB\.\t\$\s*([\d.,]+)", linea)
        if m:
            dj["a_favor_dgr"], dj["a_favor_contribuyente"] = _monto(m.group(1)), _monto(m.group(2))
        m = re.match(r"MONTO A PAGAR\t\$\s*([\d.,]+)\t+ESTADO\t(.+?)\t*$", linea)
        if m:
            dj["monto_a_pagar"], dj["estado"] = _monto(m.group(1)), m.group(2).strip()
    return djs


def parse_ret_per(texto):
    """consul_ret_per.php — secciones por tributo, adentro un bloque por
    agente (CUIT + razón social) con sus movimientos y total."""
    secciones, seccion, agente = [], None, None
    for linea in texto.splitlines():
        m = re.search(r"Tributo:\s*(.+?)\s+Posición Fiscal:\s*(\d{2}/\d{4})", linea)
        if m:
            seccion = {"tributo": m.group(1).strip(), "periodo": m.group(2),
                       "agentes": [], "total": None, "sin_datos": False}
            secciones.append(seccion)
            agente = None
            continue
        if seccion is None:
            continue
        if "No se encontraron datos" in linea:
            seccion["sin_datos"] = True
            continue
        m = re.search(r"CUIT:\s*([\d\-]+)\s+Inscripción:\s*(\d+)", linea)
        if m:
            agente = {"cuit": m.group(1).replace("-", ""), "inscripcion": m.group(2),
                      "razon_social": None, "movimientos": [], "total": None}
            seccion["agentes"].append(agente)
            continue
        m = re.search(r"Nombre/Razón Social:\s*(.+)", linea)
        if m and agente is not None:
            agente["razon_social"] = m.group(1).strip()
            continue
        m = re.match(r"(\d{2}/\d{4})\s*\t([\d/]+|NULL)?\s*\t\$\s*([\d.,]+)\s*\t([\d.,]*%?)\s*\t([^\t]*)\t([^\t]*)", linea)
        if m and agente is not None:
            agente["movimientos"].append({
                "periodo": m.group(1),
                "fecha_cobro": None if (m.group(2) or "NULL") == "NULL" else m.group(2),
                "importe": _monto(m.group(3)),
                "alicuota": (m.group(4) or "").strip() or None,
                "comprobante": (m.group(5) or "").strip() or None,
                "factura": (m.group(6) or "").strip() or None,
            })
            continue
        m = re.search(r"Total Agente\s*\t\$\s*([\d.,]+)", linea)
        if m and agente is not None:
            agente["total"] = _monto(m.group(1))
            continue
        m = re.search(r"TOTAL RETENCIONES:\s*\$\s*([\d.,]+)", linea)
        if m:
            seccion["total"] = _monto(m.group(1))
    return secciones


def parse_buzon(texto):
    """buzon caseid=notificaciones_lista — filas de la tabla de notificaciones.
    'sin_leer' = las que no tienen Fecha de Lectura."""
    filas = []
    for linea in texto.splitlines():
        celdas = [c.strip() for c in linea.split("\t")]
        if len(celdas) >= 5 and re.match(r"[\d/]+$", celdas[2] or "") \
                and not linea.startswith("Referencia"):
            filas.append({
                "referencia": celdas[0], "asunto": celdas[1],
                "fecha_disposicion": celdas[2],
                "fecha_lectura": celdas[3] or None,
                "fecha_notificacion": celdas[4] or None,
            })
    return filas


def proximo_sin_presentar(djs, hoy=None):
    """El período mensual más viejo posterior a la última DJ presentada.
    Con la lista 2026 alcanza: las DJ vienen ordenadas descendentes."""
    hoy = hoy or date.today()
    presentados = set()
    for dj in djs:
        mm, aaaa = dj["periodo"].split("/")
        presentados.add((int(aaaa), int(mm)))
    if not presentados:
        return None
    a, m = max(presentados)
    m += 1
    if m > 12:
        a, m = a + 1, 1
    # solo es "sin presentar" un período que ya cerró (mes vencido)
    if (a, m) >= (hoy.year, hoy.month):
        return None
    return f"{m:02d}/{a}"


# ── el job ───────────────────────────────────────────────────────────────────
def _texto(page):
    return page.locator("body").inner_text()


def _ir(page, url):
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    return _texto(page)


def releva_uno(alias, storage, salida_alias):
    salida_alias.mkdir(parents=True, exist_ok=True)
    hoy = date.today()
    fecha = hoy.strftime("%Y-%m-%d")
    crudos = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(storage_state=str(storage),
                                  viewport={"width": 1500, "height": 1000})
        page = ctx.new_page()
        page.goto(f"{BASE}/datos.php", wait_until="domcontentloaded")
        try:
            page.wait_for_selector(SELECTOR_LOGUEADO, timeout=15000)
        except Exception:
            print(f"  [{alias}] sesión ATP vencida — correr: py sesion_atp.py --alias {alias} --login")
            browser.close()
            return None

        # 1) datos + situación fiscal del header
        crudos["datos"] = _texto(page)
        datos = parse_datos(crudos["datos"])
        m = re.search(r"Situacion Fiscal\s*\n(\w+)", crudos["datos"])
        situacion = m.group(1) if m else None
        m = re.search(r"\n(\d+)\s*\nBUZÓN FISCAL", crudos["datos"])
        buzon_badge = int(m.group(1)) if m else None

        # 2) padrón de actividades
        crudos["padron"] = _ir(page, f"{BASE}/datos.php?caseid=actividades")
        padron = parse_padron(crudos["padron"])

        # 3) sistema de DDJJ: bases, deducciones, DJ presentadas
        crudos["actividad_lista"] = _ir(page, f"{BASE}/iibb_ddjj.php?caseid=actividad_lista")
        bases = parse_actividad_lista(crudos["actividad_lista"])
        crudos["deducciones_lista"] = _ir(page, f"{BASE}/iibb_ddjj.php?caseid=deducciones_lista")
        deducciones = parse_deducciones_lista(crudos["deducciones_lista"])
        crudos["ddjj_lista"] = _ir(page, f"{BASE}/iibb_ddjj.php?caseid=ddjj_lista")
        djs = parse_ddjj_lista(crudos["ddjj_lista"])
        page.screenshot(path=str(salida_alias / f"ATP_{fecha}_ddjj.png"), full_page=True)

        # 4) detalle de ret/perc por agente — mes actual y anterior
        detalle_ret = []
        for (a, m_) in [(hoy.year, hoy.month),
                        (hoy.year, hoy.month - 1) if hoy.month > 1 else (hoy.year - 1, 12)]:
            _ir(page, f"{BASE}/consul_ret_per.php")
            try:
                page.select_option('select[name="n_mes"]', f"{m_:02d}")
                page.select_option('select[name="n_anio"]', str(a))
                page.select_option('select[name="tributo"]', "0")
                page.click('input[name="B1"]')
                page.wait_for_timeout(3000)
            except Exception as e:
                print(f"    consul_ret_per {m_:02d}/{a}: no pude enviar el form ({str(e)[:60]})")
            crudos[f"ret_per_{a}{m_:02d}"] = _texto(page)
            detalle_ret += parse_ret_per(crudos[f"ret_per_{a}{m_:02d}"])

        # 5) estado de deuda — IIBB y IPS
        estado_deuda = {}
        for cod, nombre in (("174", "iibb"), ("179", "ips")):
            _ir(page, f"{BASE}/estado_deuda.php")
            try:
                page.select_option('select[name="tributo"]', cod)
                page.click('input[type="submit"]')
                page.wait_for_timeout(3000)
            except Exception:
                pass
            t = _texto(page)
            crudos[f"estado_deuda_{nombre}"] = t
            estado_deuda[nombre] = {
                "sin_deuda": "NO ADEUDA" in t,
                # si hay deuda, el texto crudo queda en el .txt para armar el
                # parser fino cuando veamos un caso real (el contribuyente del
                # relevamiento estaba al día)
            }
        page.screenshot(path=str(salida_alias / f"ATP_{fecha}_deuda.png"), full_page=True)

        # 6) pagos efectuados — mes actual
        crudos["pagos"] = _ir(page, f"{BASE}/consulta_pagos_efectuados.php")
        sin_pagos = "No se encontraron pagos" in crudos["pagos"]

        # 7) buzón (solo lista — NO se clickea "Leer")
        crudos["buzon"] = _ir(page, f"{BASE}/buzon_fiscal_electronico.php?caseid=notificaciones_lista")
        notificaciones = parse_buzon(crudos["buzon"])
        browser.close()

    # ── salida ──
    (salida_alias / f"ATP_{fecha}.txt").write_text(
        "\n\n".join(f"===== {k} =====\n{v}" for k, v in crudos.items()),
        encoding="utf-8")

    destino = salida_alias / f"ATP_{fecha}.xlsx"
    with pd.ExcelWriter(destino) as xl:
        pd.DataFrame(padron).to_excel(xl, sheet_name="padron", index=False)
        pd.DataFrame([
            {"periodo": p["periodo"], **f} for p in bases for f in p["filas"]
        ]).to_excel(xl, sheet_name="bases", index=False)
        pd.DataFrame(deducciones).to_excel(xl, sheet_name="deducciones", index=False)
        pd.DataFrame(djs).to_excel(xl, sheet_name="ddjj", index=False)
        pd.DataFrame([
            {"tributo": s["tributo"], "periodo": s["periodo"],
             "agente_cuit": ag["cuit"], "agente": ag["razon_social"], **mv}
            for s in detalle_ret for ag in s["agentes"] for mv in ag["movimientos"]
        ]).to_excel(xl, sheet_name="ret_per_detalle", index=False)
        pd.DataFrame(notificaciones).to_excel(xl, sheet_name="buzon", index=False)

    sin_leer = [n for n in notificaciones if not n["fecha_lectura"]]
    return {
        "situacion_fiscal": situacion,
        "datos": datos,
        "padron_actividades": padron,
        "bases_por_periodo": bases,
        "deducciones_por_periodo": deducciones,
        "ddjj_presentadas": djs,
        "ultima_dj": djs[0] if djs else None,
        "proximo_periodo_sin_presentar": proximo_sin_presentar(djs),
        "saldo_a_favor": (djs[0].get("a_favor_contribuyente") if djs else None),
        "ret_per_detalle": detalle_ret,
        "estado_deuda": estado_deuda,
        "pagos_mes_actual": None if sin_pagos else "VER TXT (hay pagos, parser pendiente)",
        "buzon_badge": buzon_badge,
        "notificaciones_sin_leer": len(sin_leer),
        "notificaciones": notificaciones[:10],
    }


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Job ATP Formosa — IIBB completo (DGR-Fsa)")
    ap.add_argument("--alias", help="solo este contribuyente")
    args = ap.parse_args()

    hoy = date.today().strftime("%Y-%m-%d")
    contribuyentes = {c["alias"]: c for c in de_fuente(FUENTE)}
    if args.alias:
        contribuyentes = {k: v for k, v in contribuyentes.items() if k == args.alias}
    if not contribuyentes:
        print(f"Ningún cliente del estudio está inscripto en '{FUENTE}'.\n"
              "  Se cargan en la pantalla Clientes del estudio.")
        return 2

    estado = {"fecha_consulta": hoy, "contribuyentes": {}}
    for alias, datos in contribuyentes.items():
        storage = storage_de(alias)
        if not storage.exists():
            print(f"  [{alias}] no hay sesión — correr: py sesion_atp.py --alias {alias} --login")
            continue
        print(f"— {alias} ({datos['cuit']})")
        resumen = releva_uno(alias, storage, SALIDA / alias)
        if resumen is not None:
            estado["contribuyentes"][alias] = {"cuit": datos["cuit"], **resumen}
            u = resumen.get("ultima_dj") or {}
            print(f"  situación {resumen['situacion_fiscal']} · última DJ {u.get('periodo')} "
                  f"({u.get('estado')}) · saldo a favor ${u.get('a_favor_contribuyente')} · "
                  f"{resumen['notificaciones_sin_leer']} notificación(es) sin leer")

    if not estado["contribuyentes"]:
        print("Ningún contribuyente pudo relevarse (sin sesión viva).")
        return 1

    SALIDA.mkdir(parents=True, exist_ok=True)
    (SALIDA / "atp_estado.json").write_text(
        json.dumps(_sin_nan(estado), indent=2, ensure_ascii=False, allow_nan=False),
        encoding="utf-8")
    print(f"Guardado atp_estado.json con {len(estado['contribuyentes'])} contribuyente(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
