"""
Tests for shop_to_meta_catalog.py's product-page parsing.

Runs against fixture HTML (tests/fixtures/), no network. Fixtures cover the
two most common shapes: a page with full JSON-LD "Product" data, and a page
that only has Open Graph tags (JSON-LD absent or incomplete).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from shop_to_meta_catalog import (
    META_CATALOG_FIELDS,
    build_product_info,
    format_price,
    normalize_key,
    product_id_from_url,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_soup(name: str) -> BeautifulSoup:
    html = (FIXTURES_DIR / name).read_text(encoding="utf-8")
    return BeautifulSoup(html, "html.parser")


class TestJsonLdProduct(unittest.TestCase):
    def setUp(self) -> None:
        self.info = build_product_info(load_soup("product_jsonld.html"))

    def test_prefers_jsonld_over_open_graph(self) -> None:
        self.assertEqual(self.info["title"], "Vintage Leather Jacket")
        self.assertEqual(self.info["description"], "A genuine 1980s leather jacket, hand-picked.")
        self.assertEqual(self.info["image_link"], "https://cdn.example.com/jacket-01.jpg")

    def test_brand_extracted_from_nested_object(self) -> None:
        self.assertEqual(self.info["brand"], "Space Is Vintage")

    def test_availability_and_condition_mapped_from_schema_org_urls(self) -> None:
        self.assertEqual(self.info["availability"], "in stock")
        self.assertEqual(self.info["condition"], "used")

    def test_price_formatting(self) -> None:
        self.assertEqual(format_price(self.info["price"], "USD"), "129.90 USD")

    def test_jsonld_only_fields(self) -> None:
        self.assertEqual(self.info["gtin"], "0012345678905")
        self.assertEqual(self.info["color"], "Brown")
        self.assertEqual(self.info["material"], "Leather")
        self.assertEqual(self.info["pattern"], "Solid")


class TestOpenGraphFallback(unittest.TestCase):
    def setUp(self) -> None:
        self.info = build_product_info(load_soup("product_og_only.html"))

    def test_fields_come_from_open_graph(self) -> None:
        self.assertEqual(self.info["title"], "Denim Skirt")
        self.assertEqual(self.info["description"], "A classic 90s denim skirt.")
        self.assertEqual(self.info["image_link"], "https://cdn.example.com/skirt-01.jpg")
        self.assertEqual(format_price(self.info["price"], "EUR"), "45.00 EUR")

    def test_missing_availability_and_condition_use_defaults(self) -> None:
        self.assertEqual(self.info["availability"], "in stock")
        self.assertEqual(self.info["condition"], "new")


class TestHelpers(unittest.TestCase):
    def test_normalize_key_strips_schema_org_prefix(self) -> None:
        self.assertEqual(normalize_key("https://schema.org/InStock"), "instock")
        self.assertEqual(normalize_key("New"), "new")
        self.assertEqual(normalize_key(None), "")

    def test_format_price_handles_thousands_separator(self) -> None:
        self.assertEqual(format_price("1,299.00", "USD"), "1299.00 USD")
        self.assertEqual(format_price(None, "USD"), "")
        self.assertEqual(format_price("no digits here", "USD"), "")

    def test_product_id_from_url_uses_last_path_segment(self) -> None:
        self.assertEqual(
            product_id_from_url("https://app.amazecommerce.com/shop/spaceisvintage/vintage-jacket"),
            "vintage-jacket",
        )

    def test_columns_match_metas_official_template(self) -> None:
        """META_CATALOG_FIELDS must stay in lockstep with Meta's own header,
        so the CSV this script writes uploads cleanly with no schema drift."""
        import csv as csv_module

        template = FIXTURES_DIR.parent.parent / "doc" / "meta_catalog_products_template.csv"
        with open(template, encoding="utf-8") as f:
            rows = list(csv_module.reader(f))
        official_header = rows[1]
        self.assertEqual(META_CATALOG_FIELDS, official_header)


if __name__ == "__main__":
    unittest.main()
