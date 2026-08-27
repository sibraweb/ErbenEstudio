# -*- coding: utf-8 -*-
"""Extracto del banco → movimientos del sistema.

El primero de los cargadores: los jobs de banco BAJAN el archivo, este lo
ACOMODA y lo mete en la base. Lee lo que dejan los parsers de SIBRA
(`H:\\My Drive\\web_sibra\\tesoreria\\<banco>\\…csv`) y también un CSV o XLSX
que alguien baje a mano del home banking.

Tres cosas que hace y que valen más que el import en sí
-------------------------------------------------------
1. **Encuentra las columnas por nombre, no por posición.** Cada banco arma su
   CSV como quiere y cambia el orden entre exportaciones. Buscar "fecha",
   "débito", "saldo" aguanta eso; contar columnas, no.

2. **Valida la cadena de saldos**: `saldo[i] == saldo[i-1] + crédito − débito`.
   Si la cadena cierra, no falta ni sobra ningún movimiento. Es el mismo
   control que ya corre en los parsers de SIBRA, y es lo único que prueba que
   el archivo está entero. Si no cierra, avisa y **no carga** salvo que se lo
   fuerce: media cadena es peor que nada, porque el saldo miente sin decirlo.

3. **Es idempotente.** La llave la calcula el servidor (huella + ordinal, sin
   el saldo). Volver a cargar el mismo mes no duplica; un mes nuevo entra.

Uso:
    py cargar_extracto.py --alias DEMO --cuenta 1 --archivo "C:\\...\\Galicia_2026-08.csv"
    py cargar_extracto.py --alias DEMO --cuenta 1 --archivo x.csv --revisar   # no carga
    py cargar_extracto.py --alias DEMO --listar-cuentas
"""
import argparse
import csv
import io
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = "http://localhost:8310"

# Cómo se llama cada cosa según el banco. Se busca por SUBCADENA y sin
# acentos: "Fecha Mov.", "FECHA VALOR" y "fecha" caen todas en la misma.
NOMBRES = {
    "fecha":       ["fecha"],
    "descripcion": ["descripcion", "concepto", "detalle", "movimiento", "referencia patron"],
    "debito":      ["debito", "debe", "egreso", "retiro"],
    "credito":     ["credito", "haber", "ingreso", "deposito"],
    "importe":     ["importe", "monto"],          # bancos que traen UNA columna con signo
    "saldo":       ["saldo"],
    "referencia":  ["nro", "numero", "operacion", "comprobante", "referencia"],
}


def _sin_acentos(s):
    tabla = str.maketrans("áéíóúÁÉÍÓÚñÑ", "aeiouAEIOUnN")
    return (s or "").translate(tabla).strip().lower()


def _num(v):
    """'1.234,56' → 1234.56 · '(1.234,56)' → -1234.56 · '' → None.

    Los paréntesis son negativos en varios exports contables, y si no se
    interpretan el egreso entra como ingreso: el saldo se va al doble."""
    t = (v or "").strip()
    if not t:
        return None
    negativo = t.startswith("(") and t.endswith(")")
    t = re.sub(r"[^\d,.\-]", "", t)
    if not t or t in ("-", ".", ","):
        return None
    if "," in t and "." in t:          # 1.234,56  (formato AR)
        t = t.replace(".", "").replace(",", ".")
    elif "," in t:                     # 1234,56
        t = t.replace(",", ".")
    try:
        n = float(t)
    except ValueError:
        return None
    return -abs(n) if negativo else n


def _fecha(v):
    """A ISO. Acepta dd/mm/aaaa, dd-mm-aa y aaaa-mm-dd."""
    t = (v or "").strip()[:10]
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", t)
    if m:
        return t
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})$", t)
    if not m:
        return None
    d, mes, a = m.groups()
    a = int(a)
    a += 2000 if a < 100 else 0
    return f"{a:04d}-{int(mes):02d}-{int(d):02d}"


