"""Select source tables and compute bounded statistics without generating facts."""

from decimal import Decimal, Inexact, InvalidOperation, localcontext
import math
import re
import unicodedata

from .presentation import inline_literal
from .structured_data import parse_table


MAX_ROWS, MAX_COLUMNS, MAX_TABLES = 100, 20, 16
MAX_CALCULATION_ROWS = 5000
_STRUCTURED_FORMATS = ("csv", "tsv", "json", "xlsx")
_NUMBER_EXPONENT = 160
# Sum the full supported digit span and allow carry from adding every row.
_SUM_PRECISION = 2 * _NUMBER_EXPONENT + len(str(MAX_CALCULATION_ROWS)) + 1
_SOURCE_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_PLAIN_POINT_NUMBER = re.compile(r"[+-]?(0|[1-9][0-9]*)(?:\.([0-9]+))?")
_ROW_COUNT_QUESTION = re.compile(
    r'\b(?:(?:сколько|число|количество)\s+(?:строк|записей)|(?:row|record)\s+count|how many rows)\b', re.I)
_SUM_QUESTION = re.compile(r'\b(?:сумм[ауеы]|sum|total)\b', re.I)
_BARE_VALUE = re.compile(r'\b(?:только|лишь|без\s+слов|only|just)\b', re.I)
_NUMBER_ONLY = re.compile(r'\b(?:(?:только|лишь)\s+число|без\s+единиц|(?:only|just)\s+(?:the\s+)?number|without\s+units)\b', re.I)
_EXPLANATION = re.compile(r'\b(?:почему|зачем|как|why|how(?!\s+many\b))\b', re.I)


def parse_calculation_table(text, suffix, *, json_envelope=False):
    """Read a bounded structured source for filtering and calculation."""
    table = parse_table(text, suffix, max_rows=MAX_CALCULATION_ROWS, max_columns=MAX_COLUMNS,
                        json_envelope=json_envelope)
    if table["format"] in ("csv", "tsv") and not table["partial"]:
        # Establish the column format on the source, before a filter can
        # remove the cells that distinguish a decimal point from grouping.
        columns = [index for index in range(len(table["headers"]))
                   if _point_decimal_column(table["rows"], index)]
        if columns:
            table["point_decimal_columns"] = columns
    return table


def table_receipt(analysis, sources):
    """Keep exact, small aggregate results instead of the source rows."""
    if analysis.get('partial') or not sources:
        return None
    columns = [name for name in analysis['headers'] if 0 < len(name) <= 100]
    exact = {entry['header']: entry for entry in analysis.get('statistics', [])
             if entry.get('sum') is not None and not entry.get('partial')
             and columns.count(entry['header']) == 1}
    return {'sources': sources[:3], 'rows': analysis['row_count'],
            'partition': analysis.get('json_path'),
            'columns': columns,
            'sums': {name: entry['sum'] for name, entry in exact.items()},
            'units': {name: entry.get('unit') for name, entry in exact.items()},
            'conditions': [{key: clause[key] for key in ('column', 'operator', 'value', 'value_type') if key in clause}
                           for clause in analysis.get('conditions', [])[:4]]}


def current_table_receipt(context):
    """Use the last observation boundary, even when the same source was read again."""
    for turn in reversed(context['turns']):
        refs = turn['refs']
        if refs.keys() & {'active_source', 'read_goal', 'table_receipt'}:
            return refs.get('table_receipt')
    return None


def total_with_unit(value, unit):
    return f'{value} {unit}' if unit else value


def _cited_metric(text, receipt):
    sources = ', '.join(inline_literal(source) for source in receipt['sources'])
    return text + '\n\n' + f'Источник — {sources}.'


def recalled_table_metric(text, receipt, *, file_reference=False):
    """Return an exact metric, or an empty string if it needs a source or column."""
    count, total = bool(_ROW_COUNT_QUESTION.search(text)), bool(_SUM_QUESTION.search(text))
    if count == total or count and file_reference or _EXPLANATION.search(text):
        return None
    if receipt is None:
        return ''
    bare = bool(_BARE_VALUE.search(text))
    if count:
        value = str(receipt['rows'])
        return value if bare else _cited_metric(f'Строк в этом расчёте — **{value}**.', receipt)
    # Older receipts did not distinguish column completeness from source
    # completeness. They can prove a row count, but cannot prove an exact sum.
    if 'columns' not in receipt:
        return ''
    sums = receipt['sums']
    names = [name for name in receipt['columns']
             if re.search(r'(?<!\w)' + re.escape(name) + r'(?!\w)', text, re.I)]
    if not names and len(sums) == 1:
        names = list(sums)
    if len(names) == 1 and names[0] in sums:
        name = names[0]
        if _NUMBER_ONLY.search(text):
            return sums[name]
        amount = total_with_unit(sums[name], receipt.get('units', {}).get(name))
        return amount if bare else _cited_metric(f'Сумма по столбцу {inline_literal(name)} — **{amount}**.', receipt)
    return ''


