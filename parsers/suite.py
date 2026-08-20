# -*- coding: utf-8 -*-
"""El catálogo de jobs del estudio — y la puerta que usa la app para correrlos.

Mismo espíritu que `Vinculacion bancos/tools/suite.py`: un solo lugar que dice
qué jobs hay, de qué rubro son, con qué fuente se loguean y qué clientes los
usan. La diferencia es que acá el catálogo también lo consume la PANTALLA
(módulo Jobs), no solo la consola.

De dónde sale cada job
----------------------
Los scrapers de bancos y ARCA ya existen y están probados en
`Vinculacion bancos/tools/`. **No se reescriben**: se invocan desde ahí. Lo
que es propio del estudio son el registro de clientes, las credenciales y los
jobs que tocan la base del estudio (`atp_iibb`, `dj_a_dgr`).

⚠ Ese préstamo está marcado `heredado` en el catálogo y sale de una sola
constante (`TOOLS_SIBRA`). El día que el estudio corra en otra máquina, o que
esos jobs se muden acá, se toca un solo lugar.

Uso:
    py suite.py                 # el catálogo
    py suite.py --credenciales  # qué falta cargar
    py suite.py --json          # lo que consume la app
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import clientes  # noqa: E402
import credenciales  # noqa: E402

AQUI = Path(__file__).parent
# Los jobs heredados de nuestro sistema (mismo disco, por ahora).
TOOLS_SIBRA = Path(os.environ.get(
    "SIBRA_TOOLS",
    Path(__file__).resolve().parents[2] / "Vinculacion bancos" / "tools"))

# clave → (archivo, título, rubro, fuente, dónde vive, qué necesita)
#   atendido = hace falta una persona (login con 2FA o captcha)
JOBS = {
    "atp_sesion": {
        "archivo": "sesion_atp.py", "propio": True,
        "titulo": "ATP Formosa — abrir sesión",
        "rubro": "impuestos", "fuente": "DGR-Fsa", "atendido": True,
        "desc": "Login del cliente en ATP y guarda la sesión. El portal tiene "
                "reCAPTCHA, así que lo hace una persona.",
        "args": ["--alias"],
    },
    "atp_relevar": {
        "archivo": "atp_iibb.py", "propio": True,
        "titulo": "ATP Formosa — relevar todo",
        "rubro": "impuestos", "fuente": "DGR-Fsa", "atendido": False,
        "desc": "Padrón de actividades, bases, deducciones, DJ presentadas, "
                "retenciones por agente, estado de deuda y buzón fiscal.",
        "args": ["--alias"],
    },
    "dj_a_dgr": {
        "archivo": "dj_a_dgr.py", "propio": True,
        "titulo": "Llevar la DJ de IIBB al portal",
        "rubro": "impuestos", "fuente": "DGR-Fsa", "atendido": True,
        "desc": "Toma la DJ liquidada acá y escribe las bases por actividad en "
                "la grilla de ATP. NO presenta: eso lo hace la persona.",
        "args": ["--alias", "--periodo"],
    },
    "arca_comprobantes": {
        "archivo": "mis_comprobantes.py", "propio": False,
        "titulo": "ARCA — Mis Comprobantes",
        "rubro": "impuestos", "fuente": "arca", "atendido": True,
        "desc": "Baja emitidos y recibidos del período. Alimenta el módulo "
                "Facturas.",
        "args": ["--alias"],
    },
    "arca_vencimientos": {
        "archivo": "vencimientos_arca.py", "propio": False,
        "titulo": "ARCA — vencimientos",
        "rubro": "impuestos", "fuente": None, "atendido": False,
        "desc": "La agenda pública por terminación de CUIT. Alimenta el módulo "
                "Impuestos.",
        "args": [],
    },
    "dgr_ctes_deuda": {
        "archivo": "dgr_deuda.py", "propio": False,
        "titulo": "DGR Corrientes — estado de cuenta",
        "rubro": "impuestos", "fuente": "DGR-Ctes", "atendido": False,
        "desc": "Deuda, intimaciones y próximos vencimientos de IIBB.",
        "args": [],
    },
    "galicia": {
        "archivo": "galicia_todo.py", "propio": False,
        "titulo": "Galicia — movimientos, cheques y tasas",
        "rubro": "bancos", "fuente": "banco", "atendido": True,
        "desc": "Todo en un login. Alimenta el módulo Bancos.",
        "args": [],
    },
    "bancorrientes": {
        "archivo": "bancorrientes_job.py", "propio": False,
        "titulo": "Banco de Corrientes — movimientos y echeqs",
        "rubro": "bancos", "fuente": "banco", "atendido": True,
        "desc": "Movimientos, tarjeta y echeqs.",
        "args": [],
    },
    "formosa_banco": {
        "archivo": "formosa_resumenes.py", "propio": False,
        "titulo": "Banco de Formosa — resúmenes",
        "rubro": "bancos", "fuente": "banco", "atendido": True,
        "desc": "Resúmenes mensuales y movimientos en XLS.",
        "args": [],
    },
}

RUBROS = {
    "impuestos": "Impuestos — ARCA y las provincias",
    "bancos": "Bancos — extractos y cheques",
}


def ruta_de(job):
    return (AQUI if job["propio"] else TOOLS_SIBRA) / job["archivo"]


def catalogo():
    """El catálogo con el estado real de cada job: si el archivo existe, qué
    clientes lo usan y cuáles ya tienen credencial."""
    cs = clientes.todos()
    out = []
    for clave, j in JOBS.items():
        ruta = ruta_de(j)
        usan = clientes.de_fuente(j["fuente"]) if j["fuente"] else cs
        out.append({
            "clave": clave, **{k: v for k, v in j.items() if k != "archivo"},
            "archivo": j["archivo"],
            "ruta": str(ruta),
            "existe": ruta.exists(),
            "origen": "estudio" if j["propio"] else "heredado de SIBRA",
            "clientes": [{
                "alias": c["alias"],
                "credencial": bool(credenciales.obtener(j["fuente"], c["alias"])) if j["fuente"] else None,
            } for c in usan],
        })
    return out


def correr(clave, args=None):
    """Lanza un job y devuelve (codigo, salida). Los atendidos abren su ventana
    y esperan a la persona: por eso no hay timeout corto."""
    j = JOBS.get(clave)
    if not j:
        return 2, f"No existe el job '{clave}'"
    ruta = ruta_de(j)
    if not ruta.exists():
        return 2, f"No encuentro el archivo:\n  {ruta}"
    cmd = [sys.executable, str(ruta)] + list(args or [])
    try:
        p = subprocess.run(cmd, cwd=str(ruta.parent), capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=3600)
        return p.returncode, (p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")
    except subprocess.TimeoutExpired:
        return 2, "El job tardó más de una hora y se cortó."
    except Exception as e:
        return 2, f"No se pudo lanzar: {e}"


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Suite de jobs del estudio")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--credenciales", action="store_true")
    ap.add_argument("--correr", help="clave del job")
    ap.add_argument("resto", nargs="*")
    a = ap.parse_args()

    if a.json:
        print(json.dumps(catalogo(), indent=1, ensure_ascii=False))
        return 0
    if a.correr:
        cod, salida = correr(a.correr, a.resto)
        print(salida)
        return cod
    if a.credenciales:
        return credenciales.main()

    cat = catalogo()
    print(f"\n  Clientes del estudio: {len(clientes.todos())}")
    print(f"  Jobs heredados desde: {TOOLS_SIBRA}\n")
    for rubro, titulo in RUBROS.items():
        print(f"{'─' * 70}\n  {titulo.upper()}\n")
        for j in [x for x in cat if x["rubro"] == rubro]:
            marca = " " if j["existe"] else "✗"
            atn = " · atendido" if j["atendido"] else ""
            print(f"   {marca} {j['clave']:<20} {j['titulo']}{atn}")
            print(f"     {'':<20} {j['desc'][:66]}")
            faltan = [c["alias"] for c in j["clientes"] if c["credencial"] is False]
            if faltan:
                print(f"     {'':<20} sin credencial: {', '.join(faltan)}")
            if not j["existe"]:
                print(f"     {'':<20} ⚠ no está en {j['ruta']}")
        print("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
