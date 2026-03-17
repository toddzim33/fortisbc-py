"""Tests for HTML/CDATA parsing — no network access required.

All tests operate on saved fixture responses from the live portal so they
catch regressions when FortisBC adjusts their portal markup.
"""
from datetime import date

import pytest
from bs4 import BeautifulSoup

from fortisbc.api import FortisbcClient, _parse_date
from fortisbc.models import BillingPeriod, ElectricAccount, GasAccount


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _client() -> FortisbcClient:
    """Return a client instance without opening a real session.

    Credentials are irrelevant for parsing-only tests; we never call login()
    or any method that touches the network.
    """
    return FortisbcClient("testuser", "testpass")


# ---------------------------------------------------------------------------
# Electric CDATA parsing
# ---------------------------------------------------------------------------

class TestParseCdataBilling:

    def test_returns_two_service_agreements(self, electric_ajax_html):
        """CDATA contains two SAs (main unit + Leah's suite) — both parsed."""
        client = _client()
        accounts = client._parse_cdata_billing(electric_ajax_html)
        assert len(accounts) == 2

    def test_sa_ids_are_correct(self, electric_ajax_html):
        client = _client()
        accounts = client._parse_cdata_billing(electric_ajax_html)
        sa_ids = {a.sa_id for a in accounts}
        assert "6533041171" in sa_ids  # main unit
        assert "5188612185" in sa_ids  # Leah's suite

    def test_premise_addresses_populated(self, electric_ajax_html):
        client = _client()
        accounts = client._parse_cdata_billing(electric_ajax_html)
        addresses = {a.premise_address for a in accounts}
        assert "1028 BULL CRES KELOWNA BC" in addresses
        assert "1028 BULL CRES 2 KELOWNA BC" in addresses

    def test_billing_periods_have_real_dates(self, electric_ajax_html):
        """Dates come from segStartDt/segEndDt — not month approximations."""
        client = _client()
        accounts = client._parse_cdata_billing(electric_ajax_html)
        suite = next(a for a in accounts if a.sa_id == "5188612185")
        current = suite.current_period
        # Most recent period: Feb 8 → Mar 9 2026
        assert current.start_date == date(2026, 2, 8)
        assert current.end_date == date(2026, 3, 9)
        assert current.days == 29

    def test_billing_periods_have_cost(self, electric_ajax_html):
        """totAmntDue is extracted and stored as cost."""
        client = _client()
        accounts = client._parse_cdata_billing(electric_ajax_html)
        suite = next(a for a in accounts if a.sa_id == "5188612185")
        current = suite.current_period
        assert current.cost == pytest.approx(111.66)

    def test_billing_periods_have_usage(self, electric_ajax_html):
        client = _client()
        accounts = client._parse_cdata_billing(electric_ajax_html)
        suite = next(a for a in accounts if a.sa_id == "5188612185")
        current = suite.current_period
        assert current.usage == pytest.approx(1653.0)
        assert current.usage_unit == "kWh"

    def test_periods_sorted_most_recent_first(self, electric_ajax_html):
        client = _client()
        accounts = client._parse_cdata_billing(electric_ajax_html)
        for account in accounts:
            dates = [p.start_date for p in account.billing_periods]
            assert dates == sorted(dates, reverse=True), \
                f"Periods not sorted for SA {account.sa_id}"

    def test_returns_empty_list_on_missing_cdata(self):
        client = _client()
        result = client._parse_cdata_billing("<html><body>no cdata here</body></html>")
        assert result == []

    def test_rate_id_populated(self, electric_ajax_html):
        client = _client()
        accounts = client._parse_cdata_billing(electric_ajax_html)
        for account in accounts:
            assert account.rate_id == "RS01A"


# ---------------------------------------------------------------------------
# Gas monthly table parsing
# ---------------------------------------------------------------------------

