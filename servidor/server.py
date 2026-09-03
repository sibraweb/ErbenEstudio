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
import bancos_ar
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

    # ── el librador con nombre, y el beneficiario del emitido ──
    cc = {f[1] for f in con.execute("PRAGMA table_info(cheques)")}
    if "librador_nombre" not in cc:
        print("  migrando: nombre del librador y beneficiario del cheque emitido…")
        con.execute("ALTER TABLE cheques ADD COLUMN librador_nombre TEXT")
        con.execute("ALTER TABLE cheques ADD COLUMN beneficiario_entidad_id INTEGER")
        con.commit()

    # ── el saldo a favor de IVA que viene de antes ──
    tablas = {f[0] for f in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "iva_saldo_inicial" not in tablas:
        print("  migrando: saldo inicial de IVA (el arrastre a favor)…")
        con.executescript("""
            CREATE TABLE iva_saldo_inicial (
                cliente_id  INTEGER PRIMARY KEY REFERENCES clientes(id),
                periodo     TEXT NOT NULL,
                a_favor     REAL NOT NULL DEFAULT 0,
                nota        TEXT,
                actualizado TEXT NOT NULL);""")
        con.commit()

    # ── percepciones discriminadas y retenciones ──
    if "retenciones" not in tablas:
        print("  migrando: percepciones por tipo y retenciones del recibo…")
        con.executescript("""
            CREATE TABLE factura_tributos (
                id INTEGER PRIMARY KEY,
                factura_id INTEGER NOT NULL REFERENCES facturas(id) ON DELETE CASCADE,
                tipo TEXT NOT NULL, jurisdiccion TEXT,
                base REAL, alicuota REAL, monto REAL NOT NULL, detalle TEXT);
            CREATE INDEX ix_facttrib ON factura_tributos(factura_id);
            CREATE TABLE retenciones (
                id INTEGER PRIMARY KEY,
                cliente_id INTEGER NOT NULL REFERENCES clientes(id),
                pago_id INTEGER REFERENCES pagos(id) ON DELETE CASCADE,
                direccion TEXT NOT NULL DEFAULT 'sufrida',
                entidad_id INTEGER REFERENCES entidades_cliente(id),
                fecha TEXT, tipo TEXT NOT NULL, codigo_regimen TEXT, concepto TEXT,
                jurisdiccion TEXT, base REAL, alicuota REAL, monto REAL NOT NULL,
                certificado TEXT, computada INTEGER NOT NULL DEFAULT 0, detalle TEXT);
            CREATE INDEX ix_reten_cliente ON retenciones(cliente_id, fecha);
            CREATE INDEX ix_reten_pago ON retenciones(pago_id);""")
        # Lo que ya estaba cargado entra como SIN CLASIFICAR y no computa en
        # ninguna DJ. Es lo honesto: «Otros Tributos» de ARCA es una sola
        # columna que mezcla percepción de IVA, de IIBB y tasas municipales, y
        # hasta hoy la posición de IVA restaba todo como si fuera crédito de
        # IVA. Restar de menos es peor que no restar.
        con.execute(
            "INSERT INTO factura_tributos (factura_id, tipo, monto, detalle) "
            "SELECT id, 'sin_clasificar', percepciones, "
            "  'Otros Tributos del export de ARCA — hay que decir de qué es' "
            "FROM facturas WHERE ABS(COALESCE(percepciones,0)) > 0.009")
        con.commit()

    # ── el banco de la cuenta, contra el maestro del BCRA ──
    cq = {f[1] for f in con.execute("PRAGMA table_info(cuentas_bancarias)")}
    if "codigo_bcra" not in cq:
        print("  migrando: código BCRA y titular en las cuentas…")
        con.execute("ALTER TABLE cuentas_bancarias ADD COLUMN codigo_bcra TEXT")
        con.execute("ALTER TABLE cuentas_bancarias ADD COLUMN titular TEXT")
        # Backfill: el CBU YA tiene el banco adentro — sus primeros 3 dígitos
        # son el código de entidad. Es el dato más confiable que hay.
        for c in con.execute("SELECT id, banco, cbu FROM cuentas_bancarias").fetchall():
            cod = None
            if c["cbu"] and len(re.sub(r"\D", "", c["cbu"])) >= 3:
                cod = re.sub(r"\D", "", c["cbu"])[:3]
            elif c["banco"]:
                m = bancos_ar.buscar(c["banco"])
                if len(m) == 1:
                    cod = m[0][0]
            if cod:
                con.execute("UPDATE cuentas_bancarias SET codigo_bcra=? WHERE id=?", (cod, c["id"]))
        con.commit()

    # ── el impuesto pagado, atado a su débito ──
    cvv = {f[1] for f in con.execute("PRAGMA table_info(vencimientos)")}
    if "movimiento_id" not in cvv:
        print("  migrando: el impuesto pagado se ata al movimiento que lo debitó…")
        con.execute("ALTER TABLE vencimientos ADD COLUMN movimiento_id INTEGER")
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
    # ⚠ NO SIEMPRE ES UN CUIT (02/09). Una factura B a una persona física la
    # identifica por DNI, y sin esto esas ventas no entran: al cargar los
    # comprobantes de ARCA, 8 ventas reales quedaban afuera porque el receptor
    # tenía DNI. El largo dice qué es —7 u 8 dígitos es DNI, 11 es CUIT— y no
    # se completa el CUIT a partir del DNI: el prefijo (20/23/24/27) y el
    # dígito verificador serían inventados.
    if len(cuit) not in (7, 8, 11):
        con.close()
        return jsonify({"error": "documento inválido: 11 dígitos (CUIT) o 7-8 (DNI)"}), 400
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
    # `tributos_clasificados` es 0 mientras quede algo en «sin_clasificar»: eso
    # es plata que no computa en ninguna DJ y la pantalla lo muestra como hueco.
    q = ("SELECT f.*, m.razon_social AS entidad, e.cuit AS entidad_cuit, "
         "  COALESCE((SELECT SUM(a.importe) FROM pago_aplicaciones a WHERE a.factura_id=f.id),0) AS pagado, "
         "  (SELECT COUNT(*) = 0 FROM factura_tributos t WHERE t.factura_id=f.id "
         "     AND t.tipo='sin_clasificar') AS tributos_clasificados "
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

    # ── EL BANCO SE RESUELVE CONTRA EL MAESTRO DEL BCRA (03/09) ──────────────
    # Escribir el nombre a mano es como se llenan las bases de «BANCO DE
    # FORMOSA», «Bco Formosa» y «formosa» conviviendo. Si viene el código, o si
    # lo que se escribió matchea UNA sola entidad, se guarda la razón social
    # prolija. Si matchea varias, no se elige por el cliente: se guarda lo que
    # escribió y queda para completar.
    cbu = re.sub(r"\D", "", b.get("cbu") or "")
    codigo = (b.get("codigo_bcra") or "").strip() or (cbu[:3] if len(cbu) >= 3 else None)
    banco = b["banco"].strip()
    if not codigo:
        m = bancos_ar.buscar(banco)
        if len(m) == 1:
            codigo = m[0][0]
    if codigo:
        ficha = bancos_ar.por_codigo(codigo)
        if ficha:
            banco = ficha[2]        # la razón social del maestro

    # El CBU trae su propio verificador: si no cierra, es un número mal
    # tipeado y una transferencia a ese CBU va a rebotar o —peor— a otro lado.
    if cbu:
        ok, motivo, cod_cbu = bancos_ar.validar_cbu(cbu)
        if not ok:
            con.close()
            return jsonify({"error": f"CBU inválido: {motivo}"}), 400
        if codigo and cod_cbu and cod_cbu != codigo:
            con.close()
            return jsonify({"error": f"el CBU es del banco {cod_cbu} y la cuenta dice {codigo}"}), 400
        codigo = codigo or cod_cbu

    cur = con.execute(
        "INSERT INTO cuentas_bancarias (cliente_id, banco, codigo_bcra, tipo, numero, cbu, "
        " moneda, alias_banco, titular) VALUES (?,?,?,?,?,?,?,?,?)",
        (cli["id"], banco, codigo, b.get("tipo"), b.get("numero"), cbu or None,
         b.get("moneda") or "ARS", b.get("alias_banco"),
         (b.get("titular") or "").strip() or None))
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
    # LOS TRES ROLES de un cheque recibido (Juan, 2026-08-31):
    #   LIBRADOR  — quién firmó el cheque. Puede no ser el cliente.
    #   CLIENTE   — quién nos lo dio, en la cobranza. Es el que cancela factura.
    #   DESTINO   — dónde terminó: un PROVEEDOR (endosado) o un BANCO (depositado).
    #
    # Acá NO hay cliente de fantasía: el cheque recibido ES una cobranza y el
    # cliente sale del recibo que lo trajo. (En el ERP existe la fantasía porque
    # ahí entran cheques de terceros sin venta detrás; ese circuito no aplica.)
    # El librador se busca primero en el maestro (por CUIT) y si no está, se
    # muestra el nombre que se escribió a mano: media entidad se conoce por uno
    # y media por el otro.
    q = ("SELECT ch.*, "
         "  COALESCE(ml.razon_social, ch.librador_nombre) AS librador, "
         "  (SELECT mb.razon_social FROM entidades_cliente eb "
         "     JOIN maestro_entidades mb ON mb.cuit=eb.cuit "
         "   WHERE eb.id=ch.beneficiario_entidad_id) AS beneficiario, "
         "  (SELECT p.numero FROM pagos p WHERE p.id=ch.pago_origen_id) AS recibo_origen, "
         "  (SELECT mc.razon_social FROM pagos p "
         "     JOIN entidades_cliente ec ON ec.id=p.entidad_id "
         "     JOIN maestro_entidades mc ON mc.cuit=ec.cuit "
         "   WHERE p.id=ch.pago_origen_id) AS cliente, "
         "  (SELECT mp.razon_social FROM entidades_cliente ep "
         "     JOIN maestro_entidades mp ON mp.cuit=ep.cuit "
         "   WHERE ep.id=ch.endoso_entidad_id) AS endosado_a, "
         "  (SELECT cb.banco FROM cuentas_bancarias cb "
         "   WHERE cb.id=ch.deposito_cuenta_id) AS depositado_en "
         "FROM cheques ch LEFT JOIN maestro_entidades ml ON ml.cuit=ch.cuit_librador "
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
    # A quién se lo damos. Es opcional porque a veces se carga la chequera
    # entera antes de saber a quién va cada uno, pero si viene tiene que ser
    # una entidad de ESTE cliente.
    benef = b.get("beneficiario_entidad_id")
    if benef and not _de_este_cliente(con, "entidades_cliente", benef, cli["id"]):
        con.close()
        return jsonify({"error": "entidad inexistente para este cliente"}), 400
    if _n(b.get("importe")) <= 0:
        con.close()
        return jsonify({"error": "el importe tiene que ser mayor que cero"}), 400
    try:
        cur = con.execute(
            "INSERT INTO cheques (cliente_id, origen, numero, banco, cuenta_id, beneficiario_entidad_id, "
            " fecha_emision, fecha_pago, importe, estado, nota) VALUES (?,'emitido',?,?,?,?,?,?,?,'emitido',?)",
            (cli["id"], b["numero"].strip(), b.get("banco"), b.get("cuenta_id"), benef or None,
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
        "SELECT p.*, m.razon_social AS entidad, "
        "  COALESCE((SELECT SUM(x.monto) FROM retenciones x WHERE x.pago_id=p.id),0) AS retenido "
        "FROM pagos p "
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

    # ── LA ECUACIÓN COMPLETA (traída del ERP, 03/09) ─────────────────────────
    #     importe aplicado a las facturas = medios de pago + retenciones
    #
    # Sin la segunda pata, una factura cobrada con retención queda impaga por
    # la diferencia PARA SIEMPRE y la cuenta corriente miente. La retención no
    # es un descuento: es plata que la contraparte le pagó al fisco en nombre
    # del cliente, y que después se computa en la DJ.
    retenciones = b.get("retenciones") or []
    for x in retenciones:
        if x.get("tipo") not in TRIBUTOS_VALIDOS:
            con.close()
            return jsonify({"error": f"concepto de retención desconocido: {x.get('tipo')}"}), 400
        if _n(x.get("monto")) <= 0:
            con.close()
            return jsonify({"error": "la retención tiene que ser mayor que cero"}), 400
    total_medios = round(sum(_n(m.get("importe")) for m in medios), 2)
    total_ret = round(sum(_n(x.get("monto")) for x in retenciones), 2)
    total_aplic = round(sum(_n(a.get("importe")) for a in aplicaciones), 2)
    if aplicaciones and abs(total_medios + total_ret - total_aplic) > 0.01:
        con.close()
        return jsonify({"error": f"los medios suman {total_medios}"
                                 + (f" más {total_ret} de retenciones" if total_ret else "")
                                 + f" y las facturas {total_aplic}"}), 400

    fecha = (b.get("fecha") or _hoy())[:10]
    cur = con.execute(
        "INSERT INTO pagos (cliente_id, entidad_id, direccion, fecha, numero, total, nota) "
        "VALUES (?,?,?,?,?,?,?)",
        # El TOTAL del comprobante es lo que cancela: medios + retenciones. Si
        # guardara solo los medios, el recibo diría menos de lo que la factura
        # dio por pagado.
        (cid, ent["id"], b["direccion"], fecha, b.get("numero"),
         round(total_medios + total_ret, 2), b.get("nota")))
    pago_id = cur.lastrowid

    for a in aplicaciones:
        con.execute("INSERT INTO pago_aplicaciones (pago_id, factura_id, importe) VALUES (?,?,?)",
                    (pago_id, a["factura_id"], _n(a.get("importe"))))

    # La dirección de la retención sale del comprobante, no se elige: en un
    # COBRO nos retuvieron (crédito fiscal); en un PAGO retuvimos al proveedor
    # (pasivo: esa plata hay que depositarla y darle el certificado).
    direccion_ret = "sufrida" if b["direccion"] == "cobro" else "practicada"
    for x in retenciones:
        con.execute(
            "INSERT INTO retenciones (cliente_id, pago_id, direccion, entidad_id, fecha, "
            " tipo, codigo_regimen, concepto, jurisdiccion, base, alicuota, monto, "
            " certificado, detalle) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, pago_id, direccion_ret, ent["id"], x.get("fecha") or fecha,
             x["tipo"], x.get("codigo_regimen"), x.get("concepto"), x.get("jurisdiccion"),
             _n(x.get("base")) or None, _n(x.get("alicuota")) or None,
             _n(x.get("monto")), x.get("certificado"), x.get("detalle")))

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
                        "INSERT INTO cheques (cliente_id, origen, numero, banco, cuit_librador, librador_nombre, "
                        " cuenta_id, beneficiario_entidad_id, fecha_emision, fecha_pago, importe, estado, "
                        " pago_origen_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (cid, "recibido" if recibido else "emitido", d["numero"].strip(), d.get("banco"),
                         # ⚠ El librador NO se completa con el CUIT del cliente. Son
                         # roles distintos: el que firma el cheque puede no ser el
                         # que nos lo dio. Ponerle el del cliente diría que lo firmó
                         # él, y es un dato que quizá nadie miró. Si no se sabe,
                         # queda vacío — el cliente ya está en el recibo.
                         (re.sub(r"\D", "", d.get("cuit_librador") or "") or None) if recibido else None,
                         ((d.get("librador_nombre") or "").strip() or None) if recibido else None,
                         d.get("cuenta_id") if not recibido else None,
                         # El emitido nace con beneficiario: es a quien le estamos pagando.
                         None if recibido else ent["id"],
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


@app.get("/api/c/facturas/<int:fid>/tributos")
def api_factura_tributos(fid):
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    if not _de_este_cliente(con, "facturas", fid, cli["id"]):
        con.close()
        return jsonify({"error": "factura inexistente para este cliente"}), 404
    r = filas(con.execute(
        "SELECT id, tipo, jurisdiccion, base, alicuota, monto, detalle "
        "FROM factura_tributos WHERE factura_id=? ORDER BY id", (fid,)))
    f = con.execute("SELECT percepciones FROM facturas WHERE id=?", (fid,)).fetchone()
    con.close()
    total = round(sum(_n(x["monto"]) for x in r), 2)
    declarado = _n(f["percepciones"])
    return jsonify({
        "tributos": r, "total_desglosado": total, "total_comprobante": declarado,
        # Si el desglose no da lo que dice el papel, falta o sobra algo. Se
        # informa en vez de bloquear: puede ser un tributo que ARCA no manda.
        "cierra": abs(total - declarado) < 0.01,
        "diferencia": round(declarado - total, 2),
    })


@app.post("/api/c/facturas/<int:fid>/tributos")
def api_factura_tributos_set(fid):
    """Reemplaza el desglose de una factura.

    Body: {tributos:[{tipo, monto, jurisdiccion?, base?, alicuota?, detalle?}]}

    `facturas.percepciones` sigue siendo el TOTAL que trae el comprobante y no
    se toca: acá se dice DE QUÉ es cada peso. Lo que queda sin clasificar no
    computa en ninguna DJ."""
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    if not _de_este_cliente(con, "facturas", fid, cli["id"]):
        con.close()
        return jsonify({"error": "factura inexistente para este cliente"}), 404
    b = request.get_json(force=True)
    lineas = b.get("tributos") or []
    for x in lineas:
        if x.get("tipo") not in TRIBUTOS_VALIDOS:
            con.close()
            return jsonify({"error": f"concepto desconocido: {x.get('tipo')}"}), 400
        if _n(x.get("monto")) == 0:
            con.close()
            return jsonify({"error": "un tributo en cero no se guarda"}), 400
    con.execute("DELETE FROM factura_tributos WHERE factura_id=?", (fid,))
    for x in lineas:
        con.execute(
            "INSERT INTO factura_tributos (factura_id, tipo, jurisdiccion, base, alicuota, "
            " monto, detalle) VALUES (?,?,?,?,?,?,?)",
            (fid, x["tipo"], x.get("jurisdiccion"),
             _n(x.get("base")) or None, _n(x.get("alicuota")) or None,
             _n(x.get("monto")), x.get("detalle")))
    con.commit()
    con.close()
    return jsonify({"ok": True, "lineas": len(lineas)})


@app.get("/api/c/pagos/<int:pid>/retenciones")
def api_pago_retenciones(pid):
    """Las retenciones del recibo, y si la ecuación cierra.

    ⚠ EL AGUJERO QUE ESTO TAPA (relevado en el ERP): una factura de
    $12.717.601 se cobra con $12.488.762 en el banco. Los $228.839 de
    diferencia no son un descuento ni una factura mal emitida: son plata que el
    cliente le pagó al fisco en nombre nuestro y que después se computa en la
    DJ. Sin registrarla, la factura queda impaga por ese resto para siempre.

        importe aplicado a las facturas = medios de pago + retenciones
    """
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    pg = _de_este_cliente(con, "pagos", pid, cli["id"])
    if not pg:
        con.close()
        return jsonify({"error": "comprobante inexistente para este cliente"}), 404
    r = filas(con.execute(
        "SELECT id, direccion, tipo, codigo_regimen, concepto, jurisdiccion, base, "
        "  alicuota, monto, certificado, computada, fecha "
        "FROM retenciones WHERE pago_id=? ORDER BY id", (pid,)))
    medios = _n(con.execute("SELECT COALESCE(SUM(importe),0) FROM pago_medios WHERE pago_id=?",
                            (pid,)).fetchone()[0])
    aplicado = _n(con.execute("SELECT COALESCE(SUM(importe),0) FROM pago_aplicaciones "
                              "WHERE pago_id=?", (pid,)).fetchone()[0])
    con.close()
    ret = round(sum(_n(x["monto"]) for x in r), 2)
    return jsonify({
        "retenciones": r, "total_retenido": ret,
        "medios_de_pago": medios, "imputado_a_facturas": aplicado,
        # Si no da, se dice cuánto falta en vez de dejar la factura media paga
        # sin explicación.
        "cierra": abs((medios + ret) - aplicado) < 0.01 or aplicado == 0,
        "diferencia": round(aplicado - (medios + ret), 2),
    })


@app.post("/api/c/pagos/<int:pid>/retenciones")
def api_pago_retenciones_set(pid):
    """Reemplaza las retenciones del recibo.

    La dirección sale del comprobante y NO se elige: en un COBRO nos retuvieron
    (sufrida, es crédito fiscal); en un PAGO retuvimos al proveedor
    (practicada, es un pasivo — esa plata no es del cliente, hay que
    depositarla y darle el certificado)."""
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    pg = _de_este_cliente(con, "pagos", pid, cli["id"])
    if not pg:
        con.close()
        return jsonify({"error": "comprobante inexistente para este cliente"}), 404
    b = request.get_json(force=True)
    lineas = b.get("retenciones") or []
    for x in lineas:
        if x.get("tipo") not in TRIBUTOS_VALIDOS:
            con.close()
            return jsonify({"error": f"concepto desconocido: {x.get('tipo')}"}), 400
        if _n(x.get("monto")) <= 0:
            con.close()
            return jsonify({"error": "la retención tiene que ser mayor que cero"}), 400
    direccion = "sufrida" if pg["direccion"] == "cobro" else "practicada"
    con.execute("DELETE FROM retenciones WHERE pago_id=?", (pid,))
    for x in lineas:
        con.execute(
            "INSERT INTO retenciones (cliente_id, pago_id, direccion, entidad_id, fecha, "
            " tipo, codigo_regimen, concepto, jurisdiccion, base, alicuota, monto, "
            " certificado, detalle) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cli["id"], pid, direccion, pg["entidad_id"], x.get("fecha") or pg["fecha"],
             x["tipo"], x.get("codigo_regimen"), x.get("concepto"), x.get("jurisdiccion"),
             _n(x.get("base")) or None, _n(x.get("alicuota")) or None,
             _n(x.get("monto")), x.get("certificado"), x.get("detalle")))
    con.commit()
    con.close()
    return jsonify({"ok": True, "lineas": len(lineas)})


@app.get("/api/c/retenciones")
def api_retenciones():
    """Todas las del cliente, para el informe y para ver qué falta computar."""
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    q = ("SELECT r.*, m.razon_social AS entidad, p.numero AS recibo "
         "FROM retenciones r "
         "LEFT JOIN entidades_cliente e ON e.id=r.entidad_id "
         "LEFT JOIN maestro_entidades m ON m.cuit=e.cuit "
         "LEFT JOIN pagos p ON p.id=r.pago_id "
         "WHERE r.cliente_id=?")
    args = [cli["id"]]
    if request.args.get("direccion") in ("sufrida", "practicada"):
        q += " AND r.direccion=?"
        args.append(request.args["direccion"])
    r = filas(con.execute(q + " ORDER BY r.fecha DESC, r.id DESC", args))
    con.close()
    # Sin certificado no se puede computar en la DJ: es el número que vale ante
    # ARCA, no el monto.
    sin_cert = [x for x in r if not (x.get("certificado") or "").strip()]
    return jsonify({"retenciones": r,
                    "total": round(sum(_n(x["monto"]) for x in r), 2),
                    "sin_certificado": len(sin_cert)})


@app.get("/api/bancos/maestro")
def api_bancos_maestro():
    """El UNIVERSO de bancos del país: código de entidad del BCRA (los 3
    primeros dígitos del CBU) y razón social prolija.

    ⚠ SON DOS LISTAS DISTINTAS y no se mezclan (criterio del ERP, 16/08):
      · esto es el universo, y se usa SOLO al dar de alta una cuenta —
        escribís "formosa" y sale BANCO DE FORMOSA S.A. bien escrito;
      · al OPERAR (elegir dónde depositar, qué cuenta debita) se ofrece lo que
        el cliente TIENE, nunca este universo entero.

    Es público: son los códigos del BCRA, no hay datos de nadie."""
    q = request.args.get("q", "")
    datos = (bancos_ar.buscar(q) if q.strip()
             else [(c, n, r) for c, n, r, _ in bancos_ar.MAESTRO])
    return jsonify([{"codigo": c, "nombre_corto": n, "nombre": r} for c, n, r in datos])


@app.get("/api/c/maestros/incompletos")
def api_maestros_incompletos():
    """Lo que falta completar en las bases del cliente.

    Idea del ERP (Juan, 16/08: *"así cada vez profesionalizamos más todo"*): en
    vez de descubrir que falta un CUIT el día que hay que emitir algo, se ve
    todo junto y se completa de a poco. Cada hueco dice por qué importa, porque
    "faltan datos" no mueve a nadie.

    Las listas se cortan en 50; `total` trae el número real."""
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    cid = cli["id"]

    def bloque(por_que, sql, params=()):
        f = filas(con.execute(sql, params))
        return {"total": len(f), "filas": f[:50], "por_que": por_que}

    out = {
        "entidades_sin_cuit": bloque(
            "Sin CUIT no entran en ninguna DJ ni se pueden cruzar con el banco.",
            "SELECT e.id, m.razon_social FROM entidades_cliente e "
            "JOIN maestro_entidades m ON m.cuit=e.cuit "
            "WHERE e.cliente_id=? AND (e.cuit IS NULL OR e.cuit='' OR LENGTH(e.cuit)<7) "
            "ORDER BY m.razon_social", (cid,)),
        "entidades_sin_condicion_iva": bloque(
            "La condición frente al IVA decide qué letra de factura corresponde.",
            "SELECT e.id, m.razon_social, e.cuit FROM entidades_cliente e "
            "JOIN maestro_entidades m ON m.cuit=e.cuit "
            "WHERE e.cliente_id=? AND (m.condicion_iva IS NULL OR m.condicion_iva='') "
            "ORDER BY m.razon_social", (cid,)),
        "cuentas_sin_cbu": bloque(
            "Sin CBU no se puede conciliar una transferencia ni validar a dónde fue.",
            "SELECT id, banco, numero, alias_banco FROM cuentas_bancarias "
            "WHERE cliente_id=? AND (cbu IS NULL OR cbu='') ORDER BY banco", (cid,)),
        "cuentas_sin_tipo": bloque(
            "Caja de ahorro o cuenta corriente: cambia si puede quedar en descubierto.",
            "SELECT id, banco, numero FROM cuentas_bancarias "
            "WHERE cliente_id=? AND (tipo IS NULL OR tipo='') ORDER BY banco", (cid,)),
        "cuentas_sin_banco_del_maestro": bloque(
            "El banco no se pudo resolver contra el maestro del BCRA: puede estar "
            "escrito de dos formas distintas y contarse dos veces.",
            "SELECT id, banco, numero FROM cuentas_bancarias "
            "WHERE cliente_id=? AND (codigo_bcra IS NULL OR codigo_bcra='') "
            "ORDER BY banco", (cid,)),
        "ventas_sin_actividad": bloque(
            "Mientras estén así, la DJ de IIBB no cierra: Σ bases ≠ Σ ventas.",
            "SELECT f.id, f.fecha, f.tipo, f.punto_venta, f.numero, f.total "
            "FROM facturas f WHERE f.cliente_id=? AND f.mov='venta' "
            "AND (f.iibb_codigo IS NULL OR f.iibb_codigo='') ORDER BY f.fecha DESC", (cid,)),
        "tributos_sin_clasificar": bloque(
            "No se computan en ninguna DJ hasta decir de qué son. Puede haber "
            "crédito de IVA o de IIBB ahí adentro.",
            "SELECT f.id, f.fecha, f.tipo, f.punto_venta, f.numero, "
            "  ROUND(SUM(t.monto),2) AS monto "
            "FROM factura_tributos t JOIN facturas f ON f.id=t.factura_id "
            "WHERE f.cliente_id=? AND t.tipo='sin_clasificar' "
            "GROUP BY f.id ORDER BY ABS(SUM(t.monto)) DESC", (cid,)),
        "retenciones_sin_certificado": bloque(
            "El número de certificado es el que vale ante ARCA: sin él la "
            "retención no se puede computar aunque el monto esté bien.",
            "SELECT r.id, r.fecha, r.tipo, r.monto, p.numero AS recibo "
            "FROM retenciones r LEFT JOIN pagos p ON p.id=r.pago_id "
            "WHERE r.cliente_id=? AND r.direccion='sufrida' "
            "AND (r.certificado IS NULL OR TRIM(r.certificado)='') "
            "ORDER BY r.fecha DESC", (cid,)),
        "cheques_sin_librador": bloque(
            "Si rebota, al librador es a quien se le reclama.",
            "SELECT id, numero, banco, importe, fecha_pago FROM cheques "
            "WHERE cliente_id=? AND origen='recibido' AND estado='en_cartera' "
            "AND (cuit_librador IS NULL OR cuit_librador='') "
            "AND (librador_nombre IS NULL OR librador_nombre='') "
            "ORDER BY fecha_pago", (cid,)),
    }
    con.close()
    out["total_huecos"] = sum(v["total"] for v in out.values() if isinstance(v, dict))
    return jsonify(out)


@app.get("/api/c/tesoreria/sin-contraparte")
def api_sin_contraparte():
    """Qué documento le falta a cada cosa para quedar explicada.

    Idea del ERP (Juan, 23/08: *"para ver qué movimientos no tienen
    contraparte"*). Es el cierre del módulo: mientras esta lista tenga algo,
    hay pesos que se movieron y nadie sabe por qué.

    ⚠ El ERP lo saca de una vista de Postgres (`v_estado_contable`) que cruza
    bancos, impuestos, cheques y facturas. Acá se arma con consultas sueltas
    sobre las mismas cuatro cosas — es la misma idea, no la misma vista.

    ⚠ Y hay un corte: solo cuenta lo VENCIDO o ya ocurrido. Una factura que
    todavía no venció no es un hueco, es el negocio andando. Una lista que
    nunca baja se termina ignorando."""
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    cid, hoy = cli["id"], _hoy()
    grupos = []

    def bloque(clase, falta, sql, params):
        f = filas(con.execute(sql, params))
        if f:
            grupos.append({
                "clase": clase, "falta": falta, "n": len(f),
                "total": round(sum(abs(_n(x["importe"])) for x in f), 2),
                "detalle": f[:100],
            })

    bloque("Movimiento del banco", "el comprobante que lo explique",
           "SELECT m.id, m.fecha, m.importe, "
           "  COALESCE(m.descripcion,'(sin descripción)') AS detalle, c.banco "
           "FROM movimientos_banco m JOIN cuentas_bancarias c ON c.id=m.cuenta_id "
           "WHERE m.cliente_id=? AND m.conciliado=0 AND m.pago_id IS NULL "
           "ORDER BY ABS(m.importe) DESC", (cid,))

    bloque("Factura vencida impaga", "el pago",
           "SELECT f.id, f.fecha, f.total AS importe, "
           "  (f.tipo || ' ' || COALESCE(f.punto_venta,'') || '-' || COALESCE(f.numero,'') "
           "   || ' · ' || m.razon_social) AS detalle, f.mov "
           "FROM facturas f JOIN entidades_cliente e ON e.id=f.entidad_id "
           "JOIN maestro_entidades m ON m.cuit=e.cuit "
           "WHERE f.cliente_id=? AND f.fecha < ? AND f.tipo<>'NC' "
           "AND ABS(f.total - COALESCE((SELECT SUM(a.importe) FROM pago_aplicaciones a "
           "     WHERE a.factura_id=f.id),0)) > 0.01 "
           "ORDER BY ABS(f.total) DESC", (cid, hoy))

    bloque("Cheque en cartera vencido", "depositarlo o endosarlo",
           "SELECT id, fecha_pago AS fecha, importe, "
           "  ('Nº ' || numero || ' · ' || COALESCE(banco,'sin banco')) AS detalle "
           "FROM cheques WHERE cliente_id=? AND origen='recibido' "
           "AND estado='en_cartera' AND fecha_pago < ? "
           "ORDER BY importe DESC", (cid, hoy))

    # Un cheque propio que ya venció y no aparece debitado es plata que el
    # cliente cree que salió y capaz no salió — o salió y nadie lo anotó.
    bloque("Cheque emitido vencido", "el débito en el banco",
           "SELECT id, fecha_pago AS fecha, importe, "
           "  ('Nº ' || numero || ' · ' || COALESCE(banco,'sin banco')) AS detalle "
           "FROM cheques WHERE cliente_id=? AND origen='emitido' "
           "AND estado='emitido' AND fecha_pago < ? "
           "ORDER BY importe DESC", (cid, hoy))

    bloque("Cobranza o pago a cuenta", "a qué factura se imputa",
           "SELECT p.id, p.fecha, p.total AS importe, "
           "  (COALESCE(p.numero,'recibo ' || p.id) || ' · ' || m.razon_social) AS detalle, "
           "  p.direccion "
           "FROM pagos p JOIN entidades_cliente e ON e.id=p.entidad_id "
           "JOIN maestro_entidades m ON m.cuit=e.cuit "
           "WHERE p.cliente_id=? AND NOT EXISTS "
           "  (SELECT 1 FROM pago_aplicaciones a WHERE a.pago_id=p.id) "
           "ORDER BY p.total DESC", (cid,))

    bloque("Obligación vencida", "presentarla o pagarla",
           "SELECT id, fecha, importe, "
           "  (impuesto || ' ' || periodo || ' · ' || fuente) AS detalle, estado "
           "FROM vencimientos WHERE cliente_id=? AND fecha < ? AND estado<>'pagado' "
           "ORDER BY fecha", (cid, hoy))

    con.close()
    return jsonify({
        "grupos": grupos,
        "cantidad": sum(g["n"] for g in grupos),
        "total": round(sum(g["total"] for g in grupos), 2),
        "corte": hoy,
    })


@app.get("/api/c/tesoreria/impuestos")
def api_tesoreria_impuestos():
    """La deuda impositiva, y si cada pago está atado a su débito del banco.

    Juan (23/08, en el ERP): *"que quede nomás pagado, y registrado en el
    resumen bancario"*. Ahí la deuda vivía escondida adentro de «Posición hoy»
    y la conciliación contra el banco no existía: 581 VEP pagados y ninguno
    atado a su débito. Un impuesto marcado como pagado sin el movimiento que lo
    respalde es una afirmación sin prueba."""
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    cid, hoy = cli["id"], _hoy()

    vs = filas(con.execute(
        "SELECT v.*, "
        "  (SELECT m.id FROM movimientos_banco m WHERE m.id=v.movimiento_id) AS mov_id, "
        "  (SELECT m.fecha FROM movimientos_banco m WHERE m.id=v.movimiento_id) AS mov_fecha, "
        "  (SELECT m.importe FROM movimientos_banco m WHERE m.id=v.movimiento_id) AS mov_importe "
        "FROM vencimientos v WHERE v.cliente_id=? ORDER BY v.fecha DESC", (cid,)))

    for v in vs:
        v["vencido"] = v["fecha"] < hoy and v["estado"] != "pagado"
        v["sin_respaldo"] = v["estado"] == "pagado" and not v["mov_id"]

    def suma(f):
        return round(sum(_n(v["importe"]) for v in vs if f(v)), 2)

    con.close()
    return jsonify({
        "vencimientos": vs,
        "deuda_total": suma(lambda v: v["estado"] != "pagado"),
        "vencido": suma(lambda v: v["vencido"]),
        "a_vencer": suma(lambda v: v["estado"] != "pagado" and not v["vencido"]),
        "pagado": suma(lambda v: v["estado"] == "pagado"),
        "sin_respaldo": suma(lambda v: v["sin_respaldo"]),
        "n_sin_respaldo": len([v for v in vs if v["sin_respaldo"]]),
    })


@app.post("/api/c/vencimientos/<int:vid>/pagar")
def api_vencimiento_pagar(vid):
    """Marca la obligación como pagada, atada al movimiento que la debitó.

    ⚠ El movimiento es OBLIGATORIO salvo que se diga expresamente que no lo
    hay (`sin_movimiento`). Marcar «pagado» sin el débito es lo que dejó 581
    VEP sin respaldo en el otro sistema: después nadie puede probar que se
    pagó, ni encontrar el comprobante si ARCA lo reclama."""
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    v = _de_este_cliente(con, "vencimientos", vid, cli["id"])
    if not v:
        con.close()
        return jsonify({"error": "obligación inexistente para este cliente"}), 404
    b = request.get_json(force=True)
    mov_id = b.get("movimiento_id")
    if not mov_id and not b.get("sin_movimiento"):
        con.close()
        return jsonify({"error": "falta el movimiento del banco que lo debitó "
                                 "(o decir sin_movimiento con el motivo)"}), 400
    if mov_id:
        mov = _de_este_cliente(con, "movimientos_banco", mov_id, cli["id"])
        if not mov:
            con.close()
            return jsonify({"error": "movimiento inexistente para este cliente"}), 400
        if mov["importe"] >= 0:
            con.close()
            return jsonify({"error": "un impuesto se paga con un DÉBITO, y ese "
                                     "movimiento acredita"}), 400
        con.execute("UPDATE movimientos_banco SET conciliado=1 WHERE id=?", (mov_id,))
    con.execute("UPDATE vencimientos SET estado='pagado', movimiento_id=?, "
                " nota=COALESCE(?, nota), actualizado=? WHERE id=?",
                (mov_id, b.get("nota"), _hoy(), vid))
    con.commit()
    con.close()
    return jsonify({"ok": True})


@app.get("/api/c/tesoreria/impuestos/proponer")
def api_impuestos_proponer():
    """Qué débito del banco puede ser cada obligación sin respaldo.

    Mismo criterio que la conciliación de facturas: coincide el IMPORTE y la
    fecha cae cerca. Y la misma regla — con dos candidatos NO elige sola."""
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    cid = cli["id"]
    pendientes = filas(con.execute(
        "SELECT * FROM vencimientos WHERE cliente_id=? AND movimiento_id IS NULL "
        "AND importe IS NOT NULL AND importe > 0 ORDER BY fecha DESC", (cid,)))
    out = []
    for v in pendientes:
        cand = filas(con.execute(
            "SELECT m.id, m.fecha, m.importe, m.descripcion, c.banco "
            "FROM movimientos_banco m JOIN cuentas_bancarias c ON c.id=m.cuenta_id "
            "WHERE m.cliente_id=? AND m.conciliado=0 AND m.importe < 0 "
            "AND ABS(ABS(m.importe) - ?) < 0.01 "
            "AND ABS(JULIANDAY(m.fecha) - JULIANDAY(?)) <= 10 "
            "ORDER BY ABS(JULIANDAY(m.fecha) - JULIANDAY(?))",
            (cid, _n(v["importe"]), v["fecha"], v["fecha"])))
        if cand:
            out.append({"vencimiento": v, "candidatos": cand, "unico": len(cand) == 1})
    con.close()
    return jsonify({"propuestas": out,
                    "unicos": len([o for o in out if o["unico"]]),
                    "ambiguos": len([o for o in out if not o["unico"]])})


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
            # El banco se nutre: al depósito le queda el CUIT del LIBRADOR, que
            # es de cuya cuenta salió la plata. Si no se sabe quién libró, cae
            # al CLIENTE que lo entregó — es el dato que sí tenemos y el que
            # sirve para conciliar contra sus facturas.
            cuit = ch["cuit_librador"] or (con.execute(
                "SELECT ec.cuit FROM pagos p JOIN entidades_cliente ec ON ec.id=p.entidad_id "
                "WHERE p.id=?", (ch["pago_origen_id"],)).fetchone() or {"cuit": None})["cuit"]
            if cuit:
                con.execute("UPDATE movimientos_banco SET cuit_contraparte=? WHERE id=?",
                            (cuit, mov["id"]))
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


# ══ PERCEPCIONES Y RETENCIONES ══════════════════════════════════════════════
# El catálogo, traído del ERP, para que la pantalla no invente nombres y el
# informe pueda agrupar. Cada uno dice si es PERCEPCIÓN (te la cobran de más en
# una factura) o RETENCIÓN (te la descuentan del pago) — no es lo mismo y no se
# recuperan por la misma vía.
#
# ⚠ Y el TIPO decide contra qué DJ va: la percepción de IVA es crédito en la DJ
# de IVA, la de IIBB en la de IIBB. Hasta el 03/09 acá había un solo campo
# `facturas.percepciones` con todo adentro, y la posición de IVA lo restaba
# entero — o sea, restaba de menos y mostraba menos impuesto del que hay.
TRIBUTOS = [
    ("percepcion_iva",       "Percepción IVA",        "percepcion", "iva"),
    ("percepcion_iibb",      "Percepción IIBB",       "percepcion", "iibb"),
    ("percepcion_ganancias", "Percepción Ganancias",  "percepcion", "ganancias"),
    ("retencion_iva",        "Retención IVA",         "retencion",  "iva"),
    ("retencion_iibb",       "Retención IIBB",        "retencion",  "iibb"),
    ("retencion_ganancias",  "Retención Ganancias",   "retencion",  "ganancias"),
    ("retencion_suss",       "Retención SUSS",        "retencion",  "suss"),
    ("tasa_municipal",       "Tasa municipal",        "tributo",    None),
    ("impuesto_interno",     "Impuesto interno",      "tributo",    None),
    ("sin_clasificar",       "Sin clasificar",        "tributo",    None),
    ("otro",                 "Otro",                  "tributo",    None),
]
TRIBUTOS_VALIDOS = {t[0] for t in TRIBUTOS}
# Los que son crédito computable en cada DJ.
CREDITO_IVA = ("percepcion_iva", "retencion_iva")
CREDITO_IIBB = ("percepcion_iibb", "retencion_iibb")


@app.get("/api/tributos")
def api_tributos():
    """El catálogo. Es público: no tiene datos de nadie."""
    return jsonify([{"codigo": c, "nombre": n, "clase": k, "impuesto": i}
                    for c, n, k, i in TRIBUTOS])


def _liquidar_mes(con, cid, periodo):
    """Lo que el mes genera por sí solo, sin arrastre."""
    desde, hasta = _rango_periodo(periodo)

    def por_alicuota(mov):
        return filas(con.execute(
            "SELECT COALESCE(alicuota_iva,0) AS alicuota, ROUND(SUM(neto),2) AS neto, "
            "  ROUND(SUM(iva),2) AS iva, COUNT(*) AS comprobantes FROM facturas "
            "WHERE cliente_id=? AND mov=? AND fecha BETWEEN ? AND ? GROUP BY 1 ORDER BY 1 DESC",
            (cid, mov, desde, hasta)))

    ventas, compras = por_alicuota("venta"), por_alicuota("compra")
    return {"ventas": ventas, "compras": compras}


def _cadena_iva(con, cid):
    """Recorre TODOS los meses con movimiento y encadena el saldo a favor.

    Traído del ERP (`_cadena_iva` en api/server.py), donde ya estaba resuelto.
    Un mes suelto no significa nada: el saldo a favor de julio depende de todos
    los meses anteriores.

        posición = débito − crédito − percepciones
        si posición <= 0    ->  el excedente ENGROSA el saldo a favor
        si alcanza el saldo ->  se consume y no se paga nada
        si no               ->  se paga la diferencia y el saldo queda en cero

    ⚠ ANTES ACÁ CADA MES SE LIQUIDABA SOLO (visto con datos reales el 02/09):
    mayo daba a favor $434.419,62 y junio pedía pagar el bruto, como si ese
    crédito no existiera.

    ⚠ LO QUE FALTA: las RETENCIONES de IVA sufridas. No vienen en Mis
    Comprobantes —son otra pantalla de ARCA— así que a un cliente al que le
    retienen mucho esta cuenta le va a mostrar más impuesto del que debe.
    """
    porm = {r["mes"]: r for r in con.execute(
        "SELECT substr(fecha,1,7) AS mes, "
        "  ROUND(SUM(CASE WHEN mov='venta'  THEN iva ELSE 0 END),2) AS debito, "
        "  ROUND(SUM(CASE WHEN mov='compra' THEN iva ELSE 0 END),2) AS credito, "
        "  SUM(CASE WHEN mov='venta'  THEN 1 ELSE 0 END) AS ventas, "
        "  SUM(CASE WHEN mov='compra' THEN 1 ELSE 0 END) AS compras "
        "FROM facturas WHERE cliente_id=? GROUP BY 1", (cid,))}

    # ⚠ SOLO LA PERCEPCIÓN DE **IVA** ES CRÉDITO ACÁ (corregido 03/09). Antes
    # se restaba `facturas.percepciones` entero, que es la columna «Otros
    # Tributos» de ARCA — ahí adentro hay percepción de IIBB, tasas municipales
    # e impuestos internos, que van contra otra DJ o contra ninguna. Restarlos
    # del IVA muestra MENOS impuesto del que hay, que es el error peligroso.
    marcas = ",".join("?" * len(CREDITO_IVA))
    percep_fact = {r["mes"]: _n(r["monto"]) for r in con.execute(
        f"SELECT substr(f.fecha,1,7) AS mes, ROUND(SUM(t.monto),2) AS monto "
        f"FROM factura_tributos t JOIN facturas f ON f.id=t.factura_id "
        f"WHERE f.cliente_id=? AND f.mov='compra' AND t.tipo IN ({marcas}) "
        f"GROUP BY 1", (cid, *CREDITO_IVA))}

    # La retención de IVA que nos hicieron al cobrarnos. Es crédito igual que
    # la percepción, pero entra por el RECIBO y no por la factura.
    reten = {r["mes"]: _n(r["monto"]) for r in con.execute(
        "SELECT substr(COALESCE(r.fecha, p.fecha),1,7) AS mes, ROUND(SUM(r.monto),2) AS monto "
        "FROM retenciones r LEFT JOIN pagos p ON p.id=r.pago_id "
        "WHERE r.cliente_id=? AND r.direccion='sufrida' AND r.tipo='retencion_iva' "
        "GROUP BY 1", (cid,))}

    # Lo que nadie clasificó todavía. NO computa —no se sabe de qué es— pero se
    # informa, porque puede haber crédito de IVA escondido ahí.
    sin_clas = {r["mes"]: _n(r["monto"]) for r in con.execute(
        "SELECT substr(f.fecha,1,7) AS mes, ROUND(SUM(t.monto),2) AS monto "
        "FROM factura_tributos t JOIN facturas f ON f.id=t.factura_id "
        "WHERE f.cliente_id=? AND t.tipo='sin_clasificar' GROUP BY 1", (cid,))}

    # La percepción de IVA que cobra el BANCO no está en ninguna factura: se la
    # debita a la cuenta. En el ERP sale del resumen bancario y es plata que
    # también resta de la posición, así que si el movimiento está clasificado
    # como percepción de IVA, entra acá.
    banco = {r["mes"]: _n(r["monto"]) for r in con.execute(
        "SELECT substr(fecha,1,7) AS mes, ROUND(SUM(-importe),2) AS monto "
        "FROM movimientos_banco WHERE cliente_id=? AND importe < 0 "
        # No hay tabla de percepciones: lo único que hay es lo que escribe
        # el banco en el concepto. Es tosco, pero es el dato que existe — y
        # dejarlo afuera sería restar de menos.
        "  AND LOWER(COALESCE(descripcion,'')) LIKE '%perc%iva%' "
        "GROUP BY 1", (cid,))}

    # De dónde arranca y con cuánto venía. Es un dato DECLARADO: sale de la
    # última DJ que presentó el cliente antes de que el estudio lo tomara, y no
    # hay comprobante del que deducirlo.
    ini = con.execute("SELECT periodo, a_favor FROM iva_saldo_inicial WHERE cliente_id=?",
                      (cid,)).fetchone()
    saldo = _n(ini["a_favor"]) if ini else 0.0
    arranque = f"{ini['periodo'][3:]}-{ini['periodo'][:2]}" if ini else None

    meses = sorted(set(porm) | set(banco) | set(reten)
                   | ({arranque} if arranque else set()))
    if arranque:
        meses = [m for m in meses if m >= arranque]

    serie = []
    for mes in meses:
        d = porm.get(mes)
        debito = _n(d["debito"]) if d else 0.0
        credito = _n(d["credito"]) if d else 0.0
        percep = round(percep_fact.get(mes, 0.0) + reten.get(mes, 0.0)
                       + banco.get(mes, 0.0), 2)
        posicion = round(debito - credito - percep, 2)
        anterior, a_pagar = saldo, 0.0
        if posicion <= 0:
            saldo = round(saldo + abs(posicion), 2)
        elif saldo >= posicion:
            saldo = round(saldo - posicion, 2)
        else:
            a_pagar, saldo = round(posicion - saldo, 2), 0.0
        serie.append({
            "periodo": mes, "debito": debito, "credito": credito,
            "percepciones": percep, "percepciones_banco": banco.get(mes, 0.0),
            "retenciones_iva": reten.get(mes, 0.0),
            "sin_clasificar": sin_clas.get(mes, 0.0),
            "posicion": posicion, "saldo_favor_anterior": round(anterior, 2),
            "a_pagar": a_pagar, "saldo_favor_final": saldo,
            "ventas": (d["ventas"] if d else 0) or 0,
            "compras": (d["compras"] if d else 0) or 0,
        })
    return serie


@app.get("/api/c/iva")
def api_iva():
    """La liquidación de un período, con su lugar en la cadena.

    Trae el detalle por alícuota (para controlarlo contra el papel) y las tres
    cifras que importan: lo que el mes genera, lo que venía arrastrado, y lo
    que realmente hay que pagar."""
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    periodo = (request.args.get("periodo") or "").strip()
    if not _rango_periodo(periodo):
        con.close()
        return jsonify({"error": "falta ?periodo=MM/YYYY"}), 400
    mes_iso = f"{periodo[3:]}-{periodo[:2]}"

    d = _liquidar_mes(con, cli["id"], periodo)
    serie = _cadena_iva(con, cli["id"])
    f = next((x for x in serie if x["periodo"] == mes_iso), None) or {
        "debito": 0.0, "credito": 0.0, "percepciones": 0.0, "percepciones_banco": 0.0,
        "posicion": 0.0, "saldo_favor_anterior": 0.0, "a_pagar": 0.0,
        "saldo_favor_final": 0.0}
    con.close()
    return jsonify({
        "periodo": periodo,
        "debito_fiscal": f["debito"], "credito_fiscal": f["credito"],
        "percepciones_sufridas": f["percepciones"],
        "percepciones_banco": f["percepciones_banco"],
        "posicion": f["posicion"],
        "a_favor_anterior": f["saldo_favor_anterior"],
        "a_pagar": f["a_pagar"], "a_favor_final": f["saldo_favor_final"],
        # `saldo` y `resultado` se mantienen porque ya los usa la pantalla.
        "saldo": f["a_pagar"] if f["a_pagar"] else f["saldo_favor_final"],
        "resultado": "a pagar" if f["a_pagar"] else (
            "a favor" if f["saldo_favor_final"] else "en cero"),
        "ventas_por_alicuota": d["ventas"], "compras_por_alicuota": d["compras"],
        "falta": "las retenciones de IVA sufridas no están cargadas: no vienen "
                 "en Mis Comprobantes",
    })


@app.get("/api/c/iva/posicion")
def api_iva_posicion():
    """La serie entera, mes a mes, con el saldo a favor encadenado.

    Es la pestaña «Posición de IVA» del módulo Facturas del ERP: se mira la
    cadena completa, no un mes, porque el arrastre es lo que explica por qué un
    mes con mucho débito puede terminar sin nada que pagar."""
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    serie = _cadena_iva(con, cli["id"])
    ini = con.execute("SELECT periodo, a_favor, nota FROM iva_saldo_inicial WHERE cliente_id=?",
                      (cli["id"],)).fetchone()
    con.close()
    ultimo = serie[-1] if serie else None
    return jsonify({
        "serie": list(reversed(serie)),          # el mes más nuevo primero
        "actual": ultimo,
        "saldo_favor_hoy": ultimo["saldo_favor_final"] if ultimo else 0.0,
        "total_a_pagar": round(sum(f["a_pagar"] for f in serie), 2),
        "inicial": dict(ini) if ini else None,
        "nota": "Posición = débito − crédito − percepciones. Si da negativa "
                "engrosa el saldo a favor; si da positiva primero consume el "
                "arrastrado y recién después se paga.",
        "falta": "No están cargadas las RETENCIONES de IVA sufridas: no vienen "
                 "en Mis Comprobantes, son otra pantalla de ARCA. A un cliente "
                 "al que le retienen mucho, esta cuenta le muestra más impuesto "
                 "del que debe.",
    })


@app.post("/api/c/iva/inicial")
def api_iva_inicial_set():
    """Desde qué período liquida el estudio y con cuánto a favor venía.

    Alguien lo tiene que declarar: sale de la última DJ presentada antes de que
    el estudio tomara al cliente. Sin esto, el arrastre arranca en cero y le
    hace perder al cliente un crédito que tenía."""
    con = db()
    cli, err = cliente_activo(con)
    if err:
        con.close()
        return err
    b = request.get_json(force=True)
    periodo = (b.get("periodo") or "").strip()
    if not _rango_periodo(periodo):
        con.close()
        return jsonify({"error": "periodo debe ser MM/YYYY"}), 400
    a_favor = _n(b.get("a_favor"))
    if a_favor < 0:
        con.close()
        return jsonify({"error": "el saldo a favor no puede ser negativo"}), 400
    con.execute(
        "INSERT INTO iva_saldo_inicial (cliente_id, periodo, a_favor, nota, actualizado) "
        "VALUES (?,?,?,?,?) ON CONFLICT(cliente_id) DO UPDATE SET "
        "  periodo=excluded.periodo, a_favor=excluded.a_favor, nota=excluded.nota, "
        "  actualizado=excluded.actualizado",
        (cli["id"], periodo, a_favor, b.get("nota"), _hoy()))
    con.commit()
    con.close()
    return jsonify({"ok": True})


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
    determinado = round(sum(b["impuesto"] for b in bases), 2)

    # ── LAS DEDUCCIONES DE IIBB (03/09) ──────────────────────────────────────
    # El impuesto determinado NO es lo que se paga: primero se descuentan las
    # percepciones y retenciones de IIBB. Son las MISMAS tablas que las de IVA
    # pero con el tipo de IIBB — mezclarlas era el error que corregimos: cada
    # tributo va contra la DJ de SU impuesto.
    marcas = ",".join("?" * len(CREDITO_IIBB))
    perc_iibb = _n(con.execute(
        f"SELECT COALESCE(SUM(t.monto),0) FROM factura_tributos t "
        f"JOIN facturas f ON f.id=t.factura_id "
        f"WHERE f.cliente_id=? AND f.fecha BETWEEN ? AND ? AND t.tipo IN ({marcas})",
        (cli["id"], desde, hasta, *CREDITO_IIBB)).fetchone()[0])
    ret_iibb = _n(con.execute(
        "SELECT COALESCE(SUM(r.monto),0) FROM retenciones r LEFT JOIN pagos p ON p.id=r.pago_id "
        "WHERE r.cliente_id=? AND r.direccion='sufrida' AND r.tipo='retencion_iibb' "
        "AND COALESCE(r.fecha, p.fecha) BETWEEN ? AND ?",
        (cli["id"], desde, hasta)).fetchone()[0])
    # Lo que nadie clasificó: no se descuenta —no se sabe de qué es— pero se
    # avisa, porque puede haber percepción de IIBB escondida ahí.
    sin_clas = _n(con.execute(
        "SELECT COALESCE(SUM(t.monto),0) FROM factura_tributos t "
        "JOIN facturas f ON f.id=t.factura_id "
        "WHERE f.cliente_id=? AND f.fecha BETWEEN ? AND ? AND t.tipo='sin_clasificar'",
        (cli["id"], desde, hasta)).fetchone()[0])
    deducciones = round(perc_iibb + ret_iibb, 2)
    saldo = round(determinado - deducciones, 2)
    con.close()
    return jsonify({
        "periodo": request.args["periodo"], "jurisdiccion": jur,
        "bases": bases,
        "impuesto_determinado": determinado,
        "percepciones_iibb": perc_iibb, "retenciones_iibb": ret_iibb,
        "deducciones": deducciones,
        "saldo": abs(saldo),
        "resultado": "a pagar" if saldo > 0 else ("a favor" if saldo < 0 else "en cero"),
        "sin_clasificar": sin_clas,
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
            # El criterio único de IVA (Juan, 26/08): TODO comprobante muestra
            # los tres números —neto · IVA · total—; todo PAGO es por el total,
            # un solo número. Son dos naturalezas distintas: el comprobante
            # devenga (y su IVA es crédito o débito), el pago solo cancela.
            "neto": f["neto"], "iva": f["iva"],
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


