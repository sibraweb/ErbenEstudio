# -*- coding: utf-8 -*-
"""El relevamiento de ATP -> la base del sistema.

`atp_iibb.py` entra al portal y deja `atp_estado.json` en el Drive. Eso NO es
tenerlo en el sistema — es el mismo eslabón que faltaba con ARCA, y el mismo
que en el otro sistema dejó comprobantes normalizados en el Drive sin entrar
nunca. Acá el paso existe como job y el relevamiento lo nombra al terminar.

Qué carga
---------
· **el padrón de actividades** — los pares (código NAES, alícuota) del
  contribuyente. Sin esto una venta no se puede clasificar;
· **las deducciones por período** — y esta es la parte que importa:

  ⚠ LAS DEDUCCIONES DE IIBB NO SALEN DE NUESTRAS FACTURAS. Las retenciones y
  percepciones las informan los AGENTES directamente a Rentas; el
  contribuyente se entera mirando el portal. Una DJ armada solo con lo que
  nosotros vemos declara de menos y paga de más. Por eso el portal MANDA sobre
  lo nuestro para este dato.

· **el saldo a favor** que arrastra el portal, que es el número más pesado y
  el que no se puede deducir de nada: viene de toda la historia del
  contribuyente, muy anterior a que el estudio lo tomara.

Uso:
    py cargar_atp.py --alias DEMO
    py cargar_atp.py --alias DEMO --simular
"""
import argparse
import json
import sys
import urllib.error
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
JURISDICCION = "DGR-Fsa"

# Los conceptos tal como los nombra ATP. La clave es la de `atp_estado.json`.
CONCEPTOS = ["retenciones", "percepciones", "ret_bancarias", "otras_retenciones",
             "sirtac", "sircupa", "pagos_a_cuenta", "otros_pagos_a_cuenta",
             "otros_creditos"]


def _api(ruta, cuerpo=None, alias=None):
    sep = "&" if "?" in ruta else "?"
    url = API + ruta + (f"{sep}cliente={alias}" if alias and "/api/c/" in ruta else "")
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    req = urllib.request.Request(url, data=datos,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"_http": e.code, **json.loads(e.read())}
    except urllib.error.URLError:
        raise SystemExit(f"No pude hablar con el sistema en {API}.\n"
                         "  Abrí el ícono ERBEN ESTUDIO y volvé a intentar.")


def main():
    ap = argparse.ArgumentParser(description="Cargar el relevamiento de ATP a la base")
    ap.add_argument("--alias", help="cliente del estudio")
    ap.add_argument("--archivo", help="otro atp_estado.json")
    ap.add_argument("--simular", action="store_true", help="no escribe: dice qué haría")
    args = ap.parse_args()

    ruta = Path(args.archivo) if args.archivo else (rutas.ESTADO / "atp_estado.json")
    if not ruta.exists():
        print(f"No está el relevamiento en {ruta}\n"
              f"  Corrélo con:  py atp_iibb.py --alias {args.alias or '<alias>'}")
        return 2
    datos = json.loads(ruta.read_text(encoding="utf-8"))
    contribuyentes = datos.get("contribuyentes") or {}

    todos = clientes.de_fuente(JURISDICCION)
    if args.alias:
        cli = clientes.por_alias(args.alias)
        if not cli:
            print(f"No existe «{args.alias}». Hay: " + ", ".join(c["alias"] for c in todos))
            return 2
    elif len(todos) == 1:
        cli = todos[0]
    else:
        print("Decí cuál con --alias: " + ", ".join(c["alias"] for c in todos))
        return 2

    r = contribuyentes.get(cli["alias"])
    if not r:
        print(f"El relevamiento no tiene a «{cli['alias']}». Trae: "
              + ", ".join(contribuyentes) or "(nada)")
        return 2

    # ⚠ CONTROL DE IDENTIDAD, igual que en los jobs del portal: el alias puede
    # coincidir y el CUIT no. Cargar las deducciones de otro contribuyente le
    # regalaría a este crédito fiscal que no tiene.
    if str(r.get("cuit", "")).strip() != str(cli["cuit"]).strip():
        print(f"⚠ El relevamiento dice CUIT {r.get('cuit')} y el cliente es "
              f"{cli['cuit']}. No cargo nada.")
        return 3

    print(f"\n  {cli['alias']} — {cli['razon_social']} ({cli['cuit']})")
    print(f"  relevado el {datos.get('fecha_consulta', '?')}\n")

    # ── el padrón ────────────────────────────────────────────────────────────
    padron = r.get("padron_actividades") or []
    if padron and not args.simular:
        res = _api("/api/c/actividades", {"jurisdiccion": JURISDICCION,
                                          "actividades": padron}, alias=cli["alias"])
        print(f"  padrón: {len(padron)} pares actividad+alícuota"
              + ("" if res.get("ok") else f"  ⚠ {res.get('error')}"))
    else:
        print(f"  padrón: {len(padron)} pares actividad+alícuota"
              + ("  (simulacro)" if args.simular else ""))

    # ── las deducciones ──────────────────────────────────────────────────────
    deds = r.get("deducciones_por_periodo") or []
    print(f"\n  deducciones informadas por el portal ({len(deds)} período/s):")
    cargadas = 0
    for d in deds:
        cuerpo = {"jurisdiccion": JURISDICCION, "periodo": d["periodo"],
                  "saldo_a_favor": d.get("saldo_a_favor") or 0}
        for c in CONCEPTOS:
            cuerpo[c] = d.get(c) or 0
        suma = round(sum(cuerpo[c] for c in CONCEPTOS), 2)
        print(f"    {d['periodo']}  deducciones {suma:>14,.2f}"
              f"   saldo a favor {cuerpo['saldo_a_favor']:>16,.2f}")
        if not args.simular:
            res = _api("/api/c/iibb/deducciones", cuerpo, alias=cli["alias"])
            if res.get("ok"):
                cargadas += 1
            else:
                print(f"      ⚠ {res.get('error')}")

    print(f"\n  {'(simulacro) ' if args.simular else ''}{cargadas or len(deds)} período(s)"
          f" de deducciones\n")
    ultimo = r.get("proximo_periodo_sin_presentar")
    if ultimo:
        print(f"  ⚠ El portal dice que falta presentar: {ultimo}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
