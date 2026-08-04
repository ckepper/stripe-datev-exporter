# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A CLI that pulls invoices, charges and balance transactions from the Stripe API and emits DATEV
"Buchungsstapel" (EXTF) CSV files for German bookkeeping, including monthly revenue recognition
(pRAP / deferred revenue) postings. Fork of `jonaswitt/stripe-datev-exporter`.

## Commands

Everything runs through `uv` (Python 3.9 per `.python-version`):

```bash
uv run stripe-datev-cli.py download <year> <month>   # main export; month=0 means whole year
uv run stripe-datev-cli.py preview in_123|ch_123|txn_123  # print records for one object, no files written
uv run stripe-datev-cli.py fees <year> <month>       # fee/contribution totals (UTC months, matching Stripe)
uv run stripe-datev-cli.py opos [<y> <m> <d>]        # open invoices now or as of end of that date
uv run stripe-datev-cli.py fill_account_numbers      # assign accountNumber metadata to customers
uv run stripe-datev-cli.py list_accounts [<file>]    # DATEV Debitoren master data (latin-1 when file given)
uv run stripe-datev-cli.py validate_customers
```

Tests (plain `unittest`, no pytest):

```bash
uv run python -m unittest discover -s tests -t .
uv run python -m unittest tests.test_recognition.RecognitionTest.test_split_months_simple
```

Formatting is autopep8 with `--indent-size=2 --ignore=E121` (see `.vscode/settings.json`); Python code
in this repo uses **2-space indentation**.

## Required local setup

- `.env` with `STRIPE_API_KEY` (gitignored). A `sk_test_` key redirects all output into `out/test/`.
- `config.toml` (gitignored, copy `config.example.toml`) — timezone, DATEV Berater/Mandant numbers and
  the chart-of-accounts numbers + DATEV tax keys (`BU-Schlüssel`).
- **`stripe_datev/config.py` opens `config.toml` with a relative path at import time**, so every command
  and every test run must start from the repo root.
- `out/` is gitignored and may be a symlink; subdirectories are created on demand.

## Architecture

### Two independent posting tracks

`download` produces two kinds of DATEV batch files per month:

1. **Revenue** (`EXTF_<month>_Revenue*.csv`) — receivable + revenue + pRAP postings, derived from
   invoices and from direct charges.
2. **Balance** (`EXTF_<month>_Balance.csv`) — bank/cash side, derived from Stripe balance transactions
   (`balance.py`): payments, refunds, payouts, topups, Stripe fees, contributions, connected-account
   transfers. Payouts and topups are mirror images on the same `transit`/`bank` account pair — payout
   `S`, topup `H`. Any `reporting_category` the branch chain doesn't know is skipped with a warning and
   never booked, so those warnings matter for bank reconciliation.

Not every payment settles into the Stripe balance. PayPal funds go straight to the merchant's PayPal
account, so the balance transaction carries `amount == 0` and only Stripe's fee. `balance.settlesExternally()`
detects this (payment method `paypal` **and** `tx.amount == 0`) and books the gross — `charge.amount_captured`
for payments, `tx.source.amount` for refunds — against `accounts.paypal` instead of `accounts.bank`.
Without that the receivable would never clear. The fee record stays on `accounts.bank`, because Stripe
does debit its fee from the Stripe balance. Card, Link and Amazon Pay settle normally and are unaffected.

Both write via `output.writeRecords`. Records are grouped by month first; postings that fall outside the
requested month land in `EXTF_<other-month>_Revenue_From_<this-month>.csv` — the exporter deliberately
emits future/past-month files rather than deferring them.

### The revenue pipeline

```
Stripe invoice / charge
  → createRevenueItems()          (invoices.py, charges.py)   — normalized dicts
  → invoices.createAccountingRecords()                        — DATEV record dicts
  → output.writeRecords()                                     — EXTF CSV
```

A **revenue item** is a plain dict with `created`, `amount_net`, `amount_with_tax`, `accounting_props`,
`line_items[]` (each with `recognition_start`/`recognition_end`/`amount_with_tax`), plus optional
`voided_at`, `marked_uncollectible_at`, `credited_at`/`credited_amount`. `charges.createRevenueItems`
produces the *same* shape from direct (invoice-less) charges, which is why both feed the single
`invoices.createAccountingRecords`. Adding a new revenue source means producing this dict, not new
record-writing code.

An **accounting record** is a dict keyed by literal German DATEV column names
(`"Umsatz (ohne Soll/Haben-Kz)"`, `"Soll/Haben-Kennzeichen"`, `"Konto"`,
`"Gegenkonto (ohne BU-Schlüssel)"`, `"BU-Schlüssel"`, `"Buchungstext"`, `"Belegfeld 1"`, …) plus one
non-DATEV key `"date"` (a tz-aware datetime). `output.printRecords` converts `date` → `Belegdatum`
(`DDMM`), quotes and truncates `Buchungstext` to 60 chars, and drops any key not in the `fields` list.

