# -*- coding: utf-8 -*-
"""El arranque de ERBEN ESTUDIO — lo que corre el ícono del escritorio.

Hace las cuatro cosas que hay que hacer para que el sistema esté andando:
  1. revisa que estén las librerías (y las instala la primera vez)
  2. si el servidor YA está prendido, no levanta otro: abre el navegador y listo
  3. arranca el servidor
  4. abre el navegador cuando el puerto empieza a contestar

Está en Python y no en el .bat a propósito: cmd se pelea con los paréntesis,
con los acentos y con los códigos de salida, y esas peleas se las lleva puestas
el usuario final. El .bat solo llama a este archivo.

    py arrancar.py            # arranca y abre el navegador
    py arrancar.py --no-abrir # arranca sin abrir el navegador
"""
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

AQUI = Path(__file__).parent
SERVER = AQUI / "servidor" / "server.py"
PUERTO = 8310
URL = f"http://localhost:{PUERTO}"
REQUISITOS = [("flask", "flask"), ("flask_cors", "flask-cors"), ("keyring", "keyring")]


def prendido(puerto=PUERTO, timeout=0.6):
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex(("127.0.0.1", puerto)) == 0


def faltantes():
    falta = []
    for modulo, paquete in REQUISITOS:
        try:
            __import__(modulo)
        except ImportError:
            falta.append(paquete)
    return falta


def instalar(paquetes):
    print(f"  Primera vez: instalando {', '.join(paquetes)}...")
    r = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                        "--disable-pip-version-check", *paquetes])
    if r.returncode != 0:
        print("\n  No se pudieron instalar las librerías.")
        print(f"  Probá a mano:  {sys.executable} -m pip install {' '.join(paquetes)}")
        return False
    return True


def abrir_cuando_conteste(espera=40):
    """El navegador se abre recién cuando el puerto contesta — si se abre antes,
    el usuario ve un error de conexión y cree que no anda."""
    for _ in range(espera * 2):
        if prendido(timeout=0.3):
            webbrowser.open(URL)
            return True
        time.sleep(0.5)
    print("  (el servidor tardó en responder — abrí " + URL + " a mano)")
    return False


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    abrir = "--no-abrir" not in sys.argv

    print("")
    print("  ================================================")
    print("    ERBEN ESTUDIO")
    print("  ================================================")
    print("")

    if not SERVER.exists():
        print(f"  No encuentro el servidor en {SERVER}")
        return 1

    if prendido():
        print("  El sistema ya estaba prendido.")
        if abrir:
            webbrowser.open(URL)
        print(f"  {URL}")
        time.sleep(1.5)
        return 0

    falta = faltantes()
    if falta and not instalar(falta):
        input("\n  ENTER para cerrar: ")
        return 1

    print("  Arrancando el servidor...")
    print("")
    print("  MIENTRAS ESTA VENTANA ESTE ABIERTA, el sistema funciona.")
    print("  Para apagarlo, cerrá esta ventana.")
    print("")
    print("  " + "-" * 48)
    if abrir:
        threading.Thread(target=abrir_cuando_conteste, daemon=True).start()

    try:
        return subprocess.call([sys.executable, str(SERVER)])
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    codigo = main()
    if codigo:
        try:
            input("\n  El servidor se detuvo. ENTER para cerrar: ")
        except (EOFError, OSError):
            pass
    sys.exit(codigo)
