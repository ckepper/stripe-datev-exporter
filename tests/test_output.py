from stripe_datev import output
import unittest
import datetime


class AccountingMonthTest(unittest.TestCase):

  # A record is filed into a monthly Buchungsstapel by accountingMonth() but carries a
  # Belegdatum from formatDateDatev(). Both must resolve in the accounting timezone, or
  # a charge created late in the evening UTC lands in the wrong month's file.

  def test_late_evening_utc_belongs_to_next_accounting_month(self):
    # 2025-07-31 22:17:48 UTC is 2025-08-01 00:17:48 in Europe/Berlin
    date = datetime.datetime(2025, 7, 31, 22, 17, 48,
                             tzinfo=datetime.timezone.utc)

    self.assertEqual(output.accountingMonth(date), "2025-08")
    self.assertEqual(output.formatDateDatev(date), "0108")

  def test_year_boundary(self):
    # 2025-12-31 23:30 UTC is 2026-01-01 00:30 in Europe/Berlin
    date = datetime.datetime(2025, 12, 31, 23, 30,
                             tzinfo=datetime.timezone.utc)

    self.assertEqual(output.accountingMonth(date), "2026-01")
    self.assertEqual(output.formatDateDatev(date), "0101")

  def test_agrees_with_belegdatum_across_a_full_day(self):
    for hour in range(24):
      date = datetime.datetime(2026, 3, 15, hour,
                               tzinfo=datetime.timezone.utc)
      month = output.accountingMonth(date)
      beleg = output.formatDateDatev(date)

      self.assertEqual(month[5:7], beleg[2:4],
                       "month {} disagrees with Belegdatum {} at {}Z".format(month, beleg, hour))
