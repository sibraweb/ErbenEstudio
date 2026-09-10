# -*- coding: utf-8 -*-
"""¿Esta instalación está al día? — el permiso firmado que trae el agente.

El sistema corre en la máquina del estudio, con sus datos y sus claves. No hay
un servidor nuestro en el medio que pueda "apagarlo", así que lo único que se
puede hacer es que el programa **se niegue a trabajar** cuando el permiso venció.

Cómo funciona
-------------
El permiso es un JSON firmado con nuestra clave privada (Ed25519). La pública
va adentro del código; la privada no sale nunca de `C:\\SIBRA\\estudio`. El
agente lo lee de disco, verifica la firma y mira la fecha.

    al_dia   → todo normal
    gracia   → venció hace poco: trabaja igual y avisa, con cuenta regresiva
    vencida  → SIN AUTOMATISMOS
    sin_licencia / adulterada → SIN AUTOMATISMOS

⚠ EL PERMISO TIENE FECHA, NO ES UNA PREGUNTA EN VIVO. Si cada arranque tuviera
que consultarnos, un corte de internet un 20 a las 11 de la noche dejaría al
estudio sin poder presentar — y esa llamada la atendemos nosotros. Con el
permiso guardado aguanta el corte y se apaga solo cuando pasa la gracia.

⚠ Y frenar el sistema NO es apagarlo (Juan, 10/09): *«que se le frene es solo
que no le funcionen los parsers y las conciliaciones automáticas — si sigue
cargando cosas a mano lo puede seguir usando»*. Se corta **lo que se paga**,
que es la automatización: los jobs no entran a ARCA ni a rentas, y la
conciliación no aparea sola. Cargar, editar, conciliar a mano, imprimir y
exportar siguen andando.

Es mejor negocio y es más limpio: el que dejó de pagar no pierde el acceso a
sus propios libros —que además está obligado a conservar— pero vuelve a hacer
a mano lo que la máquina le hacía. Esa es la factura que se siente.

El candado es de vidrio y conviene saberlo
------------------------------------------
Esto es Python en la máquina del otro: quien sepa, borra el chequeo. Lo que
retiene de verdad son los **parsers** (`paquete.py`), que se bajan firmados y
solo con el permiso vigente — y los portales de ARCA y rentas cambian seguido,
así que un sistema congelado en el tiempo deja de entrar en unos meses.
"""
import base64
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import rutas

# La pública del estudio. Se reemplaza por la de verdad al publicar; mientras
# esté vacía, `exige()` es False y el sistema no le pide permiso a nadie.
CLAVE_PUBLICA = os.environ.get("ERBEN_CLAVE_PUBLICA", "")

ARCHIVO = rutas.RUNTIME / "licencia.json"      # ⚠ fuera del Drive, como la base
GRACIA_POR_DEFECTO = 7

# La frase única, para que la pantalla, la consola y el error del API digan lo
# mismo. Que enumere lo que SÍ anda no es cortesía: es la diferencia entre
# «se rompió» y «me cortaron un servicio», y de esa lectura depende que llamen
# para pagar en vez de para putear.
SIN_AUTOMATISMOS = (
    "El sistema quedó sin automatismos: los jobs no entran a ARCA ni a rentas, "
    "y la conciliación automática no aparea sola. Todo lo demás sigue andando "
    "— cargar, editar, conciliar a mano, imprimir y exportar.")


def exige():
    """¿Esta compilación pide permiso?

    Con la clave pública vacía, no. Es lo que deja correr la instalación del
    estudio que lo desarrolla sin tener que emitirse una licencia a sí mismo, y
    lo que hace que las pruebas no dependan de una clave."""
    return bool(CLAVE_PUBLICA)


