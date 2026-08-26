# ERBEN ESTUDIO — sistema para estudio contable

Repo nuevo (2026-08-17). Sistema **aparte** del nuestro: el ERP/SIBRA es para
NUESTRAS empresas; esto es un producto para un **estudio contable** que
administra los impuestos y papeles de terceros.

> Regla que ordena todo: lo que en nuestro sistema llamamos **empresa**, acá
> se llama **CLIENTE del estudio**. Y los clientes **no se mezclan jamás**
> — ver `ARQUITECTURA.md`.

## Correr

**Doble clic en el ícono `ERBEN ESTUDIO` del Escritorio.** El servidor arranca,
el navegador se abre solo, y el sistema queda andando mientras esa ventana
siga abierta. Si ya estaba prendido, no levanta otro: abre y listo.

La primera vez, para crear el ícono:

```
powershell -ExecutionPolicy Bypass -File estudio-contable/instalar_acceso_directo.ps1
```

A mano, si hace falta:

```
py estudio-contable/arrancar.py      →  http://localhost:8310
```

La primera corrida crea `C:\SIBRA\estudio\estudio.sqlite3` y la siembra con lo
relevado en vivo de ATP Formosa el 2026-08-17 (RODRIGUEZ: ficha, las 13 filas
de su padrón actividad+alícuota, y sus agentes de retención como maestro).
También está en `.claude/launch.json` como `erben-estudio`.

## Los módulos

> **El filtro de empresa vive arriba, en el menú principal.** Se elige una y
> TODOS los módulos filtran por ella; cambiarla refiltra el módulo en el que
> estás sin sacarte de él.
>
> Es multiempresa en el sentido de que el estudio administra varias, **pero no
> se mezclan nunca**: en nuestro ERP las empresas son del mismo dueño y se
> consolidan, acá cada una es un cliente distinto y cruzarlas sería mostrarle a
> uno los datos de otro. Por eso no existe la opción "todas", y el filtro se
> aplica en el servidor.

| Módulo | Qué hace |
|---|---|
| **Tesorería** | la casa nueva, igual que la del ERP: **Tablero** (los huecos accionables), **Cuenta corriente** (le debo / me debe, con el bloque SUGERENCIAS), **Posición hoy**, **Conciliación**, **Vencimientos** y **Documentos** (la vista única con la escalera). Sin documentación de obra: un estudio no certifica obras |
| **Facturas** | compras y ventas con IVA discriminado; las ventas llevan el **par actividad+alícuota** de IIBB (default: la actividad principal) y se puede corregir en la grilla — la clasificación fiscal vive acá, no en Tesorería |
| **Impuestos** | vencimientos (ARCA y provincias) + **liquidación de IVA** del período: débito − crédito − percepciones |
| **DJ IIBB** | base por actividad del período, con el **control Σ bases == Σ ventas**: si no cierra, no se presenta |
| **Bancos** | cuentas y extracto; los movimientos se van completando con CUIT y recibo |
| **Cheques** | solo dos clases: **cobrados** (entran únicamente por una cobranza; se depositan o endosan) y **emitidos**. No hay "me dieron" ni "me prestaron" |
| **Pagos** | cancela facturas con movimientos de banco, cheques o efectivo |
| **Entidades** | la relación de este cliente, sobre el maestro único por CUIT |
| **Panel** | estado del equipo y los servicios, qué falta cargar, y la suite de jobs: traer info de bancos, ARCA y DGR, **llevar la DJ liquidada al portal**, y el registro de cada corrida |

## Lo que NO tiene (y es a propósito)

| No hay | Por qué |
|---|---|
| **módulo Obra** | sin OC, OT ni certificados, sin maestro de obras: el circuito **arranca en la factura**. Un estudio contable no certifica obras. Queda preparada la arquitectura de **centros de costo** (reparto por porcentaje) para clasificar por destino |
| **cheques "me dieron" / "me prestaron"** | solo **emitidos** y **cobrados**. Las otras dos clases arrastran el circuito real↔fantasía del ERP, que acá no aplica |
| **módulo Tarjetas** | todavía no |

## El flujo que ata todo

