"""deals: read-only hardware price lookups.

Two sources, both official JSON APIs, no page scraping:
  ebay_search          eBay Browse API (live US Buy It Now listings)
  gsa_auctions_search  GSA Auctions API (all active federal surplus lots)

The server knows nothing about particular hardware: the caller passes keywords. All price
arithmetic (shipping, per-TB, medians) is done here so the model reports numbers instead of
deriving them.

Environment (all optional; a source without its key says so instead of failing):
  EBAY_CLIENT_ID, EBAY_CLIENT_SECRET  eBay production keyset (App ID, Cert ID).
  GSA_API_KEY                         api.data.gov key. Default: the shared DEMO_KEY,
                                      which allows only a few dozen calls a day.
"""

import base64
import html
import os
import re
import statistics
import time
import urllib.parse

from mcp.server.mcpserver import MCPServer

from .httpjson import FetchError, get_json

EBAY_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
EBAY_SCOPE = "https://api.ebay.com/oauth/api_scope"
GSA_URL = "https://api.gsa.gov/assets/gsaauctions/v2/auctions"
GSA_TTL = 3600  # the full active list is about 3 MB; fetch it at most hourly

# eBay condition ids (Browse API "conditionIds" filter). 7000, "for parts", is never included.
CONDITIONS = {
    "any": "1000|1500|1750|2000|2010|2020|2030|2500|2750|3000|4000|5000|6000",
    "new": "1000|1500|1750",
    "refurbished": "2000|2010|2020|2030|2500",
    "used": "2750|3000|4000|5000|6000",
}
CAPACITY = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d)?)\s?TB\b", re.I)
# Titles that sell several units at once: the listed price is not the per-unit price.
LOT = re.compile(r"\blot\s+of\b|\blots?\b|\bqty\b|\b\d+\s?(?:x|pcs?|pack)\b|\bx\s?\d+\b|\bbulk\b", re.I)
TAGS = re.compile(r"<[^>]+>")

server = MCPServer("deals")
_token = {"value": "", "expires": 0.0}
_gsa = {"lots": [], "fetched": 0.0}


# ---------------------------------------------------------------- eBay


def _ebay_keys():
    return os.environ.get("EBAY_CLIENT_ID", ""), os.environ.get("EBAY_CLIENT_SECRET", "")


def _ebay_token():
    """An application token (client-credentials grant), reused until a minute before it expires."""
    if _token["value"] and time.time() < _token["expires"] - 60:
        return _token["value"]
    basic = base64.b64encode(":".join(_ebay_keys()).encode()).decode()
    body = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": EBAY_SCOPE}).encode()
    got = get_json(
        EBAY_TOKEN_URL,
        data=body,
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
    )
    _token.update(value=got["access_token"], expires=time.time() + float(got.get("expires_in", 7200)))
    return _token["value"]


def ebay_search_params(query, condition="any", min_price=0, max_price=0, limit=25):
    """Query parameters for item_summary/search: US Buy It Now, cheapest first."""
    filters = [
        "buyingOptions:{FIXED_PRICE}",
        "itemLocationCountry:US",
        "deliveryCountry:US",
        f"conditionIds:{{{CONDITIONS[condition]}}}",
    ]
    if min_price or max_price:
        lo = f"{min_price:g}" if min_price else ""
        hi = f"{max_price:g}" if max_price else ""
        filters += [f"price:[{lo}..{hi}]", "priceCurrency:USD"]
    return {"q": query, "filter": ",".join(filters), "sort": "price", "limit": max(1, min(int(limit), 50))}


def _money(obj):
    try:
        return float(obj["value"])
    except (KeyError, TypeError, ValueError):
        return None


def shape_ebay_item(it):
    """One Browse API item summary -> the flat record the tool returns."""
    title = it.get("title", "")
    price = _money(it.get("price"))
    ship = None
    for opt in it.get("shippingOptions") or []:
        cost = _money(opt.get("shippingCost"))
        if cost is not None and (ship is None or cost < ship):
            ship = cost
    total = None if price is None else round(price + (ship or 0.0), 2)
    cap = CAPACITY.search(title)
    tb = float(cap.group(1)) if cap else None
    lot = bool(LOT.search(title))
    seller = it.get("seller") or {}
    loc = it.get("itemLocation") or {}
    return {
        "title": title,
        "price": price,
        "shipping": ship,  # None = not stated (calculated at checkout, or pickup only); total then excludes it
        "total": total,
        "currency": (it.get("price") or {}).get("currency"),
        "capacity_tb": tb,
        "usd_per_tb": round(total / tb, 2) if total and tb and not lot else None,
        "multi_unit_listing": lot,
        "condition": it.get("condition"),
        "seller": seller.get("username"),
        "seller_feedback_pct": seller.get("feedbackPercentage"),
        "seller_feedback_count": seller.get("feedbackScore"),
        "location": ", ".join(x for x in (loc.get("city"), loc.get("stateOrProvince"), loc.get("country")) if x),
        "url": (it.get("itemWebUrl") or "").split("?")[0],
    }


def summarize_ebay(items):
    """Lowest total and the median of the five cheapest, over single-unit listings only."""
    singles = sorted(i["total"] for i in items if i["total"] is not None and not i["multi_unit_listing"])
    return {
        "lowest_total": singles[0] if singles else None,
        "median_of_cheapest_5": round(statistics.median(singles[:5]), 2) if singles else None,
        "single_unit_listings": len(singles),
    }