class TestParseMonthlyTableGas:

    def test_returns_six_periods(self, gas_ajax_html):
        client = _client()
        soup = BeautifulSoup(gas_ajax_html, "html.parser")
        periods = client._parse_monthly_table(soup, unit="GJ")
        assert len(periods) == 6

    def test_usage_values_correct(self, gas_ajax_html):
        client = _client()
        soup = BeautifulSoup(gas_ajax_html, "html.parser")
        periods = client._parse_monthly_table(soup, unit="GJ")
        usages = [p.usage for p in periods]
        assert 10.3 in usages   # February 2026
        assert 10.6 in usages   # January 2026
        assert 8.5 in usages    # December 2025

    def test_unit_is_gj(self, gas_ajax_html):
        client = _client()
        soup = BeautifulSoup(gas_ajax_html, "html.parser")
        periods = client._parse_monthly_table(soup, unit="GJ")
        assert all(p.usage_unit == "GJ" for p in periods)

    def test_dates_approximated_to_month_boundaries(self, gas_ajax_html):
        """Gas portal only provides month name — dates are first/last of month."""
        client = _client()
        soup = BeautifulSoup(gas_ajax_html, "html.parser")
        periods = client._parse_monthly_table(soup, unit="GJ")
        feb = next(p for p in periods if p.start_date.month == 2 and p.start_date.year == 2026)
        assert feb.start_date == date(2026, 2, 1)
        assert feb.end_date == date(2026, 2, 28)

    def test_handles_full_month_names(self, gas_ajax_html):
        """Gas table uses full month names ('February 2026'), not abbreviated."""
        client = _client()
        soup = BeautifulSoup(gas_ajax_html, "html.parser")
        periods = client._parse_monthly_table(soup, unit="GJ")
        assert len(periods) > 0  # Would be 0 if %B format not handled

    def test_handles_abbreviated_month_names(self, electric_ajax_html):
        """Electric table uses abbreviated names ('Mar 2026')."""
        client = _client()
        soup = BeautifulSoup(electric_ajax_html, "html.parser")
        periods = client._parse_monthly_table(soup, unit="kWh")
        assert len(periods) > 0


# ---------------------------------------------------------------------------
# Account summary parsing
# ---------------------------------------------------------------------------

class TestAccountSummaryParsing:

    def test_finds_gas_link(self, account_summary_soup):
        client = _client()
        link = client._find_account_link(account_summary_soup, "GAS")
        assert link is not None
        assert "GAS" in link.upper()

    def test_finds_electric_links(self, account_summary_soup):
        client = _client()
        links = client._find_all_electric_links(account_summary_soup)
        assert len(links) >= 1
        assert all("Electric" in l or "electric" in l.lower() for l in links)

    def test_extracts_view_state(self, account_summary_soup):
        client = _client()
        vs = client._extract_view_state(account_summary_soup)
        assert vs  # non-empty
        assert len(vs) > 4  # real ViewState tokens are long

    def test_view_state_raises_on_missing(self):
        client = _client()
        empty_soup = BeautifulSoup("<html><body></body></html>", "html.parser")
        with pytest.raises(Exception, match="ViewState"):
            client._extract_view_state(empty_soup)


# ---------------------------------------------------------------------------
# Consumption page parsing
# ---------------------------------------------------------------------------

class TestConsumptionPageParsing:

    def test_detects_suffix(self, consumption_page_soup):
        client = _client()
        suffix = client._detect_consumption_suffix(consumption_page_soup)
        assert suffix is not None
        assert suffix.isdigit()

    def test_finds_ajax_trigger(self, consumption_page_soup):
        client = _client()
        suffix = client._detect_consumption_suffix(consumption_page_soup)
        trigger = client._find_ajax_trigger(consumption_page_soup, suffix)
        assert trigger is not None
        assert trigger.startswith("consumptionHistory:j_id")

    def test_ajax_trigger_references_suffix(self, consumption_page_soup):
        """The trigger script element must contain the suffix — wrong trigger = wrong data."""
        client = _client()
        suffix = client._detect_consumption_suffix(consumption_page_soup)
        trigger = client._find_ajax_trigger(consumption_page_soup, suffix)
        # Verify by finding the script and checking its content
        script = consumption_page_soup.find("script", {"id": trigger})
        assert script is not None
        assert suffix in (script.string or "")


# ---------------------------------------------------------------------------
# SAML detection
# ---------------------------------------------------------------------------

class TestSamlDetection:

    SAML_PAGE = """
    <html><body>
    <form action="https://accounts.fortisbc.com/ACS">
      <input name="SAMLResponse" value="PHNhbWxwOlJlc3BvbnNl..." />
      <input name="RelayState" value="/pages/account/account_summary.xhtml" />
    </form>
    <script>document.forms[0].submit();</script>
    </body></html>
    """

    NON_SAML_PAGE = """
    <html><body><p>Regular portal page</p></body></html>
    """

    def test_detects_saml_page(self):
        """SAML pages have an input[name=SAMLResponse]."""
        soup = BeautifulSoup(self.SAML_PAGE, "html.parser")
        assert soup.find("input", {"name": "SAMLResponse"}) is not None

    def test_non_saml_page_passes_through(self):
        soup = BeautifulSoup(self.NON_SAML_PAGE, "html.parser")
        assert soup.find("input", {"name": "SAMLResponse"}) is None


# ---------------------------------------------------------------------------
# Billing history parsing
# ---------------------------------------------------------------------------

