"""
MAESTRO UNIVERSAL de bancos argentinos — código de entidad BCRA (los primeros
3 dígitos del CBU), razón social prolija y apodos para buscar.

El criterio (Juan, 2026-08-16): son DOS listas distintas.
  · Este maestro es el UNIVERSO: se usa solo al DAR DE ALTA una cuenta —
    escribís "hipotecario" y sale BANCO HIPOTECARIO S.A. bien escrito,
    tengamos o no parser de ese banco.
  · La tabla `bancos` de la base sigue siendo LO QUE TENEMOS (cuentas
    propias + libradores de cheques que ya aparecieron). Al operar
    (cheque propio de Sibratech, selector de cuentas) se ofrece ESO,
    nunca este universo entero.

Fuente de los códigos: tabla de entidades del CBU (BCRA / Wikipedia
"Clave Bancaria Uniforme", cruzada 2026-08-16). Los nombres se
actualizaron a los vigentes (Santander Río→Santander Argentina, HSBC→GGAL
"Galicia Más", Itaú→BMA/Macro, MBA Lazard→Voii, Wilobank→Ualá); el nombre
viejo queda como apodo así se sigue encontrando.

⚠ NUNCA renombrar un banco que ya se usó en una cuenta: este maestro solo
alimenta ALTAS y el backfill de `codigo_bcra`. Cambiarle el nombre a uno en uso
deja movimientos apuntando a un banco que ya no se llama así.

── Por qué está COPIADO y no prestado (ERBEN, 03/09) ────────────────────────
Los jobs que ERBEN toma del otro repo están marcados como préstamo temporal
(ver parsers/LEEME.md), porque hablan con portales y sesiones nuestras. Esto no:
son los códigos del BCRA, no tienen nada de nadie, y el estudio tiene que poder
arrancar en otra máquina sin el otro repo al lado. Si el BCRA cambia algo, se
actualiza en los dos — es una lista de datos públicos, no lógica compartida.
"""
import re
import unicodedata

