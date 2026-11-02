# Phase 1 – Site investigation (2026-11-01)

## Rules for automated access
- **robots.txt** allows all user agents except on account, checkout, payment, FAQ, terms and similar pages.
  Every page the tracker needs (`/`, `/shops/*`, `/clearance-sale`, `/_next/data/*`) is allowed.
- **Terms & Conditions** have no clause about bots, scraping or automated access.
  - §18.1: "content, trademarks and data … databases" belong to OneDayOnly.
    → Personal price tracking is fine. Publishing their price data (for example a public repo) is a grey area.
  - §3.1: ODO doesn't have to honour an order if the price was an obvious clerical error.
    → A 100%-off alert can't guarantee you get the item at that price.
- The site runs behind **Cloudflare**. Plain requests with a custom User-Agent worked. One request was dropped mid-transfer, so the fetcher needs retries.

## How products load
- The site is built with Next.js. Every page embeds all its products **with prices** as JSON in `<script id="__NEXT_DATA__">`.
  No JavaScript rendering is needed.
- The same data comes as pure JSON from `/_next/data/{buildId}/en-GB/{path}.json`.
  - `buildId` changes whenever ODO deploys a new version of the site. Read it from the homepage HTML, and if a JSON request returns 404, re-read it.
- Product fields include: `realId`, `id` (the URL slug, `/products/{id}`), `name`, `brand`, `price.value`, `retailPrice.value` (the was-price), `minPrice`, `hasPriceRange`, `saving.percent`, `topLevelCategories`, `isSoldOut`, `isPromotionalDeal`, `activeFromDate`, `activeToDate`.

## Sources
| Section | Path | Products | Size (gzip) |
|---|---|---|---|
| Today's deals (includes the "Last Chance Extra Time" shop) | `en-GB.json` (or `/`) | ~358 | ~300 KB |
| Extra Time | `shops/extra-time-deals` | 393 | ~2.9 MB raw |
| Clearance | `clearance-sale` | 600 (looks like a hard cap; no paging found) | ~5.4 MB raw |
| Everyday Essentials | `shops/everyday-essentials` | 50 | ~270 KB raw |
| Black Friday | `black-friday` | 0 today (empty placeholder) | — |
| Categories | `category/{slug}` | subsets of Today's deals (13 of 13 electronics items matched) | — |

- Category pages repeat items that are already in Today's deals, so the tracker won't fetch them. Each item's category comes from `topLevelCategories`.

## Data-quality notes
- Today there is one real **R0 item**, `isPromotionalDeal=true`, limit 1 per customer, excluding shipping.
  Every variant was sold out on its product page, but the listing still said `isSoldOut=false`.
- Some items have no `retailPrice` (22 of the 50 in Everyday Essentials, 3 in Extra Time). They will be flagged.
- `hasPriceRange=true` means "From R…": `price` is the cheapest variant.
