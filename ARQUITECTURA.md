# Arquitectura — ERBEN ESTUDIO

> Definido con Juan el 2026-08-17, al pie de la letra:
> *"cada CUIT que nosotros llamábamos empresa va a ser cliente del estudio,
> no se pueden mezclar entre sí como hacemos entre nuestras empresas"*.

## 1. El modelo multi-cliente (la diferencia con nuestro ERP)

En NUESTRO sistema las empresas (SIBRATECH, JUAN, CAMECOR...) son del mismo
dueño: se consolidan, se cruzan (un cheque de una paga una factura de otra) y
el `empresa_id` es un filtro *blando*.

En el ESTUDIO eso es exactamente lo prohibido:

- **Cliente = CUIT.** Cada cliente es un compartimiento estanco.
- **Se elige el cliente al entrar.** Desde ahí, TODOS los módulos (bancos,
  cheques, facturas, tesorería, impuestos) quedan filtrados a ese cliente.
- **El cambio de cliente se hace SOLO en el principal ERBEN ESTUDIO** — no
  hay selector por módulo, no hay vistas consolidadas entre clientes, no hay
  ninguna query que junte filas de dos clientes.
- Implementación: `cliente_id` obligatorio en TODA tabla operativa, y la API
  exige el cliente activo en sesión — un endpoint sin cliente activo no
  devuelve datos. El filtro va en el servidor, no en el front.

## 2. La decisión de entidades: HÍBRIDA ✅ CONFIRMADA (Juan, 2026-08-17)

La pregunta era: ¿la base de entidades (proveedores/clientes de cada cliente)
es transversal o por cliente? Porque si es transversal, varios clientes del
estudio "comparten" proveedores.

**Decisión de Juan, en sus palabras: "la base con CUIT es UNA, pero cliente
tiene relación con proveedor 3 y cliente 7 — se refieren al maestro."**
O sea: maestro único por CUIT; la relación de cada cliente es una fila propia
que APUNTA al maestro, nunca una copia de la ficha. Esquema de tablas bajado
a `servidor/esquema.sql`. Las dos mitades:

### 2a. `padron` — transversal (datos PÚBLICOS por CUIT)

Una ficha por CUIT, única para todo el estudio: razón social, condición de
IVA, domicilio fiscal, actividades. Es lo que dice la constancia de
inscripción de ARCA: **dato público**, no dice nada de con quién opera.

Por qué transversal: en una misma plaza los mismos CUIT aparecen para todos
(Aguas de Formosa, REFSA, los bancos, los mismos mayoristas). Cargar y
mantener 30 veces la misma ficha es el error de tipeo asegurado — y ya
tenemos el precedente de una empresa cargada dos veces por una letra de
diferencia en el nombre: la identidad va SIEMPRE por CUIT.

### 2b. `entidades_cliente` — POR CLIENTE (la relación comercial)

`(cliente_id, cuit)` → rol (proveedor / cliente / ambos), alias interno,
condiciones de pago, cuenta corriente, saldos, historial completo. **Esto es
lo confidencial y nunca cruza de un cliente a otro.**

La regla de oro: el padrón contesta "¿quién es este CUIT?"; solo
`entidades_cliente` contesta "¿quién opera con quién?" — y esa tabla siempre
se lee con `cliente_id` fijo. Que dos clientes del estudio tengan al mismo
proveedor en su lista NO les revela nada: cada uno ve su propia relación.

Caso borde ya contemplado: dos clientes del estudio que operan ENTRE SÍ.
Cada relación vive en la fila de su dueño; nadie ve la del otro.

## 3. Impuestos: el módulo que el ERP no tenía

El estudio vive de esto, así que acá los jobs impositivos son ciudadanos de
primera:

- **Por cliente y por fuente** (`arca`, `DGR-Ctes`, `DGR-Fsa`, ...): sesión
  persistente + job de relevamiento + generación de DJ. El patrón ya está
  probado: `sesion_atp.py` + `atp_iibb.py` (ATP Formosa, relevada 2026-08-17).
- La suite de parsers del estudio es APARTE de la nuestra: mismo motor, otro
  registro (`parsers/clientes.py`) y otro namespace de credenciales en el
  Credential Manager (propuesta: fuente con prefijo, ej. `EST/DGR-Fsa`), para
  que un alias del estudio nunca pise un alias nuestro.

## 4. Facturas de venta → actividades y alícuotas de IIBB

Pedido de Juan (2026-08-17): *"ver en facturas poder elegir las actividades y
alícuotas de las facturas de venta"*. El porqué: la DJ de IIBB se arma con la
**base imponible por actividad** — hoy eso se tipea a mano en el portal; si
cada factura de venta ya sabe a qué actividad pertenece, la DJ sale sola.

