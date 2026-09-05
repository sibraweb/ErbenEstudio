-- ERBEN ESTUDIO — esquema completo
-- Decisión de entidades CONFIRMADA por Juan (2026-08-17):
--   el MAESTRO por CUIT es uno solo (datos públicos); la RELACIÓN de cada
--   cliente con sus proveedores/clientes es una fila (cliente_id, cuit) que
--   APUNTA al maestro — nunca una copia de la ficha.
--
-- Estructura pedida por Juan (2026-08-17, segunda tanda):
--   facturas · impuestos/vencimientos ARCA + liquidar IVA · DJ de IIBB de
--   varias provincias · movimientos bancarios · cheques (recibidos SOLO de
--   cobranzas, que se depositan o endosan; emitidos propios) · pagos de
--   facturas usando movimientos de banco y cheques · el banco se va nutriendo
--   de los comprobantes (CUIT, factura, recibo) · conciliación automática de
--   lo que se pueda.
--
-- Regla de aislamiento (ARQUITECTURA.md §1): toda tabla operativa lleva
-- cliente_id NOT NULL y la API filtra SIEMPRE por el cliente activo. Ninguna
-- query junta filas de dos clientes.

-- ══ 1. MAESTRO transversal — datos PÚBLICOS por CUIT ════════════════════════
-- Lo que dice la constancia de inscripción: identidad, no operatoria.
-- La identidad va SIEMPRE por CUIT: ya nos pasó duplicar una empresa por
-- una letra de diferencia en el nombre. El nombre es solo para leer.
CREATE TABLE maestro_entidades (
    -- El documento que identifica: 11 dígitos = CUIT, 7-8 = DNI (una
    -- factura B a una persona física solo trae el DNI, y el CUIT no se
    -- deduce sin inventar el prefijo y el verificador).
    cuit            TEXT PRIMARY KEY,          -- sin guiones
    razon_social    TEXT NOT NULL,
    nombre_fantasia TEXT,
    tipo_persona    TEXT,                      -- fisica | juridica
    condicion_iva   TEXT,                      -- RI | monotributo | exento | CF
    domicilio       TEXT,
    provincia       TEXT,
    origen          TEXT NOT NULL DEFAULT 'manual',  -- arca | atp | manual
    actualizado     TEXT NOT NULL
);

-- Actividades del maestro POR JURISDICCIÓN: el mismo CUIT tiene CLAE en ARCA
-- y NAES en ATP/DGR, con alícuotas propias de cada provincia. Las cargan los
-- jobs (atp_iibb.py trae el padrón entero; actividades.py, ARCA).
CREATE TABLE maestro_actividades (
    cuit         TEXT NOT NULL REFERENCES maestro_entidades(cuit),
    jurisdiccion TEXT NOT NULL,                -- arca | DGR-Fsa | DGR-Ctes | ...
    codigo       TEXT NOT NULL,                -- CLAE o NAES según jurisdicción
    nombre       TEXT,
    alicuota     REAL,                         -- en IIBB el PAR codigo+alicuota
                                               -- es la fila real (562010 vive
                                               -- al 3, 5 y 15%)
    principal    INTEGER NOT NULL DEFAULT 0,
    exento       INTEGER NOT NULL DEFAULT 0,
    inicio       TEXT,
    PRIMARY KEY (cuit, jurisdiccion, codigo, alicuota)
);

-- ══ 2. Clientes del estudio ═════════════════════════════════════════════════
CREATE TABLE clientes (
    id       INTEGER PRIMARY KEY,
    cuit     TEXT NOT NULL UNIQUE REFERENCES maestro_entidades(cuit),
    alias    TEXT NOT NULL UNIQUE,             -- DEMO, ... (jobs y UI)
    activo   INTEGER NOT NULL DEFAULT 1,
    alta     TEXT NOT NULL,
    nota     TEXT
);

