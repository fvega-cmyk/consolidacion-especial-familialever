#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Transforma la Encuesta Especial (modelo padre-hijo) en una tabla plana.

Origen  : planilla con la hoja cabecera "BBDD Encuesta Especial" y las hojas
          de detalle HC, PC, BnW y NT (vinculadas por ID_BBDD).
Destino : planilla con una sola hoja "BBDD", donde cada fila de detalle
          genera una fila con los datos de la cabecera repetidos, la columna
          BU con el nombre de la hoja de origen y el ID de la fila hija en
          ID_Detalle.

Las encuestas sin ninguna fila de detalle tambien se escriben: van una sola
vez, con BU, ID_Detalle y las columnas de producto vacias.

HIPERVINCULOS
Las columnas de COLUMNAS_HIPERVINCULO no se copian como formula, porque una
formula del origen puede apuntar a otra celda de su misma fila (por ejemplo
=HIPERVINCULO(S2;"ver")) y esa referencia no significa nada en el destino: al
repetirse la cabecera en varias filas de detalle, todas quedarian apuntando a
la misma celda equivocada. En su lugar se lee el campo 'hyperlink' de los
metadatos de la celda, que entrega la URL ya resuelta sin importar como se
construyo (URL en texto plano, formula HIPERVINCULO o enlace insertado como
formato), y se escribe esa URL en el destino.

Modos:
  --modo total        Reconstruye la hoja destino completa. Unico modo que
                      elimina filas que ya no existen en el origen.
  --modo incremental  Agrega las filas nuevas, actualiza en el sitio las que
                      cambiaron y borra las filas vacias cuya encuesta ya
                      tiene detalle. No borra nada mas.

