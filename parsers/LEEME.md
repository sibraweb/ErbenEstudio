# La suite de parsers y jobs

Traen la información de los portales a ERBEN ESTUDIO — y llevan la DJ de vuelta.

## Cómo se corren

Desde la pantalla: módulo **Parsers y jobs**. Elegís el cliente, apretás Correr
y ves la salida ahí mismo. Los marcados **atendido** abren una ventana del
navegador y esperan que entres vos (2FA o captcha).

Desde la consola, si hace falta:

```
py parsers/suite.py                                  el catálogo
py parsers/suite.py --credenciales                   qué falta cargar
py parsers/credenciales.py --set --fuente DGR-Fsa --alias DEMO
py parsers/dj_a_dgr.py --alias DEMO --periodo 07/2026
```

## Las tres piezas de infraestructura

| Archivo | Qué es |
|---|---|
| `clientes.py` | quién es quién. **Lee de la base del sistema**, no es una lista aparte: los clientes se dan de alta en la pantalla y los jobs los ven al instante |
| `credenciales.py` | usuario y clave por (fuente, cliente) en el Credential Manager de Windows, bajo el servicio **`erben-estudio`** |
| `suite.py` | el catálogo de jobs y el lanzador que usa la pantalla |

⚠ **El namespace de credenciales es propio.** Un alias del estudio y uno
nuestro pueden llamarse igual; si compartieran namespace, cargar la clave de
uno pisaría la del otro y el síntoma sería un job fallando con "usuario o
contraseña incorrectos" — que se lee como un bug del scraper.

## De dónde sale cada job

Los scrapers de bancos y ARCA **ya existen y están probados** en
`Vinculacion bancos/tools/`. No se reescriben: se invocan desde ahí, y el
catálogo los marca `heredado de SIBRA`. La ruta sale de una sola constante
(`TOOLS_SIBRA` en `suite.py`, pisable con la variable de entorno
`SIBRA_TOOLS`), así que mudarlos o correr en otra máquina se arregla en un
solo lugar.

Propios del estudio son los que tocan su base: `sesion_atp.py`, `atp_iibb.py`
y `dj_a_dgr.py`.

## El job que cierra el círculo: `dj_a_dgr.py`

Toma la DJ de IIBB ya liquidada acá y **la escribe en la grilla del portal**
para que la persona solo revise y presente.

```
py parsers/dj_a_dgr.py --alias DEMO --periodo 07/2026
py parsers/dj_a_dgr.py --alias DEMO --periodo 07/2026 --revisar   # compara sin escribir
```

Antes de escribir nada verifica tres cosas, y si alguna falla no toca el portal:

1. **La DJ cierra** — Σ bases por actividad == Σ ventas del período. Cargar una
   DJ con ventas sin imputar es subdeclarar.
2. **Hay ventas** en ese período.
3. **El CUIT logueado es el del cliente.** Sin este control se podría cargar la
   DJ de un cliente en el portal de otro, que es lo peor que puede pasar en un
   sistema donde cada cliente es un compartimiento estanco.

Empareja por el **par (código de actividad, alícuota)**, que es la fila real de
la grilla: en Formosa la misma actividad vive con varias alícuotas. Si alguna
actividad no tiene fila en el portal, lo dice y la deja afuera — el padrón del
portal manda.

Deja constancia en `H:\My Drive\web_sibra\estudio\dj\` (JSON + captura).

### ⚠⚠ Este job NO presenta

Nunca aprieta Aceptar ni Guardar. Presentar una DJ es un acto fiscal
irreversible con nombre y apellido: lo hace la persona, mirando la pantalla.
El job prepara, el humano ejecuta — la misma regla del preparador de pagos
(`TESORERIA__DEFINICION.md` §11).

## Qué falta

- [ ] Que `atp_iibb.py` escriba en la base del estudio (hoy deja XLSX/JSON en Drive)
- [ ] Cargador de extractos bancarios → `movimientos_banco` (el endpoint por lote ya existe)
- [ ] Cargador de Mis Comprobantes de ARCA → `facturas`
- [ ] Vencimientos de ARCA → `vencimientos`