### Revenue recognition / pRAP

`recognition.split_months(start, end, amounts)` pro-rates an amount across calendar months by elapsed
seconds, quantized to cents, with the rounding remainder pushed into the last month (it asserts the
split sums back to the input). It drives both `monthly_recognition-*.csv` and the pRAP postings.

Recognition periods per line item come from `invoices.getLineItemRecognitionRange`: the Stripe
`line_item.period` if present and non-degenerate, otherwise `dateparser.find_date_range()` parsing the
line-item description text (English month names, ordinal days, years — `dateparser.YEAR_REGEX` has a
hardcoded year list that needs extending over time), otherwise a warning and a same-day period.

In `createAccountingRecords`, `apply_prap()` books the not-yet-earned portion to the pRAP account and
releases it month by month. Two suppression rules matter: pRAP records are dropped entirely if they all
fall in a single month, and per-month groups that net to zero are dropped. Void / uncollectible / credit
note events replay `apply_prap` with a negated amount at the event date, so the deferral unwinds there.

### Account & tax determination

`customer.getAccountingProps(customer, invoice=…, checkout_session=…)` is the single decision point for
which revenue account and DATEV tax key a posting gets. It resolves, in order: the customer's
`accountNumber` metadata (required for anything finalized on/after 2022-01-01; earlier invoices fall back
to `accounts.sammel_debitor`), the country from `address`/`shipping.address`, the tax-exempt status *as of
the invoice* when automatic tax is off, and a verified `eu_vat` tax ID. DE → `revenue_german_vat`;
EU with VAT ID → `revenue_reverse_charge_eu`; otherwise reverse charge → `account_reverse_charge_world`.
Invoice and payment postings use different tax keys (`datev_tax_key_*_invoice` vs `_payment`) — the
invoice-side key comes from `getAccountingProps` here, the payment-side key is applied in `balance.py`.
Anomalies are reported as `print("Warning: …")` rather than raising; `download` output is meant to be read.

The **VAT rate is never written into a record** — postings carry the gross with an empty `BU-Schlüssel`,
so the rate comes entirely from the automatic tax key on the revenue account in the chart of accounts.
`revenue_german_vat` therefore has to match what Stripe actually charges: PediaPress sells books at the
reduced rate (`EuropeanUnionVAT.fraction = .07` in the pediapress repo; Stripe checkout tags every line
item `txcd_35010001` "books 7%"), so this is SKR03 **8300** (Erlöse 7 % USt), not 8400 (19 %). Getting it
wrong is silent — the export looks correct and DATEV derives the wrong Umsatzsteuer on import.

EU consumers without a VAT ID fall through to `revenue_german_vat` as well (the `tax_exempt == "none"`
branch, "Unter Bagatellgrenze MOSS"). That is right only while the company stays under the 10,000 EUR
distance-selling threshold and charges German VAT; crossing it means OSS registration, destination-country
rates and a separate revenue account per country.

### Conventions and constraints worth knowing

- Period membership for invoices uses the **finalized** date, not `created`; `listFinalizedInvoices`
  therefore over-fetches by one month (`datedelta.MONTH` padding) and filters in Python. Increase that
  padding if invoices take longer than a month to finalize.
- All dates are converted to `config.accounting_tz` (`Europe/Berlin`) before use. The `fees` command is
  the deliberate exception and uses UTC, because Stripe invoices fees on UTC month boundaries — fee
  records use a `YYYY-MM` UTC string as `Belegfeld 1` to tie them to the Stripe invoice.
- `output.printRecords` raises if a single batch spans more than one calendar year.
- Money is `decimal.Decimal` throughout (Stripe cents ÷ 100); `output.formatDecimal` emits the German
  decimal comma. EXTF files are written latin-1 with `errors="replace"` and CRLF newlines.
- Invoice metadata `stripe-datev-exporter:ignore = "true"` skips an invoice entirely.
- Module-level dicts cache Stripe objects (`invoices_cached`, `customers_cached`, `tax_rates_cached`,
  `tax_ids_cached`, `checkoutSessionsByPaymentIntent`) — the process is a single-shot CLI, so these never
  get invalidated.
- Stripe API version is pinned to `2020-08-27` in `stripe-datev-cli.py`.
- Fully refunded direct charges are skipped; **partially** refunded direct charges raise
  `NotImplementedError`.
- `download` ends by scanning the previous 24 months for invoices voided/marked uncollectible in this
  period and for credit notes on earlier invoices, printing "consider downloading <month> again" — those
  months must be re-exported by hand.

### Verifying changes

`preview <object-id>` prints the accounting records for a single invoice/charge/balance transaction
without touching `out/`. Diffing its output before and after a change to the record-generation logic is
the intended way to check that logic edits do what you expect.