def table_comparison(text, prior, current):
    """Use both receipts when the user refers to a changed, known total."""
    if 'columns' not in prior or 'columns' not in current:
        return None
    if not (re.search(r'\b(?:до\s+этого|раньше|предыдущ\w*|earlier|before)\b', text, re.I)
            and re.search(r'\b(?:теперь|сейчас|текущ\w*|now|current)\b', text, re.I)):
        return None
    totals = [(key, prior['sums'][key], value) for key, value in current['sums'].items()
              if key in prior['sums'] and prior['sums'][key] != value
              and current.get('units', {}).get(key) == prior.get('units', {}).get(key)]
    numbers = set(re.findall(r'(?<![\w.])-?\d+(?:[.,]\d+)?', text.replace(',', '.')))
    totals = [(key, old, new) for key, old, new in totals
              if numbers.intersection((old, new)) or _SUM_QUESTION.search(text)]
    return totals[0] if len(totals) == 1 else None


_NON_METRIC = re.compile(r"(?:^|\W)(?:id|uuid|phone|telephone|mobile|date|time|year|email|тел|телефон\w*|мобил\w*|контакт\w*|дата\w*|врем\w*|год|года|годы|годов|номер\w*|артикул\w*|идентификатор\w*|код|индекс)(?:$|\W)", re.I)
_UNITS = [("рублей", "₽"), ("рубля", "₽"), ("руб.", "₽"), ("руб", "₽"), ("RUB", "₽"), ("USD", "$"), ("US$", "$"), ("EUR", "€"), ("GBP", "£"), ("₽", "₽"), ("$", "$"), ("€", "€"), ("£", "£"), ("%", "%")]
_MISSING = {"", "-", "—", "–", "n/a", "null", "none", "нет", "не указано"}
_STOP = set("таблица таблицу таблицы таблице таблиц данные данных покажи прочитай найти найди нужна нужно нужная нужную пожалуйста мне про для по и в на с сколько a an the table tables show read find data about for of in please".split())


def _tokens(value):
    words = re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", value).casefold().replace("ё", "е"))
    result = set()
    for word in words:
        if word in _STOP:
            continue
        stem = re.sub(r"(?:иями|ами|ями|ого|ему|ах|ях|ов|ев|ой|ий|ая|ое|ие|ые|ом|ем|у|ю|а|я|ы|и|е)$", "", word) if re.search("[а-я]", word) else word
        result.add(stem if len(stem) >= 3 else word)
    return result


def _vector(value):
    values = list(value)
    if not values or len(values) > 4096 or any(isinstance(item, bool) for item in values):
        raise ValueError("Invalid embedding vector")
    result = [float(item) for item in values]
    norm = math.sqrt(math.fsum(item * item for item in result))
    if not math.isfinite(norm) or norm == 0:
        raise ValueError("Invalid embedding norm")
    return [item / norm for item in result]


def _literal_table_witness(table,question):
    fields=[str(table.get('caption','')),str(table.get('heading','')),
            *[str(header) for header in table.get('headers',[])[:MAX_COLUMNS]]]
    parts=[_tokens(field) for field in fields];all_words=set().union(*parts)
    # A named caption/header must be fully accounted for, not merely share a
    # word. "Sales" does not establish that a staff-phone list is sales data.
    return bool(question and question<=all_words and any(part and part<=question for part in parts))


def _selection_floor(semantic):
    model=getattr(getattr(semantic,'embedder',None),'model',None)
    value=getattr(model,'config',{}).get('table_selection_min_score')
    if value is None:return None
    if type(value) not in {int,float} or not math.isfinite(value) or not 0<value<=1:
        raise ValueError('Invalid table selection calibration')
    return float(value)


