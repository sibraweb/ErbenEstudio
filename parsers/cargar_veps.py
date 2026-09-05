# -*- coding: utf-8 -*-
"""Los VEP del portal de ARCA -> la base.

*"Que aparezca qué impuesto pagó, un VEP o un DEBIN"* (Juan, 05/09).

Hasta ahora el vencimiento se ataba al débito del banco y nada más: se sabía que
salió la plata, no CON QUÉ se pagó ni con qué número. El VEP es el comprobante
que vale ante ARCA, y su número es lo que se busca cuando reclaman.

De dónde sale
-------------
El login de ARCA releva de paso la pantalla de VEP —un login, tres datos— y deja
`vep_pagos_*.json` en la carpeta del cliente. Este job lo lee y lo carga.

Lo que hace además de cargar
----------------------------
· **deduce el impuesto y el período del concepto**: «IVA DJ02/26» → IVA, 02/2026.
  Si no se puede leer, quedan vacíos. NO se inventan: un período mal deducido
  cancelaría la obligación equivocada.
· **ata el VEP a su obligación** cuando el impuesto y el período coinciden.
· **ata el VEP al débito del banco** por importe y fecha cercana — con la misma
  regla de siempre: si hay dos candidatos, no elige.

Uso:
    py cargar_veps.py --alias DEMO
    py cargar_veps.py --alias DEMO --simular
"""
import argparse
import json
import re
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

# Cómo nombra ARCA cada impuesto en la descripción del VEP. La izquierda es lo
# que escribe el portal; la derecha, el nombre que usa el módulo Impuestos.
IMPUESTOS = [
    ("IVA", "IVA"),
    ("SIJPDJ", "SIJP"),          # aportes y contribuciones
    ("SIJP", "SIJP"),
    ("AUTONO", "Autónomos"),
    ("GANANC", "Ganancias"),
    ("SICORE", "SICORE"),
    ("MONOTRIB", "Monotributo"),
    ("BSPERS", "Bienes Personales"),
]


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


def _monto(s):
    """«$ 450.230,15» → 450230.15."""
    t = re.sub(r"[^\d,.-]", "", str(s or "")).replace(".", "").replace(",", ".")
    try:
        return round(float(t), 2)
    except ValueError:
        return 0.0


def _fecha(s):
    """«2026-01-05 13:42:54» → 2026-01-05."""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(s or ""))
    if m:
        return m.group(0)
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", str(s or ""))
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def leer_concepto(txt):
    """«IVA DJ02/26» → ('IVA', '02/2026'). Lo que no se lee queda en None.

    ⚠ El período viene en dos dígitos de año. Se completa con 20xx, que es lo
    único razonable — pero si el portal alguna vez manda otra cosa, el que
    quede vacío es mejor que uno inventado: un período mal deducido cancela la
    obligación equivocada."""
    s = str(txt or "").upper().strip()
    impuesto = next((dest for clave, dest in IMPUESTOS if s.startswith(clave)), None)
    m = re.search(r"(\d{2})/(\d{2})$", s)
    periodo = f"{m.group(1)}/20{m.group(2)}" if m else None
    return impuesto, periodo


def main():
    ap = argparse.ArgumentParser(description="Cargar los VEP de ARCA a la base")
    ap.add_argument("--alias", help="cliente del estudio")
    ap.add_argument("--simular", action="store_true", help="no escribe: dice qué haría")
    args = ap.parse_args()

    todos = clientes.de_fuente("arca")
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

    carpeta = rutas.carpeta_cliente(cli["cuit"], cli["razon_social"], "arca")
    archivos = sorted(carpeta.glob("vep_pagos*.json"))
    if not archivos:
        print(f"No hay VEP relevados en {carpeta}\n"
              f"  Los trae el login de ARCA:  py arca_comprobantes.py --alias {cli['alias']} --login")
        return 2

    print(f"\n  {cli['alias']} — {cli['razon_social']} ({cli['cuit']})")
    print(f"  {len(archivos)} archivo(s) de VEP\n")

    vistos, veps = set(), []
    for path in archivos:
        d = json.loads(path.read_text(encoding="utf-8"))
        for tabla in d.get("tablas") or []:
            filas = tabla.get("filas") or []
            if not filas:
                continue
            enc = [str(c).strip().lower() for c in filas[0]]
            try:
                i_est = enc.index("estado")
                i_env = next(i for i, c in enumerate(enc) if "enviado" in c)
                i_nro = next(i for i, c in enumerate(enc) if "vep" in c)
                i_imp = next(i for i, c in enumerate(enc) if "importe" in c)
                i_desc = next(i for i, c in enumerate(enc) if "descripcion" in c or "descripción" in c)
                i_fec = next(i for i, c in enumerate(enc) if "fecha" in c)
            except (ValueError, StopIteration):
                continue          # no es la tabla de VEP
            for f in filas[1:]:
                if len(f) <= max(i_est, i_env, i_nro, i_imp, i_desc, i_fec):
                    continue
                numero = re.sub(r"\D", "", str(f[i_nro]))
                if not numero or numero in vistos:
                    continue      # los dos archivos se solapan
                vistos.add(numero)
                impuesto, periodo = leer_concepto(f[i_desc])
                veps.append({
                    "numero": numero, "medio": str(f[i_env]).strip(),
                    "concepto": str(f[i_desc]).strip(),
                    "impuesto": impuesto, "periodo": periodo,
                    "importe": _monto(f[i_imp]), "fecha_pago": _fecha(f[i_fec]),
                    "estado": str(f[i_est]).strip(),
                })

    if not veps:
        print("  No encontré la tabla de VEP en esos archivos.")
        return 2

    sin_leer = [v for v in veps if not v["impuesto"] or not v["periodo"]]
    print(f"  {len(veps)} VEP · {len(veps) - len(sin_leer)} con impuesto y período leídos")
    for v in sorted(veps, key=lambda x: x["fecha_pago"] or "", reverse=True)[:12]:
        print(f"    {v['fecha_pago'] or '?':<11} {v['numero']:<12} {v['medio']:<12}"
              f" {v['importe']:>14,.2f}  {v['concepto']:<16}"
              f" {'→ ' + v['impuesto'] + ' ' + v['periodo'] if v['impuesto'] and v['periodo'] else '(no se pudo leer)'}")
    if len(veps) > 12:
        print(f"    … y {len(veps) - 12} más")

    if args.simular:
        print(f"\n  (simulacro) entrarían {len(veps)}\n")
        return 0

    r = _api("/api/c/pagos-fiscales", {"veps": veps}, alias=cli["alias"])
    if not r.get("ok"):
        print(f"\n  ⚠ {r.get('error')}\n")
        return 3
    print(f"\n  {r['cargados']} cargado(s) · {r['ya_estaban']} ya estaban")
    print(f"  atados a su obligación: {r['con_obligacion']} · al débito del banco: {r['con_debito']}")
    if sin_leer:
        print(f"  ⚠ {len(sin_leer)} sin impuesto/período legible — quedan sin atar, "
              "se completan a mano")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