class TestParseBillingHistory:

    def test_returns_five_bill_rows(self, gas_billing_history_soup):
        """Fixture has 5 Bill entries; Payments and Late charges are skipped."""
        client = _client()
        results = client._parse_billing_history(gas_billing_history_soup)
        assert len(results) == 5

    def test_most_recent_bill_amount(self, gas_billing_history_soup):
        """Jan 14 – Feb 11, 2026 bill: $157.54."""
        client = _client()
        results = client._parse_billing_history(gas_billing_history_soup)
        start, end, amount = results[0]
        assert amount == pytest.approx(157.54)

    def test_most_recent_bill_dates(self, gas_billing_history_soup):
        """Jan 14 – Feb 11, 2026: start and end dates extracted correctly."""
        client = _client()
        results = client._parse_billing_history(gas_billing_history_soup)
        start, end, _ = results[0]
        assert start == date(2026, 1, 14)
        assert end == date(2026, 2, 11)

    def test_year_boundary_bill_dates(self, gas_billing_history_soup):
        """Dec 12 – Jan 13, 2026: start year is 2025 (start month > end month)."""
        client = _client()
        results = client._parse_billing_history(gas_billing_history_soup)
        start, end, amount = results[1]
        assert start == date(2025, 12, 12)
        assert end == date(2026, 1, 13)
        assert amount == pytest.approx(153.43)

    def test_payment_rows_excluded(self, gas_billing_history_soup):
        """Payments (e.g. Feb 16, 2026 for -$157.54) must not appear in results."""
        client = _client()
        results = client._parse_billing_history(gas_billing_history_soup)
        # All amounts should be positive (bills only)
        assert all(amount > 0 for _, _, amount in results)

    def test_late_charges_excluded(self, gas_billing_history_soup):
        """Late payment charges (Dec 11, 2025) should not appear in results."""
        client = _client()
        results = client._parse_billing_history(gas_billing_history_soup)
        # No single-date rows (they'd have no '-' date range)
        assert len(results) == 5  # only the 5 Bill rows

    def test_empty_on_missing_table(self):
        """Returns empty list if no table found."""
        from bs4 import BeautifulSoup
        client = _client()
        soup = BeautifulSoup("<html><body></body></html>", "html.parser")
        assert client._parse_billing_history(soup) == []


class TestApplyGasBillingCosts:

    def test_cost_applied_by_start_month(self):
        """Jan 14 billing period matches Jan 2026 consumption month."""
        client = _client()
        periods = [
            BillingPeriod(date(2026, 1, 1), date(2026, 1, 31), 31, 10.3, "GJ"),
            BillingPeriod(date(2025, 12, 1), date(2025, 12, 31), 31, 10.6, "GJ"),
        ]
        billing_costs = [
            (date(2026, 1, 14), date(2026, 2, 11), 157.54),
            (date(2025, 12, 12), date(2026, 1, 13), 153.43),
        ]
        result = client._apply_gas_billing_costs(periods, billing_costs)
        assert result[0].cost == pytest.approx(157.54)
        assert result[1].cost == pytest.approx(153.43)

    def test_unmatched_period_has_no_cost(self):
        """A consumption month with no matching billing entry keeps cost=None."""
        client = _client()
        periods = [
            BillingPeriod(date(2026, 3, 1), date(2026, 3, 31), 31, 5.0, "GJ"),
        ]
        billing_costs = [
            (date(2026, 1, 14), date(2026, 2, 11), 157.54),
        ]
        result = client._apply_gas_billing_costs(periods, billing_costs)
        assert result[0].cost is None

    def test_usage_preserved_after_cost_applied(self):
        """Original usage and unit are unchanged after cost merge."""
        client = _client()
        periods = [
            BillingPeriod(date(2026, 1, 1), date(2026, 1, 31), 31, 10.3, "GJ"),
        ]
        result = client._apply_gas_billing_costs(
            periods, [(date(2026, 1, 14), date(2026, 2, 11), 157.54)]
        )
        assert result[0].usage == pytest.approx(10.3)
        assert result[0].usage_unit == "GJ"


# ---------------------------------------------------------------------------
# Model behaviour
# ---------------------------------------------------------------------------

class TestModels:

    def test_current_period_returns_first(self):
        periods = [
            BillingPeriod(date(2026, 2, 8), date(2026, 3, 9), 29, 1653.0, "kWh", cost=111.66),
            BillingPeriod(date(2026, 1, 8), date(2026, 2, 8), 31, 1772.0, "kWh", cost=120.45),
        ]
        account = ElectricAccount(
            sa_id="123", account_id="", customer_id="",
            meter_id="", service_point_id="", premise_address="",
            rate_id="", billing_periods=periods,
        )
        assert account.current_period == periods[0]

    def test_current_period_returns_none_when_empty(self):
        account = ElectricAccount(
            sa_id="123", account_id="", customer_id="",
            meter_id="", service_point_id="", premise_address="",
            rate_id="", billing_periods=[],
        )
        assert account.current_period is None

    def test_billing_period_cost_optional(self):
        """cost defaults to None — gas periods have no cost data."""
        period = BillingPeriod(date(2026, 2, 1), date(2026, 2, 28), 28, 10.3, "GJ")
        assert period.cost is None
