"""Bind literal product evidence to the observed page without guessing prices."""

import json
import math
import re
import unicodedata
import urllib.parse


def _route_fragment(fragment):
    return urllib.parse.unquote(fragment).startswith(("/", "!/"))


def _url(value, base, *, fragment=False):
    if isinstance(value, dict):
        value = value.get("@id", value.get("url"))
    if not isinstance(value, str) or not value or len(value) > 4096 or any(char.isspace() or ord(char) < 32 for char in value):
        return None
    try:
        parsed = urllib.parse.urlsplit(urllib.parse.urljoin(base, value))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            return None
        port = parsed.port
    except ValueError:
        return None
    host = parsed.hostname.lower()
    if ":" in host:
        host = "[" + host + "]"
    if port and port != (443 if parsed.scheme == "https" else 80):
        host += ":" + str(port)
    anchor = parsed.fragment if fragment or _route_fragment(parsed.fragment) else ""
    return urllib.parse.urlunsplit((parsed.scheme, host, parsed.path or "/", parsed.query, anchor))


def _name(value):
    if not isinstance(value, str):
        return ""
    return " ".join(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", value).casefold()))


def _types(node):
    value = node.get("@type", [])
    return {item.rstrip("/").rsplit("/", 1)[-1].rsplit("#", 1)[-1] for item in (value if isinstance(value, list) else [value]) if isinstance(item, str)}


def _literal(value):
    if isinstance(value, str):
        return value[:600]
    if type(value) in {int, float} and (not isinstance(value, float) or math.isfinite(value)):
        return str(value)
    return None


def _price_key(offer):
    price, low, high = offer["price"], offer["low_price"], offer["high_price"]
    if price is not None and (low is None and high is None or low == high == price):
        return "point", price, offer["currency"]
    if price is None and low is not None and low == high:
        return "point", low, offer["currency"]
    return "range", price, low, high, offer["currency"]


def _extend_evidence(target, values):
    known = {json.dumps(item, sort_keys=True, ensure_ascii=False) for item in target}
    for item in values:
        key = json.dumps(item, sort_keys=True, ensure_ascii=False)
        if key not in known:
            target.append(item)
            known.add(key)


def _merge_representations(products):
    merged = []
    for product in products:
        description = " ".join((product["description"] or "").split())
        previous = next((other for other in merged if _name(product["name"]) and _name(other["name"]) == _name(product["name"]) and other["url"] == product["url"] and " ".join((other["description"] or "").split()) == description and (not other["sku"] or not product["sku"] or other["sku"] == product["sku"])), None)
        if previous is None:
            merged.append(product)
            continue
        _extend_evidence(previous["evidence"], product["evidence"])
        previous["sku"] = previous["sku"] or product["sku"]
        for offer in product["offers"]:
            matching = next((other for other in previous["offers"] if _price_key(other) == _price_key(offer)), None)
            if matching is None:
                previous["offers"].append(offer)
                continue
            # A literal equal low/high range corroborates an explicit exact
            # price; it never turns a varying range into a selected price.
            if matching["price"] is None and offer["price"] is not None:
                matching["price"] = offer["price"]
            matching["raw_values"].update(offer["raw_values"])
            _extend_evidence(matching["conditions"], offer["conditions"])
            _extend_evidence(matching["evidence"], offer["evidence"])
    return merged


def product_evidence(signals, page_url):
    """Only explicit URL/mainEntity links or an exact sole H1 can bind Product."""
    signals = signals if isinstance(signals, dict) else {}
    page = _url(page_url, page_url)
    canonical = _url(signals.get("canonical_url"), page_url)
    if canonical and urllib.parse.urlsplit(canonical).netloc != urllib.parse.urlsplit(page).netloc:
        canonical = None
    canonical = canonical or page
    page_addresses = {value for value in (page, canonical) if value}
    if page != canonical and any(_route_fragment(urllib.parse.urlsplit(url).fragment) for url in page_addresses):
        # A hash route can replace the whole product. Its generic canonical
        # page alone does not prove which item is visible in the current tab.
        page_addresses.discard(canonical)
    fallback_url = canonical if canonical in page_addresses else page
    headings = signals.get("h1")
    headings = headings if isinstance(headings, list) else []
    h1 = [value for value in headings[:4] if isinstance(value, str) and value.strip()]
    mentions = signals.get("monetary_mentions")
    mentions = mentions if isinstance(mentions, list) else []
    result = {"status": "unavailable", "reason": "no_linked_product", "canonical_url": canonical, "h1": h1,
              "primary_products": [], "other_product_count": 0, "unbound_monetary_mentions": mentions[:8],
              "partial": bool(signals.get("partial")), "content_trust": "untrusted_page_data"}
    records = signals.get("nodes")
    records = records if isinstance(records, list) else []
    records = [entry for entry in records[:32] if isinstance(entry, dict) and isinstance(entry.get("data"), dict)]
    index = {}
    for entry in records:
        identity = _url(entry["data"].get("@id"), page, fragment=True)
        if identity:
            index.setdefault(identity, []).append(entry)

    def resolve(value):
        if not isinstance(value, dict):
            value = {"@id": value} if isinstance(value, str) else {}
        if set(value) == {"@id"}:
            matches = index.get(_url(value["@id"], page, fragment=True), [])
            if len(matches) == 1:
                return matches[0]["data"], matches[0]
            if len(matches) > 1:
                result["partial"] = True
                return {}, None
        return value, None

    pages = [(entry, page_addresses.intersection(_url(entry["data"].get(key), page) for key in ("url", "@id")))
             for entry in records if "WebPage" in _types(entry["data"])]
    products = []
    for entry in records:
        node, path = entry["data"], entry.get("path", "")
        if "Product" not in _types(node):
            continue
        reference = _url(node.get("@id"), page, fragment=True) or _url(node.get("url"), page, fragment=True)
        declared = {key: _url(node.get(key), page) for key in ("url", "mainEntityOfPage")}
        bindings = []
        exact = bool(page and page in declared.values())
        for key, address in declared.items():
            if address in page_addresses:
                bindings.append({"kind": key, "value": address})
        for web, addresses in pages:
            if not addresses:
                continue
            main = web["data"].get("mainEntity")
            values = main if isinstance(main, list) else [main]
            nested = web.get("path", "") + "/mainEntity"
            direct = nested != "/mainEntity" and bool(re.fullmatch(re.escape(nested) + r"(?:/[0-9]+)?", path))
            linked = reference and any(_url(value, page, fragment=True) == reference for value in values)
            if direct or linked:
                bindings.append({"kind": "WebPage.mainEntity", "path": web.get("path", "")})
                exact = exact or page in addresses
        foreign = any(value and value not in page_addresses for value in declared.values())
        if not foreign and len(h1) == 1 and _name(node.get("name")) and _name(node.get("name")) == _name(h1[0]):
            bindings.append({"kind": "exact_main_h1", "value": h1[0]})
        if not bindings:
            result["other_product_count"] += 1
            continue
        products.append((entry, reference, declared["url"], bindings, exact))

    # A canonical link may cover several product variants. Prefer a product
    # explicitly tied to the page being viewed over one linked only to the
    # canonical page or through its heading. Keep canonical fallback for URLs
    # that merely add tracking parameters to the main product page.
    prefer_exact = page != canonical and any(product[-1] for product in products)
    seen = set()
    for entry, reference, product_url, bindings, exact in products:
        if prefer_exact and not exact:
            result["other_product_count"] += 1
            continue
        node, path = entry["data"], entry.get("path", "")
        signature = json.dumps(node, sort_keys=True, ensure_ascii=False)
        if signature in seen:
            continue
        seen.add(signature)
        evidence = {"kind": entry.get("kind", "json_ld"), "path": path, "source_url": page_url}
        product = {"id": reference or path, "name": _literal(node.get("name")), "url": product_url or (page if exact else fallback_url),
                   "sku": _literal(node.get("sku")),
                   "description": node.get("description")[:4000] if isinstance(node.get("description"), str) else None,
                   "description_evidence": {**evidence, "path": path + "/description"} if isinstance(node.get("description"), str) else None,
                   "evidence": [evidence, *bindings], "offers": []}
        raw_offers = node.get("offers", [])
        if isinstance(raw_offers, list):
            offers = [(offer, path + "/offers/" + str(position), evidence["kind"]) for position, offer in enumerate(raw_offers)]
        else:
            offers = [(raw_offers, path + "/offers", evidence["kind"])]
        # Separately declared Offer is usable only with an explicit itemOffered link.
        if reference:
            offers += [(other["data"], other.get("path", ""), other.get("kind", "json_ld")) for other in records
                       if "Offer" in _types(other["data"]) and _url(other["data"].get("itemOffered"), page, fragment=True) == reference]
        if len(offers) > 8:
            result["partial"] = True
        offer_signatures = set()
        for raw, offer_path, offer_kind in offers[:8]:
            offer, origin = resolve(raw)
            if not offer or not (_types(offer) & {"Offer", "AggregateOffer"} or any(key in offer for key in ("price", "lowPrice", "highPrice"))):
                continue
            offered = _url(offer.get("itemOffered"), page, fragment=True)
            if offered and offered != reference:
                continue
            signature = json.dumps(offer, sort_keys=True, ensure_ascii=False)
            if signature in offer_signatures:
                continue
            offer_signatures.add(signature)
            source = {"kind": origin.get("kind", offer_kind) if origin else offer_kind,
                      "path": origin.get("path", offer_path) if origin else offer_path, "source_url": page_url}
            conditions = [{"kind": key, "value": offer[key], "evidence": {**source, "path": source["path"] + "/" + key}} for key in ("description", "priceSpecification", "acceptedPaymentMethod", "paymentMethod", "eligibleTransactionVolume", "shippingDetails", "availability", "validFrom", "validThrough", "validForMemberTier") if key in offer]
            product["offers"].append({"price": _literal(offer.get("price")), "low_price": _literal(offer.get("lowPrice")), "high_price": _literal(offer.get("highPrice")),
                                      "currency": _literal(offer.get("priceCurrency")), "conditions": conditions,
                                      "raw_values": {key: offer[key] for key in ("price", "lowPrice", "highPrice", "priceCurrency") if key in offer}, "evidence": [source]})
        result["primary_products"].append(product)
    result["primary_products"] = _merge_representations(result["primary_products"])
    for product in result["primary_products"]:
        prices = {_price_key(offer) for offer in product["offers"] if any(offer[key] is not None for key in ("price", "low_price", "high_price"))}
        product["price_status"] = "available" if len(prices) == 1 else "ambiguous" if prices else "unavailable"
    count = len(result["primary_products"])
    if count == 1:
        result.update(status="ambiguous" if result["primary_products"][0]["price_status"] == "ambiguous" else "ok", reason="multiple_primary_offers" if result["primary_products"][0]["price_status"] == "ambiguous" else "linked_primary_product")
    elif count > 1:
        result.update(status="ambiguous", reason="multiple_linked_products")
    return result
