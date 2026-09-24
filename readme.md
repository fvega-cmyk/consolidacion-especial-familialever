# Transformación Encuesta Especial → BBDD plana

Convierte el modelo padre-hijo de la Encuesta Especial en una tabla plana de 33
columnas, donde cada fila de detalle (HC / PC / BnW / NT) genera una fila con los
datos de la cabecera repetidos hacia abajo.

| | Origen | Destino |
|---|---|---|
| Planilla | `1Iaun6-VepJv23KNnLshefKrWsUmYNNf8UVPk757FYvQ` | `12buRUwCgWxnVL9n-GDuI4eiv8SBFAFW1v80ksR8aAd4` |
| Hojas | `BBDD Encuesta Especial`, `HC`, `PC`, `BnW`, `NT` | `BBDD` |

## Estructura de la hoja destino

26 columnas de cabecera, terminando en las cuatro de link:

```
ID · Fecha · Hora · Email · Feria · Día de postura · RUT · Miembro · Tipo ·
Club · Toldo · Cantidad Toldo 3x3 · Cantidad Toldo 4,5x3 ·
Cantidad Estructura de metal con tela · Cantidad Carro ·
¿El toldo 3X3 es Familia Lever? · ¿El toldo 4,5X3 es Familia Lever? ·
Categorías · Foto Puesto General · Foto Productos Unilever 1 ·
Foto Productos Unilever 2 · Foto Productos Unilever 3 ·
Link Foto 1 · Link Foto 2 · Link Foto 3 · Link Foto 4
```

## Hipervínculos

Las columnas de `COLUMNAS_HIPERVINCULO` (las cuatro `Link Foto`) se leen en modo
FORMULA, en el origen y en el destino. Sin eso, una fórmula
`=HIPERVINCULO("url"; "texto")` se leería como `texto` y la URL se perdería; y
como el destino se leería distinto que el origen, el incremental marcaría todas
las filas como modificadas en cada corrida.

Qué sobrevive al traspaso:

| Cómo está guardado en el origen | Viaja al consolidado |
|---|---|
| URL en texto plano | Sí, y Sheets la vuelve a mostrar como link |
| Fórmula `=HIPERVINCULO(...)` | Sí, se copia la fórmula completa |
| Enlace insertado como formato (Insertar → Enlace) | **No**, la URL vive en el formato de la celda y la API de valores no la ve |

El tercer caso se avisa en el log contando las celdas de link cuyo contenido no
es ni URL ni fórmula. La solución es que el origen guarde la URL en texto plano
o una fórmula `HIPERVINCULO`.

`VALUE_INPUT_OPTION` debe quedarse en `USER_ENTERED`: con `RAW` las fórmulas se
escribirían como texto literal y los links dejarían de funcionar.

más las 7 del detalle:

```
BU · ID_Detalle · Producto Final · Marcas · Tipo de producto ·
Cantidad SKU · Unidades
```

Los nombres se comparan sin acentos, sin signos de interrogación y con los
espacios normalizados, así que un doble espacio en un encabezado no rompe nada.
Si el encabezado del destino no calza con esta lista, el script aborta **antes**
de escribir y muestra en el log el esperado contra el leído.

## Reglas de negocio implementadas

- Una fila de salida por cada fila de detalle. `BU` guarda el nombre de la hoja de origen e `ID_Detalle` el `ID` propio de esa hoja hija.
- **Las encuestas sin ninguna fila de detalle también se escriben**, una sola vez, con `BU`, `ID_Detalle` y las cinco columnas de producto vacías.
- Las filas de detalle cuyo `ID_BBDD` no existe en la cabecera se descartan y se avisan en el log.
- Orden de salida: por orden de la encuesta, y dentro de cada una HC → PC → BnW → NT.
- La columna de producto se detecta por prefijo (`Producto ...`), así que tolera que cambie el sufijo en cualquier hoja.

Total de filas = filas de HC + PC + BnW + NT + encuestas sin detalle.

## Llave de identidad

`BU + ID_Detalle + ID`

