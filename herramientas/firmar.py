# -*- coding: utf-8 -*-
"""Lo que firma: licencias y paquetes de parsers. **Se corre acá, nunca allá.**

Es la única pieza que necesita la clave privada, y la clave privada no entra al
repo ni al Drive: vive en `C:\\SIBRA\\estudio\\firma.key`, en el disco del que
emite. Si esa clave se filtra, cualquiera puede emitirse licencias y —peor—
servirle a un estudio un paquete de parsers propio, que son los que se loguean
en ARCA con las claves del contribuyente.

    py herramientas/firmar.py --generar-clave

    py herramientas/firmar.py --licencia --estudio "Estudio Pérez" \\
        --email perez@gmail.com --hasta 2026-10-10

    py herramientas/firmar.py --paquete --version 2026.09.10

⚠ El repo es PÚBLICO y los parsers ya están adentro: sacarlos ahora no los
despublica, la historia de git queda. Esto sirve de acá en adelante — las
versiones nuevas se entregan firmadas y las viejas envejecen solas cuando los
portales cambian, que es lo que pasa siempre.
"""
import argparse
import base64
import hashlib
import json
import secrets
import sys
import zipfile
from datetime import date, datetime
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent))

import licencia  # noqa: E402
import rutas  # noqa: E402

CLAVE = rutas.RUNTIME / "firma.key"
SALIDA = rutas.RUNTIME / "entrega"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _privada():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    if not CLAVE.exists():
        raise SystemExit(f"No hay clave privada en {CLAVE}.\n"
                         "  Generala con:  py herramientas/firmar.py --generar-clave")
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(CLAVE.read_text().strip()))


def firmar(datos):
    """Le pone `firma` al dict, sobre el mismo canónico que verifica el agente."""
    datos = {k: v for k, v in datos.items() if k != "firma"}
    datos["firma"] = base64.b64encode(
        _privada().sign(licencia._canonico(datos))).decode()
    return datos


def generar_clave():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    if CLAVE.exists():
        raise SystemExit(f"Ya hay una clave en {CLAVE}.\n"
                         "  ⚠ Si la reemplazás, TODAS las licencias emitidas dejan de valer.\n"
                         "  Borrala a mano si estás seguro.")
    k = Ed25519PrivateKey.generate()
    CLAVE.parent.mkdir(parents=True, exist_ok=True)
    CLAVE.write_text(base64.b64encode(k.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption())).decode())
    pub = base64.b64encode(k.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw)).decode()
    print(f"\n  Privada guardada en {CLAVE}")
    print("  ⚠ NO la copies al Drive ni al repo. Si se pierde, no se pueden emitir")
    print("     más licencias; si se filtra, cualquiera puede emitirlas.\n")
    print("  Pegá esta pública en licencia.py (CLAVE_PUBLICA):\n")
    print(f'    CLAVE_PUBLICA = os.environ.get("ERBEN_CLAVE_PUBLICA", "{pub}")\n')


def emitir_licencia(a):
    try:
        datetime.fromisoformat(a.hasta)
    except ValueError:
        raise SystemExit("--hasta va en formato AAAA-MM-DD")
    datos = firmar({
        "instalacion": a.instalacion or ("ERB-" + secrets.token_hex(4).upper()),
        "estudio": a.estudio, "email": a.email,
        "emitida": date.today().isoformat(), "vence": a.hasta,
        "gracia_dias": a.gracia,
    })
    SALIDA.mkdir(parents=True, exist_ok=True)
    destino = SALIDA / f"licencia-{datos['instalacion']}.json"
    destino.write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  Licencia emitida: {destino}")
    print(f"  {datos['estudio']} · {datos['email']}")
    print(f"  vence {datos['vence']} + {datos['gracia_dias']} días de gracia")
    print(f"\n  Se instala copiándola a  {licencia.ARCHIVO}  en la máquina del estudio.\n")


def armar_paquete(a):
    origen = Path(a.desde or (AQUI.parent / "parsers"))
    if not origen.is_dir():
        raise SystemExit(f"No encuentro la carpeta {origen}")
    archivos = sorted(p for p in origen.rglob("*.py")
                      if "__pycache__" not in p.parts)
    if not archivos:
        raise SystemExit(f"No hay .py en {origen}")

    SALIDA.mkdir(parents=True, exist_ok=True)
    nombre_zip = f"parsers-{a.version}.zip"
    ruta_zip = SALIDA / nombre_zip
    hashes = {}
    with zipfile.ZipFile(ruta_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for p in archivos:
            rel = p.relative_to(origen).as_posix()
            crudo = p.read_bytes()
            hashes[rel] = hashlib.sha256(crudo).hexdigest()
            z.writestr(rel, crudo)

    manifiesto = firmar({
        "version": a.version, "fecha": date.today().isoformat(),
        "zip": nombre_zip,
        "sha256": hashlib.sha256(ruta_zip.read_bytes()).hexdigest(),
        "archivos": hashes,
    })
    (SALIDA / "manifiesto.json").write_text(
        json.dumps(manifiesto, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  Paquete {a.version}: {len(archivos)} archivo(s)")
    print(f"  {ruta_zip}")
    print(f"  {SALIDA / 'manifiesto.json'}")
    print("\n  Subí los dos a la misma carpeta de la web y apuntá ahí")
    print("  la variable ERBEN_URL_PAQUETE del agente.\n")


def main():
    ap = argparse.ArgumentParser(description="Firma licencias y paquetes de parsers")
    ap.add_argument("--generar-clave", action="store_true")
    ap.add_argument("--licencia", action="store_true")
    ap.add_argument("--paquete", action="store_true")
    ap.add_argument("--estudio")
    ap.add_argument("--email")
    ap.add_argument("--hasta", help="AAAA-MM-DD")
    ap.add_argument("--gracia", type=int, default=licencia.GRACIA_POR_DEFECTO)
    ap.add_argument("--instalacion", help="para renovar una que ya existe")
    ap.add_argument("--version", help="del paquete, ej 2026.09.10")
    ap.add_argument("--desde", help="carpeta de parsers (default: parsers/)")
    a = ap.parse_args()

    if a.generar_clave:
        return generar_clave()
    if a.licencia:
        if not (a.estudio and a.hasta):
            raise SystemExit("Para emitir hace falta --estudio y --hasta")
        return emitir_licencia(a)
    if a.paquete:
        if not a.version:
            raise SystemExit("Para armar el paquete hace falta --version")
        return armar_paquete(a)
    ap.print_help()


if __name__ == "__main__":
    main()
