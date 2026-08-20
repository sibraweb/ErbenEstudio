# Cómo se despliega ERBEN ESTUDIO

> Dictado por Juan (2026-08-20): *"vamos a poner el sistema en GitHub y lo
> cubrimos con Vercel, y vamos a vincular al Drive del cliente para armar las
> bases; luego van a tener un local que active el server, la api y los parsers
> y jobs normalizadores y saneadores de las bases"*.

## Las tres capas

```
   ┌──────────────────────────────────────────────────────────┐
   │  VERCEL — solo la PANTALLA                               │
   │  sistema/index.html · HTML+JS, cero datos                │
   │  se actualiza sola con cada push a GitHub                │
   └───────────────────────────┬──────────────────────────────┘
                               │  el NAVEGADOR baja la pantalla de la nube…
                               ▼
   ┌──────────────────────────────────────────────────────────┐
   │  EL EQUIPO DEL ESTUDIO — server + API + parsers/jobs     │
   │  localhost:8310 · la base y las credenciales viven acá   │
   │  el ícono ERBEN ESTUDIO lo prende                        │
   └───────────────────────────┬──────────────────────────────┘
                               │  …y los DATOS los pide acá, sin salir a internet
                               ▼
   ┌──────────────────────────────────────────────────────────┐
   │  DRIVE DEL CLIENTE — el archivo y el respaldo            │
   │  extractos, comprobantes, constancias de DJ              │
   └──────────────────────────────────────────────────────────┘
```

**La propiedad que ordena todo: los datos del cliente NUNCA pasan por Vercel.**
La nube sirve un archivo HTML; el navegador lo ejecuta y le pide los datos a
`localhost`. Vercel no ve un CUIT, ni un importe, ni una clave. Eso no es un
efecto lateral: es la razón de que la API sea local y no serverless.

## Qué va a GitHub y qué no

`.vercelignore` deja subir **solo `sistema/`**. No salen de la máquina:

| Nunca sale | Por qué |
|---|---|
| `C:\SIBRA\estudio\estudio.sqlite3` | es la base con los datos de los clientes |
| credenciales | viven en el Credential Manager de Windows, nunca en archivos |
| sesiones (`atp_storage_*.json`) | son sesiones abiertas de portales fiscales |
| `servidor/` y `parsers/` | corren local; publicarlos no aporta y agranda la superficie |

⚠ El repo **tiene que ser privado**. Aunque no haya secretos adentro, el código
dice cómo opera el estudio con los portales fiscales de sus clientes.

## Cómo la pantalla encuentra el equipo local

`sistema/index.html` decide sola:

- servida desde `localhost` → API en el **mismo origen** (ruta relativa)
- servida desde internet → API en **`http://localhost:8310`**
- y se puede pisar a mano: botón *Cambiar la dirección del equipo* (queda en
  `localStorage`, útil si el server corre en otra máquina de la red)

Si el equipo está apagado, la pantalla lo dice con todas las letras y explica
cómo prenderlo — un "error de red" hace que la gente crea que se perdieron los
datos.

### ⚠ Lo que hay que saber antes de publicar

Una página **https** llamando a **http://localhost** es, para Chrome, un acceso
a red privada (PNA): manda un preflight y solo sigue si el servidor local
contesta `Access-Control-Allow-Private-Network: true`. Ya está puesto en
`servidor/server.py`.

Aun así, esto conviene probarlo en el navegador real del estudio antes de darlo
por hecho: **las reglas de PNA vienen endureciéndose versión a versión de
Chrome** y son la parte frágil de este diseño. Si un día Chrome corta el acceso
a localhost desde https, hay dos salidas, en este orden:

1. **Servir la pantalla desde el equipo local** (como hoy, con el ícono) y
   dejar Vercel solo como copia de referencia/demo. Cuesta cero: es el modo en
   el que ya funciona.
2. Darle al agente local un certificado y un nombre (`https://local.erben...`),
   que es más trabajo y más cosas para mantener.

No hay que atarse a la nube: **el ícono del escritorio funciona sin internet**,
y esa es la garantía de que el estudio nunca queda sin poder trabajar.

## Plan de contingencia — si Chrome corta el acceso a localhost

(Dictado por Juan, 2026-08-20: *"o lo hacemos correr local o ponemos el
servidor en Oracle y la base en Supabase"*. Las dos son válidas, pero **no son
equivalentes: cambian dónde vive el dato**, que es la decisión de fondo.)

### Precisión: qué es lo que se puede romper
No es que Chrome bloquee https. Lo que puede endurecer es **una página https
llamando a `http://localhost`** (Private Network Access). La pantalla sirve
igual; lo que deja de contestar es la API local.

### Salida A — servir la pantalla desde el equipo (lo que ya funciona)
Cuesta **cero**: es el ícono del escritorio. Vercel queda como demo/vidriera.

| | |
|---|---|
| Se pierde | entrar desde cualquier lado |
| Se conserva | los datos nunca viajan · funciona sin internet · una sola pieza |

