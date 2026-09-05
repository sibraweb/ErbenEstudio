# Qué se recicla del ERP, qué no, y qué falta

Relevamiento del 05/09/2026. Se compararon los **139 endpoints** del ERP de
SIBRATECH contra los **66** de ERBEN, familia por familia.

La pregunta que lo motivó (Juan): *"todo ERP está espejado, no?"*. **No.** La
cañería está —los vínculos existen en la base— pero la pantalla corta la cadena
un eslabón antes de llegar al final, y faltan tres cosas concretas: los VEP, el
resumen mensual del banco, y poder desimputar.

---

## Lo que NO se trae, y por qué

No es que falte tiempo: **no aplican a un estudio contable** y traerlas sería
cargar el sistema con conceptos que nadie va a usar.

| Familia del ERP | Endpoints | Por qué no |
|---|---|---|
| `propiedades` | 36 | Es el negocio inmobiliario de SIBRA, no del estudio |
| `obras`, `vinculos`, `utes` | 14 | **No hay obra**: el circuito arranca en la factura |
| `tarjetas` | 13 | Decisión de Juan: todavía no |
| `express`, `ingesta`, `ayudamemoria` | 20 | Son de SIBRA: emisión propia y bandeja del bot |
| `cheques/emision/*` | 3 | El estudio **mira** la cuenta del cliente, no la opera |
| `cheques/fantasia`, `cheques/roles` | 2 | Acá el cheque recibido ES una cobranza: no hay fantasía |
| `contabilidad` | 9 | Todavía no: primero la plata, después el asiento |

## Lo que YA se trajo

| Qué | Commit |
|---|---|
| Los tres roles del cheque (librador · cliente · destino) | `04f3e4e` |
| Altas de cheques y pagos, con la puerta única del recibido | `5739f2e` |
| Mis Comprobantes de ARCA a la carpeta del cliente | `b7a3e79` |
| La cadena del IVA con el saldo a favor arrastrado | `891f460` |
| El gráfico de la posición | `d9780f8` |
| Percepciones por tipo y retenciones del recibo | `3dea0d2` |
| El maestro de bancos del BCRA y el log de bases incompletas | `64e9686` |
| Tesorería: Impuestos con respaldo bancario y Sin contraparte | `14bc159` |
| Cheques duplicados y los que nombra el banco | `0abe3de` |
| ATP: base = neto, y las deducciones del portal | `1b69c09` |

## Lo que FALTA, en orden de lo que más cambia

### 1 · La cadena completa, de ida y de vuelta
Hoy cada documento muestra **un** eslabón: la factura dice "cobro REC-1" pero no
llega al movimiento del banco; el movimiento dice "recibo #5" pero no llega a la
factura ni al proveedor. Los vínculos existen —`pago_medios`, `pago_aplicaciones`,
`conciliaciones`, `cheques.pago_origen_id`— pero nadie los recorre hasta el final.

- `GET /api/c/documento/<clase>/<id>/cadena` — el árbol entero desde cualquier punta
- los **impuestos y sus VEP** como una clase más de la vista Documentos
- del ERP: `comprobantes/<id>/cadena`

### 2 · Los VEP y los DEBIN
*"Que aparezca qué impuesto pagó, un VEP o un DEBIN"*. El vencimiento ya se ata
al débito del banco, pero no se sabe **con qué** se pagó ni con qué número — y el
número del VEP es lo que se busca cuando ARCA reclama.

Ya está relevado del portal (`vep_pagos_*.json`, 39 VEP de RODRIGUEZ con número,
medio, concepto, importe y fecha). Falta la tabla, el cargador y el cruce.

- del ERP: `impuestos/veps`, `impuestos/veps/<nro>/imputar`

### 3 · El depósito del mes en el banco
*"En banco ver el depósito del mes"*. No existe una vista mensual.
- del ERP: `bancos/resumen-mensual`, `bancos/saldos`, `bancos/resumen-conciliado`

### 4 · Imputar y DESIMPUTAR a mano
Hoy solo hay imputación automática por FIFO. Si imputa mal, no hay cómo
deshacerlo: la cuenta corriente queda mintiendo y no hay botón.
- del ERP: `imputaciones` POST y DELETE, `recibos/<id>/editar-medios`, `recibos/<id>/anular`

### 5 · El cheque que rebota
Es donde el **librador** deja de ser un dato de archivo y pasa a ser a quién se
le reclama. Sin esto, un cheque rechazado no tiene estado.
- del ERP: `cheques/<id>/rechazo`, `cheques/<id>/canje`, `cheques/<id>/historial`

### 6 · Los planes de pago
Ya están relevados (`facilidades_*.json` del portal). Un plan caduco es deuda que
no se ve en ningún lado.
- del ERP: `impuestos/planes`, `impuestos/control-cruzado`

---

## Lo que se espera del ERP terminado

Juan (05/09): *"lo terminamos de ERP y te lo pasamos completo"*. Queda en pausa,
sin portar a medias:

- el validador de CBU/CUIT contra el banco (`bancos/maestro`, `bcra`)
- las reglas de estado del cheque: *"«Aceptado» significa lo contrario según de
  quién sea el cheque"* y *"un borrador que el banco nunca informó deja de contar
  como deuda"*