def leer(path):
    """El archivo → filas crudas [{columna: valor}]. CSV (con el separador que
    sea) o XLSX."""
    path = Path(path)
    if not path.exists():
        raise SystemExit(f"No encuentro el archivo:\n  {path}")
    if path.suffix.lower() in (".xlsx", ".xls"):
        try:
            import pandas as pd
        except ImportError:
            raise SystemExit("Para leer Excel hace falta pandas:  py -m pip install pandas openpyxl")
        df = pd.read_excel(path, dtype=str).fillna("")
        return [{str(k): str(v) for k, v in fila.items()} for fila in df.to_dict("records")]

    crudo = path.read_bytes().decode("utf-8-sig", errors="replace")
    # El separador se detecta contando en la primera línea con contenido: el
    # CSV de Galicia usa ';', otros ',' y algunos tabs.
    primera = next((l for l in crudo.splitlines() if l.strip()), "")
    sep = max([";", ",", "\t"], key=primera.count)
    filas = list(csv.reader(io.StringIO(crudo), delimiter=sep))
    filas = [f for f in filas if any(c.strip() for c in f)]
    if len(filas) < 2:
        raise SystemExit("El archivo no tiene filas de datos.")
    # El encabezado no siempre está en la línea 1 (hay bancos que ponen el
    # logo y el período arriba): es la primera fila que tenga una fecha.
    i_enc = next((i for i, f in enumerate(filas)
                  if any(_sin_acentos(c).startswith("fecha") for c in f)), 0)
    enc = [c.strip() for c in filas[i_enc]]
    out = []
    for f in filas[i_enc + 1:]:
        # Galicia cierra cada fila con ';' de más: sobra una columna vacía.
        out.append({enc[i]: (f[i] if i < len(f) else "") for i in range(len(enc))})
    return out


def mapear(filas):
    """Encuentra qué columna es qué. Devuelve (mapa, columnas_del_archivo)."""
    if not filas:
        raise SystemExit("El archivo no trajo filas.")
    columnas = list(filas[0].keys())
    mapa = {}
    for campo, claves in NOMBRES.items():
        for col in columnas:
            c = _sin_acentos(col)
            if any(k in c for k in claves):
                mapa[campo] = col
                break
    if "fecha" not in mapa:
        raise SystemExit(
            "No encontré la columna de FECHA. Las columnas del archivo son:\n  "
            + "\n  ".join(columnas))
    if not ({"debito", "credito"} <= set(mapa)) and "importe" not in mapa:
        raise SystemExit(
            "No encontré ni débito/crédito ni una columna de importe. Columnas:\n  "
            + "\n  ".join(columnas))
    return mapa, columnas


def normalizar(filas, mapa):
    """Filas crudas → movimientos con importe firmado (+ acredita, − debita)."""
    out = []
    for f in filas:
        fecha = _fecha(f.get(mapa["fecha"]))
        if not fecha:
            continue                       # totales, pies de página, líneas sueltas
        if "importe" in mapa and not ({"debito", "credito"} <= set(mapa)):
            importe = _num(f.get(mapa["importe"]))
        else:
            deb = _num(f.get(mapa.get("debito"))) or 0.0
            cre = _num(f.get(mapa.get("credito"))) or 0.0
            importe = round(cre - abs(deb), 2)
        if importe is None or importe == 0:
            continue
        out.append({
            "fecha": fecha,
            "descripcion": (f.get(mapa.get("descripcion")) or "").strip(),
            "importe": round(importe, 2),
            "saldo": _num(f.get(mapa.get("saldo"))),
            "referencia": (f.get(mapa.get("referencia")) or "").strip() or None,
            "origen": "extracto",
        })
    return out


def validar_cadena(movs):
    """saldo[i] == saldo[i-1] + importe[i]. Devuelve (errores, primer_corte).

    Es el control que prueba que el archivo está ENTERO. Sin saldos no se
    puede validar y se dice, en vez de dar por bueno lo que no se revisó."""
    con_saldo = [m for m in movs if m["saldo"] is not None]
    if len(con_saldo) < 2:
        return None, None
    errores, primero = 0, None
    for i in range(1, len(con_saldo)):
        esperado = round(con_saldo[i - 1]["saldo"] + con_saldo[i]["importe"], 2)
        if abs(esperado - con_saldo[i]["saldo"]) > 0.01:
            errores += 1
            if primero is None:
                primero = (con_saldo[i], esperado)
    return errores, primero


