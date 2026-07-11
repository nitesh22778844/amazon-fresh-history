# Amazon Fresh Purchase History → Salesforce

A Flask web service that logs into **amazon.in**, scrapes the last N
**Amazon Fresh / Amazon Now** orders, and syncs the results to Salesforce:

- Each unique product is **upserted** into `Grocery_Product__c`
  (`title__c` is the external ID — existing titles are updated, new titles
  are created).
- Each individual purchase is upserted into `Grocery_Order__c`
  (`External_Order_ID__c` is the external ID), linked to its product via a
  master-detail relationship — building a per-product **order history** over
  time.
- API responses enrich every product with an `order_history` array (date,
  price, weight of past purchases) queried live from Salesforce.

Sibling project to `purchase-history` (Flipkart). Both write to the same
Salesforce object; `source__c` distinguishes the origin (`"Flipkart"`,
`"Amazon Fresh"`, or `"Amazon Now"`).

Interactive **Swagger UI** at `/docs` (the root `/` redirects there).
Deployable to **Render** or **Railway** out of the box (Docker, headless
Chromium).

---

## API

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/health` | Liveness probe. |
| `GET`  | `/docs` | Swagger UI playground. |
| `GET`  | `/openapi.json` | OpenAPI 3.0 spec. |
| `GET`  | `/api/products` | Latest scrape output; each product includes `order_history`. |
| `POST` | `/api/products` | Start a scrape in a background thread. Body: `{"orders": <int>}` (default 10). |
| `GET`  | `/api/search?q=<query>` | Live product search on Amazon Now (Fresh fallback); results include `order_history`. |
| `GET`  | `/api/cart` | Result of the last add-to-cart run: `{requested, added[], not_found[], cart_count, now_cart_count}`. |
| `POST` | `/api/cart` | Add products to the cart by name (background thread). Body: `{"products": ["name", …]}`. |
| `GET`  | `/api/skills` | Skills the self-healing agent has learned: `{agent_enabled, skills[]}`. |
| `DELETE` | `/api/skills/<id>` | Forget one learned skill (e.g. after Amazon changes its DOM again). |
| `GET`  | `/api/otp` | Is a run waiting for a 2-step OTP? `{waiting, waiting_since, ttl_seconds}`. |
| `POST` | `/api/otp` | Hand the 2-step verification OTP to a waiting run. Body: `{"otp": "123456"}`. |

A scrape typically takes 3–8 minutes; poll `GET /api/products` for the result.
A scrape and a cart run cannot overlap (they share the single Amazon account) —
the second concurrent request gets `409`.

### Product shape (`GET /api/products`)

```json
{
  "product_name": "Amazon Brand - Vedaka Organic Toor Dal, 500g",
  "date": "2026-05-12",
  "number_of_times_purchased": 2,
  "current_price": 89.0,
  "last_purchased_price": 85.0,
  "product_url": "https://www.amazon.in/dp/B0...",
  "image_url": "https://m.media-amazon.com/images/I/...",
  "category": "Grocery",
  "availability": "Available",
  "rating": 4.3,
  "source": "Amazon Fresh",
  "scraped_at": "2026-05-26T10:15:00+05:30",
  "weight": "500 gm",
  "order_history": {
    "total_purchases": 2,
    "history": [
      {"date": "2026-05-12", "price": 85.0, "weight": "500 gm"},
      {"date": "2026-04-28", "price": 82.0, "weight": "500 gm"}
    ]
  }
}
```

- `current_price` is the live price read from the product page; `availability`
  is `"Available"` whenever that page shows a price, else `"Unavailable"`.
- `last_purchased_price` is the price actually paid in the **most recent**
  order containing the product (from the order item list, not the product page).
- `weight` is parsed from the product title (`500 gm`, `1 litre`,
  `2 quantity`, …); `"1 quantity"` when nothing is recognisable.
- `order_history` is queried live from Salesforce `Grocery_Order__c`, capped at
  the 10 most recent purchases per product. Empty
  (`{"total_purchases": 0, "history": []}`) when Salesforce isn't configured.

---

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate     # macOS / Linux
# or: .venv\Scripts\activate   # Windows

pip install -r requirements.txt
playwright install chromium
```

