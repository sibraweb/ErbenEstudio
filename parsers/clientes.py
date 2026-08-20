# -*- coding: utf-8 -*-
"""Quién es quién para los jobs del estudio.

A diferencia de `Vinculacion bancos/tools/contribuyentes.py` —que es un dict
escrito a mano— acá el registro **se lee de la base del sistema**. Los clientes
se dan de alta en la pantalla y los jobs los ven al instante: una sola fuente
de verdad, sin un archivo que se desincroniza en silencio.

Las FUENTES (arca, DGR-Fsa, banco…) son lo único que vive acá, porque son del
mundo de los jobs y no del ERP: qué portales opera cada cliente.

⚠ NAMESPACE PROPIO. Las credenciales del estudio se guardan bajo `EST/<fuente>`
para que un alias del estudio nunca pise uno nuestro en el Credential Manager
(ARQUITECTURA.md §3). RODRIGUEZ del estudio y un RODRIGUEZ nuestro serían dos
claves distintas aunque se llamen igual.
"""
import os
import sqlite3
from pathlib import Path

DB_PATH = Path(os.environ.get("ESTUDIO_DB", r"C:\SIBRA\estudio\estudio.sqlite3"))

# Las fuentes que los jobs saben operar. La clave es la que usan
# `credenciales.py` y las sesiones (`sesion_*.py`).
FUENTES = {
    "arca":      {"nombre": "ARCA (ex AFIP)", "rubro": "impuestos",
                  "nota": "Mis Comprobantes y vencimientos. Clave fiscal propia por CUIT."},
    "DGR-Fsa":   {"nombre": "ATP Formosa", "rubro": "impuestos",
                  "nota": "IIBB Régimen General. ⚠ el login tiene reCAPTCHA: siempre atendido."},
    "DGR-Ctes":  {"nombre": "DGR Corrientes", "rubro": "impuestos",
                  "nota": "IIBB. Login propio por contribuyente."},
    "banco":     {"nombre": "Home banking", "rubro": "bancos",
                  "nota": "Extractos y cheques. Cada banco tiene su job."},
}

# Una fuente por provincia, NUNCA un "dgr" genérico — misma convención que en
# nuestro sistema: son portales distintos y mezclarlos rompe los dos.
JURISDICCIONES_IIBB = ["DGR-Fsa", "DGR-Ctes"]


def _con():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def todos():
    """[{id, alias, cuit, razon_social, jurisdicciones}] — los clientes activos."""
    if not DB_PATH.exists():
        return []
    con = _con()
    filas = [dict(f) for f in con.execute(
        "SELECT c.id, c.alias, c.cuit, m.razon_social, "
        "  (SELECT GROUP_CONCAT(j.jurisdiccion, ',') FROM cliente_jurisdicciones j "
        "   WHERE j.cliente_id=c.id) AS jurisdicciones "
        "FROM clientes c JOIN maestro_entidades m ON m.cuit=c.cuit "
        "WHERE c.activo=1 ORDER BY c.alias")]
    con.close()
    for f in filas:
        f["jurisdicciones"] = (f["jurisdicciones"] or "").split(",") if f["jurisdicciones"] else []
    return filas


def de_fuente(fuente):
    """Los clientes que operan esa fuente.

    Para las de IIBB se deduce de las jurisdicciones donde el cliente está
    inscripto — que es el dato real, no una lista aparte que hay que recordar
    actualizar. Para el resto (arca, banco) devuelve todos: si el estudio le
    lleva los libros, le lleva ARCA."""
    if fuente in JURISDICCIONES_IIBB:
        return [c for c in todos() if fuente in c["jurisdicciones"]]
    return todos()


def por_alias(alias):
    return next((c for c in todos() if c["alias"] == alias), None)


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    cs = todos()
    print(f"\n  Base: {DB_PATH}")
    print(f"  {len(cs)} cliente(s) del estudio\n")
    for c in cs:
        print(f"  {c['alias']:<12} {c['cuit']:<13} {c['razon_social'][:34]:<36} "
              f"{', '.join(c['jurisdicciones']) or '—'}")
    if not cs:
        print("  (ninguno — se dan de alta en la pantalla Clientes del estudio)")
