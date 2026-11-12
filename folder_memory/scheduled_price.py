"""Extract a literal, currency-qualified price from a bounded static page."""

from dataclasses import dataclass, field
from decimal import Decimal
from hashlib import sha256
from html.parser import HTMLParser
import json
import re
import unicodedata

from .browser_evidence import product_evidence
from .http_client import bounded_request, validate_public_url
from .table_analysis import parse_numeric


class PriceUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class Price:
    amount: Decimal
    currency: str
    source_key: str = ""


_ROOT = re.compile(r"""//(?P<tag>\*|[a-z][a-z0-9-]*)\[@id=(?P<quote>["'])(?P<id>[^"'\s]{1,128})(?P=quote)\]""")
_STEP = re.compile(r"/([a-z][a-z0-9-]*)(?:\[([1-9][0-9]{0,3})\])?")
_VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())
_HIDDEN = frozenset("head script style noscript template iframe".split())
_PRODUCT_TYPES = {"Product", "Offer", "AggregateOffer", "WebPage"}
_CONDITIONAL = {"description", "priceSpecification", "acceptedPaymentMethod", "paymentMethod",
                "eligibleTransactionVolume", "validForMemberTier", "validFrom", "validThrough"}
_SYMBOLS = {"₽": "RUB", "€": "EUR", "₴": "UAH", "₸": "KZT", "US$": "USD", "CA$": "CAD", "A$": "AUD"}
_AMBIGUOUS_SYMBOLS = {"$": {"USD", "CAD", "AUD", "NZD", "SGD", "HKD", "MXN"}, "¥": {"JPY", "CNY"}, "£": {"GBP", "EGP"}}
_TOKEN = r"(?:US\$|CA\$|A\$|[A-Z]{3}|рублей|рубля|руб\.?|[₽€₴₸$¥£])"
_MONEY = re.compile(rf"(?:(?P<before>{_TOKEN})\s*)?(?P<number>[0-9][0-9 .,]*?)(?:\s*(?P<after>{_TOKEN}))?")
_DECIMAL = re.compile(r"[0-9]{1,12}(?:\.[0-9]{1,6})?")
_LIMITS = {"public_web": True, "timeout_seconds": 5, "response_bytes": 512 * 1024, "same_origin_redirects": 2}


def currency_code(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Z]{3}", value) is not None


def selector_steps(xpath):
    """Support one id anchor and literal child tags, optionally numbered."""
    root = _ROOT.match(xpath) if isinstance(xpath, str) and len(xpath) <= 512 else None
    if not root:
        raise ValueError('Для фоновой проверки нужен XPath вида //*[@id="price"]/span[1].')
    path = xpath[root.end():]
    steps = list(_STEP.finditer(path))
    if len(steps) > 24 or "".join(step[0] for step in steps) != path:
        raise ValueError("XPath для фоновой проверки поддерживает дочерние теги и индексы [1], [2] и т. д.")
    return root["tag"], root["id"], [(step[1], int(step[2]) if step[2] else None) for step in steps]