Copy `.env.example` to `.env` and fill in the required values.

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `AMAZON_USERNAME` | ✅ | Amazon.in login email or phone |
| `AMAZON_PASSWORD` | ✅ | Amazon.in account password |
| `SF_TOKEN_URL` | for sync | OAuth token endpoint, e.g. `https://<domain>.my.salesforce.com/services/oauth2/token` |
| `SF_CLIENT_ID` | for sync | Connected App consumer key |
| `SF_CLIENT_SECRET` | for sync | Connected App consumer secret |
| `SF_API_ENDPOINT` | for sync | `https://<domain>.my.salesforce.com/services/data/v57.0/sobjects/Grocery_Product__c/` |
| `HEADLESS` | — | `false` locally, `true` in Docker |
| `PORT` | — | `10000` (Render/Railway set this automatically) |
| `ORDERS_TO_SCRAPE` | — | Default order count when not passed explicitly (`10`) |
| `DELIVERY_ADDRESS_PREFIX` | — | Substring of the saved "Deliver to" address to select after login. **Personal (PII)** — env only |
| `DELIVERY_PINCODE` | — | 6-digit pincode fallback when no saved address matches. **Personal (PII)** — env only |
| `AMAZON_SESSION_REUSE` | — | `true` enables session caching; default `false` (full login every run) |
| `AMAZON_AUTH_STATE_PATH` | — | Session-cache file path (`auth_state.json`) |
| `OTP_TTL_SECONDS` | — | How long a pushed OTP stays valid (`300`) |
| `DEEPSEEK_API_KEY` | — | Enables the self-healing agent; unset = agent off. **Secret** — env only |
| `DEEPSEEK_MODEL` / `DEEPSEEK_BASE_URL` | — | `deepseek-chat` / `https://api.deepseek.com` |
| `AGENT_ENABLED` | — | `true`; set `false` to disable the agent without removing the key |
| `SKILLS_PATH` | — | Where learned agent skills persist (`skills.json`) |

If any of the four `SF_*` vars are missing, Salesforce sync is silently skipped
and the scrape still completes.

### Salesforce Connected App

For sync, create a Connected App with:

- **OAuth flow:** Client Credentials (with a "Run As" user set)
- **Scopes:** `api`, `refresh_token`
- **Run-as user** needs read/create/update access to `Grocery_Product__c` and
  `Grocery_Order__c` (including the custom fields below).

Fields written on `Grocery_Product__c` (matched by external ID `title__c`):
`number_of_times_purchased__c`, `last_ordered_date__c`, `current_price__c`,
`last_purchased_price__c`, `product_url__c`, `image_url__c`, `category__c`,
`availability__c`, `source__c`, `scraped_at__c`, `weight__c`, `rating__c`.

Fields written on `Grocery_Order__c` (matched by external ID
`External_Order_ID__c`): `Order_Date__c`, `Ordered_Price__c`, `weight__c`, and
the master-detail link to `Grocery_Product__c` (resolved via
`Grocery_Product__r.title__c`).

`Name` is auto-number on both objects and is never sent by the sync.

---

## Running

### Start the web service

```bash
PORT=3001 HEADLESS=false python app.py
```

Open <http://localhost:3001/docs> for the interactive playground.

```bash
# Trigger a scrape
curl -X POST http://localhost:3001/api/products \
  -H "Content-Type: application/json" \
  -d '{"orders": 5}'

# Poll for results
curl http://localhost:3001/api/products

# Live search
curl "http://localhost:3001/api/search?q=toor%20dal"
```

### Add products to the cart

```bash
curl -X POST http://localhost:3001/api/cart \
  -H "Content-Type: application/json" \
  -d '{"products": ["Amul Gold Full Cream Milk 500ml", "Bhindi"]}'

# Poll for the result (added vs not_found)
curl http://localhost:3001/api/cart
```

Each name is searched on **Amazon Now first** (the `/tez/` quick-commerce
storefront) and added to the Now cart; if Now has no confident match or the
add fails, the classic **Amazon Fresh** search is used as a per-product
fallback. Matching is fuzzy (character similarity or full word coverage —
short names like "Bhindi" match), and each added item reports its `source`
(`"Amazon Now"` or `"Amazon Fresh"`). Names that don't match confidently land
in `not_found` with the best candidate and its score.

Note: the **Amazon Now cart is separate from the main Amazon cart** — check
both in the Amazon app/site. The run **never proceeds to checkout**; items are
left in the cart for you to review and buy manually.

### Run the CLI scripts directly (no Flask)

```bash
python scrape_amazon_orders.py                  # headed, 10 orders
python scrape_amazon_orders.py --orders=5
python scrape_amazon_orders.py --headed=false   # headless

python amazon_cart.py "Amul Gold Full Cream Milk 500ml" "Vedaka Toor Dal 1kg"

python salesforce_sync.py    # re-sync the existing orders_report.json, no re-scrape
```

---

## Login: captcha, OTP, session reuse

### Captcha

If Amazon shows a captcha, the scrape stops with a screenshot and a clear
error. Re-run locally in headed mode to solve it once by hand; captchas do not
recur once Amazon trusts the browser fingerprint.

### OTP / 2-step verification

When Amazon asks for a 2-step verification code, the run blocks for up to
3 minutes waiting for the code to be pushed over HTTP:

```bash
curl $BASE_URL/api/otp        # → {"waiting": true, ...}
curl -X POST $BASE_URL/api/otp -H "Content-Type: application/json" -d '{"otp":"123456"}'
```