-- Jurisdicciones donde el cliente está inscripto (una DJ por cada una).
-- "la posibilidad de declarar IIBB de diferentes provincias" (Juan).
CREATE TABLE cliente_jurisdicciones (
    cliente_id      INTEGER NOT NULL REFERENCES clientes(id),
    jurisdiccion    TEXT NOT NULL,             -- DGR-Fsa | DGR-Ctes | ...
    nro_inscripcion TEXT,
    regimen         TEXT,                      -- local | convenio_multilateral
    coeficiente     REAL,                      -- solo CM: el unificado
    alta            TEXT,
    PRIMARY KEY (cliente_id, jurisdiccion)
);

-- ══ 3. RELACIÓN comercial — POR CLIENTE, lo confidencial ════════════════════
CREATE TABLE entidades_cliente (
    id             INTEGER PRIMARY KEY,
    cliente_id     INTEGER NOT NULL REFERENCES clientes(id),
    cuit           TEXT NOT NULL REFERENCES maestro_entidades(cuit),
    es_proveedor   INTEGER NOT NULL DEFAULT 0, -- puede ser ambos: no es enum
    es_cliente     INTEGER NOT NULL DEFAULT 0,
    alias_interno  TEXT,
    condicion_pago TEXT,
    nota           TEXT,
    alta           TEXT NOT NULL,
    UNIQUE (cliente_id, cuit)
);
CREATE INDEX ix_entcli_cliente ON entidades_cliente(cliente_id);
-- La cuenta corriente y los saldos NO viven acá: se derivan de facturas y
-- pagos. Guardar el saldo en la relación es invitarlo a desincronizarse.

-- ══ 4. FACTURAS ═════════════════════════════════════════════════════════════
-- Compras y ventas. Los campos de IVA existen para liquidar (crédito/débito)
-- y los de IIBB para armar la DJ por actividad.
CREATE TABLE facturas (
    id             INTEGER PRIMARY KEY,
    cliente_id     INTEGER NOT NULL REFERENCES clientes(id),
    entidad_id     INTEGER NOT NULL REFERENCES entidades_cliente(id),
    mov            TEXT NOT NULL,              -- compra | venta
    fecha          TEXT NOT NULL,              -- ISO
    tipo           TEXT,                       -- FA | NC | ND | ...
    letra          TEXT,
    punto_venta    TEXT,
    numero         TEXT,
    cae            TEXT,
    -- Importes: total = neto + iva + no_gravado + exento + percepciones.
    -- La NC resta: se guarda con signo NEGATIVO (tipo='NC'), así toda suma de
    -- un período es una suma y nadie tiene que acordarse de restar.
    neto           REAL NOT NULL DEFAULT 0,
    alicuota_iva   REAL,                       -- 21 | 10.5 | 27 | 0
    iva            REAL NOT NULL DEFAULT 0,
    no_gravado     REAL NOT NULL DEFAULT 0,
    exento         REAL NOT NULL DEFAULT 0,
    percepciones   REAL NOT NULL DEFAULT 0,
    total          REAL NOT NULL,
    -- IIBB (ARQUITECTURA.md §4): el PAR actividad+alícuota de la venta.
    -- Default al cargar: la actividad PRINCIPAL del cliente.
    -- Control antes de la DJ: Σ bases por actividad == Σ ventas.
    iibb_jurisdiccion TEXT,
    iibb_codigo       TEXT,
    iibb_alicuota     REAL,
    origen         TEXT NOT NULL DEFAULT 'manual',  -- manual | arca | import
    nota           TEXT
);
CREATE INDEX ix_fact_cliente ON facturas(cliente_id, fecha);
CREATE INDEX ix_fact_entidad ON facturas(entidad_id);

