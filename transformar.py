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

Modos:
  --modo total        Reconstruye la hoja destino completa. Unico modo que
                      elimina filas que ya no existen en el origen.
  --modo incremental  Agrega las filas nuevas y actualiza en el sitio las que
                      cambiaron. No borra nada.

Llave de identidad de una fila: (BU, ID_Detalle).
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
from gspread.utils import rowcol_to_a1

# ---------------------------------------------------------------------------
# CONFIGURACION
# ---------------------------------------------------------------------------

ORIGEN_ID = "1Iaun6-VepJv23KNnLshefKrWsUmYNNf8UVPk757FYvQ"
DESTINO_ID = "12buRUwCgWxnVL9n-GDuI4eiv8SBFAFW1v80ksR8aAd4"

HOJA_PADRE = "BBDD Encuesta Especial"
HOJA_DESTINO = "BBDD"

# El orden de esta lista define el orden de salida dentro de cada encuesta.
HOJAS_HIJAS = ["HC", "PC", "BnW", "NT"]

# USER_ENTERED deja que Sheets interprete fechas y numeros (recomendado para
# tablas dinamicas). RAW los guarda como texto literal.
VALUE_INPUT_OPTION = "USER_ENTERED"

# Columnas excluidas de la comparacion en modo incremental. Usar si una
# columna cambia de formato al escribirse y genera falsos positivos
# (tipicamente "Fecha" u "Hora" si las dos planillas tienen locale distinto).
COLUMNAS_SIN_COMPARAR = set()

# Las 18 columnas de cabecera, en el orden en que deben quedar en el destino.
COLUMNAS_PADRE = [
    "ID",
    "Fecha",
    "Hora",
    "Email",
    "Feria",
    "Día de postura",
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
    "Foto",
]

# Columnas de la hoja hija que se copian tal cual (despues de Producto Final).
COLUMNAS_HIJA_RESTO = ["Marcas", "Tipo de producto", "Cantidad SKU", "Unidades"]

ENCABEZADO_DESTINO = (
    COLUMNAS_PADRE
    + ["BU", "ID_Detalle", "Producto Final"]
    + COLUMNAS_HIJA_RESTO
)

IDX_BU = ENCABEZADO_DESTINO.index("BU")  # 18
IDX_ID_DETALLE = ENCABEZADO_DESTINO.index("ID_Detalle")  # 19

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
MAX_FILAS_POR_LOTE = 5000

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


def leer_tabla(planilla, nombre_hoja):
    """Devuelve (encabezado, filas_no_vacias) de una hoja."""
    hoja = reintentar(planilla.worksheet, nombre_hoja)
    valores = reintentar(hoja.get_all_values)
    if not valores:
        return [], []
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


def valor(fila, posicion):
    if posicion is None or posicion >= len(fila):
        return ""
    return str(fila[posicion]).strip()


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


# ---------------------------------------------------------------------------
# CONSTRUCCION DE LA TABLA PLANA
# ---------------------------------------------------------------------------


def construir_filas(gc):
    planilla = reintentar(gc.open_by_key, ORIGEN_ID)

    log(f"Leyendo cabecera '{HOJA_PADRE}'")
    encabezado, filas_padre = leer_tabla(planilla, HOJA_PADRE)
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
    log(f"  {len(padres)} encuestas leidas" + (f", {duplicados} ID duplicados omitidos" if duplicados else ""))

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
        for fila in filas:
            padre = valor(fila, pos_bbdd)
            if padre not in padres:
                huerfanas += 1
                continue
            registro = (
                [bu, valor(fila, pos_detalle_id), valor(fila, pos_producto)]
                + [valor(fila, p) for p in pos_resto]
            )
            detalle.setdefault((padre, bu), []).append(registro)
        huerfanas_totales += huerfanas
        contadas = sum(len(v) for k, v in detalle.items() if k[1] == bu)
        log(f"  {contadas} filas validas" + (f", {huerfanas} sin cabecera correspondiente" if huerfanas else ""))

    salida = []
    for clave in orden_padres:
        for bu in HOJAS_HIJAS:
            for registro in detalle.get((clave, bu), []):
                salida.append(padres[clave] + registro)

    encuestas_con_detalle = len({k[0] for k in detalle})
    log(
        f"Tabla plana: {len(salida)} filas desde {encuestas_con_detalle} encuestas "
        f"({len(padres) - encuestas_con_detalle} encuestas sin detalle omitidas)"
    )
    if huerfanas_totales:
        log(f"AVISO: {huerfanas_totales} filas de detalle apuntan a un ID_BBDD inexistente")
    return salida