### Salida B — server en Oracle + base en Supabase
Da acceso desde cualquier lado y respaldo automático, pero hay que mirar dos
cosas antes de elegirla.

**1. Los parsers NO se pueden mudar.** Necesitan un navegador con la sesión y
las credenciales del cliente, y login atendido (reCAPTCHA en ATP, 2FA en los
bancos). Aunque el server esté en Oracle y la base en Supabase, **sigue
haciendo falta el agente local** para scrapear y para llevar la DJ al portal.
O sea: B no reemplaza al equipo local, le **suma** dos piezas más.

```
      A: navegador ─→ equipo local ─→ base local          (1 pieza)
      B: navegador ─→ Oracle ─→ Supabase
                          ↑
                    agente local (parsers)                (3 piezas)
```

**2. Los datos fiscales de terceros pasan a vivir en tu infraestructura.** Hoy
la respuesta a "¿dónde están los datos de mis clientes?" es *"en tu computadora,
no salen de ahí"*. Con B es *"en un servidor nuestro"*. Es defendible, pero es
otra conversación con el cliente y otra responsabilidad.

**La buena noticia de B**: migrar la base ya está resuelto. El ERP hizo el mismo
salto con `ERP/api/pgcompat.py` —un adaptador que imita la API de `sqlite3` que
el código realmente usa— y este server está en las mismas condiciones: **109
llamadas a la base y un solo `db()`**. Se cambia un archivo, no se reescriben
109 consultas. (Habría que tocar además los 6 `except sqlite3.IntegrityError`,
que el ERP resolvió con una tupla de excepciones de los dos motores.)

### Recomendación
**A por defecto.** Un estudio contable trabaja en su oficina, y la simplicidad
de una sola pieza vale más que entrar desde el celular.

**B solo si aparece un requisito real**: varios contadores en lugares distintos,
trabajo remoto sostenido, o que el estudio no quiera depender de una máquina
propia. Ahí el costo extra se justifica.

**No hay que decidir hoy.** El código no se pinta en una esquina: el front ya
sabe apuntar a cualquier dirección (botón *Cambiar la dirección del equipo*) y
la base tiene un solo punto de entrada.

## Pasos para publicar (pendiente de hacer)

1. Crear el repo **privado** `erben-estudio` en GitHub.
2. `git remote add origin …` y `git push -u origin master`.
3. En Vercel: *Add New Project* → importar el repo → framework **Other** →
   Output Directory **`sistema`** (ya está en `vercel.json`).
4. Abrir la URL de Vercel con el equipo local prendido y verificar que la
   pantalla traiga los datos. Si el navegador bloquea el llamado a localhost,
   ver la nota de PNA de arriba.

## Drive del cliente — diseño, todavía sin construir

El Drive es **el archivo y el respaldo**, no la base operativa. La base es el
SQLite local; el Drive guarda lo que hay que poder mostrarle a alguien:

```
Drive del cliente/
  ERBEN ESTUDIO/
    <CUIT> - <razón social>/
      extractos/        <- lo que bajan los jobs de banco
      comprobantes/     <- Mis Comprobantes de ARCA
      djs/              <- constancias de presentación (JSON + captura)
      respaldo/         <- copia de la base, con fecha
```

Reglas que ya sabemos y no hay que volver a descubrir:

- **Una carpeta por CUIT**, nunca mezcladas: es el mismo aislamiento que en la
  base. Un cliente no puede ver los papeles de otro ni por accidente.
- **El dato manda desde la base, el archivo desde el Drive** — misma regla que
  usamos en Obra. Si hay diferencia entre el PDF y la base, gana lo que dice
  el parser que lo leyó, y se anota.
- Los jobs escriben en el Drive **del cliente**, que es de él: si mañana cambia
  de estudio, se lleva sus papeles sin que nadie los transcriba.
- ⚠ Va a hacer falta OAuth de Google con `credentials.json` propio del estudio
  (no reusar el de Obra). Eso todavía no está.

## Los jobs normalizadores y saneadores

La suite de hoy **trae** (`parsers/LEEME.md`). Falta la mitad que **acomoda**:

| Job | Qué hace | Estado |
|---|---|---|
| cargador de extractos | XLS/PDF del banco → `movimientos_banco` | falta |
| cargador de comprobantes | Mis Comprobantes → `facturas` | falta |
| cargador de vencimientos | agenda de ARCA → `vencimientos` | falta |
| saneador de duplicados | doble llave hash + clave natural | falta |
| normalizador de entidades | CUIT como identidad, nombre solo para leer | falta |

⚠ La lección del ERP que este sistema tiene que respetar desde el día uno:
**todo cargador idempotente, con DOBLE llave** (hash del contenido + clave
natural). Volver a bajar el mismo extracto no puede duplicar nada — en el ERP
esa lección costó 255 facturas duplicadas el 16/08.
