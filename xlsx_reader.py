"""Simple, streaming XLSX values reader using only Python's standard library.

Reads the sheet holding required column names; supports shared strings and inline strings.
It does not execute formulas or macros. This is intentionally a read-only importer.
"""
from io import BytesIO
from pathlib import PurePosixPath
import re
from zipfile import ZipFile, BadZipFile
from xml.etree import ElementTree as ET

M = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
P = '{http://schemas.openxmlformats.org/package/2006/relationships}'
REQUIRED = {'code', 'name', 'price', 'stock'}


def _column_index(reference):
    letters = re.match(r'([A-Z]+)', reference or '')
    if not letters:
        raise ValueError('Excel ячейка манзили нотўғри')
    out = 0
    for char in letters.group(1):
        out = out * 26 + ord(char) - ord('A') + 1
    return out - 1


def _parse_shared(z):
    if 'xl/sharedStrings.xml' not in z.namelist():
        return []
    names = []
    with z.open('xl/sharedStrings.xml') as f:
        for _, el in ET.iterparse(f, events=('end',)):
            if el.tag == M + 'si':
                names.append(''.join(t.text or '' for t in el.iter(M + 't')))
                el.clear()
    return names


def _sheets(z):
    book = ET.fromstring(z.read('xl/workbook.xml'))
    rels = ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
    mapping = {}
    for rel in rels:
        mapping[rel.attrib.get('Id')] = rel.attrib.get('Target')
    paths = []
    for sh in book.findall('.//' + M + 'sheet'):
        target = mapping.get(sh.attrib.get(R + 'id'), '')
        if not target:
            continue
        if target.startswith('/'):
            name = target.lstrip('/')
        elif target.startswith('xl/'):
            name = target
        else:
            name = 'xl/' + target
        # Normalize to avoid externally referenced sheets.
        name = str(PurePosixPath(name))
        if '..' in PurePosixPath(name).parts or name not in z.namelist():
            continue
        paths.append((sh.attrib.get('name', ''), name))
    return paths


def _iter_rows(z, path, shared):
    with z.open(path) as f:
        for _, el in ET.iterparse(f, events=('end',)):
            if el.tag != M + 'row':
                continue
            data = {}
            for cell in el.findall(M + 'c'):
                ix = _column_index(cell.get('r'))
                typ = cell.get('t')
                if typ == 'inlineStr':
                    inline = cell.find(M + 'is')
                    val = ''.join(t.text or '' for t in inline.iter(M + 't')) if inline is not None else ''
                else:
                    v = cell.find(M + 'v')
                    val = (v.text or '') if v is not None else ''
                    if typ == 's' and val:
                        val = shared[int(val)]
                if val != '':
                    data[ix] = val
            if data:
                yield data
            el.clear()


def read_products(upload_bytes):
    if len(upload_bytes) > 12 * 1024 * 1024:
        raise ValueError('Excel файл ҳажми 12 МБ дан ошмасин')
    try:
        z = ZipFile(BytesIO(upload_bytes))
    except BadZipFile:
        raise ValueError('Файл ҳақиқий .xlsx эмас') from None
    with z:
        infos = z.infolist()
        if sum(i.file_size for i in infos) > 90 * 1024 * 1024:
            raise ValueError('Excel файл жуда катта')
        try:
            sheets = _sheets(z)
            shared = _parse_shared(z)
        except (KeyError, ET.ParseError, ValueError) as err:
            raise ValueError('Excel файлни ўқиб бўлмади') from err
        sheets.sort(key=lambda x: (x[0] != 'Sayt_katalogi', x[0] != 'TOP200_Rasmlar'))
        for title, path in sheets:
            rows = _iter_rows(z, path, shared)
            header = None
            records = []
            for row in rows:
                if header is None:
                    normalized = {str(val).strip().casefold(): col for col, val in row.items()}
                    if REQUIRED.issubset(normalized):
                        header = normalized
                    continue
                def get(key):
                    ix = header.get(key)
                    return str(row.get(ix, '')).strip() if ix is not None else ''
                record = {k: get(k) for k in ('code','name','price','stock','image_url','category','featured','active')}
                if any(record[k] for k in REQUIRED):
                    records.append(record)
                    if len(records) > 30000:
                        raise ValueError('30 000 дан кўп маҳсулот учун файлни бўлиб юкланг')
            if header is not None:
                return title, records
    raise ValueError('Excel ичида code, name, price, stock устунлари топилмади. Тайёр GRAND MARKET файлини юкланг.')