# ---------------------------------------------------------------------------
# ESCRITURA
# ---------------------------------------------------------------------------


def hoja_destino(gc):
    planilla = reintentar(gc.open_by_key, DESTINO_ID)
    return reintentar(planilla.worksheet, HOJA_DESTINO)


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
        rango = f"A{inicio + 1}"
        reintentar(
            hoja.update,
            values=trozo,
            range_name=rango,
            value_input_option=VALUE_INPUT_OPTION,
        )
        log(f"  escritas filas {inicio + 1}-{inicio + len(trozo)}")
    log(f"Reconstruccion total lista: {len(filas)} filas")


def escribir_incremental(hoja, filas, dry_run):
    valores = reintentar(hoja.get_all_values)
    if not valores:
        sys.exit(
            "ERROR: la hoja destino esta vacia. Corre primero '--modo total'."
        )

    encabezado_actual = [norm(c) for c in valores[0][: len(ENCABEZADO_DESTINO)]]
    if encabezado_actual != [norm(c) for c in ENCABEZADO_DESTINO]:
        sys.exit(
            "ERROR: el encabezado de la hoja destino no coincide con el esperado.\n"
            f"  Esperado: {ENCABEZADO_DESTINO}\n"
            f"  Leido   : {valores[0]}\n"
            "Corre '--modo total' para regenerarla."
        )

    existentes = {}
    for numero, fila in enumerate(valores[1:], start=2):
        clave = (norm(valor(fila, IDX_BU)), valor(fila, IDX_ID_DETALLE))
        if clave[1]:
            existentes[clave] = (numero, fila)

    nuevas = []
    cambios = []
    sin_llave = 0
    posiciones_sin_comparar = {
        ENCABEZADO_DESTINO.index(c)
        for c in COLUMNAS_SIN_COMPARAR
        if c in ENCABEZADO_DESTINO
    }

    for fila in filas:
        id_detalle = valor(fila, IDX_ID_DETALLE)
        if not id_detalle:
            sin_llave += 1
            continue
        clave = (norm(valor(fila, IDX_BU)), id_detalle)
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
    if sin_llave:
        log(f"AVISO: {sin_llave} filas de detalle sin ID propio, no se pueden sincronizar")

    if dry_run:
        log("[dry-run] no se escribio nada")
        return

    if nuevas:
        for inicio in range(0, len(nuevas), MAX_FILAS_POR_LOTE):
            trozo = nuevas[inicio : inicio + MAX_FILAS_POR_LOTE]
            reintentar(
                hoja.append_rows,
                trozo,
                value_input_option=VALUE_INPUT_OPTION,
                table_range="A1",
            )
        log(f"  {len(nuevas)} filas agregadas al final")

    if cambios:
        ultima = rowcol_to_a1(1, len(ENCABEZADO_DESTINO))[0]
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
        log(f"  {len(cambios)} filas actualizadas en el sitio")


# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--modo",
        choices=["total", "incremental"],
        required=True,
        help="total reconstruye todo; incremental agrega y actualiza",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="calcula y reporta sin escribir en la planilla destino",
    )
    args = parser.parse_args()

    log(f"Inicio | modo={args.modo} | dry_run={args.dry_run}")
    gc = cliente_sheets()
    filas = construir_filas(gc)
    hoja = hoja_destino(gc)

    if args.modo == "total":
        escribir_total(hoja, filas, args.dry_run)
    else:
        escribir_incremental(hoja, filas, args.dry_run)

    log("Fin")


if __name__ == "__main__":
    main()
