-- ERBEN ESTUDIO — esquema base (decisión de entidades CONFIRMADA por Juan, 2026-08-17)
--
-- El modelo en una línea: el MAESTRO por CUIT es uno solo (datos públicos);
-- la RELACIÓN de cada cliente con sus proveedores/clientes es una fila por
-- (cliente_id, cuit) que apunta al maestro — nunca una copia de la ficha.
--
-- Regla de aislamiento (ARQUITECTURA.md §1): toda tabla operativa lleva
-- cliente_id NOT NULL y la API filtra SIEMPRE por el cliente activo de la
-- sesión. Ninguna query junta filas de dos clientes.

-- ── 1. MAESTRO transversal — datos PÚBLICOS por CUIT ─────────────────────────
-- Lo que dice la constancia de inscripción: identidad, no operatoria.
-- La identidad va SIEMPRE por CUIT (precedente EPHISET/EPISHET: la misma
-- entidad escrita de dos formas duplica gente — el nombre es solo para leer).
CREATE TABLE maestro_entidades (
    cuit            TEXT PRIMARY KEY,          -- 11 dígitos, sin guiones
    razon_social    TEXT NOT NULL,
    nombre_fantasia TEXT,
    tipo_persona    TEXT,                      -- fisica | juridica
    condicion_iva   TEXT,                      -- RI | monotributo | exento | CF
    domicilio       TEXT,
    provincia       TEXT,
    origen          TEXT NOT NULL DEFAULT 'manual',  -- arca | atp | manual
    actualizado     TEXT NOT NULL              -- ISO fecha del último refresco
);

-- Actividades del maestro, POR JURISDICCIÓN — porque el mismo CUIT tiene
-- CLAE en ARCA y NAES en ATP/DGR, con alícuotas propias de cada provincia.
-- Las cargan los jobs (atp_iibb.py trae el padrón entero; actividades.py, ARCA).
CREATE TABLE maestro_actividades (
    cuit         TEXT NOT NULL REFERENCES maestro_entidades(cuit),
    jurisdiccion TEXT NOT NULL,                -- arca | DGR-Fsa | DGR-Ctes | ...
    codigo       TEXT NOT NULL,                -- CLAE u NAES según jurisdicción
    nombre       TEXT,
    alicuota     REAL,                         -- NULL en ARCA; en IIBB el PAR
                                               -- codigo+alicuota es la fila real
                                               -- (562010 vive al 3, 5 y 15%)
    principal    INTEGER NOT NULL DEFAULT 0,
    exento       INTEGER NOT NULL DEFAULT 0,
    inicio       TEXT,
    PRIMARY KEY (cuit, jurisdiccion, codigo, alicuota)
);

-- ── 2. Los clientes del estudio ──────────────────────────────────────────────
-- Cliente = CUIT. Todo cliente TIENE ficha en el maestro (su propia identidad
-- también es un dato público). Acá va solo lo operativo del estudio.
CREATE TABLE clientes (
    id       INTEGER PRIMARY KEY,
    cuit     TEXT NOT NULL UNIQUE REFERENCES maestro_entidades(cuit),
    alias    TEXT NOT NULL UNIQUE,             -- RODRIGUEZ, ... (para jobs y UI)
    activo   INTEGER NOT NULL DEFAULT 1,
    alta     TEXT NOT NULL,
    nota     TEXT
);

-- ── 3. La RELACIÓN comercial — POR CLIENTE, lo confidencial ──────────────────
-- "cliente tiene relación con proveedor 3 y cliente 7" (Juan): cada fila es
-- ESA relación, apuntando al maestro. Nunca se lee sin cliente_id fijo.
CREATE TABLE entidades_cliente (
    id            INTEGER PRIMARY KEY,
    cliente_id    INTEGER NOT NULL REFERENCES clientes(id),
    cuit          TEXT NOT NULL REFERENCES maestro_entidades(cuit),
    es_proveedor  INTEGER NOT NULL DEFAULT 0,  -- puede ser ambos: no es enum
    es_cliente    INTEGER NOT NULL DEFAULT 0,
    alias_interno TEXT,                        -- cómo LO llama este cliente
    condicion_pago TEXT,
    nota          TEXT,
    alta          TEXT NOT NULL,
    UNIQUE (cliente_id, cuit)
);
CREATE INDEX ix_entcli_cliente ON entidades_cliente(cliente_id);

-- La cuenta corriente y los saldos NO viven acá: se derivan de las tablas
-- operativas (facturas, recibos, cheques), que llevan su propio cliente_id y
-- referencian entidades_cliente(id). Guardar el saldo en la relación es
-- invitarlo a desincronizarse.

-- ── 4. Plantilla de tabla operativa (el patrón para TODOS los módulos) ───────
-- Ejemplo con facturas; bancos/cheques/tesorería siguen el mismo molde:
--   cliente_id NOT NULL + FK a entidades_cliente del MISMO cliente.
CREATE TABLE facturas (
    id             INTEGER PRIMARY KEY,
    cliente_id     INTEGER NOT NULL REFERENCES clientes(id),
    entidad_id     INTEGER NOT NULL REFERENCES entidades_cliente(id),
    mov            TEXT NOT NULL,              -- compra | venta
    fecha          TEXT NOT NULL,
    tipo           TEXT, letra TEXT, punto_venta TEXT, numero TEXT, cae TEXT,
    total          REAL NOT NULL,
    -- IIBB (ARQUITECTURA.md §4): el PAR actividad+alícuota de la venta.
    -- Default al cargar: la actividad PRINCIPAL del cliente en su jurisdicción.
    -- Control antes de armar la DJ: Σ bases por actividad == Σ ventas.
    iibb_jurisdiccion TEXT,
    iibb_codigo       TEXT,
    iibb_alicuota     REAL
);
CREATE INDEX ix_fact_cliente ON facturas(cliente_id, fecha);