def select_table(tables, query, semantic=None):
    """Return one relevant table or explicit ambiguity; scores are not probabilities."""
    if not isinstance(tables, (list, tuple)) or any(not isinstance(table, dict) for table in tables):
        raise ValueError("Tables must be a list of objects")
    if not isinstance(query, str) or len(query) > 4000:
        raise ValueError("Invalid table question")
    if not tables:
        return {"tables":[], "needs_clarification":False, "candidates":[], "method":"none", "reason":"no_tables"}
    if len(tables) > MAX_TABLES:
        return {"tables":[], "needs_clarification":True, "candidates":[], "method":"none", "reason":"too_many_tables"}
    descriptions = [" ".join([str(table.get("caption", ""))[:1000], str(table.get("heading", ""))[:1000], *[str(header)[:200] for header in table.get("headers", [])[:MAX_COLUMNS]]]) for table in tables]
    question=_tokens(query)
    lexical=[len(question&_tokens(description))/len(question) if question else 0.0 for description in descriptions]
    witnesses=[index for index,table in enumerate(tables) if _literal_table_witness(table,question)]
    def literal_candidates():
        return [{"id":str(table.get('id',index)),"caption":str(table.get('caption',''))[:1000],"heading":str(table.get('heading',''))[:1000],"score":round(lexical[index],6)} for index,table in sorted(enumerate(tables),key=lambda item:(-lexical[item[0]],item[0]))]
    if not question:
        return {'tables':[tables[0]] if len(tables)==1 else [],'needs_clarification':len(tables)!=1,
                'candidates':literal_candidates(),'method':'single' if len(tables)==1 else 'none',
                'reason':'single_table' if len(tables)==1 else 'missing_table_goal'}
    if witnesses:
        return {'tables':[tables[witnesses[0]]] if len(witnesses)==1 else [],'needs_clarification':len(witnesses)!=1,
                'candidates':literal_candidates(),'method':'lexical','reason':'explicit_metadata_match' if len(witnesses)==1 else 'similar_scores'}
    eligible={index for index,description in enumerate(descriptions) if any(len(word)>=3 or word.isdecimal() for word in _tokens(description))}
    if not eligible:
        return {'tables':[],'needs_clarification':True,'candidates':literal_candidates(),'method':'none','reason':'missing_table_metadata'}
    method, fallback = "lexical", False
    scores = [];minimum=None
    if semantic is not None and query.strip():
        try:
            minimum=_selection_floor(semantic)
            q = _vector(semantic.embedder.query(query))
            vectors = [_vector(vector) for vector in semantic.embedder.encode(descriptions, query=False)]
            if len(vectors) != len(tables) or any(len(vector) != len(q) for vector in vectors):
                raise ValueError("Embedding shapes differ")
            scores = [max(-1.0, min(1.0, math.fsum(a*b for a,b in zip(q, vector)))) for vector in vectors]
            method = "semantic"
        except (AttributeError, TypeError, ValueError, OSError):
            fallback = True
    if not scores:
        scores=lexical
    ranked = sorted(eligible, key=lambda index: (-scores[index], index))
    candidate_order=[*ranked,*sorted(set(range(len(tables)))-eligible)]
    candidates = [{"id":str(tables[index].get("id", index)), "caption":str(tables[index].get("caption", ""))[:1000], "heading":str(tables[index].get("heading", ""))[:1000], "score":round(scores[index], 6),"selection_eligible":index in eligible} for index in candidate_order]
    best = scores[ranked[0]]
    margin = best - scores[ranked[1]] if len(ranked) > 1 else None
    relevant=method=='semantic' and minimum is not None and best>=minimum
    close=margin is not None and margin<=max(0.025,abs(best)*0.04)
    uncertain=not relevant or close
    reason='uncalibrated_semantic' if method=='semantic' and minimum is None else 'no_relevance_evidence' if not relevant else 'similar_scores' if close else 'calibrated_relative_match'
    return {"tables":[] if uncertain else [tables[ranked[0]]], "needs_clarification":uncertain, "candidates":candidates, "method":method, "reason":reason,
            "margin":None if margin is None else round(margin, 6), "semantic_unavailable":fallback,'minimum_score':minimum}


def _unit(value):
    result = set()
    for token, unit in _UNITS:
        if token.casefold() in value.casefold():
            result.add(unit)
    return next(iter(result)) if len(result) == 1 else None


def _point_decimal_column(rows, index):
    witness = False
    for row in rows:
        value = row[index].strip()
        if value.casefold() in _MISSING:
            continue
        if len(value) > 160 or not (match := _PLAIN_POINT_NUMBER.fullmatch(value)):
            return False
        integer, fraction = match.groups()
        if fraction and (integer == "0" or len(integer) > 3 or len(fraction) != 3):
            witness = True
    return witness


