"""Data models for FortisBC usage data."""
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass
class BillingPeriod:
    """A single billing period's usage data."""
    start_date: date
    end_date: date
    days: int
    usage: float          # kWh for electric, GJ for gas
    usage_unit: str       # "kWh" or "GJ"
    avg_temperature: Optional[float] = None  # gas only
    cost: Optional[float] = None             # CAD, from totAmntDue (electric only for now)


@dataclass
class ElectricAccount:
    """An electricity service account."""
    sa_id: str            # service agreement ID
    account_id: str
    customer_id: str
    meter_id: str
    service_point_id: str
    premise_address: str
    rate_id: str
    hourly_available: bool = False
    billing_periods: list[BillingPeriod] = field(default_factory=list)

    @property
    def current_period(self) -> Optional[BillingPeriod]:
        return self.billing_periods[0] if self.billing_periods else None


@dataclass
class GasAccount:
    """A natural gas service account."""
    sa_id: str
    account_id: str
    customer_id: str
    premise_address: str
    billing_periods: list[BillingPeriod] = field(default_factory=list)

    @property
    def current_period(self) -> Optional[BillingPeriod]:
        return self.billing_periods[0] if self.billing_periods else None