@dataclass
class _Element:
    tag: str
    attrs: dict
    hidden: bool = False
    children: list = field(default_factory=list)

    def text(self):
        if self.hidden:
            return ""
        return "".join(part.text() if isinstance(part, _Element) else part for part in self.children)


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = [_Element("", {})]
        self.elements = []
        self.canonical = None
        self.headings = []
        self.main_headings = []
        self.scripts = []

    def _add(self, tag, attributes):
        if len(self.elements) >= 12_000 or len(self.stack) >= 96:
            raise PriceUnavailable("Страница слишком сложная для фоновой проверки цены.")
        attrs = dict(attributes)
        parent = self.stack[-1]
        hidden = (parent.hidden or tag in _HIDDEN or "hidden" in attrs or "inert" in attrs
                  or (attrs.get("aria-hidden") or "").lower() == "true"
                  or bool(re.search(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse))\b", attrs.get("style") or "", re.I)))
        item = _Element(tag, attrs, hidden)
        parent.children.append(item)
        self.elements.append(item)
        if tag == "link" and self.canonical is None and "canonical" in (attrs.get("rel") or "").lower().split():
            self.canonical = attrs.get("href")
        if (tag == "script" and (attrs.get("type") or "").strip().lower() == "application/ld+json"
                and not any(parent.tag in {"template", "noscript", "iframe"} for parent in self.stack)):
            self.scripts.append(item)
        if tag == "h1" and not hidden:
            self.headings.append(item)
            if any(node.tag == "main" or node.attrs.get("role") == "main" for node in self.stack):
                self.main_headings.append(item)
        return item

    def handle_starttag(self, tag, attrs):
        item = self._add(tag, attrs)
        if tag not in _VOID:
            self.stack.append(item)

    def handle_startendtag(self, tag, attrs):
        self._add(tag, attrs)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)

    def select(self, xpath):
        tag, identifier, steps = selector_steps(xpath)
        matches = [node for node in self.elements if node.attrs.get("id") == identifier and tag in {"*", node.tag}]
        if len(matches) != 1:
            raise PriceUnavailable("XPath должен однозначно находить элемент в HTML страницы.")
        for child_tag, position in steps:
            children = [node for node in matches[0].children if isinstance(node, _Element) and node.tag == child_tag]
            matches = children if position is None else children[position - 1:position]
            if len(matches) != 1:
                raise PriceUnavailable("XPath должен однозначно находить элемент в HTML страницы.")
        if matches[0].hidden:
            raise PriceUnavailable("XPath указывает на скрытый элемент страницы.")
        return matches[0].text()

    def heading_texts(self):
        headings = self.main_headings or self.headings
        return [" ".join(node.text().split()) for node in headings] if len(headings) == 1 else []

    def product_nodes(self):
        records, visited = [], 0
        for index, script in enumerate(self.scripts):
            raw = "".join(part for part in script.children if isinstance(part, str)).strip()
            if not raw:
                continue
            try:
                data = json.loads(raw, parse_float=str)
            except (ValueError, RecursionError):
                raise PriceUnavailable("Разметка JSON-LD на странице повреждена.") from None
            stack = [(data, f"jsonld[{index}]", 0)]
            while stack:
                value, path, depth = stack.pop()
                if not isinstance(value, (dict, list)):
                    continue
                visited += 1
                if visited > 512 or depth > 12:
                    raise PriceUnavailable("Слишком много данных JSON-LD для однозначной проверки.")
                if isinstance(value, dict):
                    types = value.get("@type", [])
                    types = types if isinstance(types, list) else [types]
                    if any(item.rstrip("/").rsplit("/", 1)[-1].rsplit("#", 1)[-1] in _PRODUCT_TYPES for item in types if isinstance(item, str)):
                        offers = value.get("offers")
                        if len(records) >= 32 or isinstance(offers, list) and len(offers) > 8:
                            raise PriceUnavailable("Слишком много предложений для однозначной проверки.")
                        records.append({"data": value, "kind": "json_ld", "path": path})
                items = value.items() if isinstance(value, dict) else enumerate(value)
                stack.extend((child, f"{path}/{key}", depth + 1) for key, child in items if isinstance(child, (dict, list)))
        return records


def _amount(value):
    if not isinstance(value, str) or not _DECIMAL.fullmatch(value):
        raise PriceUnavailable("Для наблюдения нужна одна точная цена.")
    return Decimal(value)


def _currency(token, expected):
    if token is None:
        return expected
    if token in _AMBIGUOUS_SYMBOLS:
        if expected in _AMBIGUOUS_SYMBOLS[token]:
            return expected
        raise PriceUnavailable("Укажи код валюты для цены с неоднозначным символом.")
    if token.startswith("руб"):
        return "RUB"
    return _SYMBOLS.get(token, token)