def parse_numeric(value, unit_hint=None, *, typed=False):
    """Parse a literal numeric cell as (Decimal, printed unit), else None."""
    if value is None or isinstance(value, bool):
        return None
    if typed or isinstance(value, (int, float, Decimal)):
        value = str(value).strip()
        if len(value) > 160 or not _SOURCE_NUMBER.fullmatch(value):
            return None
        try:
            number = Decimal(value)
        except InvalidOperation:
            return None
        if (abs(number.adjusted()) <= _NUMBER_EXPONENT
                and abs(number.as_tuple().exponent) <= _NUMBER_EXPONENT):
            return number, unit_hint
        return None
    if not isinstance(value, str) or len(value) > 160:
        return None
    text = unicodedata.normalize("NFKC", value).strip().replace("−", "-")
    if text.casefold() in _MISSING:
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1].strip()
    units = set()
    for _ in range(2):
        found = False
        for token, unit in _UNITS:
            if text.casefold().startswith(token.casefold()):
                text = text[len(token):].strip(); units.add(unit); found = True; break
            if text.casefold().endswith(token.casefold()):
                text = text[:-len(token)].strip(); units.add(unit); found = True; break
        if not found:
            break
    if len(units) > 1:
        return None
    unit = next(iter(units), unit_hint)
    sign = ""
    if text.startswith(("-", "+")):
        sign, text = text[0], text[1:]
    if negative and sign:
        return None
    if not re.fullmatch(r"[0-9][0-9 .,]*", text) or len(re.findall(r"[0-9]", text)) > 64:
        return None
    if "," in text and "." in text:
        decimal = "," if text.rfind(",") > text.rfind(".") else "."
        group = "." if decimal == "," else ","
        if text.count(decimal) != 1:
            return None
        integer, fraction = text.rsplit(decimal, 1)
        pieces = integer.split(group)
        if not fraction.isdigit() or not 1 <= len(pieces[0]) <= 3 or any(len(piece) != 3 or not piece.isdigit() for piece in pieces[1:]):
            return None
        text = "".join(pieces) + "." + fraction
    else:
        separator = "," if "," in text else "." if "." in text else None
        if separator and text.count(separator) > 1:
            pieces = text.split(separator)
            if not 1 <= len(pieces[0]) <= 3 or any(len(piece) != 3 or not piece.isdigit() for piece in pieces[1:]):
                return None
            text = "".join(pieces)
        elif separator:
            integer, fraction = text.split(separator)
            if not fraction.isdigit():
                return None
            # A grouping prefix cannot be zero, so 0.125 has a decimal point.
            decimal_zero = integer == "0" and separator == "."
            if len(fraction) == 3 and " " not in integer and 1 <= len(integer) <= 3 and not decimal_zero:
                if unit == "$" and separator == ",":
                    text = integer + fraction
                else:
                    return None
            else:
                text = integer + "." + fraction
    integer, dot, fraction = text.partition(".")
    if " " in integer:
        pieces = integer.split(" ")
        if not 1 <= len(pieces[0]) <= 3 or any(len(piece) != 3 or not piece.isdigit() for piece in pieces[1:]):
            return None
        integer = "".join(pieces)
    normalized = ("-" if negative else sign) + integer + (dot + fraction if dot else "")
    try:
        number = Decimal(normalized)
    except InvalidOperation:
        return None
    return (number, unit) if number.is_finite() else None


