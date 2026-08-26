# -*- coding: utf-8 -*-
"""Obligaciones impositivas → el módulo Impuestos.

Toma lo que dejan los jobs de ARCA (`cct_estado.json`) y de las provincias
(`atp_estado.json`) y lo convierte en obligaciones con su ciclo de vida.

Las tres reglas que el ERP aprendió por las malas
--------------------------------------------------
**1. El estado es un CICLO, no una etiqueta.** Las tres pestañas del CCT de
ARCA son los tres momentos de la MISMA obligación:

    Vencimientos     → a_vencer                la fecha todavía no pasó
    DDJJ pendientes  → vencida_sin_presentar   sigue siendo una fecha
    Deudas           → dj_a_pagar              ya no es fecha: es plata

**2. Las pestañas SE SOLAPAN y gana el estado MÁS AVANZADO.** La misma
obligación aparece en dos a la vez. Si se cargaran las tres, el mismo
monotributo saldría verde, rojo-sin-presentar y rojo-se-debe al mismo tiempo.

**3. Los que no llevan DJ saltan el estado del medio.** El monotributo no se
liquida: la deuda se genera sola. Marcarlo "vencida sin presentar" sería
pedirle a alguien que presente algo que no existe. Se identifican por CÓDIGO,
que es lo estable — el nombre que muestra el portal cambia de redacción.

⚠ Y la que evita ensuciar la base: **la agenda pública de ARCA NO se carga
entera.** Trae los vencimientos de todo el país (consignatarios de carnes,
condominios de propietarios…). Solo sirve para ponerle la fecha oficial a los
impuestos que el contribuyente YA tiene en su cuenta.

Uso:
    py cargar_vencimientos.py --alias RODRIGUEZ
    py cargar_vencimientos.py --alias RODRIGUEZ --revisar
    py cargar_vencimientos.py --alias RODRIGUEZ --archivo otro_cct.json
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
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rutas  # noqa: E402
import clientes  # noqa: E402

API = "http://localhost:8310"
# ⚠ Esta sigue apuntando al Drive NUESTRO porque la escribe un job heredado de
# SIBRA (`cct_vencimientos.py`), que no sabe nada de ERBEN. Cuando ese job se
# mude al estudio, pasa a ser `rutas.ESTADO`.
ARCA_DIR = Path(r"H:\My Drive\web_sibra\tesoreria\arca")
ATP_JSON = rutas.ESTADO / "atp_estado.json"

# pestaña del CCT → momento del ciclo
PESTANAS = {
    "vencimientos":    "a_vencer",
    "ddjj_pendientes": "vencida_sin_presentar",
    "deudas":          "dj_a_pagar",
}
AVANCE = {"a_vencer": 0, "vencida_sin_presentar": 1, "dj_a_pagar": 2, "pagado": 3}

# Impuestos que NO se liquidan: la deuda se genera sola.
SIN_DJ = {
    "20",   # MONOTRIBUTO
    "786",  # ART TRAB. CASAS PARTICULARES
    "946",  # CONTRIB. TRAB. CASAS PARTICULARES
    "947",  # OBRA SOCIAL TRAB. CASAS PARTICULARES
}


def _col(fila, *nombres):
    """El valor de la primera columna cuyo nombre matchee. Los scrapeos traen
    los encabezados como los escribe el portal, con acentos y todo."""
    for k, v in (fila or {}).items():
        kk = (k or "").strip().lower()
        if any(n in kk for n in nombres):
            return (str(v).strip() if v is not None else "")
    return ""


def _monto(v):
    t = re.sub(r"[^\d,.-]", "", str(v or ""))
    if not t:
        return None
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".")
    elif "," in t:
        t = t.replace(",", ".")
    try:
        return round(float(t), 2)
    except ValueError:
        return None


def _fecha(v):
    t = (str(v or "")).strip()[:10]
    if re.match(r"^\d{4}-\d{2}-\d{2}$", t):
        return t
    m = re.match(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})$", t)
    if not m:
        return None
    d, mes, a = m.groups()
    a = int(a) + (2000 if int(a) < 100 else 0)
    return f"{a:04d}-{int(mes):02d}-{int(d):02d}"


def _periodo(v):
    """El período a MM/AAAA. Los portales lo escriben de tres formas."""
    t = (str(v or "")).strip()
    m = re.match(r"^(\d{2})/(\d{4})$", t)
    if m:
        return t
    m = re.match(r"^(\d{4})-(\d{2})", t)      # 2026-07
    if m:
        return f"{m.group(2)}/{m.group(1)}"
    m = re.match(r"^(\d{4})(\d{2})", t)       # 202607
    if m:
        return f"{m.group(2)}/{m.group(1)}"
    return t or None


def de_arca(datos, alias):
    """cct_estado.json → obligaciones, aplicando la regla del estado más
    avanzado sobre las tres pestañas que se solapan."""
    cont = (datos.get("contribuyentes") or {}).get(alias)
    if cont is None:
        disponibles = ", ".join((datos.get("contribuyentes") or {}).keys()) or "(ninguno)"
        raise SystemExit(f"El archivo de ARCA no tiene a {alias}. Trae: {disponibles}")

    unicas, leidas = {}, 0
    for pestana, estado in PESTANAS.items():
        for f in (cont.get(pestana) or []):
            # Las tablas scrapeadas traen filas fantasma con todo en null
            # (separadores del HTML). No son obligaciones.
            if not isinstance(f, dict) or not _col(f, "impuesto"):
                continue
            leidas += 1
            impuesto = _col(f, "impuesto")
            codigo = _col(f, "codigo", "código", "cod")
            periodo = _periodo(_col(f, "período", "periodo"))
            clave = (impuesto, periodo or "", _col(f, "ant/cuota", "cuota"))

            # Los que no llevan DJ no pasan por el estado del medio.
            if estado == "vencida_sin_presentar" and codigo in SIN_DJ:
                estado_real = "dj_a_pagar"
            else:
                estado_real = estado

            previo = unicas.get(clave)
            if previo is None or AVANCE[estado_real] >= AVANCE[previo["estado"]]:
                unicas[clave] = {
                    "fuente": "arca", "impuesto": impuesto, "codigo": codigo or None,
                    "periodo": periodo, "estado": estado_real,
                    "fecha": _fecha(_col(f, "fecha vencimiento", "vencimiento", "vto")),
                    "importe": _monto(_col(f, "importe", "saldo", "monto", "total")),
                }
    return [o for o in unicas.values() if o["fecha"] and o["periodo"]], leidas


def de_atp(datos, alias):
    """atp_estado.json → la obligación de IIBB de la provincia.

    El portal de ATP no tiene una grilla de vencimientos como el CCT: lo que
    da es la última DJ y cuál es el próximo período sin presentar. Con eso
    alcanza para una obligación."""
    cont = (datos.get("contribuyentes") or {}).get(alias)
    if not cont:
        return [], 0
    out = []
    prox = cont.get("proximo_periodo_sin_presentar")
    if prox:
        out.append({"fuente": "DGR-Fsa", "impuesto": "IIBB", "codigo": None,
                    "periodo": prox, "estado": "vencida_sin_presentar",
                    "fecha": None, "importe": None})
    u = cont.get("ultima_dj") or {}
    if u.get("periodo"):
        pagar = _monto(u.get("monto_a_pagar"))
        out.append({"fuente": "DGR-Fsa", "impuesto": "IIBB", "codigo": None,
                    "periodo": u["periodo"],
                    "estado": "pagado" if pagar in (0, None) else "dj_a_pagar",
                    "fecha": _fecha(u.get("vencimiento")), "importe": pagar})
    return [o for o in out if o["periodo"]], len(out)


def _api(ruta, cuerpo=None):
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    req = urllib.request.Request(API + ruta, data=datos,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.URLError:
        raise SystemExit(f"No pude hablar con el sistema en {API}.\n"
                         "  Arrancá ERBEN ESTUDIO y probá de nuevo.")
    except urllib.error.HTTPError as e:
        raise SystemExit("El sistema rechazó la carga: "
                         + e.read().decode("utf-8", "replace")[:200])


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Carga las obligaciones impositivas")
    ap.add_argument("--alias", required=True)
    ap.add_argument("--archivo", help="un JSON puntual en vez de los de siempre")
    ap.add_argument("--revisar", action="store_true", help="muestra sin cargar")
    a = ap.parse_args()

    if not clientes.por_alias(a.alias):
        raise SystemExit(f"'{a.alias}' no es un cliente del estudio.")

    obligaciones, leidas, fuentes = [], 0, []
    archivos = ([Path(a.archivo)] if a.archivo
                else [ARCA_DIR / "cct_estado.json", ATP_JSON])
    for arch in archivos:
        if not arch.exists():
            print(f"  (no está {arch.name} — lo saltea)")
            continue
        datos = json.loads(arch.read_text(encoding="utf-8"))
        # Se reconoce por el contenido, no por el nombre: el archivo puede
        # venir de otro lado con --archivo.
        cont = (datos.get("contribuyentes") or {}).get(a.alias) or {}
        if any(k in cont for k in PESTANAS):
            obs, n = de_arca(datos, a.alias)
        else:
            obs, n = de_atp(datos, a.alias)
        if obs:
            fuentes.append(f"{arch.name} ({len(obs)})")
        obligaciones += obs
        leidas += n

    if not obligaciones:
        raise SystemExit(
            "  No salió ninguna obligación.\n"
            "  Corré primero los jobs que las bajan (ARCA / ATP) desde el Panel.")

    print(f"\n  {leidas} fila(s) leídas → {len(obligaciones)} obligación(es)")
    print(f"  De: {' · '.join(fuentes)}")
    por_estado = {}
    for o in obligaciones:
        por_estado[o["estado"]] = por_estado.get(o["estado"], 0) + 1
    print("  " + " · ".join(f"{k}={v}" for k, v in sorted(por_estado.items())))

    if a.revisar:
        print("\n  (revisión: no se cargó nada)")
        for o in sorted(obligaciones, key=lambda x: x["fecha"] or "9")[:12]:
            print(f"    {o['fecha'] or '  sin fecha':<12} {o['fuente']:<9} "
                  f"{o['impuesto'][:26]:<28} {o['periodo']:<9} {o['estado']:<22}"
                  + (f"${o['importe']:,.2f}" if o["importe"] else ""))
        return 0

    r = _api(f"/api/c/vencimientos?cliente={a.alias}", {"vencimientos": obligaciones})
    print(f"\n  CARGADO: {r['cargados']} obligación(es) en el módulo Impuestos")
    return 0


if __name__ == "__main__":
    sys.exit(main())
