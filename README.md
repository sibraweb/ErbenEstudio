# ERBEN ESTUDIO — sistema para estudio contable

Repo nuevo (2026-08-17). Sistema **aparte** del nuestro: el ERP/SIBRA es para
NUESTRAS empresas; esto es un producto para un **estudio contable** que
administra los impuestos y papeles de terceros.

> Regla que ordena todo: lo que en nuestro sistema llamamos **empresa**, acá
> se llama **CLIENTE del estudio**. Y los clientes **no se mezclan jamás**
> — ver `ARQUITECTURA.md`.

## Qué se hereda de nuestro sistema

Se toman como base (fork adaptado, no compartido en runtime):

| Módulo | De dónde sale | Adaptación principal |
|---|---|---|
| Bancos | `ERP/bancos` + jobs de `Vinculacion bancos/tools` | filtro duro por cliente |
| Cheques | `ERP/cheques` | ídem |
| Facturas | `ERP/facturas` | + actividades/alícuotas IIBB en ventas |
| Tesorería | `ERP/tesoreria` (TESORERIA__DEFINICION.md) | ídem |
| Suite de parsers | `Vinculacion bancos/tools/suite.py` + sesiones/jobs | registro de clientes propio |

Los tres pilares de infraestructura se replican con el mismo patrón:
`clientes.py` (equivalente a `contribuyentes.py`), `credenciales.py`
(Credential Manager, **con namespace propio** para no chocar con los alias
nuestros) y las sesiones `sesion_*.py` por (fuente, cliente).

## Estructura (a poblar)

    estudio-contable/
      ARQUITECTURA.md   <- el modelo multi-cliente y la decisión de entidades
      servidor/         <- API propia (NO el server del ERP)
      parsers/          <- suite de parsers propia (clientes.py, jobs)
      sistema/          <- el front: principal ERBEN ESTUDIO + módulos

## Primer cliente real

RODRIGUEZ RUBEN ALFREDO (20216598998) — ATP Formosa relevada en vivo el
2026-08-17, jobs `sesion_atp.py` + `atp_iibb.py` ya escritos (hoy viven en
`Vinculacion bancos/tools/`, se migran acá cuando exista `parsers/`).
Relevo del portal: `H:\My Drive\web_sibra\tesoreria\atp_formosa\2026-08-17\`.

## Estado

- [x] Repo creado, arquitectura escrita, decisión de entidades tomada
- [ ] `parsers/clientes.py` + namespace de credenciales
- [ ] Migrar jobs ATP acá y sumar clientes ARCA/DGR según altas
- [ ] Servidor (elegir stack — el candidato natural es el patrón del ERP)
- [ ] Principal ERBEN ESTUDIO (selector de cliente) + primer módulo (facturas)
