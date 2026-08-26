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
tenemos el precedente EPHISET/EPISHET: la identidad va SIEMPRE por CUIT.

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

### Cheques: SOLO dos clases
**Emitidos** (propios del cliente) y **cobrados** (entran únicamente por una
cobranza; se depositan o se endosan).

**No existe "me dieron" ni "me prestaron".** En el ERP esas dos clases arrastran
todo el circuito de cliente real ↔ cliente de fantasía, el bloqueo del depósito
hasta que aparezca la factura de venta, y la cadena de endosos. Nada de eso
aplica acá y meterlo sería complejidad pura.

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
