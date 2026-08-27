# -*- coding: utf-8 -*-
"""ERBEN ESTUDIO — API server (Flask).

Servidor APARTE del ERP (ARQUITECTURA.md): la base es del estudio, no se toca
la nuestra. SQLite local — la base NO vive en el repo
(C:\\SIBRA\\estudio\\estudio.sqlite3, misma convención que siempre: código en
Proyectos CODE, datos y secretos en C:\\SIBRA).

LA regla de este server (ARQUITECTURA.md §1 — aislamiento duro):
    todo endpoint operativo vive bajo /api/c/... y EXIGE ?cliente=<alias>.
    El filtro por cliente_id se aplica ACÁ, en cada query — nunca se confía en
    que el front mande lo correcto. No existe ningún endpoint que junte filas
    de dos clientes.

Módulos (estructura dictada por Juan, 2026-08-17):
    facturas · impuestos (vencimientos ARCA + liquidación de IVA) · DJ de IIBB
    por provincia · bancos · cheques · pagos · conciliación automática.

Correr:  py servidor/server.py     (localhost:8310)
"""
import json
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

RAIZ = Path(__file__).parent.parent
SISTEMA = RAIZ / "sistema"
sys.path.insert(0, str(RAIZ))
import respaldo as _respaldo  # noqa: E402
import rutas  # noqa: E402

DB_PATH = rutas.DB_PATH
ESQUEMA = Path(__file__).parent / "esquema.sql"
# Lo que dejan los jobs de la suite (atp_iibb.py). El server lo LEE, no lo
# escribe: los jobs son los dueños de ese archivo.
ATP_ESTADO = rutas.ESTADO / "atp_estado.json"

# Ventana de fechas para la conciliación automática. Un cheque se debita cerca
# de su fecha de pago pero nunca clavado, y una transferencia puede impactar al
# día siguiente. Más ancho que esto empieza a matchear cosas distintas.
# El ciclo de vida de una obligación impositiva. El orden importa: una
# obligación AVANZA y nunca vuelve atrás (ver api_vencimientos_alta).
AVANCE_OBLIGACION = {"a_vencer": 0, "vencida_sin_presentar": 1,
                     "dj_a_pagar": 2, "pagado": 3}

DIAS_CHEQUE = 7
DIAS_FACTURA = 10

app = Flask(__name__)
CORS(app)


@app.after_request
def _permitir_red_privada(resp):
    """Deja que la pantalla publicada en la nube le hable a ESTE equipo.

    Chrome trata una página https que llama a `http://localhost` como acceso a
    red privada (PNA): manda un preflight con
    `Access-Control-Request-Private-Network` y solo sigue si el servidor local
    contesta que sí. Sin esta cabecera, la app publicada muestra un error de
    red que no explica nada.

    Lo que NO cambia: los datos del cliente nunca viajan a internet. El
    navegador baja la pantalla de la nube y los datos los pide acá, en la
    máquina del estudio.
    """
    if request.headers.get("Access-Control-Request-Private-Network"):
        resp.headers["Access-Control-Allow-Private-Network"] = "true"
    return resp


# ══ base ════════════════════════════════════════════════════════════════════
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def _hoy():
    return date.today().isoformat()


def _dias(a, b):
    """Distancia en días entre dos fechas ISO (o None si alguna no parsea)."""
    try:
        return abs((datetime.fromisoformat(a[:10]) - datetime.fromisoformat(b[:10])).days)
    except Exception:
        return None


def _rango_periodo(periodo):
    """'MM/YYYY' → (desde, hasta) ISO. None si el formato no es ese."""
    m = re.fullmatch(r"(\d{2})/(\d{4})", (periodo or "").strip())
    if not m:
        return None
    mm, aaaa = int(m.group(1)), int(m.group(2))
    desde = date(aaaa, mm, 1)
    hasta = date(aaaa + (mm == 12), (mm % 12) + 1, 1) - timedelta(days=1)
    return desde.isoformat(), hasta.isoformat()


def _n(x, defecto=0.0):
    try:
        return round(float(x), 2)
    except (TypeError, ValueError):
        return defecto


def filas(cur):
    return [dict(f) for f in cur.fetchall()]


def _huella_movimiento(fecha, importe, descripcion, referencia):
    """La huella de un movimiento del extracto.

    ⚠ NO entra el SALDO, y es a propósito. El ERP lo incluía y eso ataba la
    fila a su POSICIÓN en la cadena: bastaba un asiento retroactivo del banco
    para que todos los saldos siguientes cambiaran y, al reimportar, entrara
    duplicada toda la cola del mes. La huella describe el movimiento, no dónde
    quedó parado."""
    import hashlib
    crudo = "|".join([
        (fecha or "")[:10],
        f"{importe:.2f}",
        re.sub(r"\s+", " ", (descripcion or "")).strip().upper(),
        (referencia or "").strip().upper(),
    ])
    return hashlib.sha1(crudo.encode("utf-8")).hexdigest()[:16]


def _migrar(con):
    """Cambios de esquema sobre una base que ya existe. Se corre en cada
    arranque: barato y evita que una instalación vieja quede rota en silencio."""
    # ── el registro de corridas del panel ──
    if not con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                       "AND name='jobs_corridas'").fetchone():
        print("  migrando: registro de corridas del panel…")
        con.executescript("""
            CREATE TABLE jobs_corridas (
                id INTEGER PRIMARY KEY, job TEXT NOT NULL, args TEXT, alias TEXT,
                usuario TEXT, maquina TEXT, inicio TEXT NOT NULL, fin TEXT,
                segundos REAL, estado TEXT NOT NULL, exit_code INTEGER, salida TEXT);
            CREATE INDEX ix_corridas_job ON jobs_corridas(job, inicio DESC);""")
        con.commit()

    # ── centros de costo (arquitectura lista, sin módulo todavía) ──
    if not con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                       "AND name='centros_costo'").fetchone():
        print("  migrando: centros de costo…")
        con.executescript("""
            CREATE TABLE centros_costo (
                id INTEGER PRIMARY KEY,
                cliente_id INTEGER NOT NULL REFERENCES clientes(id),
                codigo TEXT NOT NULL, nombre TEXT NOT NULL,
                activo INTEGER NOT NULL DEFAULT 1, nota TEXT,
                UNIQUE (cliente_id, codigo));
            CREATE INDEX ix_centros_cliente ON centros_costo(cliente_id);
            CREATE TABLE factura_centros (
                id INTEGER PRIMARY KEY,
                factura_id INTEGER NOT NULL REFERENCES facturas(id) ON DELETE CASCADE,
                centro_id INTEGER NOT NULL REFERENCES centros_costo(id),
                porcentaje REAL NOT NULL,
                UNIQUE (factura_id, centro_id));
            CREATE INDEX ix_factcentro ON factura_centros(centro_id);""")
        con.commit()

    # ── ciclo de vida de las obligaciones ──
    cv = {f[1] for f in con.execute("PRAGMA table_info(vencimientos)")}
    if "codigo" not in cv:
        print("  migrando: ciclo de vida de las obligaciones impositivas…")
        con.execute("ALTER TABLE vencimientos ADD COLUMN codigo TEXT")
        con.execute("ALTER TABLE vencimientos ADD COLUMN actualizado TEXT")
        # los estados viejos al ciclo nuevo
        con.execute("UPDATE vencimientos SET estado='a_vencer' WHERE estado='pendiente'")
        con.execute("UPDATE vencimientos SET estado='dj_a_pagar' WHERE estado='presentado'")
        con.commit()

    cols = {f[1] for f in con.execute("PRAGMA table_info(movimientos_banco)")}
    if "huella" in cols:
        return
    print("  migrando: doble llave (huella + ordinal) en movimientos_banco…")
    con.execute("ALTER TABLE movimientos_banco ADD COLUMN huella TEXT NOT NULL DEFAULT ''")
    con.execute("ALTER TABLE movimientos_banco ADD COLUMN ordinal INTEGER NOT NULL DEFAULT 0")
    vistos = {}
    for m in con.execute(
            "SELECT id, cliente_id, cuenta_id, fecha, importe, descripcion, referencia "
            "FROM movimientos_banco ORDER BY id").fetchall():
        h = _huella_movimiento(m["fecha"], m["importe"], m["descripcion"], m["referencia"])
        clave = (m["cliente_id"], m["cuenta_id"], h)
        con.execute("UPDATE movimientos_banco SET huella=?, ordinal=? WHERE id=?",
                    (h, vistos.get(clave, 0), m["id"]))
        vistos[clave] = vistos.get(clave, 0) + 1
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_movb_llave "
                "ON movimientos_banco(cliente_id, cuenta_id, huella, ordinal)")
    con.commit()


