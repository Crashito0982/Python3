# -*- coding: utf-8 -*-
"""
PROSEGUR - Flujo unificado (v2)
- Soporta subcarpetas por delegación dentro de PENDIENTES (OVD, ASU, ENC, CON, CDE)
- Recorre PENDIENTES de forma recursiva
- Preserva la estructura de subcarpetas al mover a PROCESADO
- AGENCIA por prioridad: documento -> nombre de archivo -> carpeta (delegación)
- Consolida por tipo en CONSOLIDADO/<dd-mm-YYYY>/, sobrescribiendo si existe (reproceso)
"""

import os
import re
import shutil
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Optional, Union, List, Dict, Any, Tuple

import pandas as pd
import openpyxl  # noqa: F401


###############################
### UBICACION / RUTAS BASE  ###
###############################

def _find_prosegur_base() -> Path:
    """
    Intenta ser robusto:
      - Si existe ./PROSEGUR desde el cwd, usarlo.
      - Si este archivo existe (al correr como .py), usar su carpeta para resolver ./PROSEGUR.
      - Si no existe, crear ./PROSEGUR en el cwd.
    """
    try:
        here = Path(__file__).resolve().parent  # cuando se ejecuta como .py
    except NameError:
        here = Path.cwd()  # cuando se ejecuta como notebook
    # 1) cwd
    if (Path.cwd() / 'PROSEGUR').is_dir():
        return Path.cwd() / 'PROSEGUR'
    # 2) junto al script
    if (here / 'PROSEGUR').is_dir():
        return here / 'PROSEGUR'
    # 3) crear en cwd
    (Path.cwd() / 'PROSEGUR').mkdir(parents=True, exist_ok=True)
    return Path.cwd() / 'PROSEGUR'


PROSEGUR_BASE = _find_prosegur_base()
FULL_PATH = str(PROSEGUR_BASE / 'PENDIENTES')
FULL_PATH_PROCESADO = str(PROSEGUR_BASE / 'PROCESADO')
FULL_PATH_CONSOLIDADO = str(PROSEGUR_BASE / 'CONSOLIDADO')

os.makedirs(FULL_PATH, exist_ok=True)
os.makedirs(FULL_PATH_PROCESADO, exist_ok=True)
os.makedirs(FULL_PATH_CONSOLIDADO, exist_ok=True)


#########################
### FUNCIONES HELPERS ###
#########################

def _strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')


def _first_non_empty_after(row_vals: List[str], start_idx: int) -> Optional[int]:
    for i in range(start_idx + 1, len(row_vals)):
        if str(row_vals[i]).strip() != '':
            return i
    return None


def _get_cell(row_vals: List[str], idx: Optional[int], default: str = '') -> str:
    if idx is None or idx >= len(row_vals):
        return default
    v = str(row_vals[idx]).strip()
    return v if v != '' else default


def _only_digits(s: str) -> str:
    return ''.join(ch for ch in str(s) if ch.isdigit())