Diseño (para el módulo facturas del estudio, y aplicable al ERP nuestro):

1. **`actividades_cliente`**: las actividades del padrón fiscal de cada
   cliente por jurisdicción, con su alícuota — se cargan SOLAS desde los
   jobs (`atp_iibb.py` ya trae el padrón completo de ATP con código NAES,
   principal, exento; `actividades.py` hace lo propio con ARCA/CLAE).
2. **En la factura de venta**: un selector de actividad (default: la
   actividad PRINCIPAL del emisor). La alícuota NO se elige suelta: la trae
   la actividad elegida (en Formosa una misma actividad puede tener varias
   alícuotas — ej. 562010 al 3/5/15% — en ese caso el selector ofrece el par
   actividad+alícuota, que es la fila real de la grilla de ATP).
3. **La DJ se arma sola**: base por (actividad, alícuota) = Σ facturas de
   venta del período con ese par. Eso alimenta `actividad_carga` de ATP
   (los `base_imponible_N` del form) y la pantalla de DJ de DGR Corrientes.
4. Facturas sin actividad asignada → van a la principal, con aviso: el
   control es que Σ bases de la DJ == Σ ventas del período.

## 5. Qué se decide después (anotado, sin resolver)

- Stack del servidor y dónde vive la base (el candidato natural es replicar
  el patrón ERP: API + sqlite/Supabase; la base del estudio SEPARADA de la
  nuestra, misma regla que todo lo demás).
- Usuarios del estudio: ¿cada contador ve todos los clientes o hay carteras?
  (el modelo de permisos se decide cuando exista el login).
- Si el estudio factura sus honorarios desde acá o desde nuestro ERP.


## 6. EL ALCANCE — qué hay y qué NO hay (Juan, 2026-08-26)

Está acá porque la tentación permanente va a ser traer módulos del ERP "ya que
están". El ERP resuelve una constructora; esto resuelve un estudio contable, y
la mitad de aquello no aplica.

### NO existe módulo Obra
Sin OC, sin OT, sin certificados, **sin maestro de obras**. El circuito
**arranca en la FACTURA** — no hay documento anterior que la origine.

Consecuencia práctica: el Tablero de Tesorería no tiene el hueco "documentos de
obra sin factura", y la vista Documentos no tiene esa clase. Un estudio contable
no certifica obras.

**Lo que sí queda preparado: CENTROS DE COSTO.** Tablas `centros_costo` y
`factura_centros` (§11 del esquema) con endpoints andando, para clasificar una
factura por destino cuando haga falta. El reparto es **por porcentaje desde el
día uno** — una factura puede tocar dos centros, y en el ERP dejarlo como un
solo campo obligó a rehacer la tabla.

### Cheques: SOLO dos clases, y TRES roles

**Emitidos** (propios del cliente) y **cobrados** (entran únicamente por una
cobranza; se depositan o se endosan).

**No existe "me dieron" ni "me prestaron", y por lo tanto no existe el cliente
de fantasía.** En el ERP la fantasía hace falta porque ahí entran cheques de
terceros sin una venta detrás: hay que esperar a que aparezca la factura para
saber a qué cliente pertenecen. **Acá el cheque recibido ES una cobranza**, así
que el cliente se sabe desde el momento en que entra — sale del recibo que lo
trajo. Ese circuito entero (fantasía, bloqueo del depósito, cadena de endosos
en dos tiempos) no aplica y meterlo sería complejidad pura.

**Los tres roles de un cheque cobrado** (Juan, 2026-08-31), que son distintos y
no se completan uno con otro:

| Rol | Quién es | De dónde sale |
|---|---|---|
| **Librador** | el que firmó el cheque | `cuit_librador` si está en el maestro, si no `librador_nombre` — puede no saberse, y entonces queda **vacío** |
| **Cliente** | el que nos lo dio; es el que cancela factura | del recibo de cobranza (`pago_origen_id` → la entidad del pago) |
| **Destino** | dónde termina | un **proveedor** (`endoso_entidad_id`) o un **banco** (`deposito_cuenta_id`) |

El **emitido** tiene un rol solo: el **beneficiario** (`beneficiario_entidad_id`),
a quien se lo damos. Si nace dentro de un pago, es la entidad de ese pago.

⚠ **El librador no se rellena con el CUIT del cliente.** Hasta el 31/08 el alta
lo hacía: si no se cargaba librador, guardaba el del cliente. Eso afirma que el
cliente firmó el cheque, cuando puede haberlo recibido de un tercero — y nadie
se enteraría. Si no se sabe, se deja vacío; el cliente ya está en el recibo.
En la pantalla hay un tilde *"lo firmó el mismo cliente"* que **arranca
destildado**: copiarlo tiene que ser un acto de alguien, no un default.

