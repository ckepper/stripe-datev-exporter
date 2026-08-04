import stripe
import decimal
from datetime import datetime, timezone
from . import customer, output, config


def listBalanceTransactions(fromTime, toTime):
  return stripe.BalanceTransaction.list(
    created={
        "lt": int(toTime.timestamp()),
        "gte": int(fromTime.timestamp()),
      },
      expand=["data.source", "data.source.customer",
              "data.source.customer.tax_ids", "data.source.invoice", "data.source.charge",
              "data.source.charge.customer", "data.source.charge.invoice",
              "data.source.source_transaction", "data.source.source_transaction.invoice",
              "data.source.destination", "data.source.destination_payment"]
  ).auto_paging_iter()


def settlesExternally(charge, tx):
  # PayPal funds go straight into the merchant's PayPal balance and never reach the
  # Stripe balance: Stripe only debits its processing fee, so the balance transaction
  # carries amount == 0. The gross has to be booked against a clearing account instead
  # of the bank account, otherwise the receivable is never cleared.
  payment_method = (charge.get("payment_method_details", None)
                    or {}).get("type", None)
  return payment_method == "paypal" and tx.amount == 0


def createAccountingRecords(balance_transactions):
  records = []
  for tx in balance_transactions:
    created = datetime.fromtimestamp(
      tx.created, timezone.utc).astimezone(config.accounting_tz)
    amount = decimal.Decimal(tx.amount) / 100
    fee = decimal.Decimal(tx.fee) / 100

    if tx["reporting_category"] == "charge" or tx["reporting_category"] == "charge_failure":
      charge = tx.source
      cus = customer.retrieveCustomer(charge.customer)
      accounting_props = customer.getAccountingProps(cus)
      if charge.invoice:
        number = charge.invoice.number
      else:
        number = charge.receipt_number or charge.id
      fee_desc = tx.fee_details[0].description

      if settlesExternally(charge, tx):
        settlement_account = str(config.accounts["paypal"])
        settled_amount = decimal.Decimal(charge.amount_captured) / 100
      else:
        settlement_account = str(config.accounts["bank"])
        settled_amount = amount

      records.append({
        "date": created,
        "Umsatz (ohne Soll/Haben-Kz)": output.formatDecimal(abs(settled_amount)),
        "Soll/Haben-Kennzeichen": "S" if settled_amount >= 0 else "H",
        "WKZ Umsatz": "EUR",
        "Konto": settlement_account,
        "Gegenkonto (ohne BU-Schlüssel)": accounting_props["customer_account"],
        "BU-Schlüssel": accounting_props["datev_tax_key_payment"],
        "Buchungstext": "Stripe Payment ({})".format(charge.id),
        "Belegfeld 1": number,
      })

      records.append({
        "date": created,
        "Umsatz (ohne Soll/Haben-Kz)": output.formatDecimal(abs(fee)),
        "Soll/Haben-Kennzeichen": "S" if fee >= 0 else "H",
        "WKZ Umsatz": "EUR",
        "Konto": str(config.accounts["stripe_fees"]),
        "Gegenkonto (ohne BU-Schlüssel)": str(config.accounts["bank"]),
        "Buchungstext": "{} ({})".format(fee_desc or "Stripe Fee", charge.id),
        # Stripe invoices fees within the bounds of one UTC month,
        # this makes it easier to associate a fee with a montly invoice
        "Belegfeld 1": created.astimezone(timezone.utc).strftime("%Y-%m"),
      })

    elif tx["reporting_category"] == "payout":
      records.append({
        "date": created,
        "Umsatz (ohne Soll/Haben-Kz)": output.formatDecimal(-amount),
        "Soll/Haben-Kennzeichen": "S",
        "WKZ Umsatz": "EUR",
        "Konto": str(config.accounts["transit"]),
        "Gegenkonto (ohne BU-Schlüssel)": str(config.accounts["bank"]),
        "Buchungstext": "Stripe Payout {}".format(tx.source.id),
      })

    elif tx["reporting_category"] == "refund":
      charge = tx.source.charge
      cus = customer.retrieveCustomer(charge.customer)
      accounting_props = customer.getAccountingProps(cus)
      if charge.invoice:
        number = charge.invoice.number
      else:
        number = charge.receipt_number or charge.id

      if settlesExternally(charge, tx):
        settlement_account = str(config.accounts["paypal"])
        refunded_amount = decimal.Decimal(tx.source.amount) / 100
      else:
        settlement_account = str(config.accounts["bank"])
        refunded_amount = -amount

      records.append({
        "date": created,
        "Umsatz (ohne Soll/Haben-Kz)": output.formatDecimal(refunded_amount),
        "Soll/Haben-Kennzeichen": "H",
        "WKZ Umsatz": "EUR",
        "Konto": settlement_account,
        "Gegenkonto (ohne BU-Schlüssel)": accounting_props["customer_account"],
        "Buchungstext": "Stripe Payment Refund ({})".format(charge.id),
        "Belegfeld 1": number,
      })

    elif tx["reporting_category"] == "contribution":
      records.append({
        "date": created,
        "Umsatz (ohne Soll/Haben-Kz)": output.formatDecimal(-amount),
        "Soll/Haben-Kennzeichen": "S",
        "WKZ Umsatz": "EUR",
        "Konto": str(config.accounts["contributions"]),
        "Gegenkonto (ohne BU-Schlüssel)": str(config.accounts["bank"]),
        "Buchungstext": "Stripe {} {}".format(tx["description"] or "Contribution", tx["id"]),
        "Belegfeld 1": created.astimezone(timezone.utc).strftime("%Y-%m"),
      })

    elif tx["reporting_category"] == "transfer":
      transfer = tx.source
      net_amount = transfer.amount - \
          ((transfer.source_transaction.application_fee_amount if transfer.source_transaction else None) or 0)
      invoice = transfer.source_transaction.get(
        "invoice", None) if transfer.source_transaction else None
      invoiceNumber = invoice.number if invoice else None

      records.append({
        "date": created,
        "Umsatz (ohne Soll/Haben-Kz)": output.formatDecimal(decimal.Decimal(net_amount) / 100),
        "Soll/Haben-Kennzeichen": "S",
        "WKZ Umsatz": "EUR",
        "Konto": str(config.accounts["external_services"]),
        "Gegenkonto (ohne BU-Schlüssel)": transfer["destination"]["metadata"]["accountNumber"],
        "Buchungstext": "Fremdleistung {} anteilig".format(invoiceNumber or transfer.id),
        "Belegfeld 1": transfer.id,
      })

      records.append({
        "date": created,
        "Umsatz (ohne Soll/Haben-Kz)": output.formatDecimal(decimal.Decimal(net_amount) / 100),
        "Soll/Haben-Kennzeichen": "S",
        "WKZ Umsatz": "EUR",
        "Konto": transfer["destination"]["metadata"]["accountNumber"],
        "Gegenkonto (ohne BU-Schlüssel)": str(config.accounts["bank"]),
        "Buchungstext": "Fremdleistung {} anteilig".format(invoiceNumber or transfer.id),
        "Belegfeld 1": transfer.id,
      })

    elif tx["reporting_category"] == "fee":
      records.append({
        "date": created,
        "Umsatz (ohne Soll/Haben-Kz)": output.formatDecimal(-amount),
        "Soll/Haben-Kennzeichen": "S",
        "WKZ Umsatz": "EUR",
        "Konto": str(config.accounts["stripe_fees"]),
        "Gegenkonto (ohne BU-Schlüssel)": str(config.accounts["bank"]),
        "Buchungstext": tx.description or "Stripe Fee",
        # Stripe invoices fees within the bounds of one UTC month,
        # this makes it easier to associate a fee with a montly invoice
        "Belegfeld 1": created.astimezone(timezone.utc).strftime("%Y-%m"),
      })

    elif tx["reporting_category"] == "payout_minimum_balance_hold" or tx["reporting_category"] == "payout_minimum_balance_release":
      # Not relevant for accounting on the company side
      pass

    else:
      print(
        "Warning: unsupported balance transaction type:", tx["type"], "reporting_category:", tx["reporting_category"], tx["id"])

  return records


def extractCharges(balance_transactions):
  charges = []
  for tx in balance_transactions:
    if tx["type"] == "charge" or tx["type"] == "payment":
      charges.append(tx.source)

  return charges