def _literal_price(text, expected):
    text = " ".join(unicodedata.normalize("NFKC", text).split())
    match = _MONEY.fullmatch(text) if len(text) <= 160 else None
    if not match or (match["before"] and match["after"]):
        raise PriceUnavailable("В элементе нужна одна цена с валютой, например «12 490,50 ₽».")
    token = match["before"] or match["after"]
    currency = _currency(token, expected)
    number = parse_numeric(match["number"], "$" if currency == "USD" else None)
    if not currency_code(currency) or number is None:
        raise PriceUnavailable("Не получилось однозначно разобрать сумму и валюту.")
    return _amount(format(number[0], "f")), currency


def _structured_price(page, url, headings):
    signals = {"canonical_url": page.canonical, "h1": headings, "nodes": page.product_nodes()}
    result = product_evidence(signals, url)
    products = result["primary_products"]
    if result["status"] != "ok" or result["partial"] or len(products) != 1 or len(products[0]["offers"]) != 1:
        raise PriceUnavailable("На странице нужна одна однозначная цена товара. Можно указать XPath к цене в HTML.")
    product = products[0]
    offer = product["offers"][0]
    for condition in offer["conditions"]:
        if condition["kind"] in _CONDITIONAL:
            raise PriceUnavailable("У предложения есть дополнительные условия. Укажи XPath к нужной цене.")
        if condition["kind"] == "availability":
            value = condition["value"]
            if isinstance(value, dict):
                value = value.get("@id")
            if not isinstance(value, str) or value.rstrip("/").rsplit("/", 1)[-1] != "InStock":
                raise PriceUnavailable("В разметке нет подтверждения наличия товара.")
    price, low, high = offer["price"], offer["low_price"], offer["high_price"]
    if (low is not None or high is not None) and (low is None or low != high or price not in {None, low}):
        raise PriceUnavailable("Диапазон цен нельзя использовать как одну цену.")
    currency = offer["currency"]
    if not currency_code(currency):
        raise PriceUnavailable("В разметке предложения не указана однозначная валюта.")
    return _amount(price if price is not None else low), currency, [product["url"], product["sku"], product["name"]]


def check_price(job, request=bounded_request):
    """Fetch a public page with no cookies or model calls."""
    try:
        response = request({"url": job["url"], "method": "GET", "body": "", "content_type": ""}, _LIMITS)
    except TimeoutError:
        raise PriceUnavailable("Сайт не ответил за отведённое время.") from None
    except PermissionError:
        raise PriceUnavailable("Сайт ведёт на непубличный адрес или другой домен.") from None
    except (OSError, ValueError):
        raise PriceUnavailable("Не удалось получить страницу сайта.") from None
    status = response["status"]
    if status != 200:
        raise PriceUnavailable(f"Сайт вернул HTTP {status}.")
    if response["truncated"]:
        raise PriceUnavailable("Страница превышает лимит 512 КиБ для фоновой проверки.")
    if response["content_type"].partition(";")[0].strip().lower() not in {"text/html", "application/xhtml+xml"}:
        raise PriceUnavailable("Фоновая проверка принимает HTML-страницы.")
    url = response["url"]
    validate_public_url(url)
    page = _Page()
    try:
        page.feed(response["body"])
        page.close()
        headings = page.heading_texts()
    except PriceUnavailable:
        raise
    except (RecursionError, AssertionError, ValueError):
        raise PriceUnavailable("Не удалось разобрать HTML страницы.") from None
    if job["xpath"]:
        amount, currency = _literal_price(page.select(job["xpath"]), job["currency"])
        identity = [url, headings]
    else:
        amount, currency, identity = _structured_price(page, url, headings)
    if job["currency"] and job["currency"] != currency:
        raise PriceUnavailable("Валюта цены отличается от настроек наблюдения.")
    source_key = sha256(json.dumps(identity).encode()).hexdigest()
    return Price(amount, currency, source_key)