def _ordenar_y_renombrar_columnas_ec(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = [str(c).replace(' ', '_').upper() for c in df.columns]
    rename_map = {'FECHA_OPER': 'FECHA', 'MOTIVO MOVIMIENTO': 'MOTIVO_MOVIMIENTO'}
    df = df.rename(columns=rename_map)
    orden_final = [
        'FECHA', 'SUCURSAL', 'RECIBO', 'BULTOS', 'GUARANIES', 'DOLARES',
        'ING_EGR', 'CLASIFICACION', 'FECHA_ARCHIVO', 'MOTIVO_MOVIMIENTO', 'AGENCIA'
    ]
    for col in orden_final:
        if col not in df.columns:
            df[col] = ''
    return df[orden_final]


def _ordenar_y_renombrar_columnas_ec_banco(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = [str(c).replace(' ', '_').upper() for c in df.columns]
    df = df.rename(columns={
        'FECHA_OPER': 'FECHA',
        'MONTO': 'IMPORTE',
        'MOTIVO MOVIMIENTO': 'MOTIVO_MOVIMIENTO',
    })
    orden_final = [
        'FECHA', 'SUCURSAL', 'RECIBO', 'BULTOS', 'IMPORTE', 'MONEDA',
        'ING_EGR', 'CLASIFICACION', 'FECHA_ARCHIVO', 'MOTIVO_MOVIMIENTO',
        'AGENCIA', 'HOJA_ORIGEN'
    ]
    for col in orden_final:
        if col not in df.columns:
            df[col] = ''
    return df[orden_final]


def _ordenar_y_renombrar_columnas_bultos(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = [str(c).replace(' ', '_').upper() for c in df.columns]
    rename_map = {'FECHA_OPER': 'FECHA'}
    df = df.rename(columns=rename_map)
    orden_final = [
        'FECHA', 'SUCURSAL', 'RECIBO', 'BULTOS', 'MONEDA', 'IMPORTE',
        'ING_EGR', 'CLASIFICACION', 'FECHA_ARCHIVO', 'MOTIVO_MOVIMIENTO', 'AGENCIA'
    ]
    for col in orden_final:
        if col not in df.columns:
            df[col] = ''
    return df[orden_final]


def _txt(x) -> str:
    return "" if pd.isna(x) else str(x).strip()


def _upper(x) -> str:
    return re.sub(r"\s+", " ", _txt(x)).upper()


def _to_int(x) -> Optional[int]:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        try:
            return int(round(float(x)))
        except Exception:
            pass
    s = _txt(x).replace("\xa0", " ").strip()
    if s == "":
        return None
    if re.fullmatch(r"\d+\.\d+", s):
        return int(float(s))
    digits = re.sub(r"[^\d\-]", "", s)
    if digits in ("", "-", "--"):
        return None
    try:
        return int(digits)
    except Exception:
        return None


# --- AGENCIA (5 delegaciones) ---
AGENCIA_ALIASES = {
    'OVD': ['OVD', 'OVIEDO'],
    'ENC': ['ENC', 'ENCARNACION', 'ENCARNACIÓN', 'ENCA'],
    'CON': ['CON', 'CONCEPCION', 'CONCEPCIÓN'],
    'ASU': ['ASU', 'ASUNCION', 'ASUNCIÓN'],
    'CDE': ['CDE', 'CIUDAD DEL ESTE', 'C. DEL ESTE', 'C DEL ESTE', 'CDE.'],
}


def _agencia_code_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    t = _strip_accents(str(text)).upper()
    # por código exacto
    for code in AGENCIA_ALIASES.keys():
        if re.search(r'\b' + re.escape(code) + r'\b', t):
            return code
    # por alias
    for code, aliases in AGENCIA_ALIASES.items():
        for al in aliases:
            if re.search(r'\b' + re.escape(_strip_accents(al).upper()) + r'\b', t):
                return code
    return None


def _resolve_agencia(agencia_doc: str, filename: str, filepath: str) -> str:
    # 1) documento
    if agencia_doc:
        code = _agencia_code_from_text(agencia_doc)
        return code or agencia_doc
    # 2) nombre archivo
    code = _agencia_code_from_text(filename)
    if code:
        return code
    # 3) carpetas del path (más cercano primero)
    try:
        p = Path(filepath)
        for part in [str(x) for x in p.parts[::-1]]:
            code = _agencia_code_from_text(part)
            if code:
                return code
    except Exception:
        pass
    return agencia_doc or ''


############################
### PARSERS / GET_* FNs  ###
############################

def _leer_hojas_excel(path_entrada: str, sheet_name=None) -> dict:
    if sheet_name is None:
        return pd.read_excel(path_entrada, sheet_name=None, header=None, dtype=str)
    if isinstance(sheet_name, (list, tuple)):
        return pd.read_excel(path_entrada, sheet_name=list(sheet_name), header=None, dtype=str)
    df = pd.read_excel(path_entrada, sheet_name=sheet_name, header=None, dtype=str)
    return {sheet_name: df}


def _normaliza_moneda(token: str) -> str:
    t = _strip_accents(str(token)).upper()
    if 'GUARANI' in t:
        return 'GUARANIES'
    if 'DOLAR' in t or 'DOLARE' in t or 'USD' in t:
        return 'DOLARES'
    if 'EURO' in t or 'EUR' in t:
        return 'EUROS'
    if 'REAL' in t or 'BRL' in t:
        return 'REALES'
    if 'PESO' in t or 'ARS' in t:
        return 'PESOS'
    return t


def _guess_currency_from_sheet_name(sheet_name: str) -> str:
    n = _strip_accents(str(sheet_name)).upper()
    if any(k in n for k in ['USD', 'DOLAR', 'DOLARES']):
        return 'DOLARES'
    if any(k in n for k in ['EUR', 'EURO', 'EUROS']):
        return 'EUROS'
    if any(k in n for k in ['BRL', 'REAL', 'REALES']):
        return 'REALES'
    if any(k in n for k in ['ARS', 'PESO', 'PESOS', 'ARG']):
        return 'PESOS'
    if any(k in n for k in ['PYG', 'GUARANI']):
        return 'GUARANIES'
    return ''


def get_ec_atm(fecha_ejecucion: datetime,
               filename: str,
               dir_entrada: str,
               dir_consolidado: Optional[str] = None,
               sheet_name: Union[int, str] = 0,
               collect_only: bool = True,
               output_path: Optional[str] = None) -> Optional[pd.DataFrame]:
    """Devuelve DF con columnas EC_ATM normalizadas (no escribe si collect_only=True)."""
    path_entrada = filename if os.path.isabs(filename) else os.path.join(dir_entrada, filename)
    df_raw = pd.read_excel(path_entrada, sheet_name=sheet_name, header=None, dtype=str).fillna('')

    rx_fecha_cell = re.compile(r'^\s*\d{1,2}/\d{1,2}/\d{4}\s*$')
    rx_totales = re.compile(r'\b(TOTAL|SUBTOTAL)\b', re.IGNORECASE)

    agencia = ''
    fecha_archivo = ''
    clasificacion = 'ATM'
    ing_egr = ''
    motivo_actual = ''

    agencia_fallback = _resolve_agencia(agencia, Path(path_entrada).name, path_entrada)

    registros: List[Dict[str, str]] = []

    for _, row in df_raw.iterrows():
        cells = [str(x) if x is not None else '' for x in row.values]
        strip_cells = [c.strip() for c in cells]
        line_join = ' '.join([c for c in strip_cells if c])
        if not line_join:
            continue

        upper_join = _strip_accents(line_join).upper()

        if 'PROSEGUR PARAGUAY S.A.' in upper_join:
            m = re.search(r'SUCURSAL:\s*([^)]+)\)', line_join, flags=re.IGNORECASE)
            if m:
                agencia = m.group(1).strip()
            continue

        if 'ESTADO DE CUENTA DE' in upper_join:
            m_f = re.search(r'AL:\s*(\d{1,2}/\d{1,2}/\d{4})', line_join, flags=re.IGNORECASE)
            if m_f:
                fecha_archivo = m_f.group(1)
            continue

        if upper_join == 'INGRESOS':
            ing_egr = 'IN'; motivo_actual = ''; continue
        if upper_join == 'EGRESOS':
            ing_egr = 'OUT'; motivo_actual = ''; continue

        if 'INFORME DE PROCESOS' in upper_join:
            break
        if rx_totales.search(line_join):
            continue

        date_idx = next((i for i, c in enumerate(strip_cells) if rx_fecha_cell.match(c)), None)

        if ing_egr and date_idx is None:
            motivo_actual = line_join.strip()
            continue

        if ing_egr and motivo_actual and date_idx is not None:
            fecha_oper = strip_cells[date_idx]

            suc_idx = _first_non_empty_after(strip_cells, date_idx)
            sucursal = _get_cell(strip_cells, suc_idx, default='')

            rec_idx = _first_non_empty_after(strip_cells, suc_idx) if suc_idx is not None else None
            recibo_raw = _get_cell(strip_cells, rec_idx, default='')
            recibo_digits = _only_digits(recibo_raw)
            recibo = recibo_digits if recibo_digits != '' else recibo_raw

            bul_idx = _first_non_empty_after(strip_cells, rec_idx) if rec_idx is not None else None
            bultos = _get_cell(strip_cells, bul_idx, default='')

            gua_idx = _first_non_empty_after(strip_cells, bul_idx) if bul_idx is not None else None
            guaranies = _get_cell(strip_cells, gua_idx, default='0') or '0'

            usd_idx = _first_non_empty_after(strip_cells, gua_idx) if gua_idx is not None else None
            dolares = _get_cell(strip_cells, usd_idx, default='0') or '0'

            registros.append({
                'FECHA_OPER': fecha_oper,
                'SUCURSAL': sucursal,
                'RECIBO': recibo,
                'BULTOS': bultos,
                'GUARANIES': guaranies,
                'DOLARES': dolares,
                'ING_EGR': ing_egr,
                'CLASIFICACION': clasificacion,
                'FECHA_ARCHIVO': fecha_archivo,
                'MOTIVO_MOVIMIENTO': motivo_actual,
                'AGENCIA': (agencia or agencia_fallback)
            })

    df_out = pd.DataFrame(registros)
    if df_out.empty:
        df_out = pd.DataFrame(columns=[
            'FECHA_OPER', 'SUCURSAL', 'RECIBO', 'BULTOS', 'GUARANIES', 'DOLARES',
            'ING_EGR', 'CLASIFICACION', 'FECHA_ARCHIVO', 'MOTIVO_MOVIMIENTO', 'AGENCIA'
        ])

    df_out = _ordenar_y_renombrar_columnas_ec(df_out)

    if collect_only:
        return df_out

    out = output_path or os.path.join(dir_consolidado or FULL_PATH_CONSOLIDADO, Path(path_entrada).stem + '.xlsx')
    df_out.to_excel(out, index=False)
    print(f'[EC_ATM] Guardado: {out}')
    return None


def get_ec_banco(fecha_ejecucion: datetime,
                 filename: str,
                 dir_entrada: str,
                 dir_consolidado: Optional[str] = None,
                 sheet_name=None,
                 collect_only: bool = True,
                 output_path: Optional[str] = None) -> Optional[pd.DataFrame]:
    """Devuelve DF EC_BANCO consolidando todas las hojas; no escribe si collect_only=True."""
    path_entrada = filename if os.path.isabs(filename) else os.path.join(dir_entrada, filename)
    hojas = _leer_hojas_excel(path_entrada, sheet_name=sheet_name)

    rx_fecha_linea = re.compile(r'^\s*(\d{1,2}/\d{1,2}/\d{4})\b')
    rx_totales = re.compile(r'\b(TOTAL|SUBTOTAL)\b', re.IGNORECASE)
    rx_moneda = re.compile(r'\b(GUARAN[IÍ]ES|D[ÓO]LARES|EUROS?|REALES?|PESOS?)\b', re.IGNORECASE)

    mapa_clasif = {
        'BANCO': 'BCO',
        'ATM': 'ATM',
        'BULTOS DE BANCO': 'BULTO BCO',
        'BULTOS DE ATM': 'BULTO ATM',
    }

    agencia_fallback = _resolve_agencia('', Path(path_entrada).name, path_entrada)
    registros = []

    for nombre_hoja, df in hojas.items():
        df = df.fillna('')

        agencia = ''
        fecha_archivo = ''
        clasificacion = ''
        ing_egr = ''
        motivo_actual = ''

        moneda_actual = _guess_currency_from_sheet_name(nombre_hoja) or 'GUARANIES'

        for _, row in df.iterrows():
            linea = ' '.join([str(x).strip() for x in row.values if str(x).strip()])
            if not linea:
                continue

            linea_up = _strip_accents(linea).upper()

            if 'PROSEGUR PARAGUAY S.A.' in linea_up:
                m = re.search(r'SUCURSAL:\s*([^)]+)\)', linea, flags=re.IGNORECASE)
                if m:
                    agencia = m.group(1).strip()
                continue

            if 'ESTADO DE CUENTA DE' in linea_up:
                m_tipo = re.search(r'ESTADO DE CUENTA DE\s+(.*?)\s+AL:', linea, flags=re.IGNORECASE)
                if m_tipo:
                    texto = m_tipo.group(1).strip()
                    texto_norm = _strip_accents(texto).upper()
                    clasificacion = mapa_clasif.get(texto_norm, texto.strip())
                m_f = re.search(r'AL:\s*(\d{1,2}/\d{1,2}/\d{4})', linea, flags=re.IGNORECASE)
                if m_f:
                    fecha_archivo = m_f.group(1)
                continue

            if linea_up == 'INGRESOS':
                ing_egr = 'IN'; motivo_actual = ''; continue
            if linea_up == 'EGRESOS':
                ing_egr = 'OUT'; motivo_actual = ''; continue

            if 'INFORME DE PROCESOS' in linea_up:
                break

            if rx_totales.search(linea):
                continue

            m_moneda = rx_moneda.search(linea)
            if m_moneda:
                moneda_actual = _normaliza_moneda(m_moneda.group(1))
                continue

            if ing_egr and not rx_fecha_linea.match(linea):
                motivo_actual = linea.strip()
                continue

            m_date = rx_fecha_linea.match(linea)
            if ing_egr and motivo_actual and m_date:
                parts = linea.split()
                if not parts:
                    continue

                fecha_oper = parts[0]
                idx_rec = next(
                    (i for i, p in enumerate(parts[1:], 1) if re.fullmatch(r'\d{6,}', p)),
                    None
                )
                if idx_rec is None:
                    continue

                sucursal = ' '.join(parts[1:idx_rec]).strip()
                recibo = parts[idx_rec]
                bultos = parts[idx_rec + 1] if idx_rec + 1 < len(parts) else ''
                importe = parts[idx_rec + 2] if idx_rec + 2 < len(parts) else ''

                registros.append({
                    'HOJA_ORIGEN': nombre_hoja,
                    'AGENCIA': (agencia or agencia_fallback),
                    'FECHA_ARCHIVO': fecha_archivo,
                    'ING_EGR': ing_egr,
                    'CLASIFICACION': clasificacion,
                    'MOTIVO MOVIMIENTO': motivo_actual,
                    'FECHA_OPER': fecha_oper,
                    'SUCURSAL': sucursal,
                    'RECIBO': recibo,
                    'BULTOS': bultos,
                    'MONEDA': moneda_actual,
                    'MONTO': importe
                })

    df_out = pd.DataFrame(registros, columns=[
        'HOJA_ORIGEN', 'AGENCIA', 'FECHA_ARCHIVO', 'ING_EGR', 'CLASIFICACION',
        'MOTIVO MOVIMIENTO', 'FECHA_OPER', 'SUCURSAL',
        'RECIBO', 'BULTOS', 'MONEDA', 'MONTO'
    ])

    if df_out.empty:
        df_out = pd.DataFrame(columns=[
            'HOJA_ORIGEN', 'AGENCIA', 'FECHA_ARCHIVO', 'ING_EGR', 'CLASIFICACION',
            'MOTIVO MOVIMIENTO', 'FECHA_OPER', 'SUCURSAL',
            'RECIBO', 'BULTOS', 'MONEDA', 'MONTO'
        ])

    df_out = df_out.rename(columns={'MOTIVO MOVIMIENTO': 'MOTIVO_MOVIMIENTO'})
    df_out = _ordenar_y_renombrar_columnas_ec_banco(df_out)

    if collect_only:
        return df_out

    out = output_path or os.path.join(dir_consolidado or FULL_PATH_CONSOLIDADO, Path(path_entrada).stem + '.xlsx')
    df_out.to_excel(out, index=False)
    print(f'[EC_BANCO] Guardado: {out}')
    return None


def _is_zero_like(s: str) -> bool:
    t = str(s).replace(',', '').replace('.', '').strip()
    return t == '' or t == '0'


def get_ec_bultos_atm(fecha_ejecucion: datetime,
                      filename: str,
                      dir_entrada: str,
                      dir_consolidado: Optional[str] = None,
                      sheet_name: Union[int, str] = 0,
                      descartar_usd_cero: bool = True,
                      collect_only: bool = True,
                      output_path: Optional[str] = None) -> Optional[pd.DataFrame]:
    """Devuelve DF LONG (PYG/USD) para BULTOS ATM; no escribe si collect_only=True."""
    path_entrada = filename if os.path.isabs(filename) else os.path.join(dir_entrada, filename)
    df_x = pd.read_excel(path_entrada, sheet_name=sheet_name, header=None, dtype=str).fillna('')

    rx_fecha_cell = re.compile(r'^\s*\d{1,2}/\d{1,2}/\d{4}\s*$')
    rx_totales = re.compile(r'\b(TOTAL|SUBTOTAL)\b', re.IGNORECASE)

    agencia = ''
    fecha_archivo = ''
    clasificacion = 'ATM'
    ing_egr = ''
    motivo_actual = ''

    agencia_fallback = _resolve_agencia('', Path(path_entrada).name, path_entrada)

    registros: List[Dict[str, str]] = []

    for _, row in df_x.iterrows():
        cells = [str(x) if x is not None else '' for x in row.values]
        strip_cells = [c.strip() for c in cells]
        line_join = ' '.join([c for c in strip_cells if c])
        if not line_join:
            continue

        upper_join = _strip_accents(line_join).upper()

        if 'PROSEGUR PARAGUAY S.A.' in upper_join:
            m = re.search(r'SUCURSAL:\s*([^)]+)\)', line_join, flags=re.IGNORECASE)
            if m:
                agencia = m.group(1).strip()
            continue

        if 'ESTADO DE CUENTA DE BULTOS DE ATM' in upper_join:
            m_f = re.search(r'AL:\s*(\d{1,2}/\d{1,2}/\d{4})', line_join, flags=re.IGNORECASE)
            if m_f:
                fecha_archivo = m_f.group(1)
            continue

        if upper_join == 'INGRESOS':
            ing_egr = 'IN'; motivo_actual = ''; continue
        if upper_join == 'EGRESOS':
            ing_egr = 'OUT'; motivo_actual = ''; continue

        if 'INFORME DE PROCESOS' in upper_join:
            break
        if rx_totales.search(line_join):
            continue

        date_idx = next((i for i, c in enumerate(strip_cells) if rx_fecha_cell.match(c)), None)

        if ing_egr and date_idx is None:
            motivo_actual = line_join.strip()
            continue

        if ing_egr and motivo_actual and date_idx is not None:
            fecha_oper = strip_cells[date_idx]

            suc_idx = _first_non_empty_after(strip_cells, date_idx)
            sucursal = _get_cell(strip_cells, suc_idx, default='')

            rec_idx = _first_non_empty_after(strip_cells, suc_idx) if suc_idx is not None else None
            recibo_raw = _get_cell(strip_cells, rec_idx, default='')
            recibo_digits = _only_digits(recibo_raw)
            recibo = recibo_digits if recibo_digits != '' else recibo_raw

            b_pyg_idx = _first_non_empty_after(strip_cells, rec_idx)
            bultos_pyg = _get_cell(strip_cells, b_pyg_idx, default='0') or '0'

            gua_idx = _first_non_empty_after(strip_cells, b_pyg_idx)
            guaranies = _get_cell(strip_cells, gua_idx, default='0') or '0'

            b_usd_idx = _first_non_empty_after(strip_cells, gua_idx)
            bultos_usd = _get_cell(strip_cells, b_usd_idx, default='0') or '0'

            usd_idx = _first_non_empty_after(strip_cells, b_usd_idx)
            dolares = _get_cell(strip_cells, usd_idx, default='0') or '0'

            registros.append({
                'FECHA_OPER': fecha_oper,
                'SUCURSAL': sucursal,
                'RECIBO': recibo,
                'BULTOS': bultos_pyg,
                'MONEDA': 'PYG',
                'IMPORTE': guaranies,
                'ING_EGR': ing_egr,
                'CLASIFICACION': clasificacion,
                'FECHA_ARCHIVO': fecha_archivo,
                'MOTIVO_MOVIMIENTO': motivo_actual,
                'AGENCIA': (agencia or agencia_fallback)
            })
            if not (descartar_usd_cero and _is_zero_like(dolares)):
                registros.append({
                    'FECHA_OPER': fecha_oper,
                    'SUCURSAL': sucursal,
                    'RECIBO': recibo,
                    'BULTOS': bultos_usd,
                    'MONEDA': 'USD',
                    'IMPORTE': dolares,
                    'ING_EGR': ing_egr,
                    'CLASIFICACION': clasificacion,
                    'FECHA_ARCHIVO': fecha_archivo,
                    'MOTIVO_MOVIMIENTO': motivo_actual,
                    'AGENCIA': (agencia or agencia_fallback)
                })

    df_out = pd.DataFrame(registros)
    if df_out.empty:
        df_out = pd.DataFrame(columns=[
            'FECHA_OPER','SUCURSAL','RECIBO','BULTOS','MONEDA','IMPORTE',
            'ING_EGR','CLASIFICACION','FECHA_ARCHIVO','MOTIVO_MOVIMIENTO','AGENCIA'
        ])

    df_out = _ordenar_y_renombrar_columnas_bultos(df_out)

    if collect_only:
        return df_out

    out = output_path or os.path.join(dir_consolidado or FULL_PATH_CONSOLIDADO, Path(path_entrada).stem + '.xlsx')
    df_out.to_excel(out, index=False)
    print(f'[EC_BULTOS_ATM] Guardado: {out}')
    return None


def _normaliza_moneda_iso(token: str) -> str:
    t = _strip_accents(str(token)).upper().strip()
    if any(k in t for k in ['PYG', 'GS', 'G$', 'G ', '₲', 'GUARANI', 'GUARANIES']): 
        return 'PYG'
    if any(k in t for k in ['USD', 'US$', 'U$S', 'DOLAR', 'DOLARES']):
        return 'USD'
    if any(k in t for k in ['EUR', '€', 'EURO', 'EUROS']):
        return 'EUR'
    if any(k in t for k in ['BRL', 'R$', 'REAL', 'REALES']):
        return 'BRL'
    if any(k in t for k in ['ARS', 'PESO', 'PESOS', 'ARG']):
        return 'ARS'
    if '$' in t:
        return 'USD'
    return t


def get_ec_bultos_bco(fecha_ejecucion: datetime,
                       filename: str,
                       dir_entrada: str,
                       dir_consolidado: Optional[str] = None,
                       sheet_name=None,
                       collect_only: bool = True,
                       output_path: Optional[str] = None) -> Optional[pd.DataFrame]:
    """Devuelve DF LONG para BULTOS BANCO (MONEDA ISO + IMPORTE); no escribe si collect_only=True."""
    path_entrada = filename if os.path.isabs(filename) else os.path.join(dir_entrada, filename)

    # Cargar hojas
    if sheet_name is None:
        hojas = pd.read_excel(path_entrada, sheet_name=None, header=None, dtype=str)
    elif isinstance(sheet_name, (list, tuple)):
        hojas = pd.read_excel(path_entrada, sheet_name=list(sheet_name), header=None, dtype=str)
    else:
        df_one = pd.read_excel(path_entrada, sheet_name=sheet_name, header=None, dtype=str)
        hojas = {sheet_name: df_one}

    rx_fecha_linea = re.compile(r'^\s*(\d{1,2}/\d{1,2}/\d{4})\b')
    rx_totales = re.compile(r'\b(TOTAL|SUBTOTAL)\b', re.IGNORECASE)
    rx_moneda = re.compile(r'(GUARAN[IÍ]ES|D[ÓO]LARES|EUROS?|REALES?|PESOS?|PYG|GS|G\$|₲|USD|US\$|U\$S|R\$|BRL|EUR|ARS|€|\$)',
                           re.IGNORECASE)

    mapa_clasif = {
        'BANCO': 'BCO',
        'ATM': 'ATM',
        'BULTOS DE BANCO': 'BULTO BCO',
        'BULTOS DE ATM': 'BULTO ATM',
    }

    registros: List[Dict[str, str]] = []
    agencia_fallback = _resolve_agencia('', Path(path_entrada).name, path_entrada)

    for nombre_hoja, df in hojas.items():
        df = df.fillna('')

        agencia = ''
        fecha_archivo = ''
        clasificacion = 'BULTO BCO'
        ing_egr = ''
        motivo_actual = ''
        moneda_actual = _normaliza_moneda_iso(nombre_hoja)

        for _, row in df.iterrows():
            parts_all = [str(x).strip() for x in row.values if str(x).strip()]
            linea = ' '.join(parts_all)
            if not linea:
                continue

            linea_up = _strip_accents(linea).upper()

            if 'PROSEGUR PARAGUAY S.A.' in linea_up:
                m = re.search(r'SUCURSAL:\s*([^)]+)\)', linea, flags=re.IGNORECASE)
                if m:
                    agencia = m.group(1).strip()
                continue

            if 'ESTADO DE CUENTA DE' in linea_up:
                m_tipo = re.search(r'ESTADO DE CUENTA DE\s+(.*?)\s+AL[:\s]', linea, flags=re.IGNORECASE)
                if m_tipo:
                    texto = m_tipo.group(1).strip()
                    texto_norm = _strip_accents(texto).upper()
                    clasificacion = mapa_clasif.get(texto_norm, texto.strip()) or clasificacion
                m_f = re.search(r'AL[:\s]+(\d{1,2}/\d{1,2}/\d{4})', linea, flags=re.IGNORECASE)
                if m_f:
                    fecha_archivo = m_f.group(1)
                m_mon_enc = rx_moneda.search(linea)
                if m_mon_enc:
                    moneda_actual = _normaliza_moneda_iso(m_mon_enc.group(1))
                continue

            if linea_up == 'INGRESOS':
                ing_egr = 'IN'; motivo_actual = ''; continue
            if linea_up == 'EGRESOS':
                ing_egr = 'OUT'; motivo_actual = ''; continue

            if 'INFORME DE PROCESOS' in linea_up:
                break

            if rx_totales.search(linea):
                continue

            if rx_moneda.search(linea) and not rx_fecha_linea.match(linea):
                moneda_actual = _normaliza_moneda_iso(rx_moneda.search(linea).group(1))
                continue

            if ing_egr and not rx_fecha_linea.match(linea):
                motivo_actual = linea.strip()
                continue

            m_date = rx_fecha_linea.match(linea)
            if ing_egr and motivo_actual and m_date:
                parts = parts_all
                if not parts:
                    continue

                fecha_oper = parts[0]
                idx_rec = next(
                    (i for i, p in enumerate(parts[1:], 1) if re.fullmatch(r'\d{6,}', _strip_accents(p))),
                    None
                )
                if idx_rec is None:
                    continue

                sucursal = ' '.join(parts[1:idx_rec]).strip()
                recibo = parts[idx_rec]
                bultos = parts[idx_rec + 1] if idx_rec + 1 < len(parts) else ''
                importe = parts[idx_rec + 2] if idx_rec + 2 < len(parts) else ''

                m_mon_inline = rx_moneda.search(linea)
                if m_mon_inline:
                    moneda_actual = _normaliza_moneda_iso(m_mon_inline.group(1))

                registros.append({
                    'FECHA': fecha_oper,
                    'SUCURSAL': sucursal,
                    'RECIBO': recibo,
                    'BULTOS': bultos,
                    'MONEDA': moneda_actual or 'PYG',
                    'IMPORTE': importe,
                    'ING_EGR': ing_egr,
                    'CLASIFICACION': clasificacion,
                    'FECHA_ARCHIVO': fecha_archivo,
                    'MOTIVO_MOVIMIENTO': motivo_actual,
                    'AGENCIA': (agencia or agencia_fallback)
                })

    df_out = pd.DataFrame(registros, columns=[
        'FECHA', 'SUCURSAL', 'RECIBO', 'BULTOS', 'MONEDA', 'IMPORTE',
        'ING_EGR', 'CLASIFICACION', 'FECHA_ARCHIVO', 'MOTIVO_MOVIMIENTO', 'AGENCIA'
    ])

    if df_out.empty:
        df_out = pd.DataFrame(columns=[
            'FECHA', 'SUCURSAL', 'RECIBO', 'BULTOS', 'MONEDA', 'IMPORTE',
            'ING_EGR', 'CLASIFICACION', 'FECHA_ARCHIVO', 'MOTIVO_MOVIMIENTO', 'AGENCIA'
        ])

    df_out = _ordenar_y_renombrar_columnas_bultos(df_out)

    if collect_only:
        return df_out

    out = output_path or os.path.join(dir_consolidado or FULL_PATH_CONSOLIDADO, Path(path_entrada).stem + '.xlsx')
    df_out.to_excel(out, index=False)
    print(f'[EC_BULTOS_BCO] Guardado: {out}')
    return None


############################
### INVENTARIOS (ATM/BCO) ###
############################

def get_inv_atm(fecha_ejecucion: datetime,
                filename: str,
                dir_entrada: str,
                dir_consolidado: Optional[str] = None,
                include_zeros: bool = True,
                collect_only: bool = True,
                output_path: Optional[str] = None) -> Optional[pd.DataFrame]:

    AGRUP_TOKENS = ["TESORO ATM", "FAJOS ATM", "PICOS ATM"]
    TIPO_TOKENS  = ["BILLETES (LADRILLOS)", "BILLETES"]
    FIN_MONEDA_UP = "TOTAL DE LA MONEDA"
    STOP_ROW_TOKENS = {
        "SUB TOTAL", "SUBTOTAL", "TOTAL DEL DEPÓSITO", "TOTAL DEL DEPOSITO",
        "TOTAL DEPÓSITO", "TOTAL DEPOSITO"
    }
    DATE_RE    = re.compile(r"(\d{2}/\d{2}/\d{4})")
    AGENCIA_RE = re.compile(r"SUCURSAL:\s*([^)]+)\)", re.IGNORECASE)
    TITULO_RE  = re.compile(r"SALDO DE INVENTARIO DE BILLETES ATM AL", re.IGNORECASE)

    path_entrada = filename if os.path.isabs(filename) else os.path.join(dir_entrada, filename)

    def extrae_agencia_y_fecha(df: pd.DataFrame) -> Dict[str, str]:
        agencia, fecha = "", ""
        for _, row in df.iterrows():
            for cell in row:
                t = _txt(cell)
                if not agencia:
                    m = AGENCIA_RE.search(t)
                    if m:
                        agencia = m.group(1).strip()
                if not fecha and (TITULO_RE.search(t) or "SALDO DE INVENTARIO" in t.upper()):
                    m = DATE_RE.search(t)
                    if m:
                        fecha = m.group(1)
            if agencia and fecha:
                break
        if not fecha:
            for _, row in df.iterrows():
                for cell in row:
                    m = DATE_RE.search(_txt(cell))
                    if m:
                        fecha = m.group(1); break
                if fecha: break
        return {"AGENCIA": agencia, "FECHA_INVENTARIO": fecha}

    def capturar_codigo_total(row_text_upper: str) -> Optional[str]:
        m = re.search(r"TOTAL\s+DE\s+LA\s+MONEDA\s+([A-Z]{3})", row_text_upper, flags=re.IGNORECASE)
        return m.group(1).upper() if m else None

    def buscar_fin_y_codigo(df: pd.DataFrame) -> Tuple[int, Optional[str]]:
        row_end, code = len(df), None
        for i, row in df.iterrows():
            row_up = " | ".join(_upper(c) for c in row.tolist())
            if FIN_MONEDA_UP in row_up:
                row_end = i
                code = capturar_codigo_total(row_up)
                break
        return row_end, code

    def buscar_inicio_por_divisa(df: pd.DataFrame, row_end: int) -> Optional[int]:
        for i, row in df.iterrows():
            if i >= row_end: break
            ups = [_upper(c) for c in row.tolist() if _txt(c)]
            if "USD" in ups or "PYG" in ups:
                return i
        return None

    def buscar_inicio_fallback(df: pd.DataFrame, row_end: int) -> Optional[int]:
        for i, row in df.iterrows():
            if i >= row_end: break
            nums = [j for j, c in enumerate(row.tolist()) if _to_int(c) is not None]
            if not nums: continue
            denom_col = nums[0]
            left_up = " ".join(_upper(row.iloc[j]) for j in range(0, denom_col) if _txt(row.iloc[j]))
            if any(tok in left_up for tok in AGRUP_TOKENS):
                return i
        return None

    def localiza_bloque(df: pd.DataFrame) -> Dict[str, Any]:
        row_end, code_total = buscar_fin_y_codigo(df)
        row_start = buscar_inicio_por_divisa(df, row_end)
        if row_start is None:
            row_start = buscar_inicio_fallback(df, row_end)
        if row_start is None:
            raise ValueError("No se pudo determinar el inicio del bloque (USD/PYG o agrupación+denominación).")
        return {"row_start": row_start, "row_end": row_end, "moneda_codigo": code_total}

    registros: List[Dict[str, Any]] = []
    try:
        xls = pd.ExcelFile(path_entrada, engine="openpyxl")
    except Exception as e:
        print(f"[INV_ATM] {path_entrada}: No se pudo abrir el archivo ({e}).")
        return pd.DataFrame(columns=[
            "FECHA_INVENTARIO","DIVISA","AGENCIA","AGRUPACION_EFECTIVO","TIPO_VALOR",
            "DENOMINACION","DEPOSITO","CJE_DEP","CANJE","MONEDA","IMPORTE_TOTAL"
        ]) if collect_only else None

    for sheet in xls.sheet_names:
        try:
            df = pd.read_excel(path_entrada, sheet_name=sheet, header=None, dtype=object, engine="openpyxl").fillna("")
            meta = extrae_agencia_y_fecha(df)
            agencia_resuelta = _resolve_agencia(meta.get("AGENCIA", ""), Path(path_entrada).name, path_entrada)
            lim = localiza_bloque(df)

            cur_divisa = (lim.get("moneda_codigo") or "").upper()
            cur_divisa = "USD" if "USD" in cur_divisa else ("PYG" if "PYG" in cur_divisa else cur_divisa)

            cur_agrup, cur_tipo = "", ""

            for i in range(lim["row_start"], lim["row_end"]):
                row = df.iloc[i]
                up_line = " ".join(_upper(c) for c in row.tolist())
                if "TOTAL DE LA MONEDA" in up_line or any(tok in up_line for tok in STOP_ROW_TOKENS):
                    continue

                ups = [_upper(c) for c in row.tolist() if _txt(c)]
                if "USD" in ups: cur_divisa = "USD"
                elif "PYG" in ups: cur_divisa = "PYG"

                nums = [(j, _to_int(c)) for j, c in enumerate(row.tolist()) if _to_int(c) is not None]
                if not nums: continue
                denom_col, denom_val = nums[0]

                left_cells = [row.iloc[j] for j in range(0, denom_col)]
                left_up = " ".join(_upper(c) for c in left_cells if _txt(c))
                for t in TIPO_TOKENS:
                    if t in left_up: cur_tipo = t; break
                for a in AGRUP_TOKENS:
                    if a in left_up: cur_agrup = a; break

                # cinco números siguientes
                idx = denom_col
                vals = []
                for _ in range(5):
                    ncols = len(row)
                    got = 0
                    for j in range(idx + 1, ncols):
                        v = _to_int(row.iloc[j])
                        if v is not None:
                            idx = j; vals.append(v); got = 1; break
                    if not got:
                        vals.append(0); idx = ncols
                while len(vals) < 5: vals.append(0)

                reg = {
                    "FECHA_INVENTARIO": meta.get("FECHA_INVENTARIO",""),
                    "DIVISA": cur_divisa or "PYG",
                    "AGENCIA": agencia_resuelta,
                    "AGRUPACION_EFECTIVO": cur_agrup,
                    "TIPO_VALOR": cur_tipo,
                    "DENOMINACION": denom_val,
                    "DEPOSITO": vals[0] or 0,
                    "CJE_DEP": vals[1] or 0,
                    "CANJE": vals[2] or 0,
                    "MONEDA": vals[3] or 0,
                    "IMPORTE_TOTAL": vals[4] or 0,
                }
                if include_zeros or any([reg["DEPOSITO"], reg["CJE_DEP"], reg["CANJE"], reg["MONEDA"], reg["IMPORTE_TOTAL"]]):
                    registros.append(reg)

        except Exception as e:
            print(f"[INV_ATM] Hoja '{sheet}' omitida: {e}")
            continue

    cols = ["FECHA_INVENTARIO","DIVISA","AGENCIA","AGRUPACION_EFECTIVO","TIPO_VALOR",
            "DENOMINACION","DEPOSITO","CJE_DEP","CANJE","MONEDA","IMPORTE_TOTAL"]
    df_out = pd.DataFrame(registros, columns=cols)
    df_out.sort_values(by=["FECHA_INVENTARIO","AGENCIA","DIVISA","AGRUPACION_EFECTIVO","TIPO_VALOR","DENOMINACION"], inplace=True)

    if collect_only:
        return df_out

    out = output_path or os.path.join(dir_consolidado or FULL_PATH_CONSOLIDADO, Path(path_entrada).stem + '.xlsx')
    df_out.to_excel(out, index=False)
    print(f'[INV_ATM] Guardado: {out}')
    return None


def get_inv_bco(fecha_ejecucion: datetime,
                filename: str,
                dir_entrada: str,
                dir_consolidado: Optional[str] = None,
                include_zeros: bool = True,
                collect_only: bool = True,
                output_path: Optional[str] = None) -> Optional[pd.DataFrame]:

    AGRUP_TOKENS = ["TESORO EFECTIVO", "FAJOS EFECTIVOS", "PICOS EFECTIVO"]
    TIPO_TOKENS  = ["BILLETES (LADRILLOS)", "MONEDAS (BOLSAS)", "MONEDAS (PAQUETES)", "BILLETES", "MONEDAS"]
    FIN_MONEDA_UP = "TOTAL DE LA MONEDA"
    STOP_ROW_TOKENS = {
        "SUB TOTAL", "SUBTOTAL", "TOTAL DEL DEPÓSITO", "TOTAL DEL DEPOSITO",
        "TOTAL DEPÓSITO", "TOTAL DEPOSITO"
    }
    DATE_RE    = re.compile(r"(\d{2}/\d{2}/\d{4})")
    TITULO_RE  = re.compile(r"SALDOS?\s+DE\s+INVENTARIO\s+DE\s+BILLETES\s+AL", re.IGNORECASE)
    AGENCIA_RE = re.compile(r"SUCURSAL:\s*([^)]+)\)", re.IGNORECASE)

    path_entrada = filename if os.path.isabs(filename) else os.path.join(dir_entrada, filename)

    def extrae_agencia_y_fecha(df: pd.DataFrame) -> Dict[str, str]:
        agencia, fecha = "", ""
        for _, row in df.iterrows():
            for cell in row:
                t = _txt(cell)
                if not agencia:
                    m = AGENCIA_RE.search(t)
                    if m:
                        agencia = m.group(1).strip()
                if not fecha and (TITULO_RE.search(t) or "INVENTARIO" in t.upper()):
                    m = DATE_RE.search(t)
                    if m:
                        fecha = m.group(1)
            if agencia and fecha:
                break
        if not fecha:
            for _, row in df.iterrows():
                for cell in row:
                    m = DATE_RE.search(_txt(cell))
                    if m:
                        fecha = m.group(1); break
                if fecha: break
        return {"AGENCIA": agencia, "FECHA_INVENTARIO": fecha}

    def capturar_codigo_total(row_text_upper: str) -> Optional[str]:
        m = re.search(r"TOTAL\s+DE\s+LA\s+MONEDA\s+([A-Z]{3})", row_text_upper, flags=re.IGNORECASE)
        return m.group(1).upper() if m else None

    def buscar_fin_y_codigo(df: pd.DataFrame) -> Tuple[int, Optional[str]]:
        row_end, code = len(df), None
        for i, row in df.iterrows():
            row_up = " | ".join(_upper(c) for c in row.tolist())
            if FIN_MONEDA_UP in row_up:
                row_end = i
                code = capturar_codigo_total(row_up)
                break
        return row_end, code

    def buscar_inicio_por_divisa(df: pd.DataFrame, row_end: int) -> Optional[int]:
        for i, row in df.iterrows():
            if i >= row_end: break
            ups = [_upper(c) for c in row.tolist() if _txt(c)]
            if "USD" in ups or "PYG" in ups:
                return i
        return None

    def buscar_inicio_por_cabecera(df: pd.DataFrame, row_end: int) -> Optional[int]:
        for i, row in df.iterrows():
            row_up = " | ".join(_upper(c) for c in row.tolist())
            if ("DIVISA" in row_up and "DENOM" in row_up and "CJE/DEP" in row_up and "IMPORTE" in row_up):
                return i + 1
        return None

    def buscar_inicio_fallback(df: pd.DataFrame, row_end: int) -> Optional[int]:
        for i, row in df.iterrows():
            if i >= row_end: break
            nums = [j for j, c in enumerate(row.tolist()) if _to_int(c) is not None]
            if not nums: continue
            denom_col = nums[0]
            left_up = " ".join(_upper(row.iloc[j]) for j in range(0, denom_col) if _txt(row.iloc[j]))
            if any(tok in left_up for tok in AGRUP_TOKENS):
                return i
        return None

    def localiza_bloque(df: pd.DataFrame) -> Dict[str, Any]:
        row_end, code_total = buscar_fin_y_codigo(df)
        row_start = buscar_inicio_por_cabecera(df, row_end)
        if row_start is None:
            row_start = buscar_inicio_por_divisa(df, row_end)
        if row_start is None:
            row_start = buscar_inicio_fallback(df, row_end)
        if row_start is None:
            raise ValueError("No se pudo determinar el inicio del bloque.")
        return {"row_start": row_start, "row_end": row_end, "moneda_codigo": code_total}

    registros: List[Dict[str, Any]] = []
    try:
        xls = pd.ExcelFile(path_entrada, engine="openpyxl")
    except Exception as e:
        print(f"[INV_BCO] {path_entrada}: No se pudo abrir el archivo ({e}).")
        return pd.DataFrame(columns=[
            "FECHA_INVENTARIO","DIVISA","AGENCIA","AGRUPACION_EFECTIVO","TIPO_VALOR",
            "DENOMINACION","DEPOSITO","CJE_DEP","CANJE","MONEDA","IMPORTE_TOTAL"
        ]) if collect_only else None

    for sheet in xls.sheet_names:
        try:
            df = pd.read_excel(path_entrada, sheet_name=sheet, header=None, dtype=object, engine="openpyxl").fillna("")
            meta = extrae_agencia_y_fecha(df)
            agencia_resuelta = _resolve_agencia(meta.get("AGENCIA", ""), Path(path_entrada).name, path_entrada)
            lim = localiza_bloque(df)

            cur_divisa = ('DOLARES' if 'USD' in (lim.get("moneda_codigo") or '').upper()
                          else 'GUARANIES' if 'PYG' in (lim.get("moneda_codigo") or '').upper()
                          else lim.get("moneda_codigo") or '')

            cur_agrup, cur_tipo = "", ""

            for i in range(lim["row_start"], lim["row_end"]):
                row = df.iloc[i]
                up_line = " ".join(_upper(c) for c in row.tolist())
                if FIN_MONEDA_UP in up_line or any(tok in up_line for tok in STOP_ROW_TOKENS):
                    continue

                ups = [_upper(c) for c in row.tolist() if _txt(c)]
                if "USD" in ups: cur_divisa = "DOLARES"
                elif "PYG" in ups: cur_divisa = "GUARANIES"

                nums = [(j, _to_int(c)) for j, c in enumerate(row.tolist()) if _to_int(c) is not None]
                if not nums: continue
                denom_col, denom_val = nums[0]

                left_cells = [row.iloc[j] for j in range(0, denom_col)]
                left_up = " ".join(_upper(c) for c in left_cells if _txt(c))
                for t in TIPO_TOKENS:
                    if t in left_up: cur_tipo = t; break
                for a in AGRUP_TOKENS:
                    if a in left_up: cur_agrup = a; break

                # cinco números siguientes
                idx = denom_col
                vals = []
                for _ in range(5):
                    ncols = len(row)
                    got = 0
                    for j in range(idx + 1, ncols):
                        v = _to_int(row.iloc[j])
                        if v is not None:
                            idx = j; vals.append(v); got = 1; break
                    if not got:
                        vals.append(0); idx = ncols
                while len(vals) < 5: vals.append(0)

                reg = {
                    "FECHA_INVENTARIO": meta.get("FECHA_INVENTARIO",""),
                    "DIVISA": cur_divisa or "GUARANIES",
                    "AGENCIA": agencia_resuelta,
                    "AGRUPACION_EFECTIVO": cur_agrup,
                    "TIPO_VALOR": cur_tipo,
                    "DENOMINACION": denom_val,
                    "DEPOSITO": vals[0] or 0,
                    "CJE_DEP": vals[1] or 0,
                    "CANJE": vals[2] or 0,
                    "MONEDA": vals[3] or 0,
                    "IMPORTE_TOTAL": vals[4] or 0,
                }
                if include_zeros or any([reg["DEPOSITO"], reg["CJE_DEP"], reg["CANJE"], reg["MONEDA"], reg["IMPORTE_TOTAL"]]):
                    registros.append(reg)

        except Exception as e:
            print(f"[INV_BCO] Hoja '{sheet}' omitida: {e}")
            continue

    cols = ["FECHA_INVENTARIO","DIVISA","AGENCIA","AGRUPACION_EFECTIVO","TIPO_VALOR",
            "DENOMINACION","DEPOSITO","CJE_DEP","CANJE","MONEDA","IMPORTE_TOTAL"]
    df_out = pd.DataFrame(registros, columns=cols)
    df_out.sort_values(by=["FECHA_INVENTARIO","AGENCIA","DIVISA","AGRUPACION_EFECTIVO","TIPO_VALOR","DENOMINACION"], inplace=True)

    if collect_only:
        return df_out

    out = output_path or os.path.join(dir_consolidado or FULL_PATH_CONSOLIDADO, Path(path_entrada).stem + '.xlsx')
    df_out.to_excel(out, index=False)
    print(f'[INV_BCO] Guardado: {out}')
    return None


#########################
### ORQUESTADOR / MAIN ###
#########################

def _detect_type(fname_upper: str) -> str:
    if fname_upper.startswith('EC_ATM'):
        return 'EC_ATM'
    if fname_upper.startswith('EC_BANCO') or fname_upper.startswith('EC_BCO'):
        return 'EC_BANCO'
    if fname_upper.startswith('EC_BULTOS_ATM'):
        return 'EC_BULTOS_ATM'
    if fname_upper.startswith('EC_BULTOS_BCO') or fname_upper.startswith('EC_BULTOS_BANCO'):
        return 'EC_BULTOS_BCO'
    if fname_upper.startswith('INV_BILLETES_ATM') or fname_upper.startswith('INV_ATM'):
        return 'INV_ATM'
    if fname_upper.startswith('INV_BILLETES_BANCO') or fname_upper.startswith('INV_BCO') or fname_upper.startswith('INV_BANCO'):
        return 'INV_BCO'
    return ''


def _is_excel_file(path: str) -> bool:
    return path.lower().endswith(('.xls', '.xlsx'))


def run():
    # Fecha dd-mm-YYYY para nombre de carpeta
    fecha_str = datetime.now().strftime("%d-%m-%Y")
    output_dir = os.path.join(FULL_PATH_CONSOLIDADO, fecha_str)
    os.makedirs(output_dir, exist_ok=True)

    buckets: Dict[str, List[pd.DataFrame]] = {
        'EC_ATM': [],
        'EC_BANCO': [],
        'EC_BULTOS_ATM': [],
        'EC_BULTOS_BCO': [],
        'INV_ATM': [],
        'INV_BCO': [],
    }

    outnames = {
        'EC_ATM':       os.path.join(output_dir, 'PROSEGUR_EFECT_ATM.xlsx'),
        'EC_BANCO':     os.path.join(output_dir, 'PROSEGUR_EFECT_BANCO.xlsx'),
        'EC_BULTOS_ATM':os.path.join(output_dir, 'PROSEGUR_BULTOS_ATM.xlsx'),
        'EC_BULTOS_BCO':os.path.join(output_dir, 'PROSEGUR_BULTOS_BANCO.xlsx'),
        'INV_ATM':      os.path.join(output_dir, 'PROSEGUR_INV_ATM.xlsx'),
        'INV_BCO':      os.path.join(output_dir, 'PROSEGUR_INV_BANCO.xlsx'),
    }

    print('=' * 40)
    print(f'Inicio: {fecha_str}')
    print(f'Base: {PROSEGUR_BASE}')

    # Recorremos PENDIENTES de forma recursiva (para subcarpetas OVD/ASU/etc)
    files = []
    for root, dirs, filenames in os.walk(FULL_PATH):
        for fname in filenames:
            full = os.path.join(root, fname)
            if _is_excel_file(full):
                files.append(full)

    if not files:
        print('[INFO] No hay archivos Excel en PENDIENTES (ni subcarpetas).')
    else:
        for src in files:
            fname = os.path.basename(src)
            fup = fname.upper()
            tipo = _detect_type(fup)
            if not tipo:
                print(f'[SKIP] Tipo no reconocido: {src}')
                continue
            try:
                print(f'Procesando ({tipo}): {src}')
                # Pasamos ruta completa al parser para que resuelva agencia por carpeta si hace falta
                if tipo == 'EC_ATM':
                    df = get_ec_atm(datetime.now(), src, FULL_PATH, collect_only=True)
                elif tipo == 'EC_BANCO':
                    df = get_ec_banco(datetime.now(), src, FULL_PATH, collect_only=True)
                elif tipo == 'EC_BULTOS_ATM':
                    df = get_ec_bultos_atm(datetime.now(), src, FULL_PATH, collect_only=True)
                elif tipo == 'EC_BULTOS_BCO':
                    df = get_ec_bultos_bco(datetime.now(), src, FULL_PATH, collect_only=True)
                elif tipo == 'INV_ATM':
                    df = get_inv_atm(datetime.now(), src, FULL_PATH, collect_only=True)
                elif tipo == 'INV_BCO':
                    df = get_inv_bco(datetime.now(), src, FULL_PATH, collect_only=True)
                else:
                    df = None

                if df is not None and not df.empty:
                    buckets[tipo].append(df)
                else:
                    print(f'[WARN] {src}: no produjo registros.')

                # mover original a PROCESADO preservando subcarpetas
                rel = os.path.relpath(src, FULL_PATH)  # e.g. "OVD/EC_BANCO_...xlsx"
                dst = os.path.join(FULL_PATH_PROCESADO, rel)
                Path(os.path.dirname(dst)).mkdir(parents=True, exist_ok=True)
                shutil.move(src, dst)
                print(f'[MOVE] {rel}')

            except Exception as e:
                print(f'[ERROR] Procesando {src}: {e}')
                # no movemos el archivo en error; queda para revisión

    # Escribir consolidados por tipo (sobrescribe si existen)
    for tipo, dfs in buckets.items():
        if not dfs:
            continue
        df_final = pd.concat(dfs, ignore_index=True)
        outpath = outnames[tipo]
        df_final.to_excel(outpath, index=False)
        print(f'[OK] Consolidado {tipo}: {outpath}')

    print('Fin de ejecución.')
    print('=' * 40)


if __name__ == '__main__':
    run()
