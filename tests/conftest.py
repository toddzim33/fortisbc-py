"""Shared fixtures for fortisbc test suite."""
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def electric_ajax_html():
    return load_fixture("electric_ajax.html")


@pytest.fixture
def gas_ajax_html():
    return load_fixture("gas_ajax.html")


@pytest.fixture
def account_summary_html():
    return load_fixture("account_summary.html")


@pytest.fixture
def consumption_page_html():
    return load_fixture("consumption_page.html")


@pytest.fixture
def account_summary_soup(account_summary_html):
    return BeautifulSoup(account_summary_html, "html.parser")


@pytest.fixture
def consumption_page_soup(consumption_page_html):
    return BeautifulSoup(consumption_page_html, "html.parser")


@pytest.fixture
def gas_billing_history_html():
    return load_fixture("gas_billing_history.html")


@pytest.fixture
def gas_billing_history_soup(gas_billing_history_html):
    return BeautifulSoup(gas_billing_history_html, "html.parser")