# (codigo_bcra, nombre_corto, razon_social, apodos_extra)
# El buscador matchea contra corto + razón + apodos, sin acentos.
MAESTRO = [
    ("007", "Galicia",          "BANCO DE GALICIA Y BUENOS AIRES S.A.U.", "GGAL"),
    ("011", "Nación",           "BANCO DE LA NACION ARGENTINA", "BNA"),
    ("014", "Provincia (BAPRO)", "BANCO DE LA PROVINCIA DE BUENOS AIRES", "BAPRO PROVINCIA"),
    ("015", "ICBC",             "INDUSTRIAL AND COMMERCIAL BANK OF CHINA (ARGENTINA) S.A.U.", ""),
    ("016", "Citibank",         "CITIBANK N.A. (SUCURSAL ARGENTINA)", "CITI"),
    ("017", "BBVA",             "BANCO BBVA ARGENTINA S.A.", "FRANCES"),
    ("020", "Bancor (Córdoba)", "BANCO DE LA PROVINCIA DE CORDOBA S.A.", "BANCOR"),
    ("027", "Supervielle",      "BANCO SUPERVIELLE S.A.", ""),
    ("029", "Ciudad",           "BANCO DE LA CIUDAD DE BUENOS AIRES", "BANCO CIUDAD"),
    ("034", "Patagonia",        "BANCO PATAGONIA S.A.", ""),
    ("044", "Hipotecario",      "BANCO HIPOTECARIO S.A.", ""),
    ("045", "San Juan",         "BANCO DE SAN JUAN S.A.", ""),
    ("046", "Banco do Brasil",  "BANCO DO BRASIL S.A. (SUCURSAL ARGENTINA)", ""),
    ("060", "Tucumán (→Macro)", "BANCO DEL TUCUMAN S.A. (absorbido por Macro)", ""),
    ("065", "Municipal Rosario", "BANCO MUNICIPAL DE ROSARIO", ""),
    ("072", "Santander",        "BANCO SANTANDER ARGENTINA S.A.", "RIO SANTANDER RIO"),
    ("083", "Chubut",           "BANCO DEL CHUBUT S.A.", ""),
    ("086", "Santa Cruz",       "BANCO DE SANTA CRUZ S.A.", ""),
    ("093", "La Pampa",         "BANCO DE LA PAMPA S.E.M.", ""),
    ("094", "Corrientes",       "BANCO DE CORRIENTES S.A.", "BCOCTES"),
    ("097", "Neuquén (BPN)",    "BANCO PROVINCIA DEL NEUQUEN S.A.", "BPN"),
    ("143", "Brubank",          "BRUBANK S.A.U.", ""),
    ("147", "Interfinanzas",    "BANCO INTERFINANZAS S.A.", ""),
    ("150", "Galicia Más (ex HSBC)", "BANCO GGAL S.A. (ex HSBC — Galicia Más)", "HSBC GALICIA MAS"),
    ("158", "Openbank",         "OPENBANK ARGENTINA S.A.", ""),
    ("165", "JP Morgan",        "JPMORGAN CHASE BANK N.A. (SUCURSAL BUENOS AIRES)", "JPMORGAN"),
    ("191", "Credicoop",        "BANCO CREDICOOP COOPERATIVO LIMITADO", ""),
    ("198", "Banco de Valores", "BANCO DE VALORES S.A.", ""),
    ("247", "Roela",            "BANCO ROELA S.A.", ""),
    ("254", "Mariva",           "BANCO MARIVA S.A.", ""),
    ("259", "BMA (ex Itaú)",    "BANCO BMA S.A.U. (ex Itaú — grupo Macro)", "ITAU"),
    ("266", "BNP Paribas",      "BNP PARIBAS (SUCURSAL BUENOS AIRES)", ""),
    ("268", "Tierra del Fuego", "BANCO PROVINCIA DE TIERRA DEL FUEGO", "BTF"),
    ("269", "BROU",             "BANCO DE LA REPUBLICA ORIENTAL DEL URUGUAY", ""),
    ("277", "Sáenz",            "BANCO SAENZ S.A.", ""),
    ("281", "Meridian",         "BANCO MERIDIAN S.A.", ""),
    ("285", "Macro",            "BANCO MACRO S.A.", ""),
    ("299", "Comafi",           "BANCO COMAFI S.A.", ""),
    ("300", "BICE",             "BANCO DE INVERSION Y COMERCIO EXTERIOR S.A. (BICE)", ""),
    ("301", "Piano",            "BANCO PIANO S.A.", ""),
    ("305", "Julio",            "BANCO JULIO S.A.", ""),
    ("309", "Rioja",            "BANCO RIOJA S.A.U.", "LA RIOJA"),
    ("310", "Banco del Sol",    "BANCO DEL SOL S.A. (grupo Sancor Seguros)", "SANCOR"),
    ("311", "Chaco (NBCH)",     "NUEVO BANCO DEL CHACO S.A.", "NBCH"),
    ("312", "Voii",             "BANCO VOII S.A. (ex MBA Lazard)", "MBA LAZARD"),
    ("315", "Formosa",          "BANCO DE FORMOSA S.A.", ""),
    ("319", "CMF",              "BANCO CMF S.A.", ""),
    ("321", "Santiago del Estero", "BANCO DE SANTIAGO DEL ESTERO S.A.", "BSE"),
    ("322", "BIND (Industrial)", "BANCO INDUSTRIAL S.A.", "BIND"),
    ("330", "Santa Fe",         "NUEVO BANCO DE SANTA FE S.A.", ""),
    ("331", "Cetelem",          "BANCO CETELEM ARGENTINA S.A.", ""),
    ("332", "BSF (Carrefour)",  "BANCO DE SERVICIOS FINANCIEROS S.A.", "BSF"),
    ("336", "Bradesco",         "BANCO BRADESCO ARGENTINA S.A.U.", ""),
    ("338", "BST / Reba",       "BANCO DE SERVICIOS Y TRANSACCIONES S.A.", "BST REBA"),
    ("339", "RCI (Renault)",    "RCI BANQUE S.A. (SUCURSAL ARGENTINA)", "RENAULT"),
    ("340", "BACS",             "BACS BANCO DE CREDITO Y SECURITIZACION S.A.", ""),
    ("341", "Masventas",        "BANCO MASVENTAS S.A.", "BMV MAS VENTAS"),
    ("384", "Ualá (ex Wilobank)", "UALA BANK S.A.U. (ex Wilobank)", "WILOBANK UALA UILO"),
    ("386", "Entre Ríos (BERSA)", "NUEVO BANCO DE ENTRE RIOS S.A.", "BERSA"),
    ("389", "Columbia",         "BANCO COLUMBIA S.A.", ""),
    ("405", "Ford Credit",      "FORD CREDIT COMPAÑIA FINANCIERA S.A.", ""),
    ("406", "Metrópolis",       "METROPOLIS COMPAÑIA FINANCIERA S.A.", ""),
    ("408", "Efectivo Sí (CFA)", "COMPAÑIA FINANCIERA ARGENTINA S.A. (Efectivo Sí)", "EFECTIVO SI"),
    ("413", "Montemar",         "MONTEMAR COMPAÑIA FINANCIERA S.A.", ""),
    ("415", "Transatlántica",   "TRANSATLANTICA COMPAÑIA FINANCIERA S.A.", ""),
    ("426", "Bica",             "BANCO BICA S.A.", ""),
    ("428", "La Capital del Plata", "CAJA DE CREDITO COOP. LA CAPITAL DEL PLATA LTDA.", ""),
    ("431", "Coinag",           "BANCO COINAG S.A.", ""),
    ("432", "Banco de Comercio", "BANCO DE COMERCIO S.A.", ""),
    ("434", "Cuenca",           "CAJA DE CREDITO CUENCA COOP. LTDA.", ""),
    ("437", "Volkswagen",       "VOLKSWAGEN FINANCIAL SERVICES COMPAÑIA FINANCIERA S.A.", "VW"),
    ("438", "Cordial",          "CORDIAL COMPAÑIA FINANCIERA S.A. (grupo Supervielle)", ""),
    ("440", "FCA (Fiat)",       "FCA COMPAÑIA FINANCIERA S.A. (ex Fiat Crédito)", "FIAT"),
    ("441", "GPAT",             "GPAT COMPAÑIA FINANCIERA S.A.U. (grupo Patagonia)", ""),
    ("442", "Mercedes-Benz",    "MERCEDES-BENZ COMPAÑIA FINANCIERA ARGENTINA S.A.", ""),
    ("443", "Rombo",            "ROMBO COMPAÑIA FINANCIERA S.A. (Renault-Nissan)", ""),
    ("444", "John Deere",       "JOHN DEERE FINANCIAL COMPAÑIA FINANCIERA S.A.", ""),
    ("445", "PSA Finance",      "PSA FINANCE ARGENTINA COMPAÑIA FINANCIERA S.A.", "PEUGEOT"),
    ("446", "Toyota",           "TOYOTA COMPAÑIA FINANCIERA DE ARGENTINA S.A.", ""),
    ("448", "Dino (ex Finandino)", "BANCO DINO S.A. (ex Finandino)", "FINANDINO"),
    ("453", "Naranja X",        "NARANJA DIGITAL COMPAÑIA FINANCIERA S.A.U. (Naranja X)", "NARANJA"),
]