@server.tool()
def ebay_search(
    query: str,
    condition: str = "any",
    min_price: float = 0,
    max_price: float = 0,
    limit: int = 25,
) -> dict:
    """Search live eBay US listings, cheapest first. Buy It Now only, items located in the US,
    "for parts / not working" excluded. These are ASKING prices of active listings, not sold prices.

    query: eBay keywords. Be specific (model number, capacity, interface), e.g. "WUH721414AL5204",
      "12TB SAS 7200 enterprise", "Supermicro X12SPi-TF", "32GB DDR4 2666 RDIMM". Prefix a word
      with "-" to exclude it.
    condition: "any", "new", "refurbished" or "used".
    min_price / max_price: USD item price bounds, 0 = no bound. Set min_price to cut out
      accessories (caddies, cables) that match the same keywords.
    limit: listings to return, 1-50.

    Each item has price, shipping and total in USD. usd_per_tb is set when the title states a
    capacity and the listing is a single unit; multi_unit_listing marks lots and bundles, whose
    price is not per unit. "summary" gives the lowest total and the median of the five cheapest
    single-unit totals: use that median as the comparable weekly figure. Always check that a
    title is really the item you searched for before quoting it.
    """
    if not all(_ebay_keys()):
        return {"error": "eBay is not configured on this server (no API keys). Say eBay was not checked."}
    if condition not in CONDITIONS:
        return {"error": f"condition must be one of {sorted(CONDITIONS)}"}
    params = urllib.parse.urlencode(ebay_search_params(query, condition, min_price, max_price, limit))
    try:
        got = get_json(
            f"{EBAY_SEARCH_URL}?{params}",
            headers={"Authorization": f"Bearer {_ebay_token()}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
        )
    except FetchError as e:
        return {"error": str(e)}
    items = [shape_ebay_item(it) for it in got.get("itemSummaries") or []]
    return {
        "query": query,
        "matches_on_ebay": got.get("total", 0),
        "returned": len(items),
        "summary": summarize_ebay(items),
        "items": items,
    }


# ---------------------------------------------------------------- GSA Auctions


def _gsa_lots():
    if _gsa["lots"] and time.time() - _gsa["fetched"] < GSA_TTL:
        return _gsa["lots"]
    key = os.environ.get("GSA_API_KEY") or "DEMO_KEY"
    got = get_json(f"{GSA_URL}?{urllib.parse.urlencode({'api_key': key, 'format': 'JSON'})}", timeout=90)
    _gsa.update(lots=got.get("Results") or [], fetched=time.time())
    return _gsa["lots"]


def _clean(text):
    return re.sub(r"\s+", " ", html.unescape(TAGS.sub(" ", text or ""))).strip()


def match_gsa_lots(lots, keywords, state="", search_description=True, limit=25):
    """Lots whose name (or description) holds any keyword as a whole word or phrase.

    Returns (the first `limit` matches, the number of matches).
    """
    pats = [(k.strip(), re.compile(rf"(?<!\w){re.escape(k.strip())}(?!\w)", re.I)) for k in keywords if k.strip()]
    state = state.strip().upper()
    out = []
    for lot in lots:
        if state and (lot.get("propertyState") or "").strip().upper() != state:
            continue
        name = _clean(lot.get("itemName"))
        desc = _clean(lot.get("lotInfo"))
        in_name = [k for k, p in pats if p.search(name)]
        in_desc = [k for k, p in pats if p.search(desc)] if search_description else []
        if not (in_name or in_desc):
            continue
        out.append(
            {
                "name": name,
                "matched_in_name": in_name,
                "matched_in_description_only": [k for k in in_desc if k not in in_name],
                "closes": lot.get("aucEndDt"),
                "pickup": ", ".join(
                    x.strip() for x in (lot.get("propertyCity"), lot.get("propertyState")) if x and x.strip()
                ),
                "high_bid": lot.get("highBidAmount"),
                "bidders": lot.get("biddersCount"),
                "agency": lot.get("agencyName"),
                "sale_lot": f"{lot.get('saleNo')} / {lot.get('lotNo')}",
                "url": lot.get("itemDescURL"),
                "description": desc[:400],
            }
        )
    # Name matches first (a keyword in the description alone is often a passing mention), then soonest close.
    out.sort(key=lambda r: (not r["matched_in_name"], r["closes"] or ""))
    return out[:limit], len(out)


@server.tool()
def gsa_auctions_search(keywords: list[str], state: str = "", search_description: bool = True, limit: int = 25) -> dict:
    """Search every active lot on GSA Auctions (US federal surplus) for any of the keywords.

    keywords: words or phrases, matched whole and case-insensitively, e.g. ["Supermicro",
      "PowerEdge", "DDR4", "SAS", "100G", "Arista", "server rack"]. Prefer makes and model
      families over generic words: "server" and "switch" match a lot of unrelated equipment.
    state: two-letter state of the pickup location, "" = anywhere.
    search_description: also match the lot description, not only its name.
    limit: lots to return, 1-50.

    Each lot has its name, closing date, pickup city and state, current high bid, and a link.
    A lot matched only in its description is listed after name matches; read the description
    before reporting it. An empty result is a real "no matching lots today": the whole
    active list was checked.
    """
    if not [k for k in keywords if k.strip()]:
        return {"error": "give at least one keyword"}
    try:
        lots = _gsa_lots()
    except FetchError as e:
        return {"error": str(e)}
    found, total = match_gsa_lots(lots, keywords, state, search_description, max(1, min(int(limit), 50)))
    return {"active_lots_checked": len(lots), "matching": total, "returned": len(found), "lots": found}