The scraper picks it up within ~1 s, submits it, and continues. The code is
held in memory only and expires after `OTP_TTL_SECONDS` (default 300 s). Works
identically headed and headless. In a headed local browser you can also just
type the OTP into Amazon directly — the run detects the screen advancing.

### Session reuse (fewer logins / OTPs) — opt-in

By default every run does a full login. Set `AMAZON_SESSION_REUSE=true` to
save the browser session to `auth_state.json` after login and reuse it —
a still-valid session **skips login and OTP entirely**; an expired one falls
back to a full login and re-saves. You typically OTP once, then run freely for
days.

- Off by default so cloud deploys keep the proven full-login behavior —
  Render/Railway filesystems are **ephemeral**, so `auth_state.json` only
  survives within one container's lifetime.
- `auth_state.json` holds live session cookies — gitignored; never commit it.
- To recover from a bad state: unset the var and/or delete `auth_state.json`.

### Delivery location (Fresh availability)

Amazon Fresh prices and stock are **per delivery location** — with no location
set, every Fresh product reports *"currently unavailable"*. Right after login
the scraper sets the "Deliver to" location: first the saved address containing
`DELIVERY_ADDRESS_PREFIX`, else the `DELIVERY_PINCODE`. Both are personal
(PII) with no hard-coded defaults — set them in `.env` locally and in the
cloud dashboard in production.

---

## Self-healing agent (DeepSeek)

When a Playwright step fails in a way the code doesn't recognise — e.g. an Add
button that's really a "2 options" size-variant control, or a result-card
selector that stopped matching — the app tries to recover at runtime instead
of failing:

1. **Learned skills replay first** (persisted in `skills.json`) — instant, no
   LLM call.
2. On a novel failure, the goal + a snapshot of the page's interactive
   elements go to **DeepSeek**, which returns a recovery recipe. It is
   executed only after strict validation, and only persisted as a skill if the
   caller's own confirmation check passes.

Guardrails (enforced in code): recipes are data, never code; `click`/`wait`
only, max 5 steps, no navigation; selectors touching
checkout/buy/pay/address/sign-out/delete are rejected; captcha and auth pages
are never "resolved"; the happy path never calls the LLM. With no
`DEEPSEEK_API_KEY` the agent is fully off.

`GET /api/skills` lists what has been learned; `DELETE /api/skills/<id>`
forgets a stale skill. Successful recoveries show up as `resolved_by` on added
cart items.

---

## Deploying to Render

The repo is Docker-based and ready for Render's "New Web Service → connect
repo" flow (`render.yaml` declares the env vars; `railway.json` is included
for Railway).

1. **Push to GitHub.**
2. **Render → New → Web Service → connect repo.** Render auto-detects
   `Dockerfile` and `render.yaml`.
3. **Set environment variables** in the dashboard — `AMAZON_*` are required;
   the `SF_*` block enables Salesforce sync; `DELIVERY_*` are personal and
   belong in the dashboard, not the repo; the rest have working defaults (see
   the table above).
4. **Deploy.** Trigger a scrape via `POST /api/products`.

**OTP on Render:** each scrape does a full login. If Amazon asks for a code,
watch the logs for the `[auth] ACTION REQUIRED` line (or poll `GET /api/otp`),
then `POST /api/otp` with the code — the scraper picks it up within ~1 s. The
code lives only in the running container's memory. Because the OTP store is
in-process, the POST must hit the same instance that is waiting — fine for a
single web service, not for multiple replicas.

---

## Output file

Each scrape writes `orders_report.json`:

```json
{
  "scraped_at": "2026-05-26T10:15:00+05:30",
  "orders_scanned": 7,
  "products": [ { "title": "...", "last_ordered_date": "...", "...": "..." } ],
  "orders":   [ { "item_id": "vedaka_toor_dal_500g_2026-05-12", "title": "...", "date": "2026-05-12", "last_purchased_price": 85.0, "weight": "500 gm" } ]
}
```

`products` feeds `Grocery_Product__c`; `orders` feeds `Grocery_Order__c` (the
`item_id` is a stable slug of title + date, used as the external ID so re-runs
update rather than duplicate).

---

## Notes & constraints

- **Cart is the only write.** Scraping is read-only; `POST /api/cart` adds
  items to the cart but stops there — no purchase, checkout, cancel, or return.
- **Scope:** Amazon Fresh and Amazon Now orders only; regular retail orders
  are skipped. No Fresh orders → `products: []`, not an error.
- **Salesforce sync upserts** — existing titles are updated, new titles are
  created. Sync failures are logged and never fail the scrape.
- **No credential logging.** The username is masked; the password never
  reaches stdout.
- **Captchas are not bypassed.** The scrape stops with a clear error and a
  screenshot.
- **Single tenant.** One Amazon account per deployment.
- Never commit `.env` or `auth_state.json` (both gitignored).
