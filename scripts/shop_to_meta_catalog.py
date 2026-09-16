#!/usr/bin/env python3
"""
shop_to_meta_catalog.py
------------------------
Turns your online shop's product listing page into a CSV catalog ready to
upload in Meta Commerce Manager (Facebook/Instagram product catalog).

Given the shop's listing page URL (e.g. an Amaze Commerce shop front), it:

  1. finds every individual product page linked from the listing;
  2. for each product, extracts title/description/price/image/availability/
     condition/brand, trying in order:
       a) JSON-LD structured data (schema.org "Product") -- most reliable,
          nearly every shop platform embeds it for SEO and social previews;
       b) Open Graph meta tags (og:title, og:description, og:image, ...) --
          used for link previews on Facebook/WhatsApp;
       c) a blind fallback on the page's visible text, as a last resort;
  3. writes the result as a CSV with the exact columns Meta requires.

A single product failing to parse is logged and skipped -- it never aborts
the rest of the export.

Usage
-----
    pip install -r requirements.txt
    python shop_to_meta_catalog.py https://app.amazecommerce.com/shop/spaceisvintage

    python shop_to_meta_catalog.py <shop_url> --out catalog.csv --brand "Space Is Vintage"

Note
----
This hasn't been run against the real shop yet (no network access while
writing it), so the parsing relies on patterns common to many e-commerce
platforms. If a field comes out empty or wrong, share the HTML (or just the
`<script type="application/ld+json">` block) of one product page and the
parsing gets fixed accordingly.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

try:
    import requests
except ImportError:
    raise SystemExit(
        "[!] Missing requests: activate the venv and run `pip install -r requirements.txt`."
    )

try:
    from bs4 import BeautifulSoup
except ImportError:
    raise SystemExit(
        "[!] Missing beautifulsoup4: activate the venv and run `pip install -r requirements.txt`."
    )


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

DEFAULT_CONDITION = "new"
DEFAULT_AVAILABILITY = "in stock"
DEFAULT_CURRENCY = "USD"
REQUEST_DELAY = 0.5   # seconds between product page requests, polite to the server

# schema.org availability/condition values (bare or as full "schema.org/X"
# URLs, normalized to lowercase-alnum by normalize_key) mapped to the
# values Meta's catalog spec expects.
AVAILABILITY_MAP = {
    "instock": "in stock",
    "outofstock": "out of stock",
    "soldout": "out of stock",
    "discontinued": "out of stock",
    "unavailable": "out of stock",
    "preorder": "preorder",
    "presale": "preorder",
    "backorder": "available for order",
    "limitedavailability": "in stock",
    "available": "in stock",
}

CONDITION_MAP = {
    "newcondition": "new",
    "usedcondition": "used",
    "refurbishedcondition": "refurbished",
    "new": "new",
    "used": "used",
    "refurbished": "refurbished",
}

META_CATALOG_FIELDS = [
    "id", "title", "description", "availability", "condition",
    "price", "link", "image_link", "brand",
]


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


# --------------------------------------------------------------------------
# 1. Fetching pages
# --------------------------------------------------------------------------

def fetch_soup(url: str) -> BeautifulSoup:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


# --------------------------------------------------------------------------
# 2. Finding product links on the listing page
# --------------------------------------------------------------------------

def find_product_links(shop_url: str, soup: BeautifulSoup) -> list[str]:
    """
    Collects links that look like individual product pages: one path
    segment below the shop's own path (e.g. /shop/<store>/<product-slug>),
    excluding category pages (/c/...) and the cart.
    """
    parsed = urlparse(shop_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    shop_path = parsed.path.rstrip("/")

    links: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        absolute = urljoin(base, href)
        path = urlparse(absolute).path.rstrip("/")
        if not path.startswith(shop_path + "/"):
            continue
        rest = path[len(shop_path) + 1:]
        if not rest or "/" in rest or rest.startswith("c/") or rest == "cart":
            continue
        links.add(absolute)
    return sorted(links)


# --------------------------------------------------------------------------
# 3. Parsing a single product page
#    JSON-LD -> Open Graph -> visible-text fallback, in that priority order
# --------------------------------------------------------------------------

def normalize_key(value: Any) -> str:
    """"https://schema.org/InStock" -> "instock"; "New" -> "new"."""
    if not value:
        return ""
    return re.sub(r"[^a-z]", "", str(value).split("/")[-1].lower())


def extract_jsonld_product(soup: BeautifulSoup) -> dict[str, Any] | None:
    """Looks for a schema.org "Product" block among the page's JSON-LD tags."""
    for tag in soup.find_all("script", type="application/ld+json"):
        if not tag.string:
            continue
        try:
            data = json.loads(tag.string)
        except json.JSONDecodeError:
            continue

        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            if item.get("@type") == "Product":
                return item
            for g in item.get("@graph", []) or []:
                if isinstance(g, dict) and g.get("@type") == "Product":
                    return g
    return None


def parse_from_jsonld(product: dict[str, Any]) -> dict[str, Any]:
    offers = product.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    offers = offers or {}

    image = product.get("image")
    if isinstance(image, list):
        image = image[0] if image else None
    if isinstance(image, dict):
        image = image.get("url")

    brand = product.get("brand")
    if isinstance(brand, dict):
        brand = brand.get("name")

    return {
        "title": product.get("name"),
        "description": product.get("description"),
        "price": offers.get("price") or offers.get("lowPrice"),
        "image_link": image,
        "availability": AVAILABILITY_MAP.get(normalize_key(offers.get("availability"))),
        "condition": CONDITION_MAP.get(normalize_key(product.get("itemCondition"))),
        "brand": brand,
    }


