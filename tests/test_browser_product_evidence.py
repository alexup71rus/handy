"""Price binding stays on the observed product when the canonical URL is shared."""

import unittest

from folder_memory.browser_evidence import product_evidence


CANONICAL = "https://shop.example/phone"
VARIANT = CANONICAL + "?color=green"


def product(name, price, **properties):
    return {"@type": "Product", "name": name, "offers": {"@type": "Offer", "price": price, "priceCurrency": "RUB"},
            **properties}


def record(path, data):
    return {"kind": "json_ld", "path": path, "data": data}


def evidence(url, nodes, *, headings=()):
    return product_evidence({"canonical_url": CANONICAL, "h1": list(headings), "nodes": nodes}, url)


class BrowserProductEvidenceTests(unittest.TestCase):
    def test_observed_variant_wins_over_canonical_product(self):
        base = product("Телефон 128 ГБ", "29990", url=CANONICAL, sku="base")
        variant = product("Телефон 256 ГБ", "34990", url=VARIANT, sku="green", description="Зелёный корпус")
        result = evidence(VARIANT, [record("base", base), record("variant", variant)], headings=[base["name"]])

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["canonical_url"], CANONICAL)
        self.assertEqual(result["other_product_count"], 1)
        observed, = result["primary_products"]
        self.assertEqual((observed["url"], observed["sku"], observed["description"]), (VARIANT, "green", "Зелёный корпус"))
        offer, = observed["offers"]
        self.assertEqual(offer["price"], "34990")
        self.assertEqual(offer["evidence"][0]["source_url"], VARIANT)

    def test_tracking_url_without_specific_product_uses_canonical_binding(self):
        url = CANONICAL + "?campaign=example"
        result = evidence(url, [record("base", product("Телефон 128 ГБ", "29990", url=CANONICAL))])
        observed, = result["primary_products"]
        self.assertEqual((observed["url"], observed["offers"][0]["price"]), (CANONICAL, "29990"))
        self.assertEqual(observed["evidence"][0]["source_url"], url)

    def test_hash_routes_do_not_share_a_price_with_other_routes_or_the_base_page(self):
        base = record("base", product("Общая карточка", "50", url=CANONICAL))
        other = record("red", product("Красный телефон", "100", url=CANONICAL + "#/red"))
        for route in ("#/green", "#!/green", "#%2Fgreen"):
            with self.subTest(route=route):
                url = CANONICAL + route
                green = record("green", product("Зелёный телефон", "200", url=url, sku="green"))
                result = evidence(url, [base, other, green])
                observed, = result["primary_products"]
                self.assertEqual((observed["url"], observed["offers"][0]["price"]), (url, "200"))
                self.assertEqual(evidence(url, [base, other])["status"], "unavailable")

    def test_document_anchor_keeps_normal_product_binding(self):
        base = record("base", product("Телефон", "100", url=CANONICAL))
        result = evidence(CANONICAL + "#specifications", [base])
        self.assertEqual(result["primary_products"][0]["offers"][0]["price"], "100")

    def test_exact_page_main_entity_preserves_relative_offer_identity(self):
        variant = {"@type": "Product", "@id": "#variant", "name": "Телефон 256 ГБ", "offers": {"@id": "#price"}}
        result = evidence(VARIANT, [
            record("page", {"@type": "WebPage", "@id": "#page", "mainEntity": {"@id": "#variant"}}),
            record("base", product("Телефон 128 ГБ", "29990", url=CANONICAL)),
            record("variant", variant),
            record("base_price", {"@type": "Offer", "@id": CANONICAL + "#price", "price": "29990", "priceCurrency": "RUB"}),
            record("variant_price", {"@type": "Offer", "@id": "#price", "price": "34990", "priceCurrency": "RUB"}),
        ])
        self.assertFalse(result["partial"])
        observed, = result["primary_products"]
        offer, = observed["offers"]
        self.assertEqual((observed["url"], offer["price"], offer["evidence"][0]["path"]), (VARIANT, "34990", "variant_price"))

    def test_two_explicit_variants_still_require_a_choice(self):
        result = evidence(VARIANT, [record("base", product("Общая карточка", "300", url=CANONICAL)),
                                    record("one", product("Телефон 128 ГБ", "100", url=VARIANT, sku="one")),
                                    record("two", product("Телефон 256 ГБ", "200", url=VARIANT, sku="two"))])
        self.assertEqual(result["reason"], "multiple_linked_products")
        self.assertEqual({item["sku"] for item in result["primary_products"]}, {"one", "two"})

    def test_nested_recommendation_is_not_webpage_main_entity(self):
        result = evidence(CANONICAL, [
            record("page", {"@type": "WebPage", "url": CANONICAL}),
            record("page/mainEntity", product("Телефон", "100")),
            record("page/mainEntity/isRelatedTo/0", product("Чехол", "20")),
        ])
        observed, = result["primary_products"]
        self.assertEqual(observed["name"], "Телефон")
        self.assertEqual(result["other_product_count"], 1)

    def test_linked_offer_uses_product_url_if_id_is_absent(self):
        result = evidence(VARIANT, [
            record("product", {"@type": "Product", "url": VARIANT, "name": "Телефон"}),
            record("other", {"@type": "Offer", "itemOffered": CANONICAL, "price": "50", "priceCurrency": "RUB"}),
            record("matching", {"@type": "Offer", "itemOffered": {"url": VARIANT}, "price": "100", "priceCurrency": "RUB"}),
        ])
        observed, = result["primary_products"]
        offer, = observed["offers"]
        self.assertEqual((offer["price"], offer["evidence"][0]["path"]), ("100", "matching"))
