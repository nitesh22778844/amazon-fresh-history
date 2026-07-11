# CLAUDE.md

Working reference for Claude Code in this repo. **All user/developer-facing
documentation lives in README.md — update it whenever behavior changes.**

## What this is

Flask web service (Python 3.11, async Playwright + Chromium) that logs into
amazon.in, scrapes the last N Amazon Fresh/Now orders, writes
`orders_report.json`, and upserts results to Salesforce. Runs locally headed
or on Render/Railway via Docker (headless). Swagger UI at `/docs`, spec at
`/openapi.json`.

## File map

| File | Role |
|---|---|
| `app.py` | Flask entry point, all routes, OpenAPI spec, background-thread orchestration |
| `scrape_amazon_orders.py` | Core scrape: login (`open_logged_in_page`), order pagination, product pages, `extract_weight()`, calls SF sync at end |
| `amazon_cart.py` | `POST /api/cart` backend — Now cart first, Fresh fallback; `CART_SELECTORS` at top |
| `amazon_search.py` | `GET /api/search` backend (Amazon Now `/tez/` `searchByKeyword` JSON, Fresh fallback) |
| `salesforce_sync.py` | OAuth client_credentials + upserts + SOQL history queries; field constants at top |
| `agent_resolver.py` / `agent_skills.py` | DeepSeek self-healing + persisted skills (`skills.json`) |
| `otp_store.py` | In-memory single-slot OTP store for the 2FA push |

## Run / test

```bash
PORT=3001 HEADLESS=false python app.py        # web service
python scrape_amazon_orders.py --orders=5     # scraper direct (headed default)
python amazon_cart.py "Bhindi" --headed=false # cart direct
python salesforce_sync.py                     # re-sync existing orders_report.json
```

Required env: `AMAZON_USERNAME`, `AMAZON_PASSWORD`. Salesforce sync needs all
of `SF_TOKEN_URL`, `SF_CLIENT_ID`, `SF_CLIENT_SECRET`, `SF_API_ENDPOINT` —
skipped cleanly if any missing. Full env var table: README.md.

## API surface

| Endpoint | Notes |
|---|---|
| `GET/POST /api/products` | GET = last scrape (shape: `product_name`, `date`, `number_of_times_purchased`, prices, `weight`, `rating`, `order_history`); POST body `{"orders": N}` starts background scrape |
| `GET /api/search?q=` | Live Amazon Now search, Fresh fallback; results enriched with `order_history` |
| `GET/POST /api/cart` | POST `{"products": [...]}` adds by name; GET = `{requested, added[], not_found[], cart_count, now_cart_count}` |
| `GET/POST /api/otp` | 2FA push; POST only accepted while a run waits (else 409) |
| `GET /api/skills`, `DELETE /api/skills/<id>` | Learned agent skills |
| `GET /health`, `/docs`, `/openapi.json` | Infra |

- One scrape **or** cart run at a time (shared Amazon account) — second POST → `409`.
- `/api/products` GET and `/api/search` enrich each product with an
  `order_history` array via `salesforce_sync.get_bulk_order_history()` (single
  bulk SOQL, capped at 10 entries per product).

## Hard rules

- **Never past the cart.** No checkout/payment/cancel/return code paths, ever.
- **Selectors live only in the dicts at the top of each file** (`SELECTORS`,
  `CART_SELECTORS`). On selector failure: screenshot + log URL + exit non-zero;
  never continue silently with empty data. Priority: IDs > `name=` > role+name >
  visible text > structural.
- **Salesforce:** upsert `Grocery_Product__c` by external ID `title__c`;
  purchases upsert `Grocery_Order__c` by `External_Order_ID__c`
  (`<slug(title)>_<date>`), linked via master-detail `Grocery_Product__r`.
  `Name` is auto-number on both — never send it. Field constants at top of
  `salesforce_sync.py`. `current_price__c` = live product page (sole source of
  `availability__c`); `last_purchased_price__c` = price paid in the most recent
  order. The Flipkart sibling project writes the same records; `source__c`
  reflects last writer — by design.
- **Login:** full login every run by default; session reuse is opt-in
  (`AMAZON_SESSION_REUSE=true` → `auth_state.json`, gitignored, never commit).
  OTP comes via `POST /api/otp` into the in-memory `otp_store` (single slot,
  same-process only, TTL `OTP_TTL_SECONDS`); run blocks up to 3 min. Captcha =
  stop with screenshot, solve via headed local run.
- **Delivery location gates Fresh availability/price:** after login, pick saved
  address matching `DELIVERY_ADDRESS_PREFIX`, else enter `DELIVERY_PINCODE`.
  Both are PII — env only, never hard-code.
- **Cart matching:** fuzzy score = max(difflib char ratio, word coverage);
  title containing every query word scores ≥0.6 = `MATCH_THRESHOLD`. Now cart
  is separate from the main/Fresh cart.
- **Self-healing agent** (off without `DEEPSEEK_API_KEY`; failure-path only):
  learned skills replay first, then one DeepSeek call + one retry. Recipes are
  data, never code — `click`/`wait` only, ≤5 steps, no navigation; selectors
  matching `checkout|buy|pay|place order|address|sign out|delete` rejected.
  Hook contexts: `now_add_click`, `fresh_add_click`; selector slots:
  `search.result_card`, `cart.result_card`, `scrape.order_card`. Only
  caller-confirmed recipes persist to `skills.json`.
- Empty Fresh order history → report with `products: []`, sync no-ops; not an
  error. Mask credentials in logs. Never commit `.env` or `auth_state.json`.

## Report shape (`orders_report.json`)

`{scraped_at, orders_scanned, products: [...], orders: [...]}` — each product:
`title`, `last_ordered_date`, `number_of_times_purchased`, `current_price`,
`last_purchased_price`, `product_url`, `image_url`, `category`, `availability`,
`source` (`"Amazon Fresh"`/`"Amazon Now"`), `scraped_at`, `weight` (parsed from
title by `extract_weight()`, default `"1 quantity"`), `rating`. Each order:
`{item_id, title, date, last_purchased_price, weight}`.
