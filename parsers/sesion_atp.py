# -*- coding: utf-8 -*-
"""Sesión persistente de ATP Formosa para ERBEN ESTUDIO.

Copia adaptada del job de SIBRA: sesión en ``C:\\SIBRA\\estudio`` y credenciales
bajo el namespace propio del estudio (parsers/credenciales.py).

ATP es la DGR de Formosa. Login propio por contribuyente (CUIT + Contraseña
"Clave Fiscal" de ATP, sin concepto de representación como ARCA) — cada alias
tiene su `atp_storage_<alias>.json` y su credencial en el Credential Manager
bajo la fuente "DGR-Fsa" (ver tools/credenciales.py y la convención de
"una fuente por provincia" en tools/contribuyentes.py).

⚠ El login tiene reCAPTCHA: aunque haya credencial guardada y se autocomplete,
la persona tiene que resolver el captcha y apretar Ingresar. Por eso el login
es SIEMPRE con ventana visible y espera al selector de logueado (hasta 10
minutos), nunca headless.

Señal de sesión viva: el link a logout.php del header logueado. La pantalla
de login no lo tiene (verificado en el relevamiento 2026-08-17, ver
H:\\My Drive\\web_sibra\\tesoreria\\atp_formosa\\2026-08-17\\).

Uso:
    py sesion_atp.py --alias RODRIGUEZ            # verifica; si vencida, abre login manual
    py sesion_atp.py --alias RODRIGUEZ --login    # fuerza login manual
    py sesion_atp.py --alias RODRIGUEZ --check    # solo verifica (headless, scheduler)
    py sesion_atp.py --alias RODRIGUEZ --abrir    # reabre la sesión, ventana visible, para trabajar a mano
"""
import argparse
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rutas  # noqa: E402
from credenciales import obtener as obtener_credencial, pedir_y_guardar

RUNTIME_DIR = rutas.RUNTIME
URL = "https://atpformosa.gob.ar/"
TIMEOUT_LOGIN_MANUAL_MS = 10 * 60 * 1000
# señal de sesión viva: el header logueado tiene el link de salir;
# la pantalla de "Autorización" (login) no.
SELECTOR_LOGUEADO = 'a[href*="logout.php"]'
FUENTE = "DGR-Fsa"


def _autocompletar(page, usuario, clave):
    """Misma heurística genérica que sesion_dgr.py: primer input visible que
    no sea password = CUIT. Si el form cambió y no matchea, no rompe nada."""
    try:
        pw = page.locator('input[type="password"]').first
        if pw.count() == 0:
            return False
        campos = page.locator(
            'input:not([type="password"]):not([type="hidden"]):not([type="checkbox"]):not([type="submit"])'
        )
        if campos.count() == 0:
            return False
        campos.first.fill(usuario)
        pw.fill(clave)
        return True
    except Exception:
        return False


def storage_de(alias):
    return RUNTIME_DIR / f"atp_storage_{alias}.json"


def check(alias, headless=True):
    storage = storage_de(alias)
    if not storage.exists():
        print(f"No hay sesión guardada para {alias}.")
        return False
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(storage_state=str(storage))
        page = ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded")
        try:
            page.wait_for_selector(SELECTOR_LOGUEADO, timeout=15000)
            viva = True
        except Exception:
            viva = False
        print(f"Sesión ATP [{alias}] {'VIVA' if viva else 'VENCIDA'} — url: {page.url}")
        browser.close()
        return viva


def login_manual(alias):
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    storage = storage_de(alias)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded")
        cred = obtener_credencial(FUENTE, alias)
        if cred and _autocompletar(page, cred["usuario"], cred["clave"]):
            print(f"\n>>> CUIT y clave de {alias} autocompletados desde el Credential Manager.")
            print(">>> Resolvé el reCAPTCHA si aparece y apretá Ingresar.\n")
        else:
            print(f"\n>>> Logueate como {alias} (CUIT + Contraseña de ATP de ESE contribuyente).")
        print(">>> Cuando entres al portal, la sesión se guarda sola.\n")
        try:
            page.wait_for_selector(SELECTOR_LOGUEADO, timeout=TIMEOUT_LOGIN_MANUAL_MS)
        except Exception:
            print("Timeout: no se completó el login en 10 minutos. No se guardó nada.")
            browser.close()
            return False
        page.wait_for_timeout(3000)
        ctx.storage_state(path=str(storage))
        print(f"Sesión guardada en {storage}")
        browser.close()
        if not cred:
            try:
                if input(f"\n¿Guardar CUIT+clave de {alias} en el Credential Manager? (s/N): ").strip().lower() == "s":
                    pedir_y_guardar(FUENTE, alias)
            except (EOFError, KeyboardInterrupt):
                pass
        return True


def abrir(alias, minutos=20):
    """Reabre la sesión guardada en ventana visible y la deja quieta para
    trabajar a mano (ej. presentar una DJ). Se cierra al cerrar la ventana o
    al timeout."""
    storage = storage_de(alias)
    if not storage.exists():
        print(f"No hay sesión guardada para {alias} — usá --login")
        return False
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(storage_state=str(storage), viewport={"width": 1500, "height": 1000})
        page = ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded")
        print(f"Ventana de {alias} abierta en ATP. Cerrala cuando termines (o esperá {minutos} min).")
        restante = minutos * 60
        while restante > 0:
            time.sleep(4)
            restante -= 4
            if not [pg for pg in ctx.pages if not pg.is_closed()]:
                break
        try:
            browser.close()
        except Exception:
            pass
    return True


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Sesión persistente ATP Formosa (DGR-Fsa)")
    ap.add_argument("--alias", default="RODRIGUEZ", help="cliente del estudio (ver parsers/clientes.py)")
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--abrir", action="store_true", help="reabrir la sesión ya guardada, ventana visible")
    args = ap.parse_args()
    if args.abrir:
        return 0 if abrir(args.alias) else 1
    if args.check:
        return 0 if check(args.alias) else 1
    if args.login:
        return 0 if login_manual(args.alias) else 1
    if check(args.alias):
        return 0
    print("Abriendo login manual…")
    return 0 if login_manual(args.alias) else 1


if __name__ == "__main__":
    sys.exit(main())