Llave de identidad de una fila: (BU, ID_Detalle, ID).
El ID de la cabecera forma parte de la llave porque las filas sin detalle
tienen BU e ID_Detalle vacios y solo se distinguen por el.
"""

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError
from gspread.utils import ValueRenderOption, rowcol_to_a1

# ---------------------------------------------------------------------------
# CONFIGURACION
# ---------------------------------------------------------------------------

ORIGEN_ID = "1Iaun6-VepJv23KNnLshefKrWsUmYNNf8UVPk757FYvQ"
DESTINO_ID = "12buRUwCgWxnVL9n-GDuI4eiv8SBFAFW1v80ksR8aAd4"

HOJA_PADRE = "BBDD Encuesta Especial"
HOJA_DESTINO = "BBDD"

# El orden de esta lista define el orden de salida dentro de cada encuesta.
HOJAS_HIJAS = ["HC", "PC", "BnW", "NT"]

# USER_ENTERED hace que Sheets reconozca la URL y la muestre como link.
VALUE_INPUT_OPTION = "USER_ENTERED"

# Que se escribe en las columnas de link:
#   "url"     -> la URL en texto plano. Sheets la muestra como link. Inmune a
#                la configuracion regional y estable para la comparacion del
#                incremental. Es el valor recomendado.
#   "formula" -> =HIPERVINCULO("url"; "texto visible"), conserva el texto del
#                origen. Ojo: el separador de argumentos depende del locale de
#                cada planilla, y si difieren el incremental reescribira todas
#                las filas en cada corrida.
FORMATO_LINK = "url"
SEPARADOR_FORMULA = ";"

# Columnas excluidas de la comparacion en modo incremental. Usar si una
# columna cambia de formato al escribirse y genera falsos positivos
# (tipicamente "Fecha" u "Hora" si las dos planillas tienen locale distinto).
COLUMNAS_SIN_COMPARAR = set()

# Las 26 columnas de cabecera, en el orden en que deben quedar en el destino.
# Los nombres se comparan sin acentos, sin signos y con los espacios
# normalizados, asi que un doble espacio en un encabezado no rompe nada.
COLUMNAS_PADRE = [
    "ID",
    "Fecha",
    "Hora",
    "Email",
    "Feria",
    "Día de postura",
    "RUT",
    "Miembro",
    "Tipo",
    "Club",
    "Toldo",
    "Cantidad Toldo 3x3",
    "Cantidad Toldo 4,5x3",
    "Cantidad Estructura de metal con tela",
    "Cantidad Carro",
    "¿El toldo 3X3 es Familia Lever?",
    "¿El toldo 4,5X3 es Familia Lever?",
    "Categorías",
    "Foto Puesto General",
    "Foto Productos Unilever 1",
    "Foto Productos Unilever 2",
    "Foto Productos Unilever 3",
    "Link Foto 1",
    "Link Foto 2",
    "Link Foto 3",
    "Link Foto 4",
]

# Columnas cuyo contenido es un hipervinculo y se resuelve a URL.
COLUMNAS_HIPERVINCULO = ["Link Foto 1", "Link Foto 2", "Link Foto 3", "Link Foto 4"]

# Columnas de la hoja hija que se copian tal cual (despues de Producto Final).
COLUMNAS_HIJA_RESTO = ["Marcas", "Tipo de producto", "Cantidad SKU", "Unidades"]

ENCABEZADO_DESTINO = (
    COLUMNAS_PADRE
    + ["BU", "ID_Detalle", "Producto Final"]
    + COLUMNAS_HIJA_RESTO
)

IDX_ID = ENCABEZADO_DESTINO.index("ID")                  # 0
IDX_BU = ENCABEZADO_DESTINO.index("BU")                  # 26
IDX_ID_DETALLE = ENCABEZADO_DESTINO.index("ID_Detalle")  # 27

# Bloque de columnas de detalle en blanco, para las encuestas sin productos.
DETALLE_VACIO = [""] * (len(ENCABEZADO_DESTINO) - len(COLUMNAS_PADRE))

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
MAX_FILAS_POR_LOTE = 5000

CAMPOS_HIPERVINCULO = (
    "sheets/data/rowData/values("
    "formattedValue,hyperlink,textFormatRuns/format/link/uri)"
)

# ---------------------------------------------------------------------------
# UTILIDADES
# ---------------------------------------------------------------------------


def log(mensaje):
    ahora = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ahora} UTC] {mensaje}", flush=True)


def norm(texto):
    """Normaliza un encabezado o valor para comparar: sin acentos, sin signos
    de interrogacion, sin espacios repetidos, en minusculas."""
    if texto is None:
        return ""
    texto = str(texto).strip().lower()
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = texto.replace("¿", "").replace("?", "")
    texto = re.sub(r"\s+", " ", texto)
    return texto


def letra_columna(numero):
    """Numero de columna -> letras. 26 -> 'Z', 33 -> 'AG'. Sirve para
    cualquier ancho, a diferencia de recortar el primer caracter."""
    return re.sub(r"\d+", "", rowcol_to_a1(1, numero))


def reintentar(funcion, *args, **kwargs):
    """Reintenta ante errores transitorios de la API de Sheets (429 / 5xx)."""
    espera = 5
    for intento in range(1, 6):
        try:
            return funcion(*args, **kwargs)
        except APIError as error:
            codigo = getattr(error.response, "status_code", None)
            if codigo not in (429, 500, 502, 503, 504) or intento == 5:
                raise
            log(f"  API respondio {codigo}, reintento {intento}/5 en {espera}s")
            time.sleep(espera)
            espera *= 2
    raise RuntimeError("Reintentos agotados")


def cliente_sheets():
    bruto = os.environ.get("GCP_SA_KEY")
    if not bruto:
        sys.exit("ERROR: falta la variable de entorno GCP_SA_KEY")
    try:
        info = json.loads(bruto)
    except json.JSONDecodeError as error:
        sys.exit(f"ERROR: GCP_SA_KEY no es un JSON valido ({error})")
    credenciales = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(credenciales)


def valor(fila, posicion):
    if posicion is None or posicion >= len(fila):
        return ""
    return str(fila[posicion]).strip()


def posiciones_link(encabezado):
    objetivo = {norm(c) for c in COLUMNAS_HIPERVINCULO}
    return [i for i, c in enumerate(encabezado) if norm(c) in objetivo]


def url_de_celda(celda):
    """URL resuelta de una celda, venga de donde venga."""
    directa = celda.get("hyperlink")
    if directa:
        return directa
    for tramo in celda.get("textFormatRuns", []):
        uri = tramo.get("format", {}).get("link", {}).get("uri")
        if uri:
            return uri
    return ""


def componer_link(url, texto):
    if FORMATO_LINK != "formula" or not texto or texto == url:
        return url
    escapado = texto.replace('"', '""')
    return f'=HIPERVINCULO("{url}"{SEPARADOR_FORMULA}"{escapado}")'


def resolver_hipervinculos(planilla, nombre_hoja, valores):
    """Reemplaza las celdas de las columnas de link por su URL resuelta,
    leyendo los metadatos de celda en vez de los valores."""
    if not valores:
        return valores
    posiciones = posiciones_link(valores[0])
    if not posiciones:
        return valores

    primera, ultima = min(posiciones) + 1, max(posiciones) + 1
    rango = (
        f"'{nombre_hoja}'!{letra_columna(primera)}:{letra_columna(ultima)}"
    )
    metadatos = reintentar(
        planilla.fetch_sheet_metadata,
        {
            "includeGridData": "true",
            "ranges": rango,
            "fields": CAMPOS_HIPERVINCULO,
        },
    )
    hojas = metadatos.get("sheets", [])
    if not hojas:
        log(f"AVISO: no se pudieron leer los metadatos de link de '{nombre_hoja}'")
        return valores
    datos = hojas[0].get("data", [{}])[0]
    filas_meta = datos.get("rowData", [])

    desplazamiento = primera - 1
    resueltos = 0
    sin_url = 0
    for numero in range(1, len(valores)):
        celdas = (
            filas_meta[numero].get("values", []) if numero < len(filas_meta) else []
        )
        for posicion in posiciones:
            indice = posicion - desplazamiento
            celda = celdas[indice] if indice < len(celdas) else {}
            url = url_de_celda(celda)
            texto = str(celda.get("formattedValue", "")).strip()
            while len(valores[numero]) <= posicion:
                valores[numero].append("")
            if url:
                valores[numero][posicion] = componer_link(url, texto)
                resueltos += 1
            elif texto:
                # Hay texto pero ninguna URL asociada: la celda no es un link.
                valores[numero][posicion] = texto
                sin_url += 1
            else:
                valores[numero][posicion] = ""

    log(f"  links resueltos: {resueltos}")
    if sin_url:
        log(
            f"  AVISO: {sin_url} celdas de link tienen texto pero ninguna URL "
            "asociada; se copian como texto"
        )
    return valores


def leer_tabla(planilla, nombre_hoja, resolver_links=False):
    """Devuelve (encabezado, filas_no_vacias) de una hoja."""
    hoja = reintentar(planilla.worksheet, nombre_hoja)
    valores = reintentar(hoja.get_all_values)
    if not valores:
        return [], []
    if resolver_links:
        valores = resolver_hipervinculos(planilla, nombre_hoja, valores)
    encabezado = valores[0]
    filas = [f for f in valores[1:] if any(str(c).strip() for c in f)]
    return encabezado, filas


def indice_columna(encabezado, nombre, hoja, obligatoria=True):
    objetivo = norm(nombre)
    for posicion, celda in enumerate(encabezado):
        if norm(celda) == objetivo:
            return posicion
    if obligatoria:
        sys.exit(
            f"ERROR: la hoja '{hoja}' no tiene la columna '{nombre}'. "
            f"Encabezado leido: {encabezado}"
        )
    return None


def indice_columna_producto(encabezado, hoja):
    """Busca la columna de producto por prefijo, para tolerar que se llame
    'Producto HC', 'Producto BnW', etc."""
    for posicion, celda in enumerate(encabezado):
        if norm(celda).startswith("producto"):
            return posicion
    sys.exit(
        f"ERROR: la hoja '{hoja}' no tiene ninguna columna que empiece con "
        f"'Producto'. Encabezado leido: {encabezado}"
    )


def llave(fila):
    """Identidad de una fila del destino: (BU, ID_Detalle, ID de cabecera)."""
    return (
        norm(valor(fila, IDX_BU)),
        valor(fila, IDX_ID_DETALLE),
        valor(fila, IDX_ID),
    )


def equivalentes(a, b):
    """Compara dos celdas tolerando formato numerico (coma o punto decimal)."""
    a, b = str(a).strip(), str(b).strip()
    if a == b:
        return True
    try:
        return float(a.replace(".", "").replace(",", ".")) == float(
            b.replace(".", "").replace(",", ".")
        )
    except ValueError:
        return a.casefold() == b.casefold()


def agrupar_consecutivos(numeros):
    """[2,3,4,9,10] -> [(2,4),(9,10)]. Para borrar filas por rango."""
    rangos = []
    for numero in sorted(numeros):
        if rangos and numero == rangos[-1][1] + 1:
            rangos[-1][1] = numero
        else:
            rangos.append([numero, numero])
    return [tuple(r) for r in rangos]


# ---------------------------------------------------------------------------
# CONSTRUCCION DE LA TABLA PLANA
# ---------------------------------------------------------------------------


def construir_filas(gc):
    planilla = reintentar(gc.open_by_key, ORIGEN_ID)

    log(f"Leyendo cabecera '{HOJA_PADRE}'")
    encabezado, filas_padre = leer_tabla(planilla, HOJA_PADRE, resolver_links=True)
    pos_id = indice_columna(encabezado, "ID", HOJA_PADRE)
    posiciones_padre = [
        indice_columna(encabezado, c, HOJA_PADRE) for c in COLUMNAS_PADRE
    ]

    padres = {}
    orden_padres = []
    duplicados = 0
    for fila in filas_padre:
        clave = valor(fila, pos_id)
        if not clave:
            continue
        if clave in padres:
            duplicados += 1
            continue
        padres[clave] = [valor(fila, p) for p in posiciones_padre]
        orden_padres.append(clave)
    log(
        f"  {len(padres)} encuestas leidas"
        + (f", {duplicados} ID duplicados omitidos" if duplicados else "")
    )

    detalle = {}
    huerfanas_totales = 0
    for bu in HOJAS_HIJAS:
        log(f"Leyendo detalle '{bu}'")
        encabezado, filas = leer_tabla(planilla, bu)
        if not encabezado:
            log(f"  hoja '{bu}' vacia")
            continue
        pos_detalle_id = indice_columna(encabezado, "ID", bu)
        pos_bbdd = indice_columna(encabezado, "ID_BBDD", bu)
        pos_producto = indice_columna_producto(encabezado, bu)
        pos_resto = [indice_columna(encabezado, c, bu) for c in COLUMNAS_HIJA_RESTO]

        huerfanas = 0
        sin_id = 0
        for fila in filas:
            padre = valor(fila, pos_bbdd)
            if padre not in padres:
                huerfanas += 1
                continue
            id_detalle = valor(fila, pos_detalle_id)
            if not id_detalle:
                sin_id += 1
            registro = [bu, id_detalle, valor(fila, pos_producto)] + [
                valor(fila, p) for p in pos_resto
            ]
            detalle.setdefault((padre, bu), []).append(registro)
        huerfanas_totales += huerfanas
        contadas = sum(len(v) for k, v in detalle.items() if k[1] == bu)
        log(
            f"  {contadas} filas validas"
            + (f", {huerfanas} sin cabecera correspondiente" if huerfanas else "")
            + (f", {sin_id} sin ID propio" if sin_id else "")
        )

    salida = []
    sin_detalle = 0
    for clave in orden_padres:
        registros = []
        for bu in HOJAS_HIJAS:
            registros.extend(detalle.get((clave, bu), []))
        if registros:
            for registro in registros:
                salida.append(padres[clave] + registro)
        else:
            salida.append(padres[clave] + list(DETALLE_VACIO))
            sin_detalle += 1

    log(
        f"Tabla plana: {len(salida)} filas desde {len(padres)} encuestas "
        f"({sin_detalle} sin detalle, escritas con producto vacio)"
    )
    if huerfanas_totales:
        log(
            f"AVISO: {huerfanas_totales} filas de detalle apuntan a un "
            f"ID_BBDD inexistente y quedaron fuera"
        )
    return salida


# ---------------------------------------------------------------------------
# ESCRITURA
# ---------------------------------------------------------------------------


def hoja_destino(gc):
    planilla = reintentar(gc.open_by_key, DESTINO_ID)
    return planilla, reintentar(planilla.worksheet, HOJA_DESTINO)


def leer_destino(hoja):
    """Devuelve {llave: (numero_de_fila, fila)} validando el encabezado.

    Se lee en modo FORMULA para que, si FORMATO_LINK es "formula", la
    comparacion del incremental vea la misma formula que se escribio y no el
    texto visible. Con FORMATO_LINK = "url" da lo mismo: una URL en texto
    plano se lee igual en los dos modos."""
    valores = reintentar(
        hoja.get_values, value_render_option=ValueRenderOption.formula
    )
    if not valores:
        sys.exit("ERROR: la hoja destino esta vacia. Corre primero '--modo total'.")

    esperado = [norm(c) for c in ENCABEZADO_DESTINO]
    leido = [norm(c) for c in valores[0][: len(ENCABEZADO_DESTINO)]]
    if leido != esperado:
        sys.exit(
            "ERROR: el encabezado de la hoja destino no coincide con el esperado.\n"
            f"  Esperado ({len(ENCABEZADO_DESTINO)} columnas): {ENCABEZADO_DESTINO}\n"
            f"  Leido    ({len(valores[0])} columnas): {valores[0]}\n"
            "Corre '--modo total' para regenerarla."
        )

    indice = {}
    for numero, fila in enumerate(valores[1:], start=2):
        indice[llave(fila)] = (numero, fila)
    return indice


def escribir_total(hoja, filas, dry_run):
    total = len(filas) + 1
    columnas = len(ENCABEZADO_DESTINO)
    if dry_run:
        log(f"[dry-run] Reconstruccion total: escribiria {len(filas)} filas")
        return

    log("Limpiando hoja destino")
    reintentar(hoja.clear)
    reintentar(hoja.resize, rows=max(total, 2), cols=columnas)

    bloque = [ENCABEZADO_DESTINO] + filas
    for inicio in range(0, len(bloque), MAX_FILAS_POR_LOTE):
        trozo = bloque[inicio : inicio + MAX_FILAS_POR_LOTE]
        reintentar(
            hoja.update,
            values=trozo,
            range_name=f"A{inicio + 1}",
            value_input_option=VALUE_INPUT_OPTION,
        )
        log(f"  escritas filas {inicio + 1}-{inicio + len(trozo)}")
    log(f"Reconstruccion total lista: {len(filas)} filas")


def escribir_incremental(hoja, filas, dry_run):
    existentes = leer_destino(hoja)

    # Encuestas que ya tienen al menos una fila con producto en el origen.
    ids_con_detalle = {
        valor(fila, IDX_ID) for fila in filas if valor(fila, IDX_BU)
    }

    # Filas vacias en el destino cuya encuesta ya tiene detalle: quedaron
    # obsoletas y hay que sacarlas para no duplicar la encuesta.
    obsoletas = [
        numero
        for clave, (numero, _) in existentes.items()
        if not clave[0] and clave[2] in ids_con_detalle
    ]

    if obsoletas:
        log(f"Filas vacias que ya tienen detalle y se eliminan: {len(obsoletas)}")
        if not dry_run:
            for inicio, fin in sorted(agrupar_consecutivos(obsoletas), reverse=True):
                reintentar(hoja.delete_rows, inicio, fin)
            # Los numeros de fila cambiaron: hay que releer el destino.
            existentes = leer_destino(hoja)

    nuevas = []
    cambios = []
    posiciones_sin_comparar = {
        ENCABEZADO_DESTINO.index(c)
        for c in COLUMNAS_SIN_COMPARAR
        if c in ENCABEZADO_DESTINO
    }

    for fila in filas:
        clave = llave(fila)
        if clave not in existentes:
            nuevas.append(fila)
            continue
        numero, actual = existentes[clave]
        distinto = any(
            not equivalentes(valor(actual, i), fila[i])
            for i in range(len(ENCABEZADO_DESTINO))
            if i not in posiciones_sin_comparar
        )
        if distinto:
            cambios.append((numero, fila))

    log(f"Incremental: {len(nuevas)} nuevas, {len(cambios)} modificadas")

    if dry_run:
        log("[dry-run] no se escribio nada")
        return

    if nuevas:
        for inicio in range(0, len(nuevas), MAX_FILAS_POR_LOTE):
            reintentar(
                hoja.append_rows,
                nuevas[inicio : inicio + MAX_FILAS_POR_LOTE],
                value_input_option=VALUE_INPUT_OPTION,
                table_range="A1",
            )
        log(f"  {len(nuevas)} filas agregadas al final")

    if cambios:
        ultima = letra_columna(len(ENCABEZADO_DESTINO))
        lote = [
            {"range": f"A{numero}:{ultima}{numero}", "values": [fila]}
            for numero, fila in cambios
        ]
        for inicio in range(0, len(lote), 500):
            reintentar(
                hoja.batch_update,
                lote[inicio : inicio + 500],
                value_input_option=VALUE_INPUT_OPTION,
            )
        log(f"  {len(cambios)} filas actualizadas en el sitio (A:{ultima})")


# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--modo",
        choices=["total", "incremental"],
        required=True,
        help="total reconstruye todo; incremental agrega, actualiza y limpia vacias",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="calcula y reporta sin escribir en la planilla destino",
    )
    args = parser.parse_args()

    log(f"Inicio | modo={args.modo} | dry_run={args.dry_run} | links={FORMATO_LINK}")
    gc = cliente_sheets()
    filas = construir_filas(gc)
    _, hoja = hoja_destino(gc)

    if args.modo == "total":
        escribir_total(hoja, filas, args.dry_run)
    else:
        escribir_incremental(hoja, filas, args.dry_run)

    log("Fin")


if __name__ == "__main__":
    main()
