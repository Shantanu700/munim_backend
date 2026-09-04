"""Catalog ingestion.

Tiered on purpose (handoff: adapter tiers):

  feed   -- a Google/Meta product feed, when the merchant already publishes one
  jsonld -- sitemap.xml -> schema.org/Product JSON-LD on each page

Only the jsonld tier is implemented. Every ecommerce stack emits that markup
because of a decade of Google Shopping SEO, which is the whole reason this
generalises without a per-merchant integration. There is deliberately NO
HTML-scraping tier: a general-purpose crawler is the failure mode this project
cannot afford, so a page without structured data is simply skipped.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126 Safari/537.36")

DELAY_S = 0.25
TIMEOUT_S = 20

SITEMAP_NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}

SOURCE_WEIGHT = {"feed": 1.0, "jsonld": 0.9}

SCORED_FIELDS = ("title", "price", "currency", "sku", "image_url", "in_stock")


@dataclass
class Extracted:
    url: str
    title: str
    price: Decimal | None
    currency: str
    sku: str = ""
    image_url: str = ""
    in_stock: bool | None = None
    source: str = "jsonld"
    raw: dict = field(default_factory=dict)

    @property
    def price_paise(self) -> int | None:
        return None if self.price is None else int(self.price * 100)

    @property
    def confidence(self) -> float:
        present = sum(1 for f in SCORED_FIELDS if getattr(self, f) not in (None, "", []))
        weight = SOURCE_WEIGHT.get(self.source, 0.5)
        return round(weight * present / len(SCORED_FIELDS), 2)


class _LdExtractor(HTMLParser):
    """Collects the bodies of <script type="application/ld+json"> elements."""

    def __init__(self):
        super().__init__()
        self.blocks: list[str] = []
        self._capture = False
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            a = dict(attrs)
            if (a.get("type") or "").strip().lower() == "application/ld+json":
                self._capture = True
                self._buf = []

    def handle_endtag(self, tag):
        if tag == "script" and self._capture:
            self.blocks.append("".join(self._buf))
            self._capture = False

    def handle_data(self, data):
        if self._capture:
            self._buf.append(data)


def fetch(url: str, timeout: int = TIMEOUT_S) -> str | None:
    time.sleep(DELAY_S)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        log.warning("fetch failed %s: %s", url, exc)
        return None


def robots(base: str) -> tuple[RobotFileParser | None, list[str]]:
    """Returns the parsed rules and any Sitemap: URLs the file advertises."""
    txt = fetch(urljoin(base, "/robots.txt"))
    if txt is None:
        return None, []
    rp = RobotFileParser()
    rp.parse(txt.splitlines())
    sitemaps = [ln.split(":", 1)[1].strip() for ln in txt.splitlines()
                if ln.lower().startswith("sitemap:")]
    return rp, sitemaps


def sitemap_urls(url: str, seen: set[str] | None = None) -> list[str]:
    """Page URLs from a sitemap, following <sitemapindex> one level deep."""
    seen = seen if seen is not None else set()
    if url in seen:
        return []
    seen.add(url)

    body = fetch(url)
    if not body:
        return []
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        log.warning("sitemap parse failed %s: %s", url, exc)
        return []

    if root.tag.endswith("sitemapindex"):
        out: list[str] = []
        for loc in root.iterfind(".//s:sitemap/s:loc", SITEMAP_NS):
            out.extend(sitemap_urls((loc.text or "").strip(), seen))
        return out

    return [(loc.text or "").strip() for loc in root.iterfind(".//s:url/s:loc", SITEMAP_NS)
            if (loc.text or "").strip()]


def jsonld_nodes(html: str) -> list[dict]:
    """Every JSON-LD object on the page, with @graph flattened."""
    p = _LdExtractor()
    try:
        p.feed(html)
    except Exception as exc:
        log.debug("html parse issue: %s", exc)

    nodes: list[dict] = []
    for block in p.blocks:
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        for item in (data if isinstance(data, list) else [data]):
            if not isinstance(item, dict):
                continue
            nodes.append(item)
            nodes.extend(g for g in item.get("@graph", []) if isinstance(g, dict))
    return nodes


def _is_product(node: dict) -> bool:
    t = node.get("@type", "")
    types = t if isinstance(t, list) else [t]
    return any(str(x).split("/")[-1] == "Product" for x in types)


def _money(value) -> Decimal | None:
    if value is None:
        return None
    try:
        cleaned = "".join(c for c in str(value) if c.isdigit() or c == ".")
        return Decimal(cleaned) if cleaned else None
    except (InvalidOperation, ValueError):
        return None


def _first(value):
    if isinstance(value, list):
        return value[0] if value else None
    return value


def product_from_html(html: str, url: str) -> Extracted | None:
    for node in jsonld_nodes(html):
        if not _is_product(node):
            continue

        offer = _first(node.get("offers")) or {}
        if not isinstance(offer, dict):
            offer = {}

        price = _money(offer.get("price") or offer.get("lowPrice"))
        title = node.get("name") or ""
        if not title or price is None:
            continue

        availability = str(offer.get("availability") or "")
        in_stock = None
        if availability:
            in_stock = availability.split("/")[-1].lower() in {"instock", "onlineonly", "limitedavailability"}

        image = _first(node.get("image")) or ""
        if isinstance(image, dict):
            image = image.get("url", "")
        image = str(image or "").strip()
        if image:
            image = urljoin(url, image)

        return Extracted(
            url=node.get("url") or url,
            title=str(title).strip(),
            price=price,
            currency=(offer.get("priceCurrency") or "INR").upper(),
            sku=str(node.get("sku") or node.get("mpn") or "").strip(),
            image_url=image,
            in_stock=in_stock,
            source="jsonld",
            raw=node,
        )
    return None


def rebase(url: str, base: str) -> str:
    """Point `url`'s path at `base`'s scheme and host.

    A store is routinely reachable at an address that is not its canonical one
    -- a staging copy, an internal DNS name, a tunnel, a container hostname --
    while its robots.txt and sitemap still advertise production URLs. Dropping
    those as off-host yields an empty crawl; following them verbatim walks off
    to a different server entirely. Rebasing keeps the paths and pins every
    request to the host the caller actually asked for.
    """
    b, u = urlparse(base), urlparse(url)
    return urlunparse((b.scheme, b.netloc, u.path or "/", "", u.query, ""))


def normalise_source(raw: str) -> str:
    """Validate `--source` and return a bare origin.

    A path here is a silent no-op, not a filter: discovery joins sitemap.xml
    onto the origin and rebase() keeps only scheme and host, so
    "https://shop.example/product" quietly ingests the whole site. Refusing it
    is kinder than the empty result that follows.
    """
    value = (raw or "").strip()
    if not value:
        raise ValueError("--source is empty.")

    parsed = urlparse(value)
    if not parsed.scheme:
        raise ValueError(f"--source needs a scheme, e.g. https://{value.lstrip('/')}")
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"--source must be http or https, got {parsed.scheme!r}.")
    if not parsed.netloc:
        raise ValueError(f"--source has no host: {value!r}")

    origin = f"{parsed.scheme}://{parsed.netloc}"
    if parsed.path.strip("/") or parsed.query or parsed.fragment:
        raise ValueError(
            f"--source must be a store origin, not a path. Got {value!r}. "
            f"A path is ignored rather than used as a filter, so this would have "
            f"crawled the whole site. Use --source {origin}"
            f"{f' --match {parsed.path.strip(chr(47))}' if parsed.path.strip('/') else ''}"
        )
    return origin


def discover(base: str, limit: int = 0, match: str = "") -> tuple[list[str], RobotFileParser | None]:
    """Candidate page URLs for `base`, honouring robots.txt.

    `match` keeps only URLs whose path contains that substring -- the way to
    scope a run to /product on a large catalog.
    """
    base = base.rstrip("/")
    rp, advertised = robots(base)

    candidates = [rebase(sm, base) for sm in advertised] or [urljoin(base + "/", "sitemap.xml")]
    urls: list[str] = []
    for sm in dict.fromkeys(candidates):
        urls.extend(sitemap_urls(sm))

    seen: set[str] = set()
    kept: list[str] = []
    for raw_url in urls:
        u = rebase(raw_url, base)
        if u in seen:
            continue
        if match and match not in urlparse(u).path:
            continue
        if rp and not rp.can_fetch(UA, u):
            log.info("robots.txt disallows %s", u)
            continue
        seen.add(u)
        kept.append(u)
        if limit and len(kept) >= limit:
            break
    return kept, rp
