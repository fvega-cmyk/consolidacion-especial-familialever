# Transformación Encuesta Especial → BBDD plana

Convierte el modelo padre-hijo de la Encuesta Especial en una tabla plana de 25
columnas, donde cada fila de detalle (HC / PC / BnW / NT) genera una fila con los
datos de la cabecera repetidos hacia abajo.

| | Origen | Destino |
|---|---|---|
| Planilla | `1Iaun6-VepJv23KNnLshefKrWsUmYNNf8UVPk757FYvQ` | `12buRUwCgWxnVL9n-GDuI4eiv8SBFAFW1v80ksR8aAd4` |
| Hojas | `BBDD Encuesta Especial`, `HC`, `PC`, `BnW`, `NT` | `BBDD` |

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

## Puesta en marcha

1. **Service account.** En la consola de GCP, crear (o reutilizar) una service account y generar una llave JSON. No necesita ningún rol de IAM: el acceso se otorga compartiendo las planillas. Habilitar la **Google Sheets API** en ese proyecto.
2. **Compartir las planillas** con el correo de la service account:
   - Planilla origen: permiso de **Lector**.
   - Planilla destino: permiso de **Editor**.
3. **Secret en GitHub.** `Settings → Secrets and variables → Actions → pestaña Secrets → New repository secret`, nombre `GCP_SA_KEY`, y pegar el JSON completo de la llave.
4. **Primera corrida.** `Actions → Transformar Encuesta Especial → Run workflow`, modo `total` con `dry_run` activado para revisar el log. Si los conteos cuadran, repetir sin `dry_run`.

## Programación con cron-job.org

Crear un Personal Access Token con permiso `repo` (o fine-grained con `Contents: read and write`) y configurar dos jobs:

- **URL** (ambos): `https://api.github.com/repos/USUARIO/consolidacion-especial-familialever/dispatches`
- **Método**: `POST`
- **Headers**:
  ```
  Accept: application/vnd.github+json
  Authorization: Bearer TU_TOKEN
  X-GitHub-Api-Version: 2022-11-28
  Content-Type: application/json
  ```

| Job | Frecuencia sugerida | Body |
|---|---|---|
| Incremental | cada 30–60 min en horario hábil | `{"event_type": "incremental"}` |
| Total | una vez al día, de madrugada | `{"event_type": "total"}` |

Como cron-job.org maneja el huso horario de Chile directamente, no hay que
preocuparse del salto UTC-3 / UTC-4.

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