def _api(ruta, cuerpo=None):
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    req = urllib.request.Request(API + ruta, data=datos,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.URLError:
        raise SystemExit(f"No pude hablar con el sistema en {API}.\n"
                         "  Arrancá ERBEN ESTUDIO (el ícono del escritorio) y probá de nuevo.")
    except urllib.error.HTTPError as e:
        raise SystemExit("El sistema rechazó la carga: "
                         + e.read().decode("utf-8", "replace")[:200])


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Carga un extracto bancario al sistema")
    ap.add_argument("--alias", required=True, help="cliente del estudio")
    ap.add_argument("--cuenta", type=int, help="id de la cuenta bancaria")
    ap.add_argument("--archivo")
    ap.add_argument("--revisar", action="store_true", help="muestra qué haría, sin cargar")
    ap.add_argument("--forzar", action="store_true",
                    help="cargar aunque la cadena de saldos no cierre")
    ap.add_argument("--listar-cuentas", action="store_true")
    a = ap.parse_args()

    if a.listar_cuentas:
        for c in _api(f"/api/c/cuentas?cliente={a.alias}"):
            print(f"  {c['id']:>3}  {c['banco']}  {c.get('tipo') or ''} {c.get('numero') or ''}"
                  f"   saldo {c['saldo_calculado']:,.2f}   ({c['movimientos']} mov.)")
        return 0
    if not (a.archivo and a.cuenta):
        raise SystemExit("Hacen falta --archivo y --cuenta (ver --listar-cuentas)")

    filas = leer(a.archivo)
    mapa, columnas = mapear(filas)
    movs = normalizar(filas, mapa)
    print(f"\n  Archivo: {Path(a.archivo).name}")
    print(f"  Columnas encontradas: " + " · ".join(f"{k}={v}" for k, v in mapa.items()))
    print(f"  {len(filas)} fila(s) leídas → {len(movs)} movimiento(s)")
    if not movs:
        raise SystemExit("  No salió ningún movimiento. ¿Es el archivo correcto?")
    print(f"  Del {movs[0]['fecha']} al {movs[-1]['fecha']} · "
          f"suma {sum(m['importe'] for m in movs):,.2f}")

    errores, corte = validar_cadena(movs)
    if errores is None:
        print("  ⚠ El archivo no trae saldos: no se puede validar que esté entero.")
    elif errores:
        print(f"\n  ⛔ LA CADENA DE SALDOS NO CIERRA — {errores} corte(s).")
        if corte:
            m, esperado = corte
            print(f"     Primero en {m['fecha']} · {m['descripcion'][:40]}")
            print(f"     El saldo dice {m['saldo']:,.2f} y debería ser {esperado:,.2f}")
        print("     Suele significar que al extracto le faltan filas (filtro de fechas\n"
              "     mal aplicado, o el banco paginó y no se bajó todo).")
        if not a.forzar:
            raise SystemExit("     NO se cargó nada. Si igual lo querés, agregá --forzar.")
        print("     --forzar puesto: se carga igual.")
    else:
        print(f"  ✓ Cadena de saldos OK: el extracto está entero.")

    if a.revisar:
        print("\n  (revisión: no se cargó nada)")
        for m in movs[:8]:
            print(f"    {m['fecha']}  {m['importe']:>13,.2f}  {(m['descripcion'] or '')[:46]}")
        if len(movs) > 8:
            print(f"    … y {len(movs) - 8} más")
        return 0

    r = _api(f"/api/c/movimientos?cliente={a.alias}",
             {"cuenta_id": a.cuenta, "movimientos": movs})
    print(f"\n  CARGADO: {r['nuevos']} nuevo(s) · {r['repetidos']} que ya estaban")
    if r["repetidos"]:
        print("  (los repetidos no se duplicaron: la llave es huella + ordinal)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
