"""Basic model tests."""
from datetime import date
from fortisbc.models import BillingPeriod, ElectricAccount, GasAccount


def test_billing_period():
    p = BillingPeriod(
        start_date=date(2026, 2, 8),
        end_date=date(2026, 3, 9),
        days=29,
        usage=1127.0,
        usage_unit="kWh",
        amount_due=209.49,
    )
    assert p.usage == 1127.0
    assert p.usage_unit == "kWh"
    assert p.days == 29


def test_electric_account_current_period():
    period = BillingPeriod(
        start_date=date(2026, 2, 8),
        end_date=date(2026, 3, 9),
        days=29,
        usage=1127.0,
        usage_unit="kWh",
    )
    account = ElectricAccount(
        sa_id="6533041171",
        account_id="6637766963",
        customer_id="507599774",
        meter_id="6170243",
        service_point_id="54846797",
        premise_address="1028 BULL CRES KELOWNA BC",
        rate_id="RS01A",
        billing_periods=[period],
    )
    assert account.current_period == period


def test_gas_account_no_periods():
    account = GasAccount(
        sa_id="",
        account_id="",
        customer_id="",
        premise_address="",
    )
    assert account.current_period is None