-- ══ 5. BANCOS ═══════════════════════════════════════════════════════════════
CREATE TABLE cuentas_bancarias (
    id          INTEGER PRIMARY KEY,
    cliente_id  INTEGER NOT NULL REFERENCES clientes(id),
    banco       TEXT NOT NULL,
    -- El código de entidad del BCRA: los 3 primeros dígitos del CBU. Es lo que
    -- convierte al banco en un DATO y no en un texto libre — sin esto conviven
    -- «BANCO DE FORMOSA», «Bco Formosa» y «formosa» como tres bancos distintos.
    codigo_bcra TEXT,
    titular     TEXT,                          -- a nombre de quién está
    tipo        TEXT,                          -- CC | CA
    numero      TEXT,
    cbu         TEXT,
    moneda      TEXT NOT NULL DEFAULT 'ARS',
    alias_banco TEXT,
    activa      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX ix_ctas_cliente ON cuentas_bancarias(cliente_id);

-- El extracto. `importe` con signo: + acredita, − debita.
-- cuit_contraparte / pago_id son los campos que se VAN LLENANDO desde los
-- comprobantes de pago (Juan: "el banco se va nutriendo de los comprobantes
-- de pago y se va llenando de CUIT, factura, recibo").
CREATE TABLE movimientos_banco (
    id           INTEGER PRIMARY KEY,
    cliente_id   INTEGER NOT NULL REFERENCES clientes(id),
    cuenta_id    INTEGER NOT NULL REFERENCES cuentas_bancarias(id),
    fecha        TEXT NOT NULL,
    descripcion  TEXT,
    importe      REAL NOT NULL,
    saldo        REAL,
    referencia   TEXT,                         -- nro de operación del banco
    origen       TEXT NOT NULL DEFAULT 'manual',   -- extracto | job | manual
    cuit_contraparte TEXT,
    pago_id      INTEGER,
    conciliado   INTEGER NOT NULL DEFAULT 0,
    -- LA DOBLE LLAVE contra duplicados al reimportar un extracto.
    -- `huella` = hash de (fecha, importe, descripcion, referencia).
    -- ⚠ NO incluye el SALDO, y eso es deliberado: el ERP lo incluía y ataba
    -- la fila a su posición en la cadena, así que un asiento retroactivo del
    -- banco corría todos los saldos y al reimportar entraba duplicada TODA la
    -- cola del mes.
    -- `ordinal` distingue los repetidos legítimos: dos débitos idénticos el
    -- mismo día existen, y con la huella sola el segundo se perdería.
    huella       TEXT NOT NULL DEFAULT '',
    ordinal      INTEGER NOT NULL DEFAULT 0,
    UNIQUE (cliente_id, cuenta_id, huella, ordinal)
);
CREATE INDEX ix_movb_cliente ON movimientos_banco(cliente_id, fecha);
CREATE INDEX ix_movb_pend ON movimientos_banco(cliente_id, conciliado);

-- ══ 6. CHEQUES ══════════════════════════════════════════════════════════════
-- Juan: "los recibidos, que se pueden depositar o endosar, VIENEN DE
-- COBRANZAS ÚNICAMENTE, y los emitidos que son de la empresa".
--   recibido → nace SIEMPRE de una cobranza (pago_origen_id).
--              en_cartera → depositado | endosado | rechazado
--   emitido  → propio del cliente, sale en una orden de pago.
--              emitido → debitado | rechazado | anulado
CREATE TABLE cheques (
    id             INTEGER PRIMARY KEY,
    cliente_id     INTEGER NOT NULL REFERENCES clientes(id),
    origen         TEXT NOT NULL,              -- recibido | emitido
    numero         TEXT NOT NULL,
    banco          TEXT,
    cuit_librador  TEXT,                       -- recibido: quién lo firmó
    -- El librador puede no estar en el maestro (te dan un cheque de alguien con
    -- quien no operás). Sin este campo, el nombre se perdía y quedaba un CUIT
    -- pelado en la grilla — o peor, nada.
    librador_nombre TEXT,
    -- A quién se lo dimos, en los EMITIDOS. En los recibidos ese rol lo ocupa
    -- endoso_entidad_id (el proveedor al que se endosó).
    beneficiario_entidad_id INTEGER REFERENCES entidades_cliente(id),
    cuenta_id      INTEGER REFERENCES cuentas_bancarias(id),  -- emitido: chequera
    fecha_emision  TEXT,
    fecha_pago     TEXT NOT NULL,              -- el vencimiento
    importe        REAL NOT NULL,
    estado         TEXT NOT NULL,
    -- trazabilidad del recibido:
    pago_origen_id     INTEGER,                -- la COBRANZA que lo trajo
    pago_uso_id        INTEGER,                -- el PAGO donde se endosó
    deposito_cuenta_id INTEGER REFERENCES cuentas_bancarias(id),
    deposito_fecha     TEXT,
    endoso_entidad_id  INTEGER REFERENCES entidades_cliente(id),
    nota           TEXT,
    UNIQUE (cliente_id, origen, banco, numero)
);
CREATE INDEX ix_chq_cliente ON cheques(cliente_id, estado);
CREATE INDEX ix_chq_venc ON cheques(cliente_id, fecha_pago);

-- ══ 7. PAGOS Y COBRANZAS ════════════════════════════════════════════════════
-- Un comprobante tiene UNA dirección (o cancela compras o cancela ventas):
-- netear esconde que alguien te debe. Misma regla que el ERP.
CREATE TABLE pagos (
    id          INTEGER PRIMARY KEY,
    cliente_id  INTEGER NOT NULL REFERENCES clientes(id),
    entidad_id  INTEGER NOT NULL REFERENCES entidades_cliente(id),
    direccion   TEXT NOT NULL,                 -- cobro (ventas) | pago (compras)
    fecha       TEXT NOT NULL,
    numero      TEXT,                          -- nro de recibo / orden de pago
    total       REAL NOT NULL,
    nota        TEXT
);
CREATE INDEX ix_pagos_cliente ON pagos(cliente_id, fecha);

-- Con qué se pagó: efectivo, transferencia (un movimiento de banco) o cheque.
CREATE TABLE pago_medios (
    id            INTEGER PRIMARY KEY,
    pago_id       INTEGER NOT NULL REFERENCES pagos(id) ON DELETE CASCADE,
    medio         TEXT NOT NULL,               -- efectivo | transferencia | cheque
    importe       REAL NOT NULL,
    movimiento_id INTEGER REFERENCES movimientos_banco(id),
    cheque_id     INTEGER REFERENCES cheques(id)
);

-- Qué facturas cancela (y por cuánto: un pago puede ser parcial).
CREATE TABLE pago_aplicaciones (
    id         INTEGER PRIMARY KEY,
    pago_id    INTEGER NOT NULL REFERENCES pagos(id) ON DELETE CASCADE,
    factura_id INTEGER NOT NULL REFERENCES facturas(id),
    importe    REAL NOT NULL,
    UNIQUE (pago_id, factura_id)
);

-- ══ 8. PERCEPCIONES Y RETENCIONES ═══════════════════════════════════════════
-- Traído del ERP (`comprobante_tributos` y `retenciones_sufridas`), donde el
-- modelo ya estaba resuelto contra las pantallas de Xubio.
--
-- ⚠ NO ES LO MISMO UNA PERCEPCIÓN QUE UNA RETENCIÓN, y no se recuperan igual:
--   PERCEPCIÓN  te la COBRAN de más en una factura. Viene en el comprobante.
--   RETENCIÓN   te la DESCUENTAN del pago. Viene en el recibo, con certificado.
--
-- ⚠ Y NO ES LO MISMO percepción de IVA que de IIBB: la primera es crédito en la
-- DJ de IVA, la segunda en la de IIBB. Sumarlas en un solo campo —como estaba
-- hasta el 03/09— hace que la posición de IVA reste cosas que no le tocan.

-- El desglose de una factura. `facturas.percepciones` sigue siendo el TOTAL que
-- trae el comprobante (lo que ARCA llama «Otros Tributos»); acá se dice de qué
-- es cada peso. Lo que no está clasificado no se computa en ninguna DJ, y la
-- pantalla lo muestra como hueco.
CREATE TABLE factura_tributos (
    id           INTEGER PRIMARY KEY,
    factura_id   INTEGER NOT NULL REFERENCES facturas(id) ON DELETE CASCADE,
    tipo         TEXT NOT NULL,   -- ver TRIBUTOS en server.py
    jurisdiccion TEXT,            -- la provincia o el municipio, cuando aplica
    base         REAL,            -- sobre qué se calculó, si se sabe
    alicuota     REAL,
    monto        REAL NOT NULL,
    detalle      TEXT
);
CREATE INDEX ix_facttrib ON factura_tributos(factura_id);

-- Las retenciones, colgadas del recibo.
--
-- ⚠ EL AGUJERO QUE ESTO TAPA (relevado en el ERP, caso Exponenciar): una
-- factura de $12.717.601 se cobra con $12.488.762 en el banco. Los $228.839 de
-- diferencia no son un descuento: son plata que el cliente le pagó al fisco en
-- nombre nuestro. Sin registrarla, la factura queda impaga por ese resto para
-- siempre y la cuenta corriente miente.
--
--     importe aplicado a las facturas = medios de pago + retenciones
--
-- Las dos direcciones NO son simétricas contablemente:
--   'sufrida'    en un COBRO: nos retuvieron. Es CRÉDITO fiscal, va a la DJ.
--   'practicada' en un PAGO: retuvimos al proveedor. Es un PASIVO — esa plata
--                no es del cliente, hay que depositarla y darle el certificado.
CREATE TABLE retenciones (
    id             INTEGER PRIMARY KEY,
    cliente_id     INTEGER NOT NULL REFERENCES clientes(id),
    pago_id        INTEGER REFERENCES pagos(id) ON DELETE CASCADE,
    direccion      TEXT NOT NULL DEFAULT 'sufrida',
    entidad_id     INTEGER REFERENCES entidades_cliente(id),
    fecha          TEXT,
    tipo           TEXT NOT NULL,
    -- El RÉGIMEN dentro del tipo es lo que define la alícuota y el mínimo. En
    -- un certificado: «094 Gcias.: locaciones de obras». Sin esto, "retención
    -- de ganancias" no dice si el 2% estaba bien.
    codigo_regimen TEXT,
    concepto       TEXT,
    jurisdiccion   TEXT,
    -- El importe SUJETO a retención: no es el total de la factura ni el neto,
    -- es el neto menos el mínimo no imponible del régimen.
    base           REAL,
    alicuota       REAL,
    monto          REAL NOT NULL,
    certificado    TEXT,          -- sin este número no se computa en la DJ
    computada      INTEGER NOT NULL DEFAULT 0,
    detalle        TEXT
);
CREATE INDEX ix_reten_cliente ON retenciones(cliente_id, fecha);
CREATE INDEX ix_reten_pago ON retenciones(pago_id);

-- ══ 8. CONCILIACIÓN ═════════════════════════════════════════════════════════
-- Juan: "que lo que se pueda conciliar solo lo haga: cheques con movimiento
-- de banco, y facturas que coincidan monto, entorno de fecha y CUIT".
CREATE TABLE conciliaciones (
    id            INTEGER PRIMARY KEY,
    cliente_id    INTEGER NOT NULL REFERENCES clientes(id),
    movimiento_id INTEGER NOT NULL REFERENCES movimientos_banco(id),
    tipo          TEXT NOT NULL,               -- cheque | pago | factura
    cheque_id     INTEGER REFERENCES cheques(id),
    pago_id       INTEGER REFERENCES pagos(id),
    factura_id    INTEGER REFERENCES facturas(id),
    metodo        TEXT NOT NULL,               -- auto | manual
    motivo        TEXT,                        -- por qué matcheó (auditoría)
    fecha         TEXT NOT NULL,
    UNIQUE (movimiento_id)
);
CREATE INDEX ix_conc_cliente ON conciliaciones(cliente_id);

-- ══ 9. IMPUESTOS ════════════════════════════════════════════════════════════
-- Vencimientos (ARCA y las provincias). Los cargan los jobs
-- (vencimientos_arca.py, cct_vencimientos.py, atp_iibb.py) o la mano.
-- ⚠ El estado NO es una etiqueta suelta: es un CICLO que avanza. Lo aprendió
-- el ERP leyendo las tres pestañas del CCT de ARCA, que son exactamente los
-- tres momentos de la misma obligación:
--
--     a_vencer                 la fecha todavía no pasó
--        ↓ pasa la fecha sin presentar
--     vencida_sin_presentar    sigue siendo una FECHA
--        ↓ se presenta la DJ
--     dj_a_pagar               ya no es fecha: es PLATA, con su importe
--        ↓ se paga
--     pagado
--
-- ⚠⚠ Las tres pestañas de ARCA SE SOLAPAN: la misma obligación aparece en dos
-- a la vez. Al cargar gana el estado MÁS AVANZADO — una obligación está en un
-- solo momento del ciclo, si no el mismo monotributo sale verde y rojo juntos.
CREATE TABLE vencimientos (
    id         INTEGER PRIMARY KEY,
    cliente_id INTEGER NOT NULL REFERENCES clientes(id),
    fuente     TEXT NOT NULL,                  -- arca | DGR-Fsa | DGR-Ctes | ...
    impuesto   TEXT NOT NULL,                  -- IVA | IIBB | Ganancias | ...
    -- El CÓDIGO es lo estable: el nombre que muestra el portal cambia de
    -- redacción entre versiones, el código no. Por él se saben cuáles no
    -- llevan DJ (monotributo y compañía: la deuda se genera sola).
    codigo     TEXT,
    periodo    TEXT NOT NULL,                  -- MM/YYYY
    fecha      TEXT NOT NULL,
    importe    REAL,
    estado     TEXT NOT NULL DEFAULT 'a_vencer',
    -- ⚠ El movimiento que lo DEBITÓ. Marcar «pagado» sin esto es una
    -- afirmación sin prueba: en el otro sistema quedaron 581 VEP pagados y
    -- ninguno atado a su débito, y después no había cómo demostrar el pago.
    movimiento_id INTEGER REFERENCES movimientos_banco(id),
    nota       TEXT,
    actualizado TEXT,
    UNIQUE (cliente_id, fuente, impuesto, periodo)
);
CREATE INDEX ix_venc_cliente ON vencimientos(cliente_id, fecha);

-- Declaraciones juradas (IVA e IIBB por jurisdicción). El detalle por
-- actividad se recalcula siempre de las facturas; acá queda la FOTO de lo
-- presentado, que es lo que hay que poder mostrar aunque después se corrija
-- una factura vieja.
-- ══ EL SALDO A FAVOR DE IVA QUE VIENE DE ANTES ═══════════════════════════════
-- Un saldo a favor no se pierde: pasa al mes siguiente hasta consumirse. Pero
-- el primer mes que liquida el estudio arrastra algo que ningun comprobante
-- cuenta — sale de la ultima DJ que presento el cliente antes de llegar. Es un
-- dato declarado, no deducido.
CREATE TABLE iva_saldo_inicial (
    cliente_id  INTEGER PRIMARY KEY REFERENCES clientes(id),
    periodo     TEXT NOT NULL,              -- MM/YYYY: el primero que liquida el estudio
    a_favor     REAL NOT NULL DEFAULT 0,
    nota        TEXT,
    actualizado TEXT NOT NULL
);

-- ══ LAS DEDUCCIONES DE IIBB, COMO LAS INFORMA EL PORTAL ══════════════════════
-- ⚠ ESTAS NO SALEN DE NUESTRAS FACTURAS, y por eso tienen tabla propia. Las
-- retenciones y percepciones de IIBB las informan los AGENTES directamente a
-- Rentas: el contribuyente se entera mirando el portal. Si la DJ se armara con
-- lo que nosotros vemos, declararía de menos y pagaría de más.
--
-- Los conceptos son los que pide ATP Formosa, uno por uno (relevados del
-- portal el 17/08). No es una lista genérica: SIRCUPA y SIRTAC son regímenes
-- concretos, y las retenciones BANCARIAS van aparte de las comunes porque las
-- practica el banco sobre acreditaciones, no un cliente sobre una factura.
CREATE TABLE iibb_deducciones (
    id            INTEGER PRIMARY KEY,
    cliente_id    INTEGER NOT NULL REFERENCES clientes(id),
    jurisdiccion  TEXT NOT NULL,              -- DGR-Fsa | DGR-Ctes | ...
    periodo       TEXT NOT NULL,              -- MM/YYYY
    retenciones          REAL NOT NULL DEFAULT 0,
    percepciones         REAL NOT NULL DEFAULT 0,
    ret_bancarias        REAL NOT NULL DEFAULT 0,
    otras_retenciones    REAL NOT NULL DEFAULT 0,
    sirtac               REAL NOT NULL DEFAULT 0,
    sircupa              REAL NOT NULL DEFAULT 0,
    pagos_a_cuenta       REAL NOT NULL DEFAULT 0,
    otros_pagos_a_cuenta REAL NOT NULL DEFAULT 0,
    otros_creditos       REAL NOT NULL DEFAULT 0,
    -- El que arrastra el portal. Es el dato que más pesa y el que no se puede
    -- deducir de nada: viene de toda la historia del contribuyente.
    saldo_a_favor        REAL NOT NULL DEFAULT 0,
    origen        TEXT NOT NULL DEFAULT 'portal',   -- portal | manual
    actualizado   TEXT,
    UNIQUE (cliente_id, jurisdiccion, periodo)
);
CREATE INDEX ix_iibbded ON iibb_deducciones(cliente_id, periodo);

-- ══ LOS PAGOS FISCALES: VEP, DEBIN, lo que sea ══════════════════════════════
-- «Qué impuesto se pagó, con un VEP o un DEBIN» (Juan, 05/09). Hasta hoy el
-- vencimiento se ataba al débito del banco y nada más: se sabía que salió la
-- plata, no CON QUÉ se pagó ni con qué número. El VEP es el comprobante que
-- vale ante ARCA, y su número es lo que se busca cuando reclaman.
--
-- ⚠ El vínculo va en TRES puntos, y por eso se guarda acá y no como una nota:
--     el VEP  ->  la OBLIGACIÓN que cancela   (vencimiento_id)
--     el VEP  ->  el DÉBITO del banco          (movimiento_id)
--   y de ahí, para atrás: el débito -> el VEP -> el impuesto y el período.
CREATE TABLE pagos_fiscales (
    id            INTEGER PRIMARY KEY,
    cliente_id    INTEGER NOT NULL REFERENCES clientes(id),
    numero        TEXT NOT NULL,              -- el Nº de VEP
    -- A dónde se mandó: RED LINK, BANELCO, DEBIN, INTERBANKING… El portal lo
    -- llama «Enviado a» y es lo que dice CÓMO se pagó.
    medio         TEXT,
    concepto      TEXT,                       -- «IVA DJ02/26», «AUTONO08/26»
    -- Deducidos del concepto cuando se puede, para poder cruzarlo con la
    -- obligación. Si no se pueden leer, quedan vacíos: no se inventan.
    impuesto      TEXT,
    periodo       TEXT,
    importe       REAL NOT NULL,
    fecha_pago    TEXT,
    estado        TEXT,                       -- Pagado | Pendiente | …
    vencimiento_id INTEGER REFERENCES vencimientos(id),
    movimiento_id  INTEGER REFERENCES movimientos_banco(id),
    origen        TEXT NOT NULL DEFAULT 'portal',
    actualizado   TEXT,
    UNIQUE (cliente_id, numero)
);
CREATE INDEX ix_pagofis ON pagos_fiscales(cliente_id, fecha_pago);

CREATE TABLE djs (
    id            INTEGER PRIMARY KEY,
    cliente_id    INTEGER NOT NULL REFERENCES clientes(id),
    impuesto      TEXT NOT NULL,               -- IVA | IIBB
    jurisdiccion  TEXT NOT NULL,               -- arca | DGR-Fsa | ...
    periodo       TEXT NOT NULL,               -- MM/YYYY
    debito        REAL,
    credito       REAL,
    impuesto_det  REAL,                        -- IIBB: Σ base × alícuota
    deducciones   REAL,
    bonificacion  REAL,
    saldo_a_pagar REAL,
    saldo_a_favor REAL,
    estado        TEXT NOT NULL DEFAULT 'borrador',  -- borrador | presentada
    presentada    TEXT,
    detalle       TEXT,                        -- JSON con el desglose
    UNIQUE (cliente_id, impuesto, jurisdiccion, periodo)
);


-- ══ 10. EL PANEL — registro de las corridas ═════════════════════════════════
-- El ERP aprendió esto por las malas: los jobs se corrían con .bat que
-- imprimían a consola, y al cerrar la ventana no quedaba registro de NADA.
-- Sin historial no se puede contestar "¿esto se corrió?" ni "¿por qué falló?".
--
-- No lleva cliente_id: es infraestructura del estudio, no dato de un cliente.
CREATE TABLE jobs_corridas (
    id         INTEGER PRIMARY KEY,
    job        TEXT NOT NULL,
    args       TEXT,
    alias      TEXT,                           -- sobre qué cliente corrió, si aplica
    usuario    TEXT,
    maquina    TEXT,
    inicio     TEXT NOT NULL,
    fin        TEXT,
    segundos   REAL,
    estado     TEXT NOT NULL,                  -- corriendo | ok | falló
    exit_code  INTEGER,
    salida     TEXT
);
CREATE INDEX ix_corridas_job ON jobs_corridas(job, inicio DESC);


-- ══ 11. CENTROS DE COSTO — la arquitectura, sin módulo todavía ══════════════
-- Juan (2026-08-26): *"no hay maestro de obras, pero vamos a dejar la
-- arquitectura para cargar centros de costo"*.
--
-- ERBEN ESTUDIO **no tiene módulo Obra**: no existen OC, OT ni certificados, y
-- el circuito arranca en la FACTURA. Lo que sí puede hacer falta es clasificar
-- una factura por destino (una sucursal, un proyecto, una línea de negocio).
-- Eso es un centro de costo, y no arrastra nada del mundo de obra.
--
-- ⚠ El reparto es POR PORCENTAJE desde el día uno, no un centro único por
-- factura. En el ERP la lección fue justamente esa: una factura puede ser 50%
-- de un centro y 50% de otro, y cuando se mira el flujo hay que repartir TODO
-- —el banco, el efectivo, los vencimientos— con ese mismo porcentaje. Dejarlo
-- como un solo FK obligaría a rehacer la tabla y a migrar lo ya cargado.
--
-- Mientras no haya módulo, estas tablas quedan vacías y no molestan: una
-- factura sin reparto es una factura sin clasificar, que es lo normal hoy.
CREATE TABLE centros_costo (
    id         INTEGER PRIMARY KEY,
    cliente_id INTEGER NOT NULL REFERENCES clientes(id),
    codigo     TEXT NOT NULL,
    nombre     TEXT NOT NULL,
    activo     INTEGER NOT NULL DEFAULT 1,
    nota       TEXT,
    UNIQUE (cliente_id, codigo)
);
CREATE INDEX ix_centros_cliente ON centros_costo(cliente_id);

-- El reparto de una factura entre centros. Si una factura no tiene filas acá,
-- está sin clasificar. Si tiene, los porcentajes deben sumar 100.
CREATE TABLE factura_centros (
    id         INTEGER PRIMARY KEY,
    factura_id INTEGER NOT NULL REFERENCES facturas(id) ON DELETE CASCADE,
    centro_id  INTEGER NOT NULL REFERENCES centros_costo(id),
    porcentaje REAL NOT NULL,
    UNIQUE (factura_id, centro_id)
);
CREATE INDEX ix_factcentro ON factura_centros(centro_id);
