# ERBEN ESTUDIO — sistema para estudio contable

Repo nuevo (2026-08-17). Sistema **aparte** del nuestro: el ERP/SIBRA es para
NUESTRAS empresas; esto es un producto para un **estudio contable** que
administra los impuestos y papeles de terceros.

> Regla que ordena todo: lo que en nuestro sistema llamamos **empresa**, acá
> se llama **CLIENTE del estudio**. Y los clientes **no se mezclan jamás**
> — ver `ARQUITECTURA.md`.

## Correr

```
py estudio-contable/servidor/server.py      →  http://localhost:8310
```

La primera corrida crea `C:\SIBRA\estudio\estudio.sqlite3` y la siembra con lo
relevado en vivo de ATP Formosa el 2026-08-17 (RODRIGUEZ: ficha, las 13 filas
de su padrón actividad+alícuota, y sus agentes de retención como maestro).
También está en `.claude/launch.json` como `erben-estudio`.

## Los módulos

| Módulo | Qué hace |
|---|---|
| **Tablero** | ventas/compras del mes, cheques en cartera, banco sin conciliar, vencimientos, y la foto del portal que dejó el job de ATP |
| **Facturas** | compras y ventas con IVA discriminado; las ventas llevan el **par actividad+alícuota** de IIBB (default: la actividad principal) |
| **Impuestos** | vencimientos (ARCA y provincias) + **liquidación de IVA** del período: débito − crédito − percepciones |
| **DJ IIBB** | base por actividad del período, con el **control Σ bases == Σ ventas**: si no cierra, no se presenta |
| **Bancos** | cuentas y extracto; los movimientos se van completando con CUIT y recibo |
| **Cheques** | recibidos (entran **solo por una cobranza**; se depositan o endosan) y emitidos |
| **Pagos** | cancela facturas con movimientos de banco, cheques o efectivo |
| **Conciliación** | automática cuando hay **un único** candidato; con dos o más decide una persona |
| **Entidades** | la relación de este cliente, sobre el maestro único por CUIT |

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
- [x] Probado de punta a punta: 25/25 checks desde base limpia
- [ ] Migrar los jobs ATP acá (`parsers/clientes.py` + namespace `EST/` de credenciales)
- [ ] Cargar los vencimientos de ARCA desde los jobs de la suite
- [ ] Importar extractos bancarios desde los parsers (hoy el alta es manual o por lote vía API)
- [ ] Login del estudio y permisos por contador

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