def parse_from_open_graph(soup: BeautifulSoup) -> dict[str, Any]:
    def meta(name: str, prop: bool = False) -> str | None:
        attr = "property" if prop else "name"
        tag = soup.find("meta", attrs={attr: name})
        return tag["content"].strip() if tag and tag.get("content") else None

    title = meta("og:title", prop=True)
    price = meta("product:price:amount", prop=True) or meta("og:price:amount", prop=True)

    return {
        "title": title,
        "description": meta("og:description", prop=True),
        "price": price,
        "image_link": meta("og:image", prop=True),
        "availability": AVAILABILITY_MAP.get(normalize_key(meta("product:availability", prop=True))),
        "condition": CONDITION_MAP.get(normalize_key(meta("product:condition", prop=True))),
        "brand": meta("product:brand", prop=True) or meta("og:brand", prop=True),
    }


PRICE_TEXT_RE = re.compile(r"[$€£]\s?(\d[\d.,]*\d|\d)")


def parse_from_visible_text(soup: BeautifulSoup) -> dict[str, Any]:
    """Last resort: title from <title>/<h1>, price from a currency-looking
    number in the page text. No structured availability/condition/brand
    to guess here -- those fall back to the module defaults."""
    title = None
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    if not title:
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(strip=True)

    price = None
    match = PRICE_TEXT_RE.search(soup.get_text())
    if match:
        price = match.group(1)

    return {
        "title": title,
        "description": None,
        "price": price,
        "image_link": None,
        "availability": None,
        "condition": None,
        "brand": None,
    }


def build_product_info(soup: BeautifulSoup) -> dict[str, Any]:
    """
    Merges the three sources field by field, in priority order (JSON-LD,
    then Open Graph, then the visible-text fallback), and applies the
    module defaults for availability/condition if no source had them.
    Pure function of a parsed page: no network, so it's what the tests
    exercise directly against fixture HTML.
    """
    product = extract_jsonld_product(soup)
    sources = [
        parse_from_jsonld(product) if product else {},
        parse_from_open_graph(soup),
        parse_from_visible_text(soup),
    ]

    info: dict[str, Any] = {}
    for field in ("title", "description", "price", "image_link", "availability", "condition", "brand"):
        for source in sources:
            if source.get(field):
                info[field] = source[field]
                break
        else:
            info[field] = None

    info["availability"] = info["availability"] or DEFAULT_AVAILABILITY
    info["condition"] = info["condition"] or DEFAULT_CONDITION
    return info


def parse_product_page(url: str) -> dict[str, Any]:
    return build_product_info(fetch_soup(url))


# --------------------------------------------------------------------------
# 4. Formatting for Meta's catalog spec
# --------------------------------------------------------------------------

PRICE_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")


def format_price(raw_price: Any, currency: str) -> str:
    """price column must look like "12.99 USD"."""
    if raw_price is None:
        return ""
    match = PRICE_NUMBER_RE.search(str(raw_price).replace(",", ""))
    if not match:
        return ""
    return f"{float(match.group()):.2f} {currency}"


def product_id_from_url(url: str) -> str:
    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    if slug:
        return slug
    return hashlib.md5(url.encode()).hexdigest()[:12]


# --------------------------------------------------------------------------
# 5. Building and writing the catalog
# --------------------------------------------------------------------------

def build_catalog(shop_url: str, currency: str, brand_override: str | None) -> list[dict[str, str]]:
    shop_url = shop_url.rstrip("/")
    default_brand = brand_override or shop_url.rstrip("/").split("/")[-1]

    log(f"[i] Fetching listing page: {shop_url}")
    listing_soup = fetch_soup(shop_url)

    product_urls = find_product_links(shop_url, listing_soup)
    log(f"[i] Found {len(product_urls)} product link(s).")

    rows: list[dict[str, str]] = []
    for i, url in enumerate(product_urls, 1):
        log(f"[i] [{i}/{len(product_urls)}] {url}")
        try:
            info = parse_product_page(url)
        except Exception as exc:  # noqa: BLE001 -- one bad product must not kill the run
            log(f"[!] Failed on {url}, skipping: {exc}")
            continue

        product_id = product_id_from_url(url)
        rows.append({
            "id": product_id,
            "title": info.get("title") or product_id,
            "description": info.get("description") or info.get("title") or product_id,
            "availability": info["availability"],
            "condition": info["condition"],
            "price": format_price(info.get("price"), currency),
            "link": url,
            "image_link": info.get("image_link") or "",
            "brand": info.get("brand") or default_brand,
        })
        time.sleep(REQUEST_DELAY)

    incomplete = [r["id"] for r in rows if not r["price"] or not r["image_link"]]
    if incomplete:
        log(f"[!] Missing price or image for: {', '.join(incomplete)}")

    return rows


def write_catalog_csv(rows: list[dict[str, str]], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=META_CATALOG_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Builds a Meta Commerce Manager catalog CSV from a shop's product listing page.",
    )
    ap.add_argument("shop_url", help="URL of the shop's product listing page")
    ap.add_argument("--out", type=Path, default=Path("meta_catalog.csv"),
                    help="output CSV path (default: ./meta_catalog.csv)")
    ap.add_argument("--brand", default=None,
                    help="brand value used when a product page doesn't declare one "
                         "(default: last path segment of shop_url)")
    ap.add_argument("--currency", default=DEFAULT_CURRENCY,
                    help=f"ISO currency code appended to each price (default: {DEFAULT_CURRENCY})")
    args = ap.parse_args()

    rows = build_catalog(args.shop_url, args.currency, args.brand)
    if not rows:
        raise SystemExit("[!] No product parsed successfully: nothing written.")

    write_catalog_csv(rows, args.out)
    log(f"\n[OK] {len(rows)} product(s) written to {args.out}")


if __name__ == "__main__":
    main()
