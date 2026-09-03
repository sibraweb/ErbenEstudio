# -*- coding: utf-8 -*-
"""Los xlsx de ARCA -> el módulo Facturas.

Bajar los archivos NO es tenerlos en el sistema. En el ERP ese eslabón dependía
de que alguien se acordara de correr el paso siguiente, y el 31/08 pasó lo
previsible: se bajó enero-mayo, el parser corrió, y la carga a la base nunca —
los comprobantes quedaron en el Drive, normalizados, sin entrar. Acá el paso
existe como job propio y `arca_comprobantes.py` lo nombra al terminar.

Qué hace
--------
Lee los `MisComprobantes_*.xlsx` de la carpeta del cliente en el Drive del
estudio y da de alta cada comprobante por la API — no escribiendo el SQLite
derecho. Eso importa: la API es la que pone el signo negativo de las notas de
crédito, la que calcula el total si falta y la que le pone a cada venta la
actividad de IIBB por default. Un INSERT directo se saltearía las tres cosas y
la DJ saldría mal sin que nada avise.

Idempotente
-----------
Se puede correr las veces que haga falta: un comprobante ya cargado se saltea.
La llave es el CAE cuando está, y si no la combinación tipo+punto de venta+
número+contraparte. Hace falta de verdad, porque los tramos de ARCA se
solapan cuando se vuelve a pedir un rango.

⚠ CONTROL DE DIRECCIÓN. El archivo dice si son compras o ventas por su nombre,
pero eso solo no alcanza: se verifica que el CUIT del cliente esté del lado que
corresponde (receptor en los recibidos, emisor en los emitidos). Un archivo mal
nombrado cargaría las ventas como compras y el IVA saldría al revés.

Uso:
    py cargar_comprobantes.py --alias DEMO
    py cargar_comprobantes.py --alias DEMO --archivo <ruta.xlsx>
    py cargar_comprobantes.py --alias DEMO --simular   # no escribe nada
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

# Las alícuotas que ARCA trae como columnas propias. El export da el neto de
# cada una por separado; la base guarda UNA alícuota, así que se toma la del
# neto más grande y se avisa cuando el comprobante tiene más de una.
ALICUOTAS = [0, 2.5, 5, 10.5, 21, 27]

# La venta al mostrador. AFIP la identifica con tipo de documento 99 y número
# 0; acá se guarda con esta llave reservada para que todas caigan juntas y el
# libro de IVA ventas cierre.
CONSUMIDOR_FINAL = "00000000000"


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


def _doc(v):
    """El número de documento tal cual, sin puntos ni guiones.

    ⚠ pandas lee estas columnas como float y las muestra en notación
    científica (3.070624e+10). El valor es exacto —un entero de 11 dígitos
    entra en un float64 sin perder nada— pero hay que convertirlo con int(),
    nunca con str(), o queda «30706240000.0»."""
    if v is None:
        return ""
    s = str(v).strip()
    if s in ("", "nan", "None"):
        return ""
    try:
        return str(int(float(s)))
    except ValueError:
        return re.sub(r"\D", "", s)


def _n(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if f != f else round(f, 2)      # f != f descarta los NaN


def _fecha(v):
    """dd/mm/aaaa -> aaaa-mm-dd."""
    s = str(v).strip()[:10]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}" if m else None


def _tipo(valor):
    """«6 - Factura B» -> ('FA', 'B'). «8 - Nota de Crédito B» -> ('NC', 'B').

    El nombre manda sobre el código: ARCA tiene decenas de códigos (1, 6, 11,
    81 «Tique Factura A»…) y mapearlos uno por uno es una lista que se queda
    vieja. Lo que no cambia es que diga «Crédito» o «Débito»."""
    s = str(valor or "")
    nombre = s.split("-", 1)[1].strip() if "-" in s else s.strip()
    bajo = nombre.lower()
    tipo = "NC" if "cr" in bajo and "dito" in bajo else (
        "ND" if "b" in bajo and "dito" in bajo and "cr" not in bajo else "FA")
    letra = None
    ultimo = nombre.split()[-1] if nombre.split() else ""
    if len(ultimo) == 1 and ultimo.upper() in "ABCEM":
        letra = ultimo.upper()
    return tipo, letra


def _alicuota(fila, cols):
    """De qué alícuota es el comprobante: la del neto gravado más grande."""
    mejor, neto_mejor = None, 0.0
    multiples = 0
    for a in ALICUOTAS:
        etiqueta = str(a).replace(".0", "").replace(".", ",")
        col = cols.get(f"neto grav. iva {etiqueta}%")
        if not col:
            continue
        neto = abs(_n(fila.get(col)))
        if neto > 0:
            multiples += 1
        if neto > neto_mejor:
            mejor, neto_mejor = a, neto
    return mejor, multiples


def _norm(s):
    return re.sub(r"\s+", " ", str(s)).strip().lower()


def leer(path):
    import pandas as pd
    # ⚠ header=1: la primera fila del export es el título del informe, no los
    # nombres de columna.
    df = pd.read_excel(path, header=1)
    cols = {_norm(c): c for c in df.columns}
    return df, cols


def direccion(path, df, cols, cuit_cliente):
    """compra | venta, verificada contra el CUIT del cliente."""
    nombre = path.name.lower()
    if "recibid" in nombre:
        mov, col = "compra", cols.get("nro. doc. receptor")
    elif "emitid" in nombre:
        mov, col = "venta", cols.get("nro. doc. emisor")
    else:
        return None, f"el nombre no dice si son recibidos o emitidos: {path.name}"
    if col is None or df.empty:
        return mov, None
    propios = {_doc(v) for v in df[col].head(20)}
    if cuit_cliente not in propios:
        return None, (f"{path.name} dice «{mov}» pero del lado del cliente "
                      f"aparece {sorted(propios)[:3]}, no {cuit_cliente}. "
                      "Cargarlo pondría el IVA al revés.")
    return mov, None


def main():
    ap = argparse.ArgumentParser(description="Cargar Mis Comprobantes de ARCA al módulo Facturas")
    ap.add_argument("--alias", help="cliente del estudio")
    ap.add_argument("--archivo", help="un xlsx puntual (default: toda la carpeta)")
    ap.add_argument("--simular", action="store_true", help="no escribe: dice qué haría")
    args = ap.parse_args()

    todos = clientes.todos()
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

    cuit_cliente = re.sub(r"\D", "", cli["cuit"])
    carpeta = rutas.carpeta_cliente(cli["cuit"], cli["razon_social"], "comprobantes")
    archivos = ([Path(args.archivo)] if args.archivo
                else sorted(carpeta.glob("MisComprobantes_*.xlsx")))
    if not archivos:
        print(f"No hay archivos en {carpeta}\n"
              f"  Bajalos con:  py arca_comprobantes.py --alias {cli['alias']}")
        return 2

    print(f"\n  {cli['alias']} — {cli['razon_social']} ({cli['cuit']})")
    print(f"  {len(archivos)} archivo(s) en {carpeta}\n")

    # Lo que ya está, para no duplicar. Dos llaves: el CAE (único de verdad) y
    # la combinación del comprobante, para los que no lo traen.
    previas = _api("/api/c/facturas", alias=cli["alias"])
    if isinstance(previas, dict):
        print(f"  No pude leer las facturas: {previas.get('error')}")
        return 2
    caes = {f["cae"] for f in previas if f.get("cae")}
    claves = {(f["mov"], f["tipo"], f["letra"], f["punto_venta"], f["numero"])
              for f in previas}
    entidades = {re.sub(r"\D", "", e["cuit"]): e["id"]
                 for e in _api("/api/c/entidades", alias=cli["alias"])}

    altas = saltados = fallas = 0
    entidades_nuevas = 0
    avisos = []

    for path in archivos:
        df, cols = leer(path)
        mov, problema = direccion(path, df, cols, cuit_cliente)
        if problema:
            print(f"  ⚠ {problema}")
            if mov is None:
                continue
        lado = "emisor" if mov == "compra" else "receptor"
        c_doc, c_nom = cols.get(f"nro. doc. {lado}"), cols.get(f"denominación {lado}",
                                                              cols.get(f"denominacion {lado}"))
        print(f"  {path.name}  ({mov}, {len(df)} filas)")

        for _, fila in df.iterrows():
            fecha = _fecha(fila.get(cols.get("fecha")))
            if not fecha:
                continue
            tipo, letra = _tipo(fila.get(cols.get("tipo")))
            pv = _doc(fila.get(cols.get("punto de venta"))).zfill(4)
            numero = _doc(fila.get(cols.get("número desde", cols.get("numero desde")))).zfill(8)
            cae = _doc(fila.get(cols.get("cód. autorización", cols.get("cod. autorizacion"))))
            if cae and cae in caes:
                saltados += 1
                continue
            if (mov, tipo, letra, pv, numero) in claves:
                saltados += 1
                continue

            doc = _doc(fila.get(c_doc))
            nombre = str(fila.get(c_nom) or "").strip()
            # Factura B sin receptor = CONSUMIDOR FINAL. No es un dato que
            # falte: es la venta al mostrador, y ARCA la manda con las tres
            # columnas vacías. Van todas contra una entidad reservada —la
            # convención de AFIP (tipo doc 99, número 0)— porque si no, tres
            # ventas por $8.350.000 se quedaban afuera del libro.
            if not doc and mov == "venta" and (letra or "") == "B":
                doc, nombre = CONSUMIDOR_FINAL, "CONSUMIDOR FINAL"
            if len(doc) not in (7, 8, 11):
                avisos.append(f"{path.name}: {tipo} {pv}-{numero} sin documento "
                              f"de la contraparte ({doc or 'vacío'}) — no se carga")
                fallas += 1
                continue

            eid = entidades.get(doc)
            if not eid and args.simular:
                # Un simulacro no escribe. Antes daba de alta las entidades
                # igual, que es justo lo que uno quiere evitar al simular.
                entidades[doc] = -1
                entidades_nuevas += 1
                altas += 1
                continue
            if not eid:
                r = _api("/api/c/entidades", {
                    "cuit": doc, "razon_social": nombre or f"SIN NOMBRE {doc}",
                    "es_proveedor": mov == "compra", "es_cliente": mov == "venta"},
                    alias=cli["alias"])
                if not r.get("ok") and "_http" in r:
                    avisos.append(f"entidad {doc} {nombre}: {r.get('error')}")
                    fallas += 1
                    continue
                # Refrescar: si la relación ya existía con otro rol, el alta
                # devuelve 409 y el id sale de la lista.
                entidades = {re.sub(r"\D", "", e["cuit"]): e["id"]
                             for e in _api("/api/c/entidades", alias=cli["alias"])}
                eid = entidades.get(doc)
                entidades_nuevas += 1
                if not eid:
                    fallas += 1
                    continue

            alic, multiples = _alicuota(fila, cols)
            if multiples > 1:
                avisos.append(f"{path.name}: {tipo} {pv}-{numero} tiene {multiples} "
                              f"alícuotas de IVA — se guarda como {alic}%")

            cuerpo = {
                "mov": mov, "entidad_id": eid, "fecha": fecha,
                "tipo": tipo, "letra": letra, "punto_venta": pv, "numero": numero,
                "cae": cae or None,
                "neto": abs(_n(fila.get(cols.get("neto gravado total")))),
                "alicuota_iva": alic,
                "iva": abs(_n(fila.get(cols.get("total iva")))),
                "no_gravado": abs(_n(fila.get(cols.get("neto no gravado")))),
                "exento": abs(_n(fila.get(cols.get("op. exentas")))),
                "percepciones": abs(_n(fila.get(cols.get("otros tributos")))),
                "total": abs(_n(fila.get(cols.get("imp. total")))),
                "origen": "arca",
            }
            if args.simular:
                altas += 1
                continue
            r = _api("/api/c/facturas", cuerpo, alias=cli["alias"])
            if r.get("ok"):
                altas += 1
                if cae:
                    caes.add(cae)
                claves.add((mov, tipo, letra, pv, numero))
            else:
                avisos.append(f"{path.name}: {tipo} {pv}-{numero} — {r.get('error')}")
                fallas += 1

    print(f"\n  {'(simulacro) ' if args.simular else ''}{altas} cargado(s) · "
          f"{saltados} ya estaban · {fallas} sin cargar · "
          f"{entidades_nuevas} entidad(es) nueva(s)")
    if avisos:
        print(f"\n  {len(avisos)} aviso(s):")
        for a in avisos[:25]:
            print(f"    · {a}")
        if len(avisos) > 25:
            print(f"    … y {len(avisos) - 25} más")
    print()
    return 0 if not fallas else 3


if __name__ == "__main__":
    sys.exit(main())
