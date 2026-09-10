# -*- coding: utf-8 -*-
"""El resumen del banco en PDF → las mismas filas que un CSV.

Los bancos mandan el resumen en PDF y muchos clientes no tienen otra cosa: no
hay CSV para bajar, o el home banking lo da con tres meses de retraso. Hasta
acá `cargar_extracto.py` leía CSV y XLSX; esto agrega el PDF sin tocar nada de
lo que ya andaba — devuelve filas con nombre de columna y el cargador sigue con
su camino de siempre (mapear → normalizar → validar la cadena de saldos).

Cómo lee
--------
**Por la posición horizontal de cada número, no por el orden del texto.** El
volcado plano de un PDF entrega los renglones mezclados: la fecha, después el
importe, después el concepto, y el saldo colgando en la línea siguiente. Ahí no
hay forma de saber si un número es débito o crédito — y confundirlos invierte
el signo del movimiento, que es el peor error posible en un extracto.

Cada columna tiene su borde derecho fijo porque los importes van alineados a la
derecha, así que el número se clasifica por dónde TERMINA. Los cortes salen del
encabezado del propio archivo, no de una tabla escrita a mano.

Y hay una segunda prueba, independiente: el resumen trae el SALDO de cada
renglón. Si la lectura está bien, `saldo[i] = saldo[i-1] + crédito − débito`
cierra para las cientos de filas del mes. Eso lo verifica el cargador y es lo
que permite dar el archivo por entero — no que el parser "parezca" andar.

Qué NO hace
-----------
No deduce. Si el banco deja la columna vacía —y este resumen deja vacías las
de CBU y CUIT de las transferencias— acá va vacío. La única unión que se hace
es con el anexo de DÉBITOS AUTOMÁTICOS, y no es una inferencia: el anexo trae
la misma referencia que el renglón de la cuenta, así que el nombre de la
empresa lo está diciendo el mismo resumen.

Probado contra
--------------
Banco de Formosa S.A. (CBU 315), cuenta corriente en pesos, resúmenes de julio
y agosto de 2026: 435 movimientos, la cadena de saldos cierra al centavo en los
dos meses y el saldo final de julio es el inicial de agosto.
"""
import re

FECHA = re.compile(r"^(\d{2}/\d{2}/\d{2})(.*)$")
IMPORTE = re.compile(r"^\d[\d,]*\.\d{2}$")

# Dónde termina cada columna de importes, en puntos del PDF. Salen del
# encabezado (DEBITOS / CREDITOS / SALDO) y los importes van alineados a la
# derecha, así que el borde derecho es el dato estable: el izquierdo se corre
# según cuántos dígitos tenga el número.
CORTE_DEBITO = 440
CORTE_CREDITO = 525
X_CONCEPTO = 195      # lo que está más a la izquierda es concepto
X_REFERENCIA = 282    # entre 195 y esto, la referencia
X_CHEQUE = 340        # entre 282 y esto, el número de cheque


def _num(s):
    return float(s.replace(",", ""))


def _renglones(pagina):
    """Los words agrupados por renglón real.

    ⚠ Agrupar por `round(top / n)` NO sirve: dos palabras del mismo renglón
    pueden caer a los dos lados del redondeo (349.48 y 350.23 con n=4 dan 87 y
    88) y el saldo se separa de su movimiento. Se agrupa por cercanía."""
    salida, actual, top = [], [], None
    for w in sorted(pagina.extract_words(), key=lambda w: (w["top"], w["x0"])):
        if top is not None and w["top"] - top > 2:
            salida.append(actual)
            actual, top = [], None
        actual.append(w)
        if top is None:
            top = w["top"]
    if actual:
        salida.append(actual)
    return salida