**Cómo se cargan** (02/09). El módulo tiene los dos botones —`+ Cheque recibido`
y `+ Cheque emitido`— pero no son simétricos por dentro:

- el **emitido** se da de alta derecho contra `POST /api/c/cheques`;
- el **recibido** arma una **cobranza** (`POST /api/c/pagos`, dirección cobro,
  medio cheque). O sea: el botón es nuevo, la puerta es la misma de siempre. Un
  cheque recibido suelto sería plata en cartera sin dueño, y el endpoint de
  cheques lo sigue rechazando con 400.

El formulario del recibido ofrece las facturas impagas de ese cliente y propone
el importe con lo que suman las tildadas; si no se tilda ninguna, la cobranza
queda **a cuenta** y se imputa después por FIFO.

**Depositar y endosar** se eligen de una lista, no se escriben. Es la misma
corrección que el ERP ya hizo: con `prompt()` había que transcribir el número de
cuenta o la razón social, y así se cargan cuentas y entidades repetidas.

### Facturas: los comprobantes y la POSICIÓN de IVA

Traído del módulo Facturas del ERP, con sus pestañas menos la de obra:
**Ventas · Compras · IVA del período · Posición de IVA**.

La liquidación de IVA vive acá y no en Impuestos (Juan, 02/09: *"el módulo de
facturas tiene eso, es la posición de IVA"*). Tiene sentido: sale de los
comprobantes. En Impuestos quedan los vencimientos.

**El saldo a favor se arrastra.** Es la parte que no se puede improvisar y que
el ERP ya tenía resuelta (`_cadena_iva`):

```
posición = débito − crédito − percepciones
  posición <= 0     ->  el excedente ENGROSA el saldo a favor
  alcanza el saldo  ->  se consume y no se paga nada
  no alcanza        ->  se paga la diferencia y el saldo queda en cero
```

⚠ Hasta el 02/09 cada mes se liquidaba solo. Se vio con datos reales: mayo daba
a favor $434.419 y junio pedía pagar el bruto, ignorando ese crédito. Por eso
la pantalla muestra la **cadena entera**, no un mes: un mes aislado no se puede
auditar.

**El saldo inicial es un dato declarado** (`iva_saldo_inicial`). El crédito que
el cliente traía de la última DJ antes de llegar al estudio no está en ningún
comprobante; si no se declara, el arrastre arranca en cero y se lo hace perder.

⚠⚠ **FALTAN LAS RETENCIONES DE IVA SUFRIDAS.** No vienen en Mis Comprobantes —
son otra pantalla de ARCA. A un cliente al que le retienen mucho, esta cuenta le
muestra **más impuesto del que debe**. Las percepciones sí entran: las de las
facturas de compra (*Otros Tributos*) y las que cobra el banco, si el concepto
del movimiento las nombra.

### Pagos: se cancelan FACTURAS, y nada más

El comprobante tiene una dirección (un **pago** cancela compras, una **cobranza**
cancela ventas) y se arma con tres medios: efectivo, un movimiento del banco, o
un cheque —de cartera para endosar, o uno nuevo que se emite ahí mismo—.

Del ERP no viene nada del circuito de obra: **no hay certificados, ni órdenes de
compra, ni imputación por obra**. Se elige la entidad, se tildan sus facturas
impagas y listo. Los centros de costo, cuando se usen, cuelgan de la factura
(§ arriba), no del pago.

Los dos botones del módulo —`+ Nuevo pago` y `+ Nueva cobranza`— fijan la
dirección al abrir. Antes se abría un panel y había que acordarse de tildar la
pestaña: quedaban cobranzas cargadas como pagos.

Lo que se paga por banco deja el movimiento con el CUIT de la contraparte, el
número de recibo y conciliado — es el "el banco se va nutriendo" del pedido
original.

### Tarjetas: todavía no
El módulo no existe y no está previsto por ahora.

### Los módulos que se usan
Tesorería · Bancos · Cheques · Facturas · Pagos.
Más los fiscales que se pidieron aparte (Impuestos y DJ IIBB) y la
infraestructura (Entidades, Panel, Clientes del estudio).

### El filtro de empresa
Va **arriba, en el menú principal**. Se elige una empresa y todos los módulos
muestran solo lo de ella.

⚠ Es multiempresa en el sentido de que el estudio administra varias, **pero no
se mezclan nunca**. En nuestro ERP las empresas son del mismo dueño y se
consolidan; acá cada una es un cliente distinto del estudio y cruzarlas sería
mostrarle a uno los datos de otro. Por eso no existe la opción "todas" — y el
filtro se aplica en el servidor, no en la pantalla.


## 7. SERVIDOR Y PARSERS PROPIOS — dónde estamos (Juan, 2026-08-27)

> *"Erben estudio debe tener su propio server separado de lo nuestro y sus
> propios parsers después"*.

### Ya está separado, y se puede verificar

| | |
|---|---|
| Servidor | propio, puerto **8310**. No comparte proceso ni una línea de código con el ERP |
| Base | propia, `C:\SIBRA\estudio\estudio.sqlite3` |
| Credenciales | namespace propio **`erben-estudio`** en el Credential Manager — un alias del estudio no pisa uno nuestro aunque se llamen igual |
| Drive | propio, `H:\My Drive\ERBEN` (una sola constante en `rutas.py`) |
| Registro de clientes | propio: `parsers/clientes.py` lee de SU base, no del `contribuyentes.py` nuestro |

Apagar todo nuestro sistema no afecta a ERBEN. Lo único que pierde son los 6
jobs prestados de abajo, y el Panel los muestra como *falta el archivo* en vez
de romperse.

### Lo único que falta para la independencia total: 6 parsers

**Propios (5)** — tocan la base del estudio:
`atp_sesion` · `atp_relevar` · `dj_a_dgr` · `cargar_extracto` · `cargar_vencimientos`

**Prestados de SIBRA (6)** — se invocan desde `Vinculacion bancos/tools/` vía la
constante `TOOLS_SIBRA` (pisable con la variable de entorno `SIBRA_TOOLS`):

| Job | Qué hay que escribir |
|---|---|
| `arca_comprobantes` | Mis Comprobantes por clave fiscal del cliente |
| `arca_vencimientos` | agenda pública por terminación de CUIT |
| `dgr_ctes_deuda` | estado de cuenta de IIBB Corrientes |
| `galicia` · `bancorrientes` · `formosa_banco` | extractos y cheques de cada banco |

**Por qué no se copiaron ya**: están probados contra los portales reales y
copiarlos sería mantener dos veces el mismo scraper — cada cambio de pantalla
de un banco habría que arreglarlo en dos lugares. Se reescriben **cuando el
estudio corra en otra máquina**, que es cuando la dependencia se vuelve real.

**El día que toque, es un trabajo acotado y ya está preparado**: los 6 salen de
una sola constante, el catálogo ya los marca `heredado de SIBRA`, y el Panel ya
avisa si no los encuentra. No hay que rediseñar nada, hay que escribir seis
scrapers.

⚠ La otra atadura, más chica: `cargar_vencimientos.py` lee el `cct_estado.json`
que deja el job de ARCA **en nuestro Drive**. Busca primero en el Drive del
estudio y usa el nuestro como respaldo, así el día que exista el job propio
deja de mirar para afuera sin tocar código.


## 8. LO QUE SE TRAJO DEL ERP (revisión 2026-08-31)

Juan: *"ya que estamos tomando este sistema de los módulos del ERP, fijate qué
hubo de nuevo que podamos importar acá"*. Lo que se revisó y qué se decidió:

| Del ERP | Acá | Por qué |
|---|---|---|
| `shared/tabla-orden.js` | **importado** | Ordenar cualquier tabla por su encabezado. Trae resueltas las dos trampas: los números argentinos (el punto agrupa, la coma decide) y las fechas dd/mm/aaaa. Se sumó una propia: las celdas traen HTML, así que compara por texto visible |
| `shared/entidades.js` | **importado** | Buscar por nombre, CUIT **o alias**. ⚠ El alias sirve para BUSCAR, no para decidir identidad: si es ambiguo se ofrecen las opciones, nunca se elige solo |
| `IVA__CRITERIO_UNICO.md` | **aplicado** | Todo COMPROBANTE muestra neto · IVA · total; todo PAGO es por el total. En la vista Documentos los pagos van con guión, no con cero: cero diría que su IVA es cero, y lo que pasa es que no aplica |
| `shared/naturaleza.js` | no aplica | Clasifica al proveedor para costear obras, y acá no hay obras. El principio sí se aplicó antes: el dato se corrige donde uno se da cuenta de que falta (la actividad de IIBB, en la grilla de Facturas) |
| `shared/formato.js` | ya resuelto | Allá convivían cinco formas de escribir un negativo. Acá siempre hubo una sola `plata()` |
| `entidades_limpiar.py` | **anotado para después** | El maestro tiene 8 filas; el problema llega cuando crezca. De allá sirve el criterio de quién sobrevive (gana la que tiene CUIT) y qué es basura. ⚠ Lo que NO se trae es la excepción de la fantasía: **acá no hay clientes de fantasía** (ver §6) |
| `AUDITORIA__CONCILIACION_BANCARIA.md` | alineado | Su regla madre —*"lo que queda sin par no es un error de suma: es trabajo que falta hacer, y hay que poder verlo por nombre"*— ya está en el Tablero, que los lista por nombre y no como un número |
