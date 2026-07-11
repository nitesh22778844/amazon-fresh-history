"""
Sync scraped purchase history to Salesforce Grocery_Product__c and Grocery_Order__c records.

For each unique product title in the scrape result we PATCH (upsert) by the
external ID `title__c`:
    PATCH /services/data/vXX.X/sobjects/Grocery_Product__c/title__c/<title>

Salesforce returns 201 when a new record is created and 204 when an existing
record is updated. `title__c` must be configured as an External ID
(Unique, Case-Insensitive) on the object.

Salesforce auth uses the OAuth 2.0 client_credentials flow against the Connected
App identified by SF_CLIENT_ID / SF_CLIENT_SECRET.

Required env vars:
    SF_TOKEN_URL       e.g. https://<domain>.my.salesforce.com/services/oauth2/token
    SF_CLIENT_ID
    SF_CLIENT_SECRET
    SF_API_ENDPOINT    e.g. https://<domain>.my.salesforce.com/services/data/v57.0/sobjects/Grocery_Product__c/

If any of those are missing, sync_products() returns a "skipped" stats dict
without raising — the scraper still completes successfully.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Iterable
from urllib.parse import quote, quote_plus, urlparse

import requests

# Force UTF-8 stdout/stderr so unicode characters print on Windows consoles.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# External ID field — used in the upsert URL, NOT in the request body.
TITLE_FIELD = "title__c"

# Fields that get written on every upsert (when a value is available).
COUNT_FIELD = "number_of_times_purchased__c"
DATE_FIELD = "last_ordered_date__c"
PRICE_FIELD = "current_price__c"
LAST_PURCHASED_PRICE_FIELD = "last_purchased_price__c"
URL_FIELD = "product_url__c"
IMAGE_FIELD = "image_url__c"
CATEGORY_FIELD = "category__c"
AVAILABILITY_FIELD = "availability__c"
SOURCE_FIELD = "source__c"
SCRAPED_AT_FIELD = "scraped_at__c"
WEIGHT_FIELD = "weight__c"
RATING_FIELD = "rating__c"

# Grocery_Order__c custom object for order history tracking
ORDER_OBJECT = "Grocery_Order__c"
ORDER_EXTERNAL_ID_FIELD = "External_Order_ID__c"
ORDER_DATE_FIELD = "Order_Date__c"
ORDER_PRICE_FIELD = "Ordered_Price__c"
ORDER_WEIGHT_FIELD = "weight__c"
ORDER_PRODUCT_RELATIONSHIP = "Grocery_Product__r"

_REQUIRED_ENV = ("SF_TOKEN_URL", "SF_CLIENT_ID", "SF_CLIENT_SECRET", "SF_API_ENDPOINT")
_TOKEN_CACHE: dict[str, str] = {}


class SalesforceError(RuntimeError):
    pass


def config_present() -> bool:
    return all((os.getenv(k) or "").strip() for k in _REQUIRED_ENV)


def _env(name: str) -> str:
    val = (os.getenv(name) or "").strip()
    if not val:
        raise SalesforceError(f"Environment variable {name} is required for Salesforce sync.")
    return val


def _sobject_base() -> str:
    base = _env("SF_API_ENDPOINT")
    return base if base.endswith("/") else base + "/"


def _instance_root() -> str:
    parsed = urlparse(_sobject_base())
    return f"{parsed.scheme}://{parsed.netloc}"


def _api_version_path() -> str:
    m = re.search(r"/services/data/v\d+\.\d+", _sobject_base())
    if not m:
        raise SalesforceError(
            "SF_API_ENDPOINT must include '/services/data/v<XX.X>/' (e.g. v57.0)."
        )
    return m.group(0)


def get_access_token(force_refresh: bool = False) -> str:
    if not force_refresh and _TOKEN_CACHE.get("access_token"):
        return _TOKEN_CACHE["access_token"]

    resp = requests.post(
        _env("SF_TOKEN_URL"),
        data={
            "grant_type": "client_credentials",
            "client_id": _env("SF_CLIENT_ID"),
            "client_secret": _env("SF_CLIENT_SECRET"),
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise SalesforceError(
            f"Salesforce OAuth failed: {resp.status_code} {resp.text[:300]}"
        )
    token = resp.json().get("access_token")
    if not token:
        raise SalesforceError(f"Salesforce OAuth response missing access_token: {resp.text[:300]}")
    _TOKEN_CACHE["access_token"] = token
    print("[salesforce] Obtained access token via client_credentials grant.")
    return token


def _auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {get_access_token()}",
        "Content-Type": "application/json",
    }


def _request(method: str, url: str, **kwargs) -> requests.Response:
    """Wrap requests so that a 401 triggers one token refresh + retry."""
    resp = requests.request(method, url, headers=_auth_headers(), timeout=30, **kwargs)
    if resp.status_code == 401:
        get_access_token(force_refresh=True)
        resp = requests.request(method, url, headers=_auth_headers(), timeout=30, **kwargs)
    return resp


def _upsert_by_title(title: str, payload: dict) -> str:
    """
    PATCH /sobjects/Grocery_Product__c/title__c/<title>
    Returns "created" (201) or "updated" (204/200).
    """
    encoded = quote(title, safe="")
    url = f"{_instance_root()}{_api_version_path()}/sobjects/Grocery_Product__c/{TITLE_FIELD}/{encoded}"
    resp = _request("PATCH", url, json=payload)
    if resp.status_code == 201:
        return "created"
    if resp.status_code in (200, 204):
        return "updated"
    raise SalesforceError(
        f"Upsert {title[:60]} failed: {resp.status_code} {resp.text[:300]}"
    )


def _upsert_order(item_id: str, payload: dict) -> str:
    """
    PATCH /sobjects/Grocery_Order__c/External_Order_ID__c/<item_id>
    Returns "created" (201) or "updated" (204/200).
    """
    encoded = quote(item_id, safe="")
    url = f"{_instance_root()}{_api_version_path()}/sobjects/{ORDER_OBJECT}/{ORDER_EXTERNAL_ID_FIELD}/{encoded}"
    resp = _request("PATCH", url, json=payload)
    if resp.status_code == 201:
        return "created"
    if resp.status_code in (200, 204):
        return "updated"
    raise SalesforceError(
        f"Upsert order {item_id[:60]} failed: {resp.status_code} {resp.text[:300]}"
    )


def _build_payload(entry: dict) -> dict:
    """Build the upsert body. Omit None values so partial retries do not blank
    existing fields. title__c is excluded — it lives in the URL."""
    body: dict = {}

    def put(field: str, value) -> None:
        if value is None:
            return
        if isinstance(value, str) and not value.strip():
            return
        body[field] = value

    put(COUNT_FIELD, entry.get("number_of_times_purchased"))
    put(DATE_FIELD, entry.get("last_ordered_date"))
    put(PRICE_FIELD, entry.get("current_price"))
    put(LAST_PURCHASED_PRICE_FIELD, entry.get("last_purchased_price"))
    put(URL_FIELD, entry.get("product_url"))
    put(IMAGE_FIELD, entry.get("image_url"))
    put(CATEGORY_FIELD, entry.get("category"))
    put(AVAILABILITY_FIELD, entry.get("availability"))
    put(SOURCE_FIELD, entry.get("source"))
    put(SCRAPED_AT_FIELD, entry.get("scraped_at"))
    put(WEIGHT_FIELD, entry.get("weight"))
    put(RATING_FIELD, entry.get("rating"))
    return body


def _build_order_payload(entry: dict) -> dict:
    """Build the Grocery_Order__c upsert body."""
    body: dict = {}

    def put(field: str, value) -> None:
        if value is None:
            return
        if isinstance(value, str) and not value.strip():
            return
        body[field] = value

    put(ORDER_DATE_FIELD, entry.get("date"))
    put(ORDER_PRICE_FIELD, entry.get("last_purchased_price"))
    put(ORDER_WEIGHT_FIELD, entry.get("weight"))

    if entry.get("title"):
        body[ORDER_PRODUCT_RELATIONSHIP] = {
            TITLE_FIELD: entry["title"]
        }

    return body


def get_order_history(title: str) -> dict:
    """
    Query Salesforce for the order history of a specific product title.
    Returns a dictionary of purchase history (limited to the last 10 entries),
    or an empty template if disabled/error.
    """
    empty_result = {"total_purchases": 0, "history": []}
    if not config_present() or not title:
        return empty_result

    try:
        escaped_title = title.replace("'", "\\'")
        soql = (
            f"SELECT {ORDER_DATE_FIELD}, {ORDER_PRICE_FIELD}, {ORDER_WEIGHT_FIELD} "
            f"FROM {ORDER_OBJECT} "
            f"WHERE {ORDER_PRODUCT_RELATIONSHIP}.{TITLE_FIELD} = '{escaped_title}' "
            f"ORDER BY {ORDER_DATE_FIELD} DESC "
            f"LIMIT 10"
        )
        url = f"{_instance_root()}{_api_version_path()}/query/?q={quote_plus(soql)}"
        resp = _request("GET", url)
        if resp.status_code != 200:
            print(f"[salesforce] Query failed: {resp.status_code} {resp.text[:300]}")
            return empty_result
        
        records = resp.json().get("records", [])
        history = []
        for r in records:
            history.append({
                "date": r.get(ORDER_DATE_FIELD),
                "price": r.get(ORDER_PRICE_FIELD),
                "weight": r.get(ORDER_WEIGHT_FIELD)
            })
        return {
            "total_purchases": len(history),
            "history": history
        }
    except Exception as exc:
        print(f"[salesforce] Error querying history for '{title}': {exc}")
        return empty_result


def get_bulk_order_history(titles: list[str]) -> dict[str, dict]:
    """
    Query Salesforce for the order history of multiple product titles in a single API call.
    Returns a dictionary mapping product title -> {"total_purchases": int, "history": [...]}.
    Capped at the most recent 10 purchase history records per product.
    """
    result_map = {t: {"total_purchases": 0, "history": []} for t in titles}
    if not config_present() or not titles:
        return result_map

    try:
        escaped_titles = []
        for t in titles:
            if t:
                escaped = t.replace("'", "\\'")
                escaped_titles.append(f"'{escaped}'")
        if not escaped_titles:
            return result_map
            
        titles_in_clause = ", ".join(escaped_titles)
        # Limit query fetch to len(titles) * 10 to avoid massive scans
        soql_limit = len(titles) * 10
        soql = (
            f"SELECT {ORDER_PRODUCT_RELATIONSHIP}.{TITLE_FIELD}, {ORDER_DATE_FIELD}, {ORDER_PRICE_FIELD}, {ORDER_WEIGHT_FIELD} "
            f"FROM {ORDER_OBJECT} "
            f"WHERE {ORDER_PRODUCT_RELATIONSHIP}.{TITLE_FIELD} IN ({titles_in_clause}) "
            f"ORDER BY {ORDER_DATE_FIELD} DESC "
            f"LIMIT {soql_limit}"
        )
        url = f"{_instance_root()}{_api_version_path()}/query/?q={quote_plus(soql)}"
        resp = _request("GET", url)
        if resp.status_code != 200:
            print(f"[salesforce] Bulk query failed: {resp.status_code} {resp.text[:300]}")
            return result_map
        
        records = resp.json().get("records", [])
        for r in records:
            prod_ref = r.get(ORDER_PRODUCT_RELATIONSHIP) or {}
            title = prod_ref.get(TITLE_FIELD)
            if not title or title not in result_map:
                continue
            
            # Capped at last 10 order records per product
            if len(result_map[title]["history"]) >= 10:
                continue
                
            result_map[title]["history"].append({
                "date": r.get(ORDER_DATE_FIELD),
                "price": r.get(ORDER_PRICE_FIELD),
                "weight": r.get(ORDER_WEIGHT_FIELD)
            })
            
        for t in titles:
            result_map[t]["total_purchases"] = len(result_map[t]["history"])
            
        return result_map
    except Exception as exc:
        print(f"[salesforce] Error querying bulk history: {exc}")
        return result_map


def _dedupe(products: Iterable[dict]) -> list[dict]:
    """Collapse duplicate titles, keep the latest date and highest count.

    The scraper now emits one row per unique title, so this is mainly a safety
    net for older report files or ad-hoc CLI runs."""
    grouped: dict[str, dict] = {}
    for p in products:
        title = (p.get("title") or "").strip()
        if not title:
            continue

        # Accept both the new keys and the legacy keys for backwards compat.
        date = p.get("last_ordered_date") or p.get("purchase_date")
        if date == "unknown":
            date = None
        count = p.get("number_of_times_purchased")
        if count is None:
            count = p.get("purchase_count_in_last_10_orders")
        try:
            count = int(count or 0)
        except (TypeError, ValueError):
            count = 0

        merged = dict(p)
        merged["title"] = title
        merged["last_ordered_date"] = date
        merged["number_of_times_purchased"] = count
        merged["last_purchased_price"] = p.get("last_purchased_price")

        cur = grouped.get(title)
        if cur is None:
            grouped[title] = merged
            continue
        if date and (not cur.get("last_ordered_date") or date > cur["last_ordered_date"]):
            cur["last_ordered_date"] = date
            if "last_purchased_price" in merged:
                cur["last_purchased_price"] = merged["last_purchased_price"]
        cur["number_of_times_purchased"] = max(
            cur.get("number_of_times_purchased") or 0, count
        )
        # Prefer non-empty values for the per-product page fields.
        for k in (
            "current_price", "last_purchased_price", "product_url", "image_url",
            "category", "availability", "source", "scraped_at", "weight", "rating",
        ):
            if not cur.get(k) and merged.get(k):
                cur[k] = merged[k]
    return list(grouped.values())


def sync_products(products: Iterable[dict], orders: Iterable[dict] | None = None) -> dict:
    """
    Upsert `products` (rows from orders_report.json) into Grocery_Product__c.
    Optionally upsert `orders` (individual purchases) into Grocery_Order__c.

    title__c acts as the external ID for products.
    External_Order_ID__c acts as the external ID for orders.
    """
    stats = {
        "created": 0,
        "updated": 0,
        "errors": 0,
        "skipped": 0,
        "orders_created": 0,
        "orders_updated": 0,
        "orders_errors": 0,
    }

    if not config_present():
        missing = [k for k in _REQUIRED_ENV if not (os.getenv(k) or "").strip()]
        print(f"[salesforce] Sync skipped — missing env vars: {', '.join(missing)}")
        stats["skipped"] = 1
        return stats

    deduped = _dedupe(products)
    if not deduped:
        print("[salesforce] No products to sync.")
    else:
        print(f"[salesforce] Upserting {len(deduped)} unique product(s) into Grocery_Product__c …")
        for entry in deduped:
            title = entry["title"]
            body = _build_payload(entry)
            try:
                result = _upsert_by_title(title, body)
                stats[result] += 1
                tag = f"[{result}]".ljust(10)
                print(
                    f"  {tag} {title[:55]:<55}  "
                    f"count={entry.get('number_of_times_purchased')}  "
                    f"date={entry.get('last_ordered_date')}  "
                    f"price={entry.get('current_price')}  "
                    f"last_purchased={entry.get('last_purchased_price')}  "
                    f"avail={entry.get('availability')}  "
                    f"rating={entry.get('rating')}"
                )
            except SalesforceError as exc:
                stats["errors"] += 1
                print(f"  [error]    {title[:55]} → {exc}")
            except Exception as exc:
                stats["errors"] += 1
                print(f"  [error]    {title[:55]} → unexpected: {exc}")

    if orders:
        print(f"[salesforce] Upserting {len(orders)} order(s) into Grocery_Order__c …")
        for entry in orders:
            item_id = entry.get("item_id")
            if not item_id:
                continue
            body = _build_order_payload(entry)
            try:
                result = _upsert_order(item_id, body)
                stats[f"orders_{result}"] += 1
                tag = f"[{result}]".ljust(10)
                print(
                    f"  {tag} order {item_id[:35]:<35}  "
                    f"title={entry.get('title')[:30]}  "
                    f"date={entry.get('date')}  "
                    f"price={entry.get('last_purchased_price')}  "
                    f"weight={entry.get('weight')}"
                )
            except SalesforceError as exc:
                stats["orders_errors"] += 1
                print(f"  [error]    order {item_id[:35]} → {exc}")
            except Exception as exc:
                stats["orders_errors"] += 1
                print(f"  [error]    order {item_id[:35]} → unexpected: {exc}")

    print(
        f"[salesforce] Sync complete: "
        f"{stats['created']} products created, {stats['updated']} products updated, "
        f"{stats['errors']} product errors | "
        f"{stats['orders_created']} orders created, {stats['orders_updated']} orders updated, "
        f"{stats['orders_errors']} order errors."
    )
    return stats


def _cli() -> None:
    """Run sync against the local orders_report.json — handy for ad-hoc reruns."""
    import json
    from pathlib import Path
    from dotenv import load_dotenv

    load_dotenv()
    path = Path("orders_report.json")
    if not path.exists():
        print("[salesforce] orders_report.json not found — run the scraper first.")
        return
    report = json.loads(path.read_text(encoding="utf-8"))
    sync_products(report.get("products", []), report.get("orders", []))


if __name__ == "__main__":
    _cli()