def leer(path):
    """El PDF → [{fecha, concepto, referencia, debitos, creditos, saldo}].

    Los nombres de las claves son los del encabezado del resumen a propósito:
    `cargar_extracto.mapear()` busca las columnas por nombre, así que el PDF
    entra por la misma puerta que un CSV del home banking."""
    try:
        import pdfplumber
    except ImportError:
        raise SystemExit("Para leer el resumen en PDF hace falta pdfplumber:\n"
                         "  py -m pip install pdfplumber")

    filas, automaticos = [], {}
    # `en_anexo` arranca en False y se prende con SALDO FINAL: de ahí para
    # abajo el PDF sigue con los anexos (transferencias, débitos automáticos),
    # que tienen fechas e importes pero NO son movimientos de la cuenta.
    # Cargarlos duplicaría el mes entero.
    en_anexo = False
    with pdfplumber.open(path) as pdf:
        for pagina in pdf.pages:
            for ws in _renglones(pagina):
                ws = sorted(ws, key=lambda w: w["x0"])
                texto = " ".join(w["text"] for w in ws)
                if texto.startswith("SALDO FINAL"):
                    en_anexo = True
                    continue
                if texto.startswith("FECHA CONCEPTO REFERENCIA"):
                    en_anexo = False          # la tabla sigue en la hoja nueva
                    continue
                m = FECHA.match(ws[0]["text"])
                if not m:
                    continue
                if en_anexo:
                    _anexo(texto, automaticos)
                    continue
                # La fecha viene pegada al concepto ("01/07/26DB Camara Rec")
                # porque el PDF no pone espacio entre columnas.
                concepto = (m.group(2) + " " + " ".join(
                    w["text"] for w in ws[1:] if w["x0"] < X_CONCEPTO)).strip()
                deb = cre = saldo = None
                for w in ws:
                    if not IMPORTE.match(w["text"]) or w["x1"] < X_CHEQUE:
                        continue
                    if w["x1"] < CORTE_DEBITO:
                        deb = _num(w["text"])
                    elif w["x1"] < CORTE_CREDITO:
                        cre = _num(w["text"])
                    else:
                        saldo = _num(w["text"])
                ref = " ".join(w["text"] for w in ws
                               if X_CONCEPTO <= w["x0"] < X_REFERENCIA).strip()
                cheque = " ".join(w["text"] for w in ws
                                  if X_REFERENCIA <= w["x0"] < X_CHEQUE).strip()
                filas.append({
                    "fecha": m.group(1),
                    "concepto": re.sub(r"\s+", " ", concepto),
                    "referencia": " · ".join(
                        x for x in (ref, f"cheque {cheque}" if cheque else "") if x),
                    "debitos": f"{deb:.2f}" if deb is not None else "",
                    "creditos": f"{cre:.2f}" if cre is not None else "",
                    "saldo": f"{saldo:.2f}" if saldo is not None else "",
                })
    _pegar_automaticos(filas, automaticos)
    return filas


def _anexo(texto, automaticos):
    """El anexo de DÉBITOS AUTOMÁTICOS: guarda quién cobró, por referencia.

    Formato: `05/08/26 SANCOR COOP.SEG. PAGOSEGURO 0200000… SS0030133611-11
    117,918.50`. La referencia (la anteúltima columna) es la MISMA que aparece
    en el renglón de la cuenta, y por eso se pueden unir sin adivinar nada."""
    partes = texto.split()
    if len(partes) < 4:
        return
    ref = partes[-2]
    empresa = " ".join(partes[1:-3]).strip()
    if ref and empresa and not IMPORTE.match(ref):
        automaticos[ref] = empresa


def _pegar_automaticos(filas, automaticos):
    """Le pone el nombre de la empresa al débito automático que la nombra.

    El renglón de la cuenta dice «DB Directo SNP» y nada más — no sirve para
    nada. El anexo del mismo resumen dice que ese débito fue de SANCOR. No es
    una deducción: es un dato que el banco ya escribió dos hojas más adelante.
    """
    for f in filas:
        for ref, empresa in automaticos.items():
            if ref and ref in f["referencia"]:
                f["referencia"] = f"{f['referencia']} · {empresa}"
                break