El `ID` de la cabecera forma parte de la llave porque las filas sin detalle tienen
`BU` e `ID_Detalle` vacíos, y solo el `ID` las distingue entre sí.

## Modos

| Modo | Agrega | Actualiza | Elimina | Uso |
|---|---|---|---|---|
| `incremental` | Sí | Sí, en el sitio | Solo filas vacías obsoletas | Varias veces al día |
| `total` | Reconstruye | Reconstruye | Sí, todo lo que ya no existe | Una vez al día |

**Filas vacías obsoletas:** si una encuesta entró sin productos y después le
agregaron uno, la fila vacía que ya está escrita quedaría duplicando la encuesta.
El incremental la elimina antes de agregar las filas nuevas. Es el único borrado
que hace: no elimina filas con producto aunque desaparezcan del origen, porque
una lectura parcial por un error de la API podría vaciar la planilla. Ese trabajo
queda en el modo total.

El incremental agrega al final, así que durante el día el orden se va desordenando
respecto de la encuesta. La reconstrucción total lo vuelve a dejar ordenado.

## Cambios de estructura en las planillas

Al agregar, quitar o renombrar una columna hay que hacer tres cosas, en este orden:

1. Aplicar el cambio en la planilla origen y en la de destino.
2. Actualizar `COLUMNAS_PADRE` (o `COLUMNAS_HIJA_RESTO`) en `transformar.py`. Los índices, el ancho de la hoja y el rango de actualización se derivan solos de esa lista.
3. Correr una vez en modo `total`, no incremental, para que las filas ya escritas queden con la estructura nueva.

## Puesta en marcha

1. **Service account.** En la consola de GCP, crear (o reutilizar) una service account y generar una llave JSON. No necesita ningún rol de IAM: el acceso se otorga compartiendo las planillas. Habilitar la **Google Sheets API** en ese proyecto.
2. **Compartir las planillas** con el correo de la service account: origen como **Lector**, destino como **Editor**.
3. **Secret en GitHub.** `Settings → Secrets and variables → Actions → pestaña Secrets → New repository secret`, nombre `GCP_SA_KEY`, y pegar el JSON completo.
4. **Primera corrida.** `Actions → Transformar Encuesta Especial → Run workflow`, modo `total` con `dry_run` activado para revisar el log.

## Programación con cron-job.org

Dos jobs, ambos `POST` a
`https://api.github.com/repos/USUARIO/consolidacion-especial-familialever/dispatches`
con los headers `Accept: application/vnd.github+json`,
`Authorization: Bearer TOKEN`, `X-GitHub-Api-Version: 2022-11-28` y
`Content-Type: application/json`.

| Job | Frecuencia | Body |
|---|---|---|
| Incremental | cada 30–60 min en horario hábil | `{"event_type": "incremental"}` |
| Total | una vez al día, de madrugada | `{"event_type": "total"}` |

La respuesta correcta es **204 sin contenido**. `repository_dispatch` solo dispara
workflows que estén en la rama por defecto del repositorio.

## Ajustes en `transformar.py`

- `VALUE_INPUT_OPTION`: `USER_ENTERED` (por defecto) deja que Sheets interprete fechas y números. Cambiar a `RAW` si se prefiere texto literal.
- `COLUMNAS_SIN_COMPARAR`: si el incremental reporta que **todas** las filas cambiaron en cada corrida, la causa casi siempre es que las dos planillas tienen configuración regional distinta y `Fecha` u `Hora` se leen con formato diferente. Agregarlas aquí:
  ```python
  COLUMNAS_SIN_COMPARAR = {"Fecha", "Hora"}
  ```
- `HOJAS_HIJAS`: agregar una BU nueva es sumar el nombre de la hoja a esta lista, siempre que respete la estructura `ID | ID_BBDD | Producto X | Marcas | Tipo de producto | Cantidad SKU | Unidades`.

## Prueba local

```bash
pip install -r requirements.txt
export GCP_SA_KEY="$(cat llave.json)"
python transformar.py --modo total --dry-run
```