def crear_y_sembrar():
    """Crea la base si no existe y, si hay semilla, la carga.

    ⚠ El código sabe CÓMO sembrar, no A QUIÉN. Los datos del alta inicial
    —CUIT, actividades, agentes— viven en `semilla.json` en el Drive del
    estudio, NO en el repo (Juan, 2026-08-27: *"esos datos van en la base de
    datos, el repo solo es para ver las cosas y operar"*).

    Antes esto estaba hardcodeado acá: el perfil fiscal de un contribuyente
    real adentro del código. En un repo público eso se publica.

    Sin semilla la base arranca vacía y los clientes se dan de alta por
    pantalla, que es el camino normal."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = db()
    if con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='clientes'").fetchone():
        _migrar(con)
        con.close()
        return
    con.executescript(ESQUEMA.read_text(encoding="utf-8"))
    con.commit()

    if not rutas.SEMILLA.exists():
        con.close()
        print(f"  base nueva y vacía — no hay semilla en {rutas.SEMILLA}")
        return
    try:
        s = json.loads(rutas.SEMILLA.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        con.close()
        print(f"  ⚠ no pude leer la semilla ({e}) — la base queda vacía")
        return

    hoy = _hoy()
    for m in s.get("maestro", []):
        con.execute(
            "INSERT OR IGNORE INTO maestro_entidades (cuit, razon_social, tipo_persona, "
            " condicion_iva, provincia, origen, actualizado) VALUES (?,?,?,?,?,?,?)",
            (m["cuit"], m["razon_social"], m.get("tipo_persona"), m.get("condicion_iva"),
             m.get("provincia"), m.get("origen") or "manual", hoy))
    for a in s.get("actividades", []):
        con.execute(
            "INSERT OR IGNORE INTO maestro_actividades (cuit, jurisdiccion, codigo, nombre, "
            " alicuota, principal, exento, inicio) VALUES (?,?,?,?,?,?,?,?)",
            (a["cuit"], a["jurisdiccion"], a["codigo"], a.get("nombre"), a.get("alicuota"),
             a.get("principal", 0), a.get("exento", 0), a.get("inicio")))
    for c in s.get("clientes", []):
        con.execute("INSERT OR IGNORE INTO clientes (cuit, alias, activo, alta, nota) "
                    "VALUES (?,?,1,?,?)", (c["cuit"], c["alias"], hoy, c.get("nota")))
        fila = con.execute("SELECT id FROM clientes WHERE alias=?", (c["alias"],)).fetchone()
        if not fila:
            continue
        cid = fila["id"]
        for j in c.get("jurisdicciones", []):
            con.execute(
                "INSERT OR IGNORE INTO cliente_jurisdicciones (cliente_id, jurisdiccion, "
                " nro_inscripcion, regimen, alta) VALUES (?,?,?,?,?)",
                (cid, j["jurisdiccion"], j.get("nro_inscripcion"), j.get("regimen"), j.get("alta")))
        for e in c.get("entidades", []):
            con.execute(
                "INSERT OR IGNORE INTO entidades_cliente (cliente_id, cuit, es_proveedor, "
                " es_cliente, alta) VALUES (?,?,?,?,?)",
                (cid, e["cuit"], e.get("es_proveedor", 0), e.get("es_cliente", 0), hoy))
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM clientes").fetchone()[0]
    con.close()
    print(f"  base nueva · semilla cargada: {n} cliente(s)")


# ══ el candado ══════════════════════════════════════════════════════════════
def cliente_activo(con):
    """Resuelve ?cliente=<alias> a la fila del cliente — o corta.
    TODO endpoint /api/c/* pasa por acá: es el candado del aislamiento."""
    alias = (request.args.get("cliente") or "").strip()
    if not alias:
        return None, (jsonify({"error": "falta ?cliente= — no hay datos sin cliente activo"}), 400)
    fila = con.execute("SELECT * FROM clientes WHERE alias=? AND activo=1", (alias,)).fetchone()
    if not fila:
        return None, (jsonify({"error": f"cliente '{alias}' no existe o está inactivo"}), 404)
    return fila, None


def _de_este_cliente(con, tabla, id_, cid):
    """Una fila de otro cliente sencillamente NO EXISTE para este."""
    if id_ in (None, ""):
        return None
    return con.execute(f"SELECT * FROM {tabla} WHERE id=? AND cliente_id=?", (id_, cid)).fetchone()


# ══ front ═══════════════════════════════════════════════════════════════════
@app.get("/")
def index():
    return send_from_directory(SISTEMA, "index.html")


@app.get("/<path:archivo>")
def estaticos(archivo):
    return send_from_directory(SISTEMA, archivo)


# ══ clientes y maestro (lo único no filtrado: identidad pública) ════════════
@app.get("/api/clientes")
def api_clientes():
    con = db()
    r = filas(con.execute(
        "SELECT c.id, c.alias, c.cuit, m.razon_social, c.alta, "
        "  (SELECT GROUP_CONCAT(jurisdiccion, ', ') FROM cliente_jurisdicciones j "
        "   WHERE j.cliente_id=c.id) AS jurisdicciones "
        "FROM clientes c JOIN maestro_entidades m ON m.cuit=c.cuit "
        "WHERE c.activo=1 ORDER BY c.alias"))
    con.close()
    return jsonify(r)


@app.post("/api/clientes")
def api_clientes_alta():
    b = request.get_json(force=True)
    cuit = re.sub(r"\D", "", b.get("cuit") or "")
    alias = (b.get("alias") or "").strip().upper()
    if len(cuit) != 11 or not alias:
        return jsonify({"error": "hace falta CUIT de 11 dígitos y alias"}), 400
    con = db()
    if not con.execute("SELECT 1 FROM maestro_entidades WHERE cuit=?", (cuit,)).fetchone():
        rs = (b.get("razon_social") or "").strip()
        if not rs:
            con.close()
            return jsonify({"error": "CUIT nuevo: falta razon_social"}), 400
        con.execute(
            "INSERT INTO maestro_entidades (cuit, razon_social, tipo_persona, condicion_iva, origen, actualizado) "
            "VALUES (?,?,?,?,'manual',?)",
            (cuit, rs, b.get("tipo_persona"), b.get("condicion_iva"), _hoy()))
    try:
        con.execute("INSERT INTO clientes (cuit, alias, activo, alta, nota) VALUES (?,?,1,?,?)",
                    (cuit, alias, _hoy(), b.get("nota")))
    except sqlite3.IntegrityError:
        con.close()
        return jsonify({"error": "ese CUIT o alias ya es cliente del estudio"}), 409
    con.commit()
    con.close()
    return jsonify({"ok": True, "alias": alias})


@app.get("/api/maestro/<cuit>")
def api_maestro(cuit):
    con = db()
    ent = con.execute("SELECT * FROM maestro_entidades WHERE cuit=?", (cuit,)).fetchone()
    if not ent:
        con.close()
        return jsonify({"error": "CUIT no está en el maestro"}), 404
    acts = filas(con.execute(
        "SELECT jurisdiccion, codigo, nombre, alicuota, principal, exento, inicio "
        "FROM maestro_actividades WHERE cuit=? ORDER BY jurisdiccion, codigo, alicuota", (cuit,)))
    con.close()
    return jsonify({**dict(ent), "actividades": acts})


# ══ tablero ═════════════════════════════════════════════════════════════════
@app.get("/api/c/resumen")
def api_resumen():
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    cid = cli["id"]
    uno = lambda q, a=(): con.execute(q, a).fetchone()[0]
    r = {
        "cliente": {"alias": cli["alias"], "cuit": cli["cuit"],
                    "razon_social": con.execute(
                        "SELECT razon_social FROM maestro_entidades WHERE cuit=?",
                        (cli["cuit"],)).fetchone()["razon_social"]},
        "jurisdicciones": filas(con.execute(
            "SELECT jurisdiccion, nro_inscripcion, regimen FROM cliente_jurisdicciones "
            "WHERE cliente_id=?", (cid,))),
        "entidades": uno("SELECT COUNT(*) FROM entidades_cliente WHERE cliente_id=?", (cid,)),
        "facturas": uno("SELECT COUNT(*) FROM facturas WHERE cliente_id=?", (cid,)),
        "ventas_mes": _n(uno(
            "SELECT COALESCE(SUM(total),0) FROM facturas WHERE cliente_id=? AND mov='venta' "
            "AND substr(fecha,1,7)=?", (cid, _hoy()[:7]))),
        "compras_mes": _n(uno(
            "SELECT COALESCE(SUM(total),0) FROM facturas WHERE cliente_id=? AND mov='compra' "
            "AND substr(fecha,1,7)=?", (cid, _hoy()[:7]))),
        "cheques_cartera": uno(
            "SELECT COUNT(*) FROM cheques WHERE cliente_id=? AND estado='en_cartera'", (cid,)),
        "cheques_cartera_importe": _n(uno(
            "SELECT COALESCE(SUM(importe),0) FROM cheques WHERE cliente_id=? AND estado='en_cartera'", (cid,))),
        "cheques_a_vencer": filas(con.execute(
            "SELECT id, origen, numero, banco, fecha_pago, importe, estado FROM cheques "
            "WHERE cliente_id=? AND estado IN ('en_cartera','emitido') AND fecha_pago <= ? "
            "ORDER BY fecha_pago LIMIT 8",
            (cid, (date.today() + timedelta(days=30)).isoformat()))),
        "banco_sin_conciliar": uno(
            "SELECT COUNT(*) FROM movimientos_banco WHERE cliente_id=? AND conciliado=0", (cid,)),
        "vencimientos": filas(con.execute(
            "SELECT id, fuente, impuesto, periodo, fecha, importe, estado FROM vencimientos "
            "WHERE cliente_id=? AND estado<>'pagado' ORDER BY fecha LIMIT 8", (cid,))),
    }
    # lo que dejó el job de ATP, si corrió
    r["impuestos_portal"] = None
    try:
        estado = json.loads(ATP_ESTADO.read_text(encoding="utf-8"))
        d = estado.get("contribuyentes", {}).get(cli["alias"])
        if d:
            r["impuestos_portal"] = {
                "fecha_consulta": estado.get("fecha_consulta"),
                "situacion_fiscal": d.get("situacion_fiscal"),
                "ultima_dj": d.get("ultima_dj"),
                "proximo_periodo_sin_presentar": d.get("proximo_periodo_sin_presentar"),
                "saldo_a_favor": d.get("saldo_a_favor"),
                "notificaciones_sin_leer": d.get("notificaciones_sin_leer"),
            }
    except Exception:
        pass
    con.close()
    return jsonify(r)


# ══ entidades y actividades ═════════════════════════════════════════════════
@app.get("/api/c/actividades")
def api_actividades():
    """Los pares actividad+alícuota del cliente — el selector de las facturas
    de venta. Salen del MAESTRO por su CUIT: los carga el job, no la mano."""
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    r = filas(con.execute(
        "SELECT jurisdiccion, codigo, nombre, alicuota, principal FROM maestro_actividades "
        "WHERE cuit=? AND jurisdiccion<>'arca' ORDER BY principal DESC, codigo, alicuota",
        (cli["cuit"],)))
    con.close()
    return jsonify(r)


@app.get("/api/c/entidades")
def api_entidades():
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    r = filas(con.execute(
        "SELECT e.id, e.cuit, m.razon_social, e.es_proveedor, e.es_cliente, e.alias_interno, "
        "  e.condicion_pago, e.nota, "
        "  (SELECT COALESCE(SUM(f.total),0) FROM facturas f WHERE f.entidad_id=e.id AND f.mov='compra') AS compras, "
        "  (SELECT COALESCE(SUM(f.total),0) FROM facturas f WHERE f.entidad_id=e.id AND f.mov='venta') AS ventas "
        "FROM entidades_cliente e JOIN maestro_entidades m ON m.cuit=e.cuit "
        "WHERE e.cliente_id=? ORDER BY m.razon_social", (cli["id"],)))
    con.close()
    return jsonify(r)


@app.post("/api/c/entidades")
def api_entidades_alta():
    """Alta de relación. Si el CUIT no está en el maestro, exige razon_social y
    crea la ficha pública de paso — una sola vez para todo el estudio."""
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    b = request.get_json(force=True)
    cuit = re.sub(r"\D", "", b.get("cuit") or "")
    if len(cuit) != 11:
        con.close()
        return jsonify({"error": "CUIT inválido (11 dígitos)"}), 400
    if not con.execute("SELECT 1 FROM maestro_entidades WHERE cuit=?", (cuit,)).fetchone():
        rs = (b.get("razon_social") or "").strip()
        if not rs:
            con.close()
            return jsonify({"error": "CUIT nuevo: falta razon_social para la ficha del maestro"}), 400
        con.execute(
            "INSERT INTO maestro_entidades (cuit, razon_social, condicion_iva, origen, actualizado) "
            "VALUES (?,?,?,'manual',?)", (cuit, rs, b.get("condicion_iva"), _hoy()))
    try:
        con.execute(
            "INSERT INTO entidades_cliente (cliente_id, cuit, es_proveedor, es_cliente, alias_interno, alta) "
            "VALUES (?,?,?,?,?,?)",
            (cli["id"], cuit, 1 if b.get("es_proveedor") else 0, 1 if b.get("es_cliente") else 0,
             (b.get("alias_interno") or "").strip() or None, _hoy()))
    except sqlite3.IntegrityError:
        con.close()
        return jsonify({"error": "esa relación ya existe para este cliente"}), 409
    con.commit()
    con.close()
    return jsonify({"ok": True})


# ══ FACTURAS ════════════════════════════════════════════════════════════════
@app.get("/api/c/facturas")
def api_facturas():
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    q = ("SELECT f.*, m.razon_social AS entidad, e.cuit AS entidad_cuit, "
         "  COALESCE((SELECT SUM(a.importe) FROM pago_aplicaciones a WHERE a.factura_id=f.id),0) AS pagado "
         "FROM facturas f JOIN entidades_cliente e ON e.id=f.entidad_id "
         "JOIN maestro_entidades m ON m.cuit=e.cuit WHERE f.cliente_id=?")
    args = [cli["id"]]
    if request.args.get("mov") in ("compra", "venta"):
        q += " AND f.mov=?"
        args.append(request.args["mov"])
    if request.args.get("periodo"):
        rango = _rango_periodo(request.args["periodo"])
        if rango:
            q += " AND f.fecha BETWEEN ? AND ?"
            args += list(rango)
    if request.args.get("impagas"):
        q += (" AND COALESCE((SELECT SUM(a.importe) FROM pago_aplicaciones a "
              "WHERE a.factura_id=f.id),0) < f.total - 0.01")
    r = filas(con.execute(q + " ORDER BY f.fecha DESC, f.id DESC", args))
    for f in r:
        f["saldo"] = round(f["total"] - f["pagado"], 2)
    con.close()
    return jsonify(r)


@app.post("/api/c/facturas")
def api_facturas_alta():
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    b = request.get_json(force=True)
    if b.get("mov") not in ("compra", "venta"):
        con.close()
        return jsonify({"error": "mov debe ser compra o venta"}), 400
    if not _de_este_cliente(con, "entidades_cliente", b.get("entidad_id"), cli["id"]):
        con.close()
        return jsonify({"error": "entidad inexistente para este cliente"}), 400

    neto = _n(b.get("neto"))
    alic = b.get("alicuota_iva")
    alic = None if alic in (None, "") else _n(alic)
    # Si no vino el IVA, se calcula; si vino, manda lo que dice el papel.
    iva = _n(b.get("iva")) if b.get("iva") not in (None, "") else (
        round(neto * alic / 100, 2) if alic else 0.0)
    no_gr, exe, perc = _n(b.get("no_gravado")), _n(b.get("exento")), _n(b.get("percepciones"))
    total = _n(b.get("total")) if b.get("total") not in (None, "") else round(
        neto + iva + no_gr + exe + perc, 2)
    if total == 0:
        con.close()
        return jsonify({"error": "el total no puede ser 0"}), 400
    # La NC resta: se guarda en negativo para que todo período sea una suma.
    if (b.get("tipo") or "").upper() == "NC" and total > 0:
        neto, iva, no_gr, exe, perc, total = (-neto, -iva, -no_gr, -exe, -perc, -total)

    jur = cod = alic_iibb = None
    if b["mov"] == "venta":
        jur, cod = b.get("iibb_jurisdiccion"), b.get("iibb_codigo")
        alic_iibb = None if b.get("iibb_alicuota") in (None, "") else _n(b.get("iibb_alicuota"))
        if not cod:
            # DEFAULT: la actividad PRINCIPAL del cliente (ARQUITECTURA.md §4)
            p = con.execute(
                "SELECT jurisdiccion, codigo, alicuota FROM maestro_actividades "
                "WHERE cuit=? AND jurisdiccion<>'arca' AND principal=1 LIMIT 1",
                (cli["cuit"],)).fetchone()
            if p:
                jur, cod, alic_iibb = p["jurisdiccion"], p["codigo"], p["alicuota"]
        elif not con.execute(
                "SELECT 1 FROM maestro_actividades WHERE cuit=? AND codigo=? AND alicuota=?",
                (cli["cuit"], cod, alic_iibb)).fetchone():
            con.close()
            return jsonify({"error": "ese par actividad+alícuota no está en el padrón del cliente"}), 400

    cur = con.execute(
        "INSERT INTO facturas (cliente_id, entidad_id, mov, fecha, tipo, letra, punto_venta, numero, cae, "
        " neto, alicuota_iva, iva, no_gravado, exento, percepciones, total, "
        " iibb_jurisdiccion, iibb_codigo, iibb_alicuota, origen, nota) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cli["id"], b["entidad_id"], b["mov"], b.get("fecha") or _hoy(),
         (b.get("tipo") or "FA").upper(), b.get("letra"), b.get("punto_venta"), b.get("numero"),
         b.get("cae"), neto, alic, iva, no_gr, exe, perc, total,
         jur, cod, alic_iibb, b.get("origen") or "manual", b.get("nota")))
    con.commit()
    fid = cur.lastrowid
    con.close()
    return jsonify({"ok": True, "id": fid})


# ══ BANCOS ══════════════════════════════════════════════════════════════════
@app.get("/api/c/cuentas")
def api_cuentas():
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    r = filas(con.execute(
        "SELECT c.*, "
        "  (SELECT COALESCE(SUM(m.importe),0) FROM movimientos_banco m WHERE m.cuenta_id=c.id) AS saldo_calculado, "
        "  (SELECT COUNT(*) FROM movimientos_banco m WHERE m.cuenta_id=c.id) AS movimientos "
        "FROM cuentas_bancarias c WHERE c.cliente_id=? AND c.activa=1 ORDER BY c.banco",
        (cli["id"],)))
    con.close()
    return jsonify(r)


@app.post("/api/c/cuentas")
def api_cuentas_alta():
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    b = request.get_json(force=True)
    if not (b.get("banco") or "").strip():
        con.close()
        return jsonify({"error": "falta el banco"}), 400
    cur = con.execute(
        "INSERT INTO cuentas_bancarias (cliente_id, banco, tipo, numero, cbu, moneda, alias_banco) "
        "VALUES (?,?,?,?,?,?,?)",
        (cli["id"], b["banco"].strip(), b.get("tipo"), b.get("numero"),
         re.sub(r"\D", "", b.get("cbu") or "") or None, b.get("moneda") or "ARS", b.get("alias_banco")))
    con.commit()
    cid_cta = cur.lastrowid
    con.close()
    return jsonify({"ok": True, "id": cid_cta})


@app.get("/api/c/movimientos")
def api_movimientos():
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    q = ("SELECT m.*, c.banco, c.numero AS cuenta_numero, "
         "  co.tipo AS conciliado_con, co.motivo AS conciliado_motivo "
         "FROM movimientos_banco m JOIN cuentas_bancarias c ON c.id=m.cuenta_id "
         "LEFT JOIN conciliaciones co ON co.movimiento_id=m.id WHERE m.cliente_id=?")
    args = [cli["id"]]
    if request.args.get("cuenta_id"):
        q += " AND m.cuenta_id=?"
        args.append(request.args["cuenta_id"])
    if request.args.get("pendientes"):
        q += " AND m.conciliado=0"
    r = filas(con.execute(q + " ORDER BY m.fecha DESC, m.id DESC LIMIT 500", args))
    con.close()
    return jsonify(r)


@app.post("/api/c/movimientos")
def api_movimientos_alta():
    """Alta de movimientos: uno suelto o una tanda (`movimientos`), que es como
    los deja el cargador de extractos.

    ES IDEMPOTENTE, y esa es la parte que importa: volver a cargar el mismo
    extracto no duplica nada. La llave es doble —huella + ordinal— y la calcula
    el servidor, no el que manda los datos.
    """
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    b = request.get_json(force=True)
    lote = b.get("movimientos") or [b]
    cuenta_id = b.get("cuenta_id")
    if not _de_este_cliente(con, "cuentas_bancarias", cuenta_id, cli["id"]):
        con.close()
        return jsonify({"error": "cuenta inexistente para este cliente"}), 400

    nuevos = repetidos = 0
    vistos = {}          # huella -> cuántas veces vino ya EN ESTA TANDA
    for m in lote:
        fecha = (m.get("fecha") or _hoy())[:10]
        importe = _n(m.get("importe"))
        desc = (m.get("descripcion") or "").strip()
        ref = (m.get("referencia") or "").strip()
        huella = _huella_movimiento(fecha, importe, desc, ref)
        # El ordinal desempata los repetidos legítimos: dos débitos idénticos el
        # mismo día existen y los dos tienen que entrar.
        #
        # ⚠ Se cuenta la posición DENTRO DE ESTA TANDA, arrancando de cero — no
        # desde lo que ya hay guardado. Contando desde lo guardado, reimportar
        # el mismo extracto le daba ordinales nuevos a los gemelos (0,1 → 2,3)
        # y entraban duplicados: justo lo que la llave existe para evitar.
        # Empezando de cero, el par (huella, ordinal) ya existe y el UNIQUE lo
        # rebota, mientras que un tercer gemelo de verdad sí entra.
        ordinal = vistos.get(huella, 0)
        vistos[huella] = ordinal + 1
        try:
            con.execute(
                "INSERT INTO movimientos_banco (cliente_id, cuenta_id, fecha, descripcion, importe, saldo, "
                " referencia, origen, cuit_contraparte, huella, ordinal) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (cli["id"], cuenta_id, fecha, desc or None, importe,
                 None if m.get("saldo") in (None, "") else _n(m.get("saldo")),
                 ref or None, m.get("origen") or "manual",
                 re.sub(r"\D", "", m.get("cuit_contraparte") or "") or None,
                 huella, ordinal))
            nuevos += 1
        except sqlite3.IntegrityError:
            repetidos += 1
    con.commit()
    con.close()
    return jsonify({"ok": True, "nuevos": nuevos, "repetidos": repetidos})


# ══ CHEQUES ═════════════════════════════════════════════════════════════════
@app.get("/api/c/cheques")
def api_cheques():
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    q = ("SELECT ch.*, m.razon_social AS librador, "
         "  (SELECT p.numero FROM pagos p WHERE p.id=ch.pago_origen_id) AS recibo_origen "
         "FROM cheques ch LEFT JOIN maestro_entidades m ON m.cuit=ch.cuit_librador "
         "WHERE ch.cliente_id=?")
    args = [cli["id"]]
    if request.args.get("origen") in ("recibido", "emitido"):
        q += " AND ch.origen=?"
        args.append(request.args["origen"])
    if request.args.get("estado"):
        q += " AND ch.estado=?"
        args.append(request.args["estado"])
    r = filas(con.execute(q + " ORDER BY ch.fecha_pago", args))
    con.close()
    return jsonify(r)


@app.post("/api/c/cheques")
def api_cheques_alta():
    """Alta directa: SOLO cheques EMITIDOS (los propios de la empresa).

    Los RECIBIDOS no se dan de alta acá a propósito — Juan: "los recibidos
    vienen de cobranzas únicamente". Nacen en POST /api/c/pagos con
    direccion=cobro y medio=cheque, con su recibo colgado. Si se pudieran
    cargar sueltos, aparecería plata en cartera sin decir de quién vino."""
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    b = request.get_json(force=True)
    if b.get("origen") == "recibido":
        con.close()
        return jsonify({"error": "un cheque recibido se carga desde una COBRANZA "
                                 "(POST /api/c/pagos con direccion=cobro y medio=cheque)"}), 400
    if not (b.get("numero") or "").strip():
        con.close()
        return jsonify({"error": "falta el número"}), 400
    if b.get("cuenta_id") and not _de_este_cliente(con, "cuentas_bancarias", b["cuenta_id"], cli["id"]):
        con.close()
        return jsonify({"error": "cuenta inexistente para este cliente"}), 400
    try:
        cur = con.execute(
            "INSERT INTO cheques (cliente_id, origen, numero, banco, cuenta_id, fecha_emision, fecha_pago, "
            " importe, estado, nota) VALUES (?,'emitido',?,?,?,?,?,?,'emitido',?)",
            (cli["id"], b["numero"].strip(), b.get("banco"), b.get("cuenta_id"),
             b.get("fecha_emision") or _hoy(), (b.get("fecha_pago") or _hoy())[:10],
             _n(b.get("importe")), b.get("nota")))
    except sqlite3.IntegrityError:
        con.close()
        return jsonify({"error": "ese cheque ya existe (mismo banco y número)"}), 409
    con.commit()
    chid = cur.lastrowid
    con.close()
    return jsonify({"ok": True, "id": chid})


@app.post("/api/c/cheques/<int:chid>/depositar")
def api_cheque_depositar(chid):
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    ch = _de_este_cliente(con, "cheques", chid, cli["id"])
    if not ch:
        con.close()
        return jsonify({"error": "cheque inexistente"}), 404
    if ch["estado"] != "en_cartera":
        con.close()
        return jsonify({"error": f"solo se deposita un cheque en cartera (está '{ch['estado']}')"}), 400
    b = request.get_json(force=True)
    if not _de_este_cliente(con, "cuentas_bancarias", b.get("cuenta_id"), cli["id"]):
        con.close()
        return jsonify({"error": "cuenta inexistente para este cliente"}), 400
    con.execute("UPDATE cheques SET estado='depositado', deposito_cuenta_id=?, deposito_fecha=? WHERE id=?",
                (b["cuenta_id"], (b.get("fecha") or _hoy())[:10], chid))
    con.commit()
    con.close()
    return jsonify({"ok": True})


@app.post("/api/c/cheques/<int:chid>/endosar")
def api_cheque_endosar(chid):
    """Endoso suelto (sin recibo). Lo normal es endosarlo dentro de una orden
    de pago — eso lo hace POST /api/c/pagos con medio=cheque y cheque_id."""
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    ch = _de_este_cliente(con, "cheques", chid, cli["id"])
    if not ch:
        con.close()
        return jsonify({"error": "cheque inexistente"}), 404
    if ch["estado"] != "en_cartera":
        con.close()
        return jsonify({"error": f"solo se endosa un cheque en cartera (está '{ch['estado']}')"}), 400
    b = request.get_json(force=True)
    if not _de_este_cliente(con, "entidades_cliente", b.get("entidad_id"), cli["id"]):
        con.close()
        return jsonify({"error": "entidad inexistente para este cliente"}), 400
    con.execute("UPDATE cheques SET estado='endosado', endoso_entidad_id=? WHERE id=?",
                (b["entidad_id"], chid))
    con.commit()
    con.close()
    return jsonify({"ok": True})


# ══ PAGOS Y COBRANZAS ═══════════════════════════════════════════════════════
@app.get("/api/c/pagos")
def api_pagos():
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    r = filas(con.execute(
        "SELECT p.*, m.razon_social AS entidad FROM pagos p "
        "JOIN entidades_cliente e ON e.id=p.entidad_id JOIN maestro_entidades m ON m.cuit=e.cuit "
        "WHERE p.cliente_id=? ORDER BY p.fecha DESC, p.id DESC", (cli["id"],)))
    for p in r:
        p["medios"] = filas(con.execute(
            "SELECT medio, importe, movimiento_id, cheque_id FROM pago_medios WHERE pago_id=?", (p["id"],)))
        p["aplicaciones"] = filas(con.execute(
            "SELECT a.factura_id, a.importe, f.tipo, f.punto_venta, f.numero "
            "FROM pago_aplicaciones a JOIN facturas f ON f.id=a.factura_id WHERE a.pago_id=?", (p["id"],)))
    con.close()
    return jsonify(r)


@app.post("/api/c/pagos")
def api_pagos_alta():
    """El corazón del sistema (Juan): generar pagos de facturas usando los
    movimientos de banco y los cheques cargados — y que el banco se vaya
    nutriendo de los comprobantes.

    Cuerpo:
      direccion: cobro | pago       entidad_id, fecha, numero
      aplicaciones: [{factura_id, importe}]
      medios: [{medio: efectivo|transferencia|cheque, importe,
                movimiento_id?,            <- transferencia: el mov del extracto
                cheque_id?,                <- cheque YA en cartera (endoso)
                cheque:{numero,banco,fecha_pago,cuit_librador?}}]  <- cheque nuevo

    Efectos:
      · cobro + cheque nuevo  → nace el cheque RECIBIDO en cartera, colgado de
                                este recibo (es su única puerta de entrada)
      · pago  + cheque_id     → ese cheque en cartera se ENDOSA a la entidad
      · pago  + cheque nuevo  → se emite un cheque propio
      · transferencia         → el movimiento queda con pago_id, el CUIT de la
                                contraparte y conciliado (el banco "se llena")
    """
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    b = request.get_json(force=True)
    cid = cli["id"]
    if b.get("direccion") not in ("cobro", "pago"):
        con.close()
        return jsonify({"error": "direccion debe ser cobro o pago"}), 400
    ent = _de_este_cliente(con, "entidades_cliente", b.get("entidad_id"), cid)
    if not ent:
        con.close()
        return jsonify({"error": "entidad inexistente para este cliente"}), 400
    medios = b.get("medios") or []
    aplicaciones = b.get("aplicaciones") or []
    if not medios:
        con.close()
        return jsonify({"error": "hace falta al menos un medio de pago"}), 400

    # Un comprobante tiene UNA dirección: cobros cancelan ventas, pagos
    # cancelan compras. Netear esconde que alguien te debe.
    esperado = "venta" if b["direccion"] == "cobro" else "compra"
    for a in aplicaciones:
        f = _de_este_cliente(con, "facturas", a.get("factura_id"), cid)
        if not f:
            con.close()
            return jsonify({"error": "factura inexistente para este cliente"}), 400
        if f["mov"] != esperado:
            con.close()
            return jsonify({"error": f"un {b['direccion']} solo cancela {esperado}s"}), 400

    total_medios = round(sum(_n(m.get("importe")) for m in medios), 2)
    total_aplic = round(sum(_n(a.get("importe")) for a in aplicaciones), 2)
    if aplicaciones and abs(total_medios - total_aplic) > 0.01:
        con.close()
        return jsonify({"error": f"los medios suman {total_medios} y las facturas {total_aplic}"}), 400

    fecha = (b.get("fecha") or _hoy())[:10]
    cur = con.execute(
        "INSERT INTO pagos (cliente_id, entidad_id, direccion, fecha, numero, total, nota) "
        "VALUES (?,?,?,?,?,?,?)",
        (cid, ent["id"], b["direccion"], fecha, b.get("numero"), total_medios, b.get("nota")))
    pago_id = cur.lastrowid

    for a in aplicaciones:
        con.execute("INSERT INTO pago_aplicaciones (pago_id, factura_id, importe) VALUES (?,?,?)",
                    (pago_id, a["factura_id"], _n(a.get("importe"))))

    for m in medios:
        medio, imp = m.get("medio"), _n(m.get("importe"))
        mov_id = cheque_id = None

        if medio == "transferencia":
            mov = _de_este_cliente(con, "movimientos_banco", m.get("movimiento_id"), cid)
            if not mov:
                con.rollback()
                con.close()
                return jsonify({"error": "movimiento de banco inexistente para este cliente"}), 400
            mov_id = mov["id"]
            # Acá el banco se nutre del comprobante: queda con el recibo, el
            # CUIT de la contraparte y conciliado.
            con.execute("UPDATE movimientos_banco SET pago_id=?, cuit_contraparte=?, conciliado=1 WHERE id=?",
                        (pago_id, ent["cuit"], mov_id))
            con.execute(
                "INSERT OR REPLACE INTO conciliaciones (cliente_id, movimiento_id, tipo, pago_id, metodo, motivo, fecha) "
                "VALUES (?,?,'pago',?,'auto',?,?)",
                (cid, mov_id, pago_id, "transferencia declarada en el comprobante", _hoy()))

        elif medio == "cheque":
            if m.get("cheque_id"):
                ch = _de_este_cliente(con, "cheques", m["cheque_id"], cid)
                if not ch:
                    con.rollback()
                    con.close()
                    return jsonify({"error": "cheque inexistente para este cliente"}), 400
                if ch["estado"] != "en_cartera":
                    con.rollback()
                    con.close()
                    return jsonify({"error": f"el cheque {ch['numero']} no está en cartera "
                                             f"(está '{ch['estado']}')"}), 400
                if b["direccion"] != "pago":
                    con.rollback()
                    con.close()
                    return jsonify({"error": "un cheque de cartera se usa para PAGAR (endoso)"}), 400
                cheque_id = ch["id"]
                con.execute("UPDATE cheques SET estado='endosado', pago_uso_id=?, endoso_entidad_id=? WHERE id=?",
                            (pago_id, ent["id"], cheque_id))
            else:
                d = m.get("cheque") or {}
                if not (d.get("numero") or "").strip():
                    con.rollback()
                    con.close()
                    return jsonify({"error": "el cheque nuevo necesita número"}), 400
                recibido = b["direccion"] == "cobro"
                try:
                    c2 = con.execute(
                        "INSERT INTO cheques (cliente_id, origen, numero, banco, cuit_librador, cuenta_id, "
                        " fecha_emision, fecha_pago, importe, estado, pago_origen_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (cid, "recibido" if recibido else "emitido", d["numero"].strip(), d.get("banco"),
                         re.sub(r"\D", "", d.get("cuit_librador") or ent["cuit"]) if recibido else None,
                         d.get("cuenta_id") if not recibido else None,
                         d.get("fecha_emision") or fecha, (d.get("fecha_pago") or fecha)[:10], imp,
                         "en_cartera" if recibido else "emitido", pago_id if recibido else None))
                except sqlite3.IntegrityError:
                    con.rollback()
                    con.close()
                    return jsonify({"error": "ese cheque ya existe (mismo banco y número)"}), 409
                cheque_id = c2.lastrowid
                if not recibido:
                    con.execute("UPDATE cheques SET pago_uso_id=? WHERE id=?", (pago_id, cheque_id))

        elif medio != "efectivo":
            con.rollback()
            con.close()
            return jsonify({"error": f"medio desconocido: {medio}"}), 400

        con.execute(
            "INSERT INTO pago_medios (pago_id, medio, importe, movimiento_id, cheque_id) VALUES (?,?,?,?,?)",
            (pago_id, medio, imp, mov_id, cheque_id))

    con.commit()
    con.close()
    return jsonify({"ok": True, "id": pago_id})


# ══ CONCILIACIÓN ════════════════════════════════════════════════════════════
def _candidatos(con, cid, mov):
    """Todo lo que podría explicar un movimiento del banco. Devuelve una lista
    de (tipo, id, motivo) — vacía si nada matchea.

    Reglas (Juan): cheques contra el movimiento, y facturas que coincidan en
    MONTO, ENTORNO DE FECHA y CUIT."""
    imp, fecha = round(mov["importe"], 2), mov["fecha"]
    out = []

    # 1) Cheque EMITIDO que se debita: movimiento negativo, mismo importe,
    #    fecha cerca de la de pago.
    if imp < 0:
        for ch in con.execute(
                "SELECT * FROM cheques WHERE cliente_id=? AND origen='emitido' AND estado='emitido' "
                "AND ABS(importe - ?) < 0.01", (cid, abs(imp))):
            d = _dias(fecha, ch["fecha_pago"])
            if d is not None and d <= DIAS_CHEQUE:
                out.append(("cheque", ch["id"],
                            f"cheque emitido {ch['numero']} por {abs(imp):.2f}, "
                            f"vence {ch['fecha_pago']} ({d} día/s del movimiento)"))

    # 2) Cheque DEPOSITADO que acredita: movimiento positivo, mismo importe,
    #    fecha cerca del depósito (o de la fecha de pago si no se anotó).
    if imp > 0:
        for ch in con.execute(
                "SELECT * FROM cheques WHERE cliente_id=? AND origen='recibido' AND estado='depositado' "
                "AND ABS(importe - ?) < 0.01", (cid, imp)):
            ref = ch["deposito_fecha"] or ch["fecha_pago"]
            d = _dias(fecha, ref)
            if d is not None and d <= DIAS_CHEQUE:
                out.append(("cheque", ch["id"],
                            f"cheque recibido {ch['numero']} depositado el {ref} ({d} día/s)"))

    # 3) Factura impaga: monto, entorno de fecha y CUIT. El signo tiene que
    #    dar: una compra sale (débito) y una venta entra (crédito).
    mov_esperado = "compra" if imp < 0 else "venta"
    for f in con.execute(
            "SELECT f.*, e.cuit, m.razon_social FROM facturas f "
            "JOIN entidades_cliente e ON e.id=f.entidad_id "
            "JOIN maestro_entidades m ON m.cuit=e.cuit "
            "WHERE f.cliente_id=? AND f.mov=? AND ABS(ABS(f.total) - ?) < 0.01 "
            "AND COALESCE((SELECT SUM(a.importe) FROM pago_aplicaciones a WHERE a.factura_id=f.id),0) "
            "    < ABS(f.total) - 0.01", (cid, mov_esperado, abs(imp))):
        d = _dias(fecha, f["fecha"])
        if d is None or d > DIAS_FACTURA:
            continue
        # El CUIT: o lo trae el movimiento, o aparece en la descripción (los
        # extractos suelen escribirlo), o está el nombre del proveedor.
        desc = (mov["descripcion"] or "").upper()
        solo_digitos = re.sub(r"\D", "", desc)
        por_cuit = (mov["cuit_contraparte"] == f["cuit"]) or (f["cuit"] in solo_digitos)
        por_nombre = f["razon_social"] and f["razon_social"].split()[0].upper() in desc
        if not (por_cuit or por_nombre):
            continue
        out.append(("factura", f["id"],
                    f"{f['mov']} {f['tipo']} {f['punto_venta'] or ''}-{f['numero'] or ''} de "
                    f"{f['razon_social']} por {abs(f['total']):.2f}, {d} día/s, "
                    f"{'CUIT' if por_cuit else 'nombre'} en el movimiento"))
    return out


def _pago_intermedio(con, cid, mov, factura_id):
    """Fabrica el RECIBO que falta entre el banco y la factura.

    Regla de la definición de Tesorería: *toda aplicación fabrica el documento
    intermedio* — movimiento → PAGO → imputación → factura. Un movimiento
    conciliado "a la factura" sin recibo deja la cuenta corriente mintiendo: el
    banco figura explicado y el proveedor sigue figurando impago. Pasó acá
    mismo la primera vez que se miró la cuenta corriente (2026-08-18).

    Devuelve el id del pago creado.
    """
    f = con.execute(
        "SELECT f.*, e.cuit, e.id AS eid FROM facturas f JOIN entidades_cliente e ON e.id=f.entidad_id "
        "WHERE f.id=?", (factura_id,)).fetchone()
    aplicado = _n(con.execute(
        "SELECT COALESCE(SUM(importe),0) FROM pago_aplicaciones WHERE factura_id=?",
        (factura_id,)).fetchone()[0])
    # lo que se aplica es lo menor entre lo que trae el banco y lo que falta
    importe = min(abs(mov["importe"]), round(abs(f["total"]) - aplicado, 2))
    direccion = "cobro" if f["mov"] == "venta" else "pago"
    cur = con.execute(
        "INSERT INTO pagos (cliente_id, entidad_id, direccion, fecha, numero, total, nota) "
        "VALUES (?,?,?,?,?,?,?)",
        (cid, f["eid"], direccion, mov["fecha"], f"AUTO-{mov['id']}", importe,
         "recibo generado por la conciliación automática"))
    pago_id = cur.lastrowid
    con.execute(
        "INSERT INTO pago_medios (pago_id, medio, importe, movimiento_id) VALUES (?,'transferencia',?,?)",
        (pago_id, importe, mov["id"]))
    con.execute("INSERT INTO pago_aplicaciones (pago_id, factura_id, importe) VALUES (?,?,?)",
                (pago_id, factura_id, importe))
    # y el banco se nutre: CUIT de la contraparte + el recibo reción creado
    con.execute("UPDATE movimientos_banco SET cuit_contraparte=?, pago_id=? WHERE id=?",
                (f["cuit"], pago_id, mov["id"]))
    return pago_id


@app.get("/api/c/conciliacion")
def api_conciliacion_ver():
    """Los movimientos sin conciliar con sus candidatos. `unico=True` es lo
    que el motor puede resolver solo; con 2+ candidatos decide una persona."""
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    pend = []
    for mov in con.execute(
            "SELECT m.*, c.banco FROM movimientos_banco m JOIN cuentas_bancarias c ON c.id=m.cuenta_id "
            "WHERE m.cliente_id=? AND m.conciliado=0 ORDER BY m.fecha DESC", (cli["id"],)):
        cands = _candidatos(con, cli["id"], mov)
        pend.append({**dict(mov),
                     "candidatos": [{"tipo": t, "id": i, "motivo": mo} for t, i, mo in cands],
                     "unico": len(cands) == 1})
    con.close()
    return jsonify(pend)


@app.post("/api/c/conciliacion/auto")
def api_conciliacion_auto():
    """Concilia SOLO lo que tiene un único candidato.

    La regla que evita el desastre: con dos o más candidatos NO se elige — se
    deja pendiente para que lo mire una persona. Dos facturas del mismo
    proveedor por el mismo importe en la misma semana existen, y adivinar
    cuál es rompe la cuenta corriente en silencio."""
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    cid = cli["id"]
    hechas, ambiguos, sin_match = [], 0, 0
    for mov in con.execute(
            "SELECT * FROM movimientos_banco WHERE cliente_id=? AND conciliado=0 ORDER BY fecha", (cid,)).fetchall():
        cands = _candidatos(con, cid, mov)
        if not cands:
            sin_match += 1
            continue
        if len(cands) > 1:
            ambiguos += 1
            continue
        tipo, oid, motivo = cands[0]
        con.execute(
            "INSERT OR REPLACE INTO conciliaciones (cliente_id, movimiento_id, tipo, cheque_id, factura_id, "
            " metodo, motivo, fecha) VALUES (?,?,?,?,?,'auto',?,?)",
            (cid, mov["id"], tipo, oid if tipo == "cheque" else None,
             oid if tipo == "factura" else None, motivo, _hoy()))
        con.execute("UPDATE movimientos_banco SET conciliado=1 WHERE id=?", (mov["id"],))
        if tipo == "cheque":
            ch = con.execute("SELECT * FROM cheques WHERE id=?", (oid,)).fetchone()
            # el cheque se debitó/acreditó de verdad: ya no está en el aire
            con.execute("UPDATE cheques SET estado=? WHERE id=?",
                        ("debitado" if ch["origen"] == "emitido" else "cobrado", oid))
            # y el banco se nutre: al depósito le queda el CUIT del librador
            if ch["cuit_librador"]:
                con.execute("UPDATE movimientos_banco SET cuit_contraparte=? WHERE id=?",
                            (ch["cuit_librador"], mov["id"]))
        else:
            pago_id = _pago_intermedio(con, cid, mov, oid)
            con.execute("UPDATE conciliaciones SET pago_id=? WHERE movimiento_id=?", (pago_id, mov["id"]))
        hechas.append({"movimiento_id": mov["id"], "tipo": tipo, "id": oid, "motivo": motivo})
    con.commit()
    con.close()
    return jsonify({"conciliados": len(hechas), "ambiguos": ambiguos,
                    "sin_candidato": sin_match, "detalle": hechas})


@app.post("/api/c/conciliacion/manual")
def api_conciliacion_manual():
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    b = request.get_json(force=True)
    mov = _de_este_cliente(con, "movimientos_banco", b.get("movimiento_id"), cli["id"])
    if not mov:
        con.close()
        return jsonify({"error": "movimiento inexistente"}), 404
    tipo = b.get("tipo")
    if tipo not in ("cheque", "factura", "pago"):
        con.close()
        return jsonify({"error": "tipo debe ser cheque, factura o pago"}), 400
    tabla = {"cheque": "cheques", "factura": "facturas", "pago": "pagos"}[tipo]
    obj = _de_este_cliente(con, tabla, b.get("id"), cli["id"])
    if not obj:
        con.close()
        return jsonify({"error": f"{tipo} inexistente para este cliente"}), 400
    # Contra una factura pasa lo mismo que en la automática: hace falta el
    # recibo intermedio, si no la cuenta corriente sigue mostrando la deuda.
    pago_id = obj["id"] if tipo == "pago" else None
    if tipo == "factura":
        pago_id = _pago_intermedio(con, cli["id"], mov, obj["id"])
    elif tipo == "cheque":
        con.execute("UPDATE cheques SET estado=? WHERE id=?",
                    ("debitado" if obj["origen"] == "emitido" else "cobrado", obj["id"]))
        if obj["cuit_librador"]:
            con.execute("UPDATE movimientos_banco SET cuit_contraparte=? WHERE id=?",
                        (obj["cuit_librador"], mov["id"]))
    con.execute(
        "INSERT OR REPLACE INTO conciliaciones (cliente_id, movimiento_id, tipo, cheque_id, factura_id, pago_id, "
        " metodo, motivo, fecha) VALUES (?,?,?,?,?,?,'manual',?,?)",
        (cli["id"], mov["id"], tipo,
         obj["id"] if tipo == "cheque" else None,
         obj["id"] if tipo == "factura" else None,
         pago_id,
         b.get("motivo") or "conciliado a mano", _hoy()))
    con.execute("UPDATE movimientos_banco SET conciliado=1 WHERE id=?", (mov["id"],))
    con.commit()
    con.close()
    return jsonify({"ok": True, "pago_id": pago_id})


# ══ IMPUESTOS ═══════════════════════════════════════════════════════════════
@app.get("/api/c/vencimientos")
def api_vencimientos():
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    r = filas(con.execute(
        "SELECT * FROM vencimientos WHERE cliente_id=? ORDER BY fecha DESC", (cli["id"],)))
    con.close()
    return jsonify(r)


@app.post("/api/c/vencimientos")
def api_vencimientos_alta():
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    b = request.get_json(force=True)
    lote = b.get("vencimientos") or [b]
    n = 0
    for v in lote:
        if not (v.get("impuesto") and v.get("periodo")):
            continue
        estado = v.get("estado") or "a_vencer"
        if estado not in AVANCE_OBLIGACION:
            estado = "a_vencer"
        # ⚠ NUNCA retroceder el estado. Una obligación avanza:
        #   a_vencer → vencida_sin_presentar → dj_a_pagar → pagado
        # Si el portal la sigue mostrando en la pestaña vieja (se solapan), o
        # si alguien la marcó pagada a mano, una recarga NO puede volverla
        # atrás: haría reaparecer como pendiente algo que ya se pagó.
        previo = con.execute(
            "SELECT estado, importe FROM vencimientos WHERE cliente_id=? AND fuente=? "
            "AND impuesto=? AND periodo=?",
            (cli["id"], v.get("fuente") or "arca", v["impuesto"], v["periodo"])).fetchone()
        if previo and AVANCE_OBLIGACION.get(previo["estado"], 0) > AVANCE_OBLIGACION[estado]:
            estado = previo["estado"]
        fecha = (v.get("fecha") or (previo["fecha"] if previo and "fecha" in previo.keys() else None)
                 or _hoy())[:10]
        con.execute(
            "INSERT INTO vencimientos (cliente_id, fuente, impuesto, codigo, periodo, fecha, importe, "
            " estado, nota, actualizado) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (cliente_id, fuente, impuesto, periodo) DO UPDATE SET "
            " fecha=excluded.fecha, importe=COALESCE(excluded.importe, vencimientos.importe), "
            " codigo=COALESCE(excluded.codigo, vencimientos.codigo), "
            " estado=excluded.estado, actualizado=excluded.actualizado",
            (cli["id"], v.get("fuente") or "arca", v["impuesto"], v.get("codigo"),
             v["periodo"], fecha,
             None if v.get("importe") in (None, "") else _n(v["importe"]),
             estado, v.get("nota"), _hoy()))
        n += 1
    con.commit()
    con.close()
    return jsonify({"ok": True, "cargados": n})


@app.get("/api/c/iva")
def api_iva():
    """Liquidación de IVA del período: débito de ventas − crédito de compras.
    Sale de las facturas, con el detalle por alícuota para poder controlarlo
    contra el papel."""
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    rango = _rango_periodo(request.args.get("periodo"))
    if not rango:
        con.close()
        return jsonify({"error": "falta ?periodo=MM/YYYY"}), 400
    desde, hasta = rango

    def por_alicuota(mov):
        return filas(con.execute(
            "SELECT COALESCE(alicuota_iva,0) AS alicuota, ROUND(SUM(neto),2) AS neto, "
            "  ROUND(SUM(iva),2) AS iva, COUNT(*) AS comprobantes FROM facturas "
            "WHERE cliente_id=? AND mov=? AND fecha BETWEEN ? AND ? GROUP BY 1 ORDER BY 1 DESC",
            (cli["id"], mov, desde, hasta)))

    ventas, compras = por_alicuota("venta"), por_alicuota("compra")
    debito = round(sum(v["iva"] for v in ventas), 2)
    credito = round(sum(c["iva"] for c in compras), 2)
    # Las percepciones sufridas en compras también son pago a cuenta
    perc = _n(con.execute(
        "SELECT COALESCE(SUM(percepciones),0) FROM facturas WHERE cliente_id=? AND mov='compra' "
        "AND fecha BETWEEN ? AND ?", (cli["id"], desde, hasta)).fetchone()[0])
    saldo = round(debito - credito - perc, 2)
    con.close()
    return jsonify({
        "periodo": request.args["periodo"],
        "debito_fiscal": debito, "credito_fiscal": credito,
        "percepciones_sufridas": perc,
        "saldo": abs(saldo),
        "resultado": "a pagar" if saldo > 0 else ("a favor" if saldo < 0 else "en cero"),
        "ventas_por_alicuota": ventas, "compras_por_alicuota": compras,
    })


@app.get("/api/c/dj/base")
def api_dj_base():
    """La base de la DJ de IIBB por par actividad+alícuota, con EL control:
    Σ bases == Σ ventas del período (ARQUITECTURA.md §4). Si hay ventas sin
    actividad, el control falla y no se debería presentar."""
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    rango = _rango_periodo(request.args.get("periodo"))
    if not rango:
        con.close()
        return jsonify({"error": "falta ?periodo=MM/YYYY"}), 400
    desde, hasta = rango
    jur = request.args.get("jurisdiccion")

    q = ("SELECT iibb_jurisdiccion AS jurisdiccion, iibb_codigo AS codigo, iibb_alicuota AS alicuota, "
         "  ROUND(SUM(total),2) AS base, COUNT(*) AS facturas FROM facturas "
         "WHERE cliente_id=? AND mov='venta' AND fecha BETWEEN ? AND ?")
    args = [cli["id"], desde, hasta]
    if jur:
        q += " AND (iibb_jurisdiccion=? OR iibb_codigo IS NULL)"
        args.append(jur)
    todo = filas(con.execute(q + " GROUP BY 1,2,3 ORDER BY 2,3", args))

    bases, sin_act = [], []
    for b in todo:
        (bases if b["codigo"] else sin_act).append(b)
    for b in bases:
        b["nombre"] = (con.execute(
            "SELECT nombre FROM maestro_actividades WHERE cuit=? AND codigo=? AND alicuota=?",
            (cli["cuit"], b["codigo"], b["alicuota"])).fetchone() or {"nombre": None})["nombre"]
        b["impuesto"] = round(b["base"] * (b["alicuota"] or 0) / 100, 2)

    total_ventas = _n(con.execute(
        "SELECT COALESCE(SUM(total),0) FROM facturas WHERE cliente_id=? AND mov='venta' "
        "AND fecha BETWEEN ? AND ?", (cli["id"], desde, hasta)).fetchone()[0])
    suma_bases = round(sum(b["base"] for b in bases), 2)
    con.close()
    return jsonify({
        "periodo": request.args["periodo"], "jurisdiccion": jur,
        "bases": bases,
        "impuesto_determinado": round(sum(b["impuesto"] for b in bases), 2),
        "total_ventas": total_ventas, "suma_bases": suma_bases,
        "control_ok": abs(suma_bases - total_ventas) < 0.01,
        "diferencia": round(total_ventas - suma_bases, 2),
        "sin_actividad": sin_act,
    })


@app.get("/api/c/djs")
def api_djs():
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    r = filas(con.execute(
        "SELECT * FROM djs WHERE cliente_id=? ORDER BY periodo DESC, impuesto", (cli["id"],)))
    con.close()
    return jsonify(r)


@app.post("/api/c/djs")
def api_djs_guardar():
    """Guarda la FOTO de una DJ. El detalle se recalcula siempre de las
    facturas, pero lo presentado tiene que poder mostrarse tal cual aunque
    después se corrija una factura vieja."""
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    b = request.get_json(force=True)
    if b.get("impuesto") not in ("IVA", "IIBB") or not b.get("periodo"):
        con.close()
        return jsonify({"error": "hace falta impuesto (IVA|IIBB) y periodo MM/YYYY"}), 400
    con.execute(
        "INSERT INTO djs (cliente_id, impuesto, jurisdiccion, periodo, debito, credito, impuesto_det, "
        " deducciones, bonificacion, saldo_a_pagar, saldo_a_favor, estado, presentada, detalle) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT (cliente_id, impuesto, jurisdiccion, periodo) DO UPDATE SET "
        " debito=excluded.debito, credito=excluded.credito, impuesto_det=excluded.impuesto_det, "
        " deducciones=excluded.deducciones, bonificacion=excluded.bonificacion, "
        " saldo_a_pagar=excluded.saldo_a_pagar, saldo_a_favor=excluded.saldo_a_favor, "
        " estado=excluded.estado, presentada=excluded.presentada, detalle=excluded.detalle",
        (cli["id"], b["impuesto"], b.get("jurisdiccion") or "arca", b["periodo"],
         b.get("debito"), b.get("credito"), b.get("impuesto_det"), b.get("deducciones"),
         b.get("bonificacion"), b.get("saldo_a_pagar"), b.get("saldo_a_favor"),
         b.get("estado") or "borrador", b.get("presentada"),
         json.dumps(b.get("detalle"), ensure_ascii=False) if b.get("detalle") else None))
    con.commit()
    con.close()
    return jsonify({"ok": True})


# ══ TESORERÍA ══════════════════════════════════════════════════════════════
# La casa nueva de Tesorería, misma forma que la del ERP
# (SIBRA_SERVER/MDs/TESORERIA__DEFINICION.md §6.0 y §9): Cuenta Corriente ·
# Posición hoy · Vencimientos · Documentos.
#
# Diferencia estructural del estudio (Juan, 2026-08-18): allá cada vista tiene
# chips por empresa con opción ALL; acá NO existe el ALL — el cliente se elige
# arriba una sola vez y todo cuelga de él.
#
# La REGLA FUNDAMENTAL del módulo (§1) es la que manda: antes de dejar crear un
# medio de pago, la pantalla muestra lo que YA SE SABE de esa entidad — sus NC,
# sus cheques en cartera y los movimientos del banco con su CUIT sin aplicar.
# Eso es el bloque SUGERENCIAS de la cuenta corriente.

@app.get("/api/c/tesoreria/cuentacorriente")
def api_cc():
    """A quién le debo y quién me debe. Los dos mayores separados: netear
    compras contra ventas esconde que a alguien le debés."""
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    r = filas(con.execute(
        "SELECT e.id, e.cuit, m.razon_social, e.alias_interno, "
        "  COALESCE((SELECT SUM(f.total) FROM facturas f WHERE f.entidad_id=e.id AND f.mov='compra'),0) AS comprado, "
        "  COALESCE((SELECT SUM(f.total) FROM facturas f WHERE f.entidad_id=e.id AND f.mov='venta'),0) AS vendido, "
        "  COALESCE((SELECT SUM(a.importe) FROM pago_aplicaciones a JOIN facturas f ON f.id=a.factura_id "
        "            WHERE f.entidad_id=e.id AND f.mov='compra'),0) AS pagado, "
        "  COALESCE((SELECT SUM(a.importe) FROM pago_aplicaciones a JOIN facturas f ON f.id=a.factura_id "
        "            WHERE f.entidad_id=e.id AND f.mov='venta'),0) AS cobrado "
        "FROM entidades_cliente e JOIN maestro_entidades m ON m.cuit=e.cuit "
        "WHERE e.cliente_id=? ORDER BY m.razon_social", (cli["id"],)))
    for x in r:
        x["le_debo"] = round(x["comprado"] - x["pagado"], 2)
        x["me_debe"] = round(x["vendido"] - x["cobrado"], 2)
        x["estado"] = ("saldado" if abs(x["le_debo"]) < 0.01 and abs(x["me_debe"]) < 0.01
                       else "le debés" if x["le_debo"] > 0.01 and x["me_debe"] <= 0.01
                       else "te debe" if x["me_debe"] > 0.01 and x["le_debo"] <= 0.01
                       else "cruzado")
    con.close()
    return jsonify(r)


@app.get("/api/c/tesoreria/entidad/<int:eid>")
def api_cc_detalle(eid):
    """El mayor de una entidad + SUGERENCIAS: la plata real de esa entidad que
    todavía no entró al circuito (§A2 de la definición). Es la regla
    fundamental hecha pantalla — mostrar antes de dejar crear."""
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    ent = _de_este_cliente(con, "entidades_cliente", eid, cli["id"])
    if not ent:
        con.close()
        return jsonify({"error": "entidad inexistente para este cliente"}), 404

    facturas = filas(con.execute(
        "SELECT f.id, f.mov, f.fecha, f.tipo, f.letra, f.punto_venta, f.numero, f.total, "
        "  COALESCE((SELECT SUM(a.importe) FROM pago_aplicaciones a WHERE a.factura_id=f.id),0) AS pagado "
        "FROM facturas f WHERE f.entidad_id=? ORDER BY f.fecha DESC", (eid,)))
    for f in facturas:
        f["saldo"] = round(f["total"] - f["pagado"], 2)
    pagos = filas(con.execute(
        "SELECT id, direccion, fecha, numero, total FROM pagos WHERE entidad_id=? "
        "ORDER BY fecha DESC", (eid,)))

    # ── SUGERENCIAS ──
    # 1. Notas de crédito con saldo: se imputan como pago, van primero.
    ncs = [f for f in facturas if (f["tipo"] or "").upper() == "NC" and abs(f["saldo"]) > 0.01]
    # 2. Cheques en cartera librados por esta entidad.
    cheques = filas(con.execute(
        "SELECT id, numero, banco, fecha_pago, importe FROM cheques "
        "WHERE cliente_id=? AND estado='en_cartera' AND cuit_librador=?",
        (cli["id"], ent["cuit"])))
    # 3. Movimientos del banco con su CUIT que nadie aplicó todavía.
    movs = filas(con.execute(
        "SELECT m.id, m.fecha, m.descripcion, m.importe, c.banco FROM movimientos_banco m "
        "JOIN cuentas_bancarias c ON c.id=m.cuenta_id "
        "WHERE m.cliente_id=? AND m.cuit_contraparte=? AND m.pago_id IS NULL",
        (cli["id"], ent["cuit"])))
    con.close()
    return jsonify({
        "entidad": {"id": ent["id"], "cuit": ent["cuit"],
                    "es_proveedor": ent["es_proveedor"], "es_cliente": ent["es_cliente"]},
        "facturas": facturas, "pagos": pagos,
        "sugerencias": {"notas_credito": ncs, "cheques_cartera": cheques,
                        "movimientos_sin_aplicar": movs},
    })


def _lineas_que_vienen(con, cid):
    """Todo lo que vence, de todas las fuentes, en una sola lista ordenada.
    A pagar en negativo, a cobrar en positivo — así el total dice de verdad
    cómo queda la caja."""
    lineas = []
    for ch in con.execute(
            "SELECT * FROM cheques WHERE cliente_id=? AND estado IN ('en_cartera','emitido','depositado')",
            (cid,)):
        cobra = ch["origen"] == "recibido"
        lineas.append({
            "fecha": ch["fecha_pago"], "fuente": "cheque",
            "detalle": f"Cheque {ch['origen']} Nº{ch['numero']}" + (f" · {ch['banco']}" if ch["banco"] else ""),
            "importe": ch["importe"] if cobra else -ch["importe"],
            "estado": ch["estado"], "ref": {"tipo": "cheque", "id": ch["id"]}})
    for f in con.execute(
            "SELECT f.*, m.razon_social, "
            "  COALESCE((SELECT SUM(a.importe) FROM pago_aplicaciones a WHERE a.factura_id=f.id),0) AS pagado "
            "FROM facturas f JOIN entidades_cliente e ON e.id=f.entidad_id "
            "JOIN maestro_entidades m ON m.cuit=e.cuit WHERE f.cliente_id=?", (cid,)):
        saldo = round(f["total"] - f["pagado"], 2)
        if abs(saldo) < 0.01:
            continue
        cobra = f["mov"] == "venta"
        lineas.append({
            "fecha": f["fecha"], "fuente": "factura",
            "detalle": f"{f['mov'].capitalize()} {f['tipo']} {f['punto_venta'] or ''}-{f['numero'] or ''} "
                       f"· {f['razon_social']}",
            "importe": saldo if cobra else -saldo,
            "estado": "impaga", "ref": {"tipo": "factura", "id": f["id"]}})
    for v in con.execute(
            "SELECT * FROM vencimientos WHERE cliente_id=? AND estado<>'pagado'", (cid,)):
        lineas.append({
            "fecha": v["fecha"], "fuente": v["fuente"],
            "detalle": f"{v['impuesto']} {v['periodo']}",
            "importe": -(v["importe"] or 0), "estado": v["estado"],
            "liquidado": v["importe"] is not None,
            "ref": {"tipo": "vencimiento", "id": v["id"]}})
    lineas.sort(key=lambda x: x["fecha"] or "")
    return lineas


@app.get("/api/c/tesoreria/posicion")
def api_posicion():
    """Cómo está la caja HOY y qué se viene. Solo lo LIQUIDADO entra en los
    números; las DJs sin monto viven en Vencimientos (§9 de la definición)."""
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    cid = cli["id"]
    cuentas = filas(con.execute(
        "SELECT c.id, c.banco, c.tipo, c.numero, c.moneda, "
        "  COALESCE((SELECT SUM(m.importe) FROM movimientos_banco m WHERE m.cuenta_id=c.id),0) AS saldo, "
        "  (SELECT MAX(m.fecha) FROM movimientos_banco m WHERE m.cuenta_id=c.id) AS ultimo "
        "FROM cuentas_bancarias c WHERE c.cliente_id=? AND c.activa=1 ORDER BY c.banco", (cid,)))
    uno = lambda q, a: _n(con.execute(q, a).fetchone()[0])
    lineas = _lineas_que_vienen(con, cid)
    hoy = _hoy()
    kpis = {
        "bancos": round(sum(c["saldo"] for c in cuentas), 2),
        "cheques_cartera": uno("SELECT COALESCE(SUM(importe),0) FROM cheques WHERE cliente_id=? "
                               "AND estado='en_cartera'", (cid,)),
        "cheques_a_pagar": uno("SELECT COALESCE(SUM(importe),0) FROM cheques WHERE cliente_id=? "
                               "AND origen='emitido' AND estado='emitido'", (cid,)),
        "le_debo": uno("SELECT COALESCE(SUM(f.total),0) - COALESCE((SELECT SUM(a.importe) "
                       "  FROM pago_aplicaciones a JOIN facturas f2 ON f2.id=a.factura_id "
                       "  WHERE f2.cliente_id=? AND f2.mov='compra'),0) "
                       "FROM facturas f WHERE f.cliente_id=? AND f.mov='compra'", (cid, cid)),
        "me_deben": uno("SELECT COALESCE(SUM(f.total),0) - COALESCE((SELECT SUM(a.importe) "
                        "  FROM pago_aplicaciones a JOIN facturas f2 ON f2.id=a.factura_id "
                        "  WHERE f2.cliente_id=? AND f2.mov='venta'),0) "
                        "FROM facturas f WHERE f.cliente_id=? AND f.mov='venta'", (cid, cid)),
        "impuestos_liquidados": round(sum(-l["importe"] for l in lineas
                                          if l["fuente"] != "cheque" and l["fuente"] != "factura"
                                          and l.get("liquidado")), 2),
    }
    con.close()
    return jsonify({
        "cuentas": cuentas, "kpis": kpis,
        "vencido": [l for l in lineas if (l["fecha"] or "") < hoy],
        "lineas": lineas,
        "impuestos": [l for l in lineas if l["ref"]["tipo"] == "vencimiento"],
    })


@app.get("/api/c/tesoreria/vencimientos")
def api_calendario():
    """El calendario completo — cheques, facturas e impuestos, liquidados o
    no. Es la vista que la Posición deja afuera a propósito."""
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    lineas = _lineas_que_vienen(con, cli["id"])
    con.close()
    return jsonify(lineas)


@app.get("/api/c/tesoreria/documentos")
def api_documentos():
    """LA VISTA ÚNICA: todo lo que el sistema genera y toma, con su estado,
    lo aplicado, el saldo y LA CADENA debajo.

    La escalera (§A1): FACTURA se aplica con PAGO · PAGO con efectivo/banco/
    cheques · CHEQUE y BANCO con RECIBOS. Cada fila dice en qué escalón está y
    qué le falta — la medida de completitud es cuántos eslabones tiene."""
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    cid = cli["id"]
    docs = []

    for f in con.execute(
            "SELECT f.*, m.razon_social, "
            "  COALESCE((SELECT SUM(a.importe) FROM pago_aplicaciones a WHERE a.factura_id=f.id),0) AS aplicado "
            "FROM facturas f JOIN entidades_cliente e ON e.id=f.entidad_id "
            "JOIN maestro_entidades m ON m.cuit=e.cuit WHERE f.cliente_id=? ORDER BY f.fecha DESC", (cid,)):
        cadena = filas(con.execute(
            "SELECT p.id, p.numero, p.fecha, p.direccion, a.importe FROM pago_aplicaciones a "
            "JOIN pagos p ON p.id=a.pago_id WHERE a.factura_id=?", (f["id"],)))
        saldo = round(f["total"] - f["aplicado"], 2)
        docs.append({
            "clase": "factura", "id": f["id"], "fecha": f["fecha"],
            "detalle": f"{f['mov'].capitalize()} {f['tipo']} {f['punto_venta'] or ''}-{f['numero'] or ''}",
            "entidad": f["razon_social"], "total": f["total"], "aplicado": f["aplicado"],
            "saldo": saldo, "flujo": "ingreso" if f["mov"] == "venta" else "egreso",
            "estado": "saldada" if abs(saldo) < 0.01 else ("parcial" if f["aplicado"] else "sin aplicar"),
            "se_aplica_con": "PAGO",
            "cadena": [f"{c['direccion']} {c['numero'] or c['id']} · {plata_txt(c['importe'])}"
                       for c in cadena]})

    for p in con.execute(
            "SELECT p.*, m.razon_social FROM pagos p JOIN entidades_cliente e ON e.id=p.entidad_id "
            "JOIN maestro_entidades m ON m.cuit=e.cuit WHERE p.cliente_id=? ORDER BY p.fecha DESC", (cid,)):
        medios = filas(con.execute(
            "SELECT medio, importe, movimiento_id, cheque_id FROM pago_medios WHERE pago_id=?", (p["id"],)))
        aplicado = _n(con.execute(
            "SELECT COALESCE(SUM(importe),0) FROM pago_aplicaciones WHERE pago_id=?", (p["id"],)).fetchone()[0])
        docs.append({
            "clase": "pago", "id": p["id"], "fecha": p["fecha"],
            "detalle": ("Cobranza " if p["direccion"] == "cobro" else "Orden de pago ") + (p["numero"] or f"#{p['id']}"),
            "entidad": p["razon_social"], "total": p["total"], "aplicado": aplicado,
            "saldo": round(p["total"] - aplicado, 2),
            "flujo": "ingreso" if p["direccion"] == "cobro" else "egreso",
            "estado": "a cuenta" if aplicado < p["total"] - 0.01 else "imputado",
            "se_aplica_con": "efectivo / banco / cheques",
            "cadena": [f"{m['medio']} {plata_txt(m['importe'])}" for m in medios]})

    for ch in con.execute(
            "SELECT ch.*, m.razon_social AS librador FROM cheques ch "
            "LEFT JOIN maestro_entidades m ON m.cuit=ch.cuit_librador "
            "WHERE ch.cliente_id=? ORDER BY ch.fecha_pago DESC", (cid,)):
        cadena = []
        if ch["pago_origen_id"]:
            cadena.append(f"vino en la cobranza #{ch['pago_origen_id']}")
        if ch["pago_uso_id"]:
            cadena.append(f"usado en el pago #{ch['pago_uso_id']}")
        conc = con.execute("SELECT movimiento_id FROM conciliaciones WHERE cheque_id=?", (ch["id"],)).fetchone()
        if conc:
            cadena.append(f"conciliado con el movimiento #{conc['movimiento_id']}")
        docs.append({
            "clase": "cheque", "id": ch["id"], "fecha": ch["fecha_pago"],
            "detalle": f"Cheque {ch['origen']} Nº{ch['numero']}",
            "entidad": ch["librador"] or "—", "total": ch["importe"],
            "aplicado": None, "saldo": None,
            "flujo": "ingreso" if ch["origen"] == "recibido" else "egreso",
            "estado": ch["estado"], "se_aplica_con": "RECIBO", "cadena": cadena})

    for mv in con.execute(
            "SELECT m.*, c.banco, co.tipo AS conc_tipo, co.motivo FROM movimientos_banco m "
            "JOIN cuentas_bancarias c ON c.id=m.cuenta_id "
            "LEFT JOIN conciliaciones co ON co.movimiento_id=m.id "
            "WHERE m.cliente_id=? ORDER BY m.fecha DESC", (cid,)):
        cadena = []
        if mv["cuit_contraparte"]:
            cadena.append(f"CUIT {mv['cuit_contraparte']}")
        if mv["pago_id"]:
            cadena.append(f"recibo #{mv['pago_id']}")
        if mv["motivo"]:
            cadena.append(mv["motivo"])
        docs.append({
            "clase": "banco", "id": mv["id"], "fecha": mv["fecha"],
            "detalle": f"{mv['banco']} · {mv['descripcion'] or 'sin descripción'}",
            "entidad": mv["cuit_contraparte"] or "—", "total": mv["importe"],
            "aplicado": None, "saldo": None,
            "flujo": "ingreso" if mv["importe"] > 0 else "egreso",
            "estado": "conciliado" if mv["conciliado"] else "sin registrar",
            "se_aplica_con": "RECIBO", "cadena": cadena})

    docs.sort(key=lambda d: d["fecha"] or "", reverse=True)
    con.close()
    return jsonify(docs)


def plata_txt(n):
    """Importe en texto para las cadenas de la vista Documentos."""
    return "$" + f"{n:,.2f}".replace(",", "@").replace(".", ",").replace("@", ".")


# ── EL TABLERO — "la cuenta corriente potenciada" ───────────────────────────
# §B5 de la definición: los HUECOS visibles y accionables en un solo lugar.
#
# En el ERP los huecos son "docs de obra sin factura, pagos sin imputar, banco
# sin registrar". Acá NO hay documentación de obra (Juan, 2026-08-18) — un
# estudio contable no certifica obras — así que ese hueco simplemente no
# existe y el tablero queda con los tres de plata.
#
# ⚠ Las ventas sin actividad de IIBB NO son un hueco de Tesorería aunque
# rompan la DJ (Juan, 2026-08-18: «eso es en el módulo de factura»). Tesorería
# cruza plata; la clasificación fiscal del comprobante vive donde vive el
# comprobante. El aviso y el selector están en Facturas, y la DJ avisa aparte.

@app.get("/api/c/tesoreria/tablero")
def api_tablero():
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    cid = cli["id"]
    eid = request.args.get("entidad_id")
    filtro_ent = " AND e.id=?" if eid else ""
    arg_ent = [eid] if eid else []

    # 1) Banco sin registrar: movimientos que nadie explicó.
    banco = []
    for mov in con.execute(
            "SELECT m.*, c.banco FROM movimientos_banco m JOIN cuentas_bancarias c ON c.id=m.cuenta_id "
            "WHERE m.cliente_id=? AND m.conciliado=0 ORDER BY m.fecha DESC", (cid,)):
        if eid:
            ent = con.execute("SELECT cuit FROM entidades_cliente WHERE id=? AND cliente_id=?",
                              (eid, cid)).fetchone()
            if not ent or mov["cuit_contraparte"] != ent["cuit"]:
                continue
        cands = _candidatos(con, cid, mov)
        banco.append({**dict(mov),
                      "candidatos": [{"tipo": t, "id": i, "motivo": mo} for t, i, mo in cands],
                      "unico": len(cands) == 1})

    # 2) Pagos a cuenta: cobraron o pagaron, pero no se imputaron a factura.
    pagos = filas(con.execute(
        "SELECT p.id, p.fecha, p.numero, p.direccion, p.total, p.entidad_id, m.razon_social AS entidad, "
        "  COALESCE((SELECT SUM(a.importe) FROM pago_aplicaciones a WHERE a.pago_id=p.id),0) AS aplicado "
        "FROM pagos p JOIN entidades_cliente e ON e.id=p.entidad_id "
        "JOIN maestro_entidades m ON m.cuit=e.cuit "
        "WHERE p.cliente_id=?" + filtro_ent + " ORDER BY p.fecha", [cid] + arg_ent))
    pagos = [p for p in pagos if p["total"] - p["aplicado"] > 0.01]
    for p in pagos:
        p["a_cuenta"] = round(p["total"] - p["aplicado"], 2)

    # 3) Facturas impagas, con su antigüedad.
    hoy_d = date.today()
    facturas = filas(con.execute(
        "SELECT f.id, f.mov, f.fecha, f.tipo, f.letra, f.punto_venta, f.numero, f.total, f.entidad_id, "
        "  m.razon_social AS entidad, "
        "  COALESCE((SELECT SUM(a.importe) FROM pago_aplicaciones a WHERE a.factura_id=f.id),0) AS pagado "
        "FROM facturas f JOIN entidades_cliente e ON e.id=f.entidad_id "
        "JOIN maestro_entidades m ON m.cuit=e.cuit "
        "WHERE f.cliente_id=?" + filtro_ent + " ORDER BY f.fecha", [cid] + arg_ent))
    impagas = []
    for f in facturas:
        f["saldo"] = round(f["total"] - f["pagado"], 2)
        if abs(f["saldo"]) < 0.01:
            continue
        try:
            f["dias"] = (hoy_d - datetime.fromisoformat(f["fecha"][:10]).date()).days
        except Exception:
            f["dias"] = None
        impagas.append(f)

    # 4) Cheques en cartera: plata parada que no se depositó ni se endosó.
    cheques = filas(con.execute(
        "SELECT ch.id, ch.numero, ch.banco, ch.fecha_pago, ch.importe, ch.cuit_librador, "
        "  m.razon_social AS librador FROM cheques ch "
        "LEFT JOIN maestro_entidades m ON m.cuit=ch.cuit_librador "
        "WHERE ch.cliente_id=? AND ch.estado='en_cartera' ORDER BY ch.fecha_pago", (cid,)))

    con.close()
    return jsonify({
        "banco_sin_registrar": banco,
        "pagos_a_cuenta": pagos,
        "facturas_impagas": impagas,
        "cheques_en_cartera": cheques,
        "resumen": {
            "banco": len(banco),
            "banco_resolubles": sum(1 for b in banco if b["unico"]),
            "pagos": len(pagos),
            "pagos_importe": round(sum(p["a_cuenta"] for p in pagos), 2),
            "impagas": len(impagas),
            "impagas_importe": round(sum(f["saldo"] for f in impagas), 2),
            "cheques": len(cheques),
            "cheques_importe": round(sum(c["importe"] for c in cheques), 2),
        },
    })


@app.post("/api/c/pagos/<int:pid>/imputar")
def api_pago_imputar(pid):
    """Imputa un pago a cuenta contra las facturas impagas de esa entidad.

    Por defecto FIFO —de la más vieja a la más nueva, regla de B1— y se puede
    mandar `facturas: [ids]` para elegir a mano. Nunca imputa más de lo que le
    queda al pago ni más de lo que debe cada factura."""
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    pago = _de_este_cliente(con, "pagos", pid, cli["id"])
    if not pago:
        con.close()
        return jsonify({"error": "pago inexistente para este cliente"}), 404
    aplicado = _n(con.execute(
        "SELECT COALESCE(SUM(importe),0) FROM pago_aplicaciones WHERE pago_id=?", (pid,)).fetchone()[0])
    resto = round(pago["total"] - aplicado, 2)
    if resto <= 0.01:
        con.close()
        return jsonify({"error": "este pago ya está imputado por completo"}), 400

    b = request.get_json(force=True, silent=True) or {}
    # Un cobro cancela ventas y un pago cancela compras: la dirección no se mezcla.
    esperado = "venta" if pago["direccion"] == "cobro" else "compra"
    q = ("SELECT f.id, f.total, f.fecha, "
         "  COALESCE((SELECT SUM(a.importe) FROM pago_aplicaciones a WHERE a.factura_id=f.id),0) AS pagado "
         "FROM facturas f WHERE f.cliente_id=? AND f.entidad_id=? AND f.mov=? ")
    args = [cli["id"], pago["entidad_id"], esperado]
    if b.get("facturas"):
        q += " AND f.id IN (%s)" % ",".join("?" * len(b["facturas"]))
        args += list(b["facturas"])
    candidatas = [f for f in filas(con.execute(q + " ORDER BY f.fecha", args))
                  if round(f["total"] - f["pagado"], 2) > 0.01]

    hechas = []
    for f in candidatas:
        if resto <= 0.01:
            break
        cuanto = round(min(resto, f["total"] - f["pagado"]), 2)
        con.execute(
            "INSERT INTO pago_aplicaciones (pago_id, factura_id, importe) VALUES (?,?,?) "
            "ON CONFLICT (pago_id, factura_id) DO UPDATE SET importe=importe+excluded.importe",
            (pid, f["id"], cuanto))
        resto = round(resto - cuanto, 2)
        hechas.append({"factura_id": f["id"], "importe": cuanto})
    con.commit()
    con.close()
    return jsonify({"ok": True, "imputado": hechas, "queda_a_cuenta": resto})


@app.post("/api/c/facturas/<int:fid>/actividad")
def api_factura_actividad(fid):
    """Asigna el par actividad+alícuota a una venta que quedó sin él — es lo
    que destraba el control de la DJ."""
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    f = _de_este_cliente(con, "facturas", fid, cli["id"])
    if not f:
        con.close()
        return jsonify({"error": "factura inexistente para este cliente"}), 404
    if f["mov"] != "venta":
        con.close()
        return jsonify({"error": "la actividad de IIBB es de las ventas"}), 400
    b = request.get_json(force=True)
    alic = None if b.get("alicuota") in (None, "") else _n(b["alicuota"])
    act = con.execute(
        "SELECT * FROM maestro_actividades WHERE cuit=? AND codigo=? AND alicuota=?",
        (cli["cuit"], b.get("codigo"), alic)).fetchone()
    if not act:
        con.close()
        return jsonify({"error": "ese par actividad+alícuota no está en el padrón del cliente"}), 400
    con.execute(
        "UPDATE facturas SET iibb_jurisdiccion=?, iibb_codigo=?, iibb_alicuota=? WHERE id=?",
        (act["jurisdiccion"], act["codigo"], act["alicuota"], fid))
    con.commit()
    con.close()
    return jsonify({"ok": True})


# ══ JOBS — la suite de parsers, manejada desde la pantalla ═════════════════
# El catálogo y el lanzador viven en `parsers/suite.py`; acá solo se exponen.
# Los jobs ATENDIDOS abren su propia ventana y esperan a una persona, así que
# corren en un hilo y su salida se lee después: si el request esperara, el
# navegador se quedaría colgado los 8 minutos del login.
# ══ CENTROS DE COSTO ═══════════════════════════════════════════════════════
# ERBEN no tiene módulo Obra (Juan, 2026-08-26): sin OC, sin OT, sin
# certificados — el circuito arranca en la FACTURA. Esto es solo la
# clasificación por destino, y el reparto es por porcentaje desde el principio
# porque una factura puede tocar dos centros (lección del ERP).

@app.get("/api/c/centros")
def api_centros():
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    r = filas(con.execute(
        "SELECT c.*, "
        "  (SELECT COUNT(*) FROM factura_centros fc WHERE fc.centro_id=c.id) AS facturas, "
        "  (SELECT ROUND(SUM(f.total * fc.porcentaje / 100.0), 2) FROM factura_centros fc "
        "   JOIN facturas f ON f.id=fc.factura_id WHERE fc.centro_id=c.id) AS imputado "
        "FROM centros_costo c WHERE c.cliente_id=? AND c.activo=1 ORDER BY c.codigo",
        (cli["id"],)))
    con.close()
    return jsonify(r)


@app.post("/api/c/centros")
def api_centros_alta():
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    b = request.get_json(force=True)
    codigo = (b.get("codigo") or "").strip()
    nombre = (b.get("nombre") or "").strip()
    if not (codigo and nombre):
        con.close()
        return jsonify({"error": "hacen falta código y nombre"}), 400
    try:
        cur = con.execute(
            "INSERT INTO centros_costo (cliente_id, codigo, nombre, nota) VALUES (?,?,?,?)",
            (cli["id"], codigo, nombre, b.get("nota")))
    except sqlite3.IntegrityError:
        con.close()
        return jsonify({"error": "ya existe un centro con ese código"}), 409
    con.commit()
    cid = cur.lastrowid
    con.close()
    return jsonify({"ok": True, "id": cid})


@app.post("/api/c/facturas/<int:fid>/centros")
def api_factura_centros(fid):
    """Reparte una factura entre centros. `centros: [{centro_id, porcentaje}]`.

    Los porcentajes tienen que sumar 100: un reparto que no cierra deja plata
    sin imputar y el informe por centro miente sin avisar. Lista vacía = sacar
    el reparto (la factura vuelve a estar sin clasificar)."""
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    if not _de_este_cliente(con, "facturas", fid, cli["id"]):
        con.close()
        return jsonify({"error": "factura inexistente para este cliente"}), 404
    lote = (request.get_json(force=True) or {}).get("centros") or []
    for c in lote:
        if not con.execute("SELECT 1 FROM centros_costo WHERE id=? AND cliente_id=?",
                           (c.get("centro_id"), cli["id"])).fetchone():
            con.close()
            return jsonify({"error": "centro inexistente para este cliente"}), 400
    if lote:
        suma = round(sum(_n(c.get("porcentaje")) for c in lote), 2)
        if abs(suma - 100) > 0.01:
            con.close()
            return jsonify({"error": f"los porcentajes suman {suma}, tienen que sumar 100"}), 400
    con.execute("DELETE FROM factura_centros WHERE factura_id=?", (fid,))
    for c in lote:
        con.execute("INSERT INTO factura_centros (factura_id, centro_id, porcentaje) VALUES (?,?,?)",
                    (fid, c["centro_id"], _n(c["porcentaje"])))
    con.commit()
    con.close()
    return jsonify({"ok": True, "centros": len(lote)})


# ══ RESPALDO ═══════════════════════════════════════════════════════════════
@app.post("/api/respaldo")
def api_respaldo():
    return jsonify(_respaldo.copiar(motivo="desde el panel"))


# ══ EL PANEL — servidores, jobs y el registro de lo que se corrió ══════════
# Trae la lección del panel del ERP: los jobs se corrían con .bat que imprimían
# a consola, y al cerrar la ventana no quedaba registro de NADA. Sin historial
# no se puede contestar "¿esto se corrió?" ni "¿por qué falló?".
#
# Acá las corridas van a la BASE, no a memoria: reiniciar el sistema no borra
# el historial.
import getpass
import platform
import socket
import threading

sys.path.insert(0, str(RAIZ / "parsers"))


def _suite():
    """Se importa al vuelo para que un error en la suite no impida arrancar el
    sistema entero: sin jobs se puede trabajar, sin servidor no."""
    import importlib
    import suite as _s
    return importlib.reload(_s)


def _puerto_vivo(puerto, host="127.0.0.1", timeout=0.4):
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex((host, puerto)) == 0


# Los servicios que el estudio puede tener prendidos. El de ERBEN es este
# mismo; los otros son de SIBRA y solo se MIRAN — el panel del estudio no
# arranca procesos de otro sistema.
SERVICIOS = [
    {"id": "erben", "nombre": "ERBEN ESTUDIO", "puerto": 8310, "propio": True,
     "nota": "El sistema. Lo prende el ícono del escritorio."},
    {"id": "tesoreria", "nombre": "Tesorería SIBRA", "puerto": 8300, "propio": False,
     "nota": "De nuestro sistema. No hace falta para que ERBEN funcione."},
]


@app.get("/api/panel")
def api_panel():
    """Cómo está el equipo: el sistema, la base, los clientes y qué falta."""
    con = db()
    uno = lambda q: con.execute(q).fetchone()[0]
    clientes_n = uno("SELECT COUNT(*) FROM clientes WHERE activo=1")
    datos = {
        "clientes": clientes_n,
        "entidades": uno("SELECT COUNT(*) FROM maestro_entidades"),
        "facturas": uno("SELECT COUNT(*) FROM facturas"),
        "movimientos": uno("SELECT COUNT(*) FROM movimientos_banco"),
        "sin_conciliar": uno("SELECT COUNT(*) FROM movimientos_banco WHERE conciliado=0"),
        "cheques": uno("SELECT COUNT(*) FROM cheques"),
    }
    corridas = filas(con.execute(
        "SELECT id, job, alias, inicio, fin, segundos, estado, exit_code "
        "FROM jobs_corridas ORDER BY inicio DESC LIMIT 25"))
    con.close()

    # lo que falta para que el sistema trabaje solo
    faltan = []
    try:
        s = _suite()
        import credenciales as cred
        for j in s.catalogo():
            for c in j["clientes"]:
                if c["credencial"] is False:
                    faltan.append({"que": "credencial", "fuente": j["fuente"],
                                   "alias": c["alias"]})
        faltan = [dict(x) for x in {tuple(sorted(f.items())): f for f in faltan}.values()]
    except Exception:
        pass
    drive = rutas.CREDENCIALES_GOOGLE
    if not drive.exists():
        faltan.append({"que": "credentials.json de Google", "fuente": "drive", "alias": None})

    return jsonify({
        "base": {"ruta": str(DB_PATH), "existe": DB_PATH.exists(),
                 "mb": round(DB_PATH.stat().st_size / 1048576, 2) if DB_PATH.exists() else 0},
        "respaldo": _respaldo.estado(),
        "drive": {"ruta": str(rutas.DRIVE), "montado": rutas.hay_drive()},
        "equipo": {"maquina": platform.node(), "usuario": getpass.getuser()},
        "datos": datos,
        "servicios": [{**s, "vivo": _puerto_vivo(s["puerto"])} for s in SERVICIOS],
        "faltan": faltan,
        "corridas": corridas,
    })


@app.get("/api/jobs")
def api_jobs():
    try:
        cat = _suite().catalogo()
    except Exception as e:
        return jsonify({"error": f"no pude leer la suite de jobs: {e}"}), 500
    con = db()
    for j in cat:
        u = con.execute(
            "SELECT id, estado, inicio, fin, segundos, exit_code, alias FROM jobs_corridas "
            "WHERE job=? ORDER BY inicio DESC LIMIT 1", (j["clave"],)).fetchone()
        j["ultima_corrida"] = dict(u) if u else None
    con.close()
    return jsonify(cat)


@app.post("/api/jobs/<clave>/correr")
def api_job_correr(clave):
    b = request.get_json(force=True, silent=True) or {}
    args, alias = [], None
    for k, v in (b.get("args") or {}).items():
        if v in (None, ""):
            continue
        args += [k, str(v)]
        if k == "--alias":
            alias = str(v)

    con = db()
    cur = con.execute(
        "INSERT INTO jobs_corridas (job, args, alias, usuario, maquina, inicio, estado) "
        "VALUES (?,?,?,?,?,?,'corriendo')",
        (clave, " ".join(args), alias, getpass.getuser(), platform.node(),
         datetime.now().isoformat(timespec="seconds")))
    cid = cur.lastrowid
    con.commit()
    con.close()

    arranque = time.time()

    def correr():
        try:
            codigo, salida = _suite().correr(clave, args)
        except Exception as e:
            codigo, salida = 2, f"No se pudo lanzar el job: {e}"
        c2 = db()
        c2.execute(
            "UPDATE jobs_corridas SET estado=?, exit_code=?, salida=?, fin=?, segundos=? WHERE id=?",
            ("ok" if codigo == 0 else "falló", codigo, salida,
             datetime.now().isoformat(timespec="seconds"),
             round(time.time() - arranque, 1), cid))
        c2.commit()
        c2.close()

    threading.Thread(target=correr, daemon=True).start()
    return jsonify({"ok": True, "id": cid})


@app.get("/api/jobs/corrida/<int:cid>")
def api_job_corrida(cid):
    con = db()
    c = con.execute("SELECT * FROM jobs_corridas WHERE id=?", (cid,)).fetchone()
    con.close()
    return jsonify(dict(c)) if c else (jsonify({"error": "no existe esa corrida"}), 404)


@app.get("/api/jobs/corridas")
def api_job_corridas():
    con = db()
    cs = filas(con.execute(
        "SELECT id, job, args, alias, usuario, inicio, fin, segundos, estado, exit_code "
        "FROM jobs_corridas ORDER BY inicio DESC LIMIT 30"))
    con.close()
    return jsonify(cs)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    crear_y_sembrar()
    # Respaldo al arrancar: es el momento en que la base está quieta y nadie
    # está escribiendo. Si falla, se avisa y se sigue — que no haya Drive no
    # puede dejar al estudio sin poder trabajar.
    _r = _respaldo.copiar(motivo="al arrancar")
    print("  respaldo: " + (f"{Path(_r['archivo']).name} ({_r['mb']} MB)"
                            if _r.get("ok") else "⚠ " + _r.get("error", "")))
    print("ERBEN ESTUDIO — http://localhost:8310")
    app.run(host="127.0.0.1", port=8310, debug=False)