def _canonico(datos):
    """El JSON exacto que se firma: sin la firma, ordenado y sin espacios.

    Tiene que dar byte por byte lo mismo del lado que firma y del que verifica,
    o toda licencia legítima daría por adulterada."""
    limpio = {k: v for k, v in datos.items() if k != "firma"}
    return json.dumps(limpio, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def firma_valida(datos, clave_publica=None):
    pub = clave_publica if clave_publica is not None else CLAVE_PUBLICA
    if not pub or not datos.get("firma"):
        return False
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        k = Ed25519PublicKey.from_public_bytes(base64.b64decode(pub))
        try:
            k.verify(base64.b64decode(datos["firma"]), _canonico(datos))
            return True
        except InvalidSignature:
            return False
    except Exception:
        # Sin la librería no se puede verificar. Se responde que NO: dar por
        # buena una firma que no se pudo mirar es peor que rechazarla.
        return False


def leer(archivo=None):
    p = Path(archivo or ARCHIVO)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def estado(archivo=None, hoy=None):
    """Cómo está esta instalación, en una sola llamada.

    Devuelve siempre la misma forma —la pantalla la muestra tal cual— con
    `automatiza` ya resuelto, para que nadie tenga que volver a razonar la
    regla en el front."""
    hoy = hoy or date.today()
    if not exige():
        return {"estado": "sin_control", "automatiza": True,
                "titulo": "Sin control de licencia",
                "detalle": "Esta compilación no pide permiso.",
                "dias": None, "vence": None, "estudio": None}

    datos = leer(archivo)
    if not datos:
        return {"estado": "sin_licencia", "automatiza": False,
                "titulo": "Sin licencia",
                "detalle": SIN_AUTOMATISMOS,
                "dias": None, "vence": None, "estudio": None}

    if not firma_valida(datos):
        return {"estado": "adulterada", "automatiza": False,
                "titulo": "La licencia no es válida",
                "detalle": "La firma no cierra: el archivo fue modificado o no "
                           "lo emitimos nosotros. " + SIN_AUTOMATISMOS,
                "dias": None, "vence": None,
                "estudio": datos.get("estudio")}

    try:
        vence = datetime.fromisoformat(str(datos["vence"])[:10]).date()
    except (KeyError, ValueError):
        return {"estado": "adulterada", "automatiza": False,
                "titulo": "La licencia no es válida",
                "detalle": "No dice hasta cuándo vale.",
                "dias": None, "vence": None, "estudio": datos.get("estudio")}

    gracia = int(datos.get("gracia_dias") or GRACIA_POR_DEFECTO)
    dias = (vence - hoy).days
    comun = {"vence": vence.isoformat(), "estudio": datos.get("estudio"),
             "email": datos.get("email"), "instalacion": datos.get("instalacion")}

    if dias >= 0:
        return {**comun, "estado": "al_dia", "automatiza": True, "dias": dias,
                "titulo": "Licencia al día",
                "detalle": f"Vence en {dias} día(s)." if dias <= 10 else ""}
    if hoy <= vence + timedelta(days=gracia):
        quedan = (vence + timedelta(days=gracia) - hoy).days
        return {**comun, "estado": "gracia", "automatiza": True, "dias": -dias,
                "titulo": f"La licencia venció hace {-dias} día(s)",
                "detalle": f"Los jobs y la conciliación automática funcionan "
                           f"{quedan} día(s) más."}
    return {**comun, "estado": "vencida", "automatiza": False, "dias": -dias,
            "titulo": f"La licencia venció hace {-dias} día(s)",
            "detalle": SIN_AUTOMATISMOS}


def automatiza(archivo=None):
    return estado(archivo)["automatiza"]


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    e = estado()
    print(f"\n  {e['titulo']}")
    if e["detalle"]:
        print(f"  {e['detalle']}")
    print(f"\n  archivo:  {ARCHIVO}   {'✓' if ARCHIVO.exists() else '✗ no está'}")
    print(f"  estudio:  {e.get('estudio') or '—'}")
    print(f"  vence:    {e.get('vence') or '—'}")
    print(f"  jobs:     {'sí' if e['automatiza'] else 'NO (sin automatismos)'}\n")