# CVU: los 22 dígitos que arrancan con 000 son cuentas VIRTUALES (billeteras/
# PSP — Mercado Pago, etc.). El "banco" no sale del código de entidad; el PSP
# está en los dígitos 4-8 y no lo mapeamos acá.
CODIGO_CVU = "000"


def _norm(s):
    """mayúsculas + sin acentos + espacios colapsados — para buscar."""
    s = unicodedata.normalize("NFD", str(s or ""))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s.upper()).strip()


def buscar(texto):
    """Typeahead sobre el maestro: devuelve [(codigo, corto, razon), ...].

    Matchea por inclusión contra corto + razón social + apodos, sin
    acentos. "hipotecario" → [("044", "Hipotecario", "BANCO HIPOTECARIO
    S.A.")]. Orden: primero los que arrancan con lo tipeado.
    """
    q = _norm(texto)
    if not q:
        return []
    out = []
    for cod, corto, razon, apodos in MAESTRO:
        pajar = _norm(f"{corto} {razon} {apodos}")
        if q in pajar or q == cod:
            arranca = _norm(corto).startswith(q) or _norm(razon).startswith(q)
            out.append((0 if arranca else 1, cod, corto, razon))
    out.sort()
    return [(c, n, r) for _, c, n, r in out]


def por_codigo(codigo):
    """→ (codigo, corto, razon) o None. Acepta '44', '044' o un CBU entero."""
    s = re.sub(r"\D", "", str(codigo or ""))
    if len(s) >= 22:            # vino un CBU
        s = s[:3]
    s = s.zfill(3)
    for cod, corto, razon, _ in MAESTRO:
        if cod == s:
            return (cod, corto, razon)
    return None


def _dv(digitos, pesos):
    suma = sum(int(d) * p for d, p in zip(digitos, pesos))
    return (10 - suma % 10) % 10


def validar_cbu(cbu):
    """→ (ok, error, codigo_entidad).

    Chequea 22 dígitos y los DOS dígitos verificadores (pesos 7139713 /
    3971397139713 — estándar COELSA). Devuelve el código de entidad (3
    primeros) para cruzar contra el banco elegido; '000' = CVU de
    billetera virtual (válido, pero no identifica banco).
    """
    s = re.sub(r"\D", "", str(cbu or ""))
    if len(s) != 22:
        return (False, f"el CBU debe tener 22 dígitos (vinieron {len(s)})", None)
    if int(s[7]) != _dv(s[:7], [7, 1, 3, 9, 7, 1, 3]):
        return (False, "dígito verificador del primer bloque no cierra (¿número mal tipeado?)", s[:3])
    if int(s[21]) != _dv(s[8:21], [3, 9, 7, 1, 3, 9, 7, 1, 3, 9, 7, 1, 3]):
        return (False, "dígito verificador del segundo bloque no cierra (¿número mal tipeado?)", s[:3])
    return (True, None, s[:3])