def _decimal_text(number):
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def analyze_table(table):
    """Calculate over observed rows and keep a bounded preview of the source cells."""
    if isinstance(table,dict) and table.get('unsupported_layout'):
        return {'headers':table.get('headers',[])[:MAX_COLUMNS],'rows':[],'statistics':[],'partial':True,'unsupported_layout':table['unsupported_layout']}
    if not isinstance(table, dict) or not isinstance(table.get("rows", []), (list, tuple)) or not isinstance(table.get("headers", []), (list, tuple)):
        raise ValueError("Invalid DOM table")
    headers = list(table.get("headers", []))[:MAX_COLUMNS]
    source = table.get("rows", [])
    types = table.get("cell_types")
    point_decimal_columns = table.get("point_decimal_columns", ())
    structured = table.get("format") in _STRUCTURED_FORMATS
    max_rows = MAX_CALCULATION_ROWS if structured else MAX_ROWS
    if any(not isinstance(header, str) for header in headers):
        raise ValueError("Headers must be source strings")
    rows, source_indices, removed = [], [], 0
    partial = bool(table.get("partial")) or len(table.get("headers", [])) > MAX_COLUMNS
    folded_headers = [header.strip().casefold() for header in headers]
    for index, raw in enumerate(source[:max_rows + 1]):
        if not isinstance(raw, (list, tuple)) or any(not isinstance(value, (str, int, float, type(None))) or isinstance(value, bool) or isinstance(value, float) and not math.isfinite(value) for value in raw):
            raise ValueError("Cells must be finite source scalars")
        row = list(raw[:MAX_COLUMNS])
        if not structured and headers and len(row) == len(headers) and [str(value).strip().casefold() for value in row] == folded_headers:
            removed += 1
            continue
        if len(rows) == max_rows:
            partial = True
            break
        partial = partial or len(raw) > MAX_COLUMNS
        rows.append(row)
        source_indices.append(index)
    if len(source) > max_rows + 1:
        partial = True
    count_columns = max(len(headers), max(map(len, rows), default=0))
    statistics, excluded = [], []
    currency_columns=[i for i,h in enumerate(headers) if h.strip().casefold() in {'currency','валюта'}]
    for column in range(count_columns):
        header = headers[column] if column < len(headers) else None
        if header and _NON_METRIC.search(header):
            excluded.append({"column":column,"header":header,"reason":"identifier_date_or_contact"})
            continue
        numeric, missing, other = [], 0, 0
        hint = _unit(header or "")
        point_decimal = column in point_decimal_columns
        monetary=bool(header and re.search(r'price|amount|cost|total|цен|стоим|сумм',header,re.I))
        for index, row in enumerate(rows):
            cell = row[column] if column < len(row) else None
            if cell is None or isinstance(cell, str) and cell.strip().casefold() in _MISSING:
                missing += 1; continue
            row_hint=hint
            if monetary and len(currency_columns)==1:
                currency=row[currency_columns[0]] if currency_columns[0]<len(row) else None
                if isinstance(currency,str):row_hint=_unit(currency) or currency.strip().upper() or hint
            tag = types[source_indices[index]][column] if types is not None and column < len(row) else None
            parsed = parse_numeric(cell, row_hint, typed=tag == "number" or point_decimal)
            if parsed is None:
                other += 1
            else:
                numeric.append((index, parsed[0], parsed[1]))
        if not numeric:
            continue
        units = {item[2] for item in numeric}
        if len(units) > 1:
            excluded.append({"column":column,"header":header,"reason":"mixed_units","count":len(numeric)})
            continue
        if hint and units != {hint}:
            excluded.append({"column":column,"header":header,"reason":"header_unit_conflict","count":len(numeric)})
            continue
        values = [item[1] for item in numeric]
        with localcontext() as context:
            context.prec = _SUM_PRECISION
            context.traps[Inexact] = True
            total = sum(values, Decimal(0))
        with localcontext() as context:
            context.prec = 28
            context.clear_flags()
            mean = total / Decimal(len(values))
            rounded = bool(context.flags[Inexact])
        low, high = min(values), max(values)
        statistics.append({
            "column": column,
            "header": header,
            "unit": next(iter(units)),
            "count": len(values),
            "missing": missing,
            "non_numeric": other,
            "min": _decimal_text(low),
            "max": _decimal_text(high),
            "mean": _decimal_text(mean),
            "mean_rounded": rounded,
            "sum": _decimal_text(total),
            "minrows": [index for index, value, _ in numeric if index < MAX_ROWS and value == low],
            "maxrows": [index for index, value, _ in numeric if index < MAX_ROWS and value == high],
            "partial": partial or bool(other) or bool(missing),
        })
    preview_indices = source_indices[:MAX_ROWS]
    if (original_indices := table.get("source_row_indices")) is not None:
        preview_indices = [original_indices[index] for index in preview_indices]
    return {
        "id": table.get("id"),
        "caption": table.get("caption", ""),
        "heading": table.get("heading", ""),
        "headers": headers,
        "rows": rows[:MAX_ROWS],
        "source_row_indices": preview_indices,
        "row_count": len(rows),
        "source_total_rows": table.get("source_total_rows", table.get("total_rows", len(source))),
        "removed_header_rows": removed,
        "partial": partial,
        "statistics": statistics,
        "excluded_columns": excluded,
    }