```
factura ──┐
          ├─→ PAGO / COBRANZA ──→ medio: efectivo | transferencia | cheque
cheque ───┘                              │              │
                                         │              └─→ cobranza: NACE el
                                         │                  cheque recibido
                                         └─→ el movimiento del banco queda con
                                             el CUIT y el recibo pegados
                                                     │
                                             CONCILIACIÓN automática
```

La conciliación matchea cheques contra el movimiento (importe + fecha de pago
±7 días) y facturas impagas por **monto + entorno de fecha (±10 días) + CUIT**
(del campo, de la descripción o por nombre). Con más de un candidato **no
elige**: dos facturas iguales del mismo proveedor en la misma semana existen, y
adivinar rompe la cuenta corriente en silencio.

## Estado

- [x] Repo, arquitectura y decisión de entidades (maestro único + relaciones)
- [x] Esquema completo (`servidor/esquema.sql`) y API (`servidor/server.py`)
- [x] App con selector de cliente y los 9 módulos (`sistema/index.html`)
- [x] Módulo Tesorería (5 pestañas, con Tablero) y filtro de empresa arriba
- [x] Probado de punta a punta: 53/53 checks desde base limpia
- [x] Suite de parsers propia (`parsers/`) + ícono de escritorio que arranca todo
- [x] Job que lleva la DJ al portal de la provincia (`dj_a_dgr.py`) — no presenta
- [ ] Cargador de Mis Comprobantes de ARCA → facturas
- [x] **Panel** con estado de servicios y el registro de corridas en la base
- [x] **Cargador de obligaciones impositivas** con el ciclo de vida del ERP
- [x] Arquitectura de **centros de costo** (sin módulo, listo para cargar)
- [x] **Cargador de extractos** (`parsers/cargar_extracto.py`) — CSV/XLSX de cualquier banco, valida la cadena de saldos, idempotente
- [ ] Login del estudio y permisos por contador

## Dónde corre

Tres capas, y la que importa entender: **los datos del cliente nunca pasan por
internet**. La nube sirve la pantalla; el navegador le pide los datos al equipo
del estudio. Ver `DESPLIEGUE.md`.

| Capa | Qué tiene |
|---|---|
| Vercel | solo `sistema/` — HTML y JS, cero datos |
| El equipo del estudio | server + API + **la base** + credenciales + parsers |
| Drive (`H:\My Drive\ERBEN`) | **respaldos** de la base, estado de los jobs y la carpeta de cada cliente |

⚠ **La base NO vive en el Drive**: un SQLite adentro de una carpeta que
sincroniza se corrompe (Drive puede agarrarlo a mitad de una escritura). Al
Drive suben solo las COPIAS, con fecha, y se guardan las últimas 10.

Todo lo del Drive cuelga de **una sola constante** (`rutas.py`, pisable con
`ERBEN_DRIVE`): hoy usa nuestro Drive y el día que ERBEN tenga cuenta propia se
mueve la carpeta y se cambia una línea.

## Base de datos

**SQLite**, y alcanza de sobra. Medido con un cliente activo (200 facturas y
300 movimientos por mes): 50 clientes × 10 años pesan **~600 MB**, y SQLite
trabaja cómodo hasta cientos de GB.

Cambiaría solo por dos motivos, ninguno presente hoy: que **varias máquinas
escriban a la vez** (SQLite sobre red se corrompe) o que el sistema **se vaya a
la nube**. En los dos casos la migración ya está medida — 109 llamadas con un
solo `db()`, y el ERP hizo el mismo salto con `pgcompat.py`.

## Qué se hereda de nuestro sistema

| Módulo | De dónde sale | Adaptación principal |
|---|---|---|
| Bancos / Cheques / Facturas / Tesorería | `ERP/` | `cliente_id` obligatorio, filtro en el servidor |
| Suite de parsers | `Vinculacion bancos/tools/` | registro de clientes propio, credenciales con namespace |

## Primer cliente real

RODRIGUEZ RUBEN ALFREDO (20216598998) — ATP Formosa relevada en vivo el
2026-08-17, jobs `sesion_atp.py` + `atp_iibb.py` ya escritos (hoy viven en
`Vinculacion bancos/tools/`).
Relevo del portal: `H:\My Drive\web_sibra\tesoreria\atp_formosa\2026-08-17\`.
