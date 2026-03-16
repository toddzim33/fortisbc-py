"""FortisBC web portal scraper.

Auth flow (confirmed from HAR 2026-03-16):
  1. POST login_standalone.fcc with User/Password/target/smagentname/Login — NO prior GET
     smagentname is static (JS-rendered on www.fortisbc.com, confirmed from HAR)
     Username is the FortisBC account username (NOT email address)
  2. SiteMinder → 302 /protected → 302 www.fortisbc.com/ — session lands here
  3. Lazy SAML: GET account_summary.xhtml → SAML2 → AOConsumerService → account_summary

Data flow (per account):
  4. GET  account_summary.xhtml     → extract ViewState + account link IDs
  5. POST account_summary.xhtml     → select account (gas or electric)
  6. GET  account_details.xhtml     → extract ViewState
  7. POST account_details.xhtml     → navigate to consumption history
  8. GET  consumtionHis.xhtml       → extract ViewState + hidden params + AJAX trigger IDs
  9. POST consumtionHis.xhtml?javax.portlet.faces.DirectLink=true → AJAX: billing period data
"""
import logging
import re
from calendar import monthrange
from datetime import date, datetime
from typing import Optional

from curl_cffi.requests import Session as CurlSession
from bs4 import BeautifulSoup

from .exceptions import FortisbcAuthError, FortisbcError
from .models import BillingPeriod, ElectricAccount, GasAccount

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://accounts.fortisbc.com/hcl-axon.com~iem~cssweb"
LOGIN_URL = "https://ciam.fortisbc.com/siteminderagent/forms/login_standalone.fcc"
LOGIN_TARGET = "https://ciam.fortisbc.com/protected"
SMAGENTNAME = "agent_accessgateway_sovmprdcamagp01"  # static; JS-rendered on www.fortisbc.com

ACCOUNT_SUMMARY_URL = f"{BASE_URL}/pages/account/account_summary.xhtml"
ACCOUNT_DETAILS_URL = f"{BASE_URL}/pages/account/account_details.xhtml"
CONSUMPTION_URL = f"{BASE_URL}/pages/account/consumtionHis.xhtml"
CONSUMPTION_AJAX_URL = f"{CONSUMPTION_URL}?javax.portlet.faces.DirectLink=true"


class FortisbcClient:
    """Scrapes FortisBC MyAccount portal for usage data.

    Usage:
        client = FortisbcClient("user@example.com", "password")
        await client.login()
        accounts = await client.fetch_all()
    """

    def __init__(self, username: str, password: str) -> None:
        self._username = username
        self._password = password
        # impersonate="chrome110" gives us a real Chrome TLS fingerprint —
        # SiteMinder rejects Python's default SSL ClientHello as a bot
        self._session = CurlSession(impersonate="chrome110")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def login(self) -> None:
        """Authenticate with FortisBC portal.

        Cold POST to login_standalone.fcc — no prior GET needed, no cookies required.
        smagentname is static (JS-rendered on www.fortisbc.com; confirmed from HAR).
        Username is the FortisBC account username, NOT an email address.

        On success, session lands on www.fortisbc.com/ with SiteMinder cookies set.
        The SAML handshake to accounts.fortisbc.com completes lazily when
        _get_account_summary() makes its first GET.
        """
        resp = self._session.post(
            LOGIN_URL,
            data={
                "User": self._username,
                "Password": self._password,
                "target": LOGIN_TARGET,
                "smagentname": SMAGENTNAME,
                "Login": "Log in",
            },
            headers={
                "Origin": "https://www.fortisbc.com",
                "Referer": "https://www.fortisbc.com/",
            },
            allow_redirects=True,
        )
        _LOGGER.debug("Post-login URL: %s  status: %s", resp.url, resp.status_code)

        # Success: SiteMinder redirects to www.fortisbc.com/ after auth
        # Failure: stays on ciam.fortisbc.com (login page re-displayed)
        if "ciam.fortisbc.com" in resp.url:
            raise FortisbcAuthError(
                f"Login failed — still on CIAM after POST: {resp.url}"
            )

    def fetch_all(self) -> dict:
        """Fetch data for all accounts. Returns dict with 'gas' and 'electric' keys."""
        result = {"gas": None, "electric": []}

        summary_html = self._get_account_summary()
        soup = BeautifulSoup(summary_html, "html.parser")
        view_state = self._extract_view_state(soup)

        gas_link = self._find_account_link(soup, "GAS")
        electric_links = self._find_all_electric_links(soup)

        if gas_link:
            result["gas"] = self._fetch_gas_account(gas_link, view_state)

        for link_id in electric_links:
            account = self._fetch_electric_account(link_id, view_state)
            if account:
                result["electric"].append(account)

        return result

    # ------------------------------------------------------------------
    # Internal: auth
    # ------------------------------------------------------------------

    def _complete_saml_if_needed(self, resp) -> "requests.Response":
        """If the response is a SAML assertion page, POST it to complete the handshake.

        The SAML intermediate page is a 200 OK with a JS auto-submit form containing
        SAMLResponse + RelayState. Browsers execute the JS; we must POST manually.
        """
        from urllib.parse import urljoin
        soup = BeautifulSoup(resp.text, "html.parser")
        saml_input = soup.find("input", {"name": "SAMLResponse"})
        if not saml_input:
            return resp
        form = soup.find("form")
        action = form.get("action") if form else None
        if not action:
            _LOGGER.warning("SAML page found but no form action; cannot complete handshake")
            return resp
        action = urljoin(resp.url, action)
        form_data = {
            inp["name"]: inp.get("value", "")
            for inp in soup.find_all("input")
            if inp.get("name")
        }
        _LOGGER.debug("Completing SAML handshake → %s", action)
        return self._session.post(action, data=form_data, allow_redirects=True)

    # ------------------------------------------------------------------
    # Internal: navigation
    # ------------------------------------------------------------------

    def _get_account_summary(self) -> str:
        resp = self._session.get(ACCOUNT_SUMMARY_URL, allow_redirects=True)
        resp = self._complete_saml_if_needed(resp)
        html = resp.text
        # Dismiss "Link accounts now" registration dialog if present
        soup = BeautifulSoup(html, "html.parser")
        if soup.find("input", {"name": "regnLink1Form"}):
            vs = self._extract_view_state(soup)
            _LOGGER.debug("Dismissing account-linking dialog")
            resp = self._session.post(
                ACCOUNT_SUMMARY_URL,
                data={
                    "regnLink1Form": "regnLink1Form",
                    "javax.faces.ViewState": vs,
                    "regnLink1Form:j_id133": "No, Thanks",
                },
                allow_redirects=True,
            )
            html = resp.text
        return html

    def _select_account(self, link_id: str, view_state: str) -> str:
        """POST to account_summary to select an account. Returns account_details HTML."""
        resp = self._session.post(
            ACCOUNT_SUMMARY_URL,
            data={
                "account_summary": "account_summary",
                "javax.faces.ViewState": view_state,
                link_id: link_id,
            },
            allow_redirects=True,
        )
        return resp.text

    def _navigate_to_consumption(self, view_state: str, is_electric: bool = True) -> str:
        """POST account_details to navigate to consumption history page."""
        data = {
            "graph1": "graph1",
            "graph1:arrHist": "View consumption history",
            "javax.faces.ViewState": view_state,
        }
        if is_electric:
            # Electric accounts need these NET flags
            data["graph1:customerOnNETTrueFalse11"] = "false"
            data["graph1:customerOnNETTrueFalse21"] = "false"

        resp = self._session.post(
            ACCOUNT_DETAILS_URL,
            data=data,
            allow_redirects=True,
        )
        return resp.text

    def _get_consumption_page(self) -> str:
        resp = self._session.get(CONSUMPTION_URL, allow_redirects=True)
        return resp.text

    # ------------------------------------------------------------------
    # Internal: account fetchers
    # ------------------------------------------------------------------

    def _fetch_gas_account(self, link_id: str, summary_view_state: str) -> Optional[GasAccount]:
        """Full navigation flow for a gas account."""
        try:
            details_html = self._select_account(link_id, summary_view_state)
            details_soup = BeautifulSoup(details_html, "html.parser")
            details_vs = self._extract_view_state(details_soup)

            self._navigate_to_consumption(details_vs, is_electric=False)
            consumption_html = self._get_consumption_page()
            consumption_soup = BeautifulSoup(consumption_html, "html.parser")
            consumption_vs = self._extract_view_state(consumption_soup)

            return self._parse_gas_account(consumption_soup, consumption_vs)
        except Exception:
            _LOGGER.exception("Failed to fetch gas account")
            return None

    def _fetch_electric_account(self, link_id: str, summary_view_state: str) -> Optional[ElectricAccount]:
        """Full navigation flow for an electric account."""
        try:
            details_html = self._select_account(link_id, summary_view_state)
            details_soup = BeautifulSoup(details_html, "html.parser")
            details_vs = self._extract_view_state(details_soup)

            self._navigate_to_consumption(details_vs, is_electric=True)
            consumption_html = self._get_consumption_page()
            consumption_soup = BeautifulSoup(consumption_html, "html.parser")

            return self._parse_electric_account(consumption_soup, link_id, details_vs)
        except Exception:
            _LOGGER.exception("Failed to fetch electric account %s", link_id)
            return None

    # ------------------------------------------------------------------
    # Internal: parsing
    # ------------------------------------------------------------------

    def _parse_gas_account(self, soup: BeautifulSoup, view_state: str) -> Optional[GasAccount]:
        """Trigger gas AJAX and parse the monthly billing table."""
        suffix = self._detect_consumption_suffix(soup)
        if not suffix:
            _LOGGER.warning("No consumption table found on gas page")
            return None
        trigger_id = self._find_ajax_trigger(soup, suffix)
        if not trigger_id:
            _LOGGER.warning("No AJAX trigger found for gas suffix %s", suffix)
            return None
        ajax_html = self._fetch_gas_ajax(suffix, view_state, trigger_id)
        if ajax_html:
            ajax_soup = BeautifulSoup(ajax_html, "html.parser")
            periods = self._parse_monthly_table(ajax_soup, unit="GJ")
        else:
            # Fall back to parsing whatever is already on the page
            periods = self._parse_monthly_table(soup, unit="GJ")
        if not periods:
            return None
        # FIXME: sa_id/account_id/customer_id/premise_address need live gas account debug
        return GasAccount(
            sa_id="",
            account_id="",
            customer_id="",
            premise_address="",
            billing_periods=periods,
        )

    def _parse_electric_account(
        self,
        soup: BeautifulSoup,
        link_id: str,
        view_state: str,
    ) -> Optional[ElectricAccount]:
        """Parse electric consumption history page via AJAX."""
        suffix = self._detect_consumption_suffix(soup)
        if not suffix:
            return None

        hidden = self._extract_electric_hidden_params(soup, suffix)
        trigger_id = self._find_ajax_trigger(soup, suffix)
        if not trigger_id:
            return None

        ajax_html = self._fetch_electric_ajax(soup, suffix, hidden, trigger_id, view_state)
        if not ajax_html:
            return None

        ajax_soup = BeautifulSoup(ajax_html, "html.parser")
        periods = self._parse_monthly_table(ajax_soup, unit="kWh")
        cdata = self._extract_cdata_json(ajax_html, suffix)

        return ElectricAccount(
            sa_id=hidden.get("c", ""),
            account_id=hidden.get("e", ""),
            customer_id=hidden.get("d", ""),
            meter_id=cdata.get("meterId", ""),
            service_point_id=cdata.get("servicePointId", ""),
            premise_address=cdata.get("premiseAddr", ""),
            rate_id=cdata.get("rateId", ""),
            hourly_available=cdata.get("hourlyDataAvailable", False),
            billing_periods=periods,
        )

    def _fetch_electric_ajax(
        self,
        consumption_soup: BeautifulSoup,
        suffix: str,
        hidden: dict,
        trigger_id: str,
        view_state: str,
    ) -> Optional[str]:
        """POST the Ajax4JSF request for electric consumption data."""
        # Build the full form body — must include all fields present on the page
        data = {
            "AJAXREQUEST": "_viewRoot",
            "consumptionHistory": "consumptionHistory",
            "consumptionHistory:DElRECVALl": "DEL",
            f"consumptionHistory:selectDateRangeEle{suffix}": "-6",
            f"consumptionHistory:ShowPasswordEle{suffix}": "on",
            f"consumptionHistory:dateFromElectric{suffix}": "",
            f"consumptionHistory:dateToElectric{suffix}": "",
            f"consumptionHistory:ShowPasswordEleCustom{suffix}": "on",
            f"consumptionHistory:customerOnNETTrueFalse{suffix}": "false",
            f"consumptionHistory:customerOnNETNew2{suffix}": "false",
            f"consumptionHistory:selectFileTypeEle{suffix}": "1",
            "javax.faces.ViewState": view_state,
            f"c{suffix}": hidden.get("c", ""),
            f"d{suffix}": hidden.get("d", ""),
            f"e{suffix}": hidden.get("e", ""),
            f"f{suffix}": hidden.get("f", "M"),
            f"g{suffix}": hidden.get("g", "0"),
            f"h{suffix}": hidden.get("h", "false"),
            f"param3{suffix}": hidden.get("param3", "ABC"),
            f"param4{suffix}": hidden.get("param4", "undefined"),
            trigger_id: trigger_id,
        }
        resp = self._session.post(CONSUMPTION_AJAX_URL, data=data)
        if resp.status_code == 200 and resp.text:
            return resp.text
        return None

    # ------------------------------------------------------------------
    # Internal: HTML helpers
    # ------------------------------------------------------------------

    def _extract_view_state(self, soup: BeautifulSoup) -> str:
        field = soup.find("input", {"name": "javax.faces.ViewState"})
        return field["value"] if field else ""

    def _find_account_link(self, soup: BeautifulSoup, account_type: str) -> Optional[str]:
        """Find the first account link ID for the given type (GAS or Electric).

        Account links are JSF commandLink <a> elements, not <input> elements.
        IDs like: account_summary:acctSummaryGASCmdLnkActNum1
        """
        pattern = re.compile(rf"acctSummary{account_type}CmdLnkActNum\d+$", re.IGNORECASE)
        el = soup.find("a", {"id": pattern})
        return el.get("id", "") if el else None

    def _find_all_electric_links(self, soup: BeautifulSoup) -> list[str]:
        pattern = re.compile(r"acctSummaryElectricCmdLnkActNum\d+$", re.IGNORECASE)
        return [el.get("id", "") for el in soup.find_all("a", {"id": pattern})]

    def _detect_consumption_suffix(self, soup: BeautifulSoup) -> Optional[str]:
        """Find the first consumptionHistory:conspdt{X} table and return its suffix."""
        table = soup.find("table", id=re.compile(r"consumptionHistory:conspdt\w+"))
        if table:
            m = re.search(r":conspdt(\w+)$", table.get("id", ""))
            return m.group(1) if m else None
        return None

    def _extract_electric_hidden_params(self, soup: BeautifulSoup, suffix: str) -> dict:
        """Extract hidden form fields c##, d##, e##, f##, g##, h## from consumption page."""
        result = {}
        for key in ["c", "d", "e", "f", "g", "h", "param3", "param4"]:
            field_name = f"{key}{suffix}"
            el = soup.find("input", {"name": field_name})
            if el:
                result[key] = el.get("value", "")
        return result

    def _find_ajax_trigger(self, soup: BeautifulSoup, suffix: str) -> Optional[str]:
        """Find the AJAX trigger ID for this suffix.

        The trigger is a <script id="consumptionHistory:j_id<N>"> element whose
        body contains an A4J.AJAX.Submit call referencing the suffix. The script's
        id attribute is then included as a POST parameter to identify the action.
        """
        for el in soup.find_all("script", id=re.compile(r"^consumptionHistory:j_id")):
            content = el.string or ""
            if suffix in content and "A4J.AJAX.Submit" in content:
                return el.get("id", "")
        return None

    def _parse_billing_table(
        self,
        soup: BeautifulSoup,
        unit: str,
        has_temperature: bool = False,
    ) -> list[BillingPeriod]:
        """Parse billing period table from page or AJAX response HTML."""
        periods = []
        # Tables have class "table table-bordered"
        tables = soup.find_all("table", class_="table-bordered")
        for table in tables:
            rows = table.find("tbody").find_all("tr") if table.find("tbody") else []
            for row in rows:
                cells = [td.get_text(strip=True) for td in row.find_all("td")]
                if len(cells) < 4:
                    continue
                try:
                    start = _parse_date(cells[0])
                    end = _parse_date(cells[1])
                    days = int(cells[2])
                    usage = float(cells[3].replace(",", ""))
                    temp = float(cells[4]) if has_temperature and len(cells) > 4 else None
                    periods.append(BillingPeriod(
                        start_date=start,
                        end_date=end,
                        days=days,
                        usage=usage,
                        usage_unit=unit,
                        avg_temperature=temp,
                    ))
                except (ValueError, IndexError):
                    continue
        return periods

    def _fetch_gas_ajax(self, suffix: str, view_state: str, trigger_id: str) -> Optional[str]:
        """POST the Ajax4JSF request for gas consumption data.

        Gas uses selectDateRange3{S}/dateFrom2{S}/dateTo2{S} field names,
        distinct from the electric selectDateRangeEle{S} fields.
        """
        data = {
            "AJAXREQUEST": "_viewRoot",
            "consumptionHistory": "consumptionHistory",
            "consumptionHistory:DElRECVALl": "DEL",
            f"consumptionHistory:selectDateRange3{suffix}": "-6",
            f"consumptionHistory:dateFrom2{suffix}": "",
            f"consumptionHistory:dateTo2{suffix}": "",
            f"consumptionHistory:selectFileTypeGas2{suffix}": "1",
            "javax.faces.ViewState": view_state,
            f"param1{suffix}": "",
            f"param2{suffix}": "",
            f"param3{suffix}": "ABC",
            trigger_id: trigger_id,
        }
        resp = self._session.post(CONSUMPTION_AJAX_URL, data=data)
        if resp.status_code == 200 and resp.text:
            return resp.text
        return None

    def _parse_monthly_table(self, soup: BeautifulSoup, unit: str) -> list[BillingPeriod]:
        """Parse monthly usage table: 'Mon YYYY' + usage value.

        Both gas (GJ) and electric (kWh) portals use this 2-column format.
        Dates are approximate: first and last day of the named month.
        """
        periods = []
        tables = soup.find_all("table", class_="table-bordered")
        for table in tables:
            rows = table.find("tbody").find_all("tr") if table.find("tbody") else []
            for row in rows:
                cells = [td.get_text(strip=True) for td in row.find_all("td")]
                if len(cells) < 2:
                    continue
                try:
                    for fmt in ("%B %Y", "%b %Y"):
                        try:
                            dt = datetime.strptime(cells[0], fmt)
                            break
                        except ValueError:
                            continue
                    else:
                        continue
                    start = date(dt.year, dt.month, 1)
                    last_day = monthrange(dt.year, dt.month)[1]
                    end = date(dt.year, dt.month, last_day)
                    usage = float(cells[1].replace(",", ""))
                    periods.append(BillingPeriod(
                        start_date=start,
                        end_date=end,
                        days=last_day,
                        usage=usage,
                        usage_unit=unit,
                    ))
                except (ValueError, IndexError):
                    continue
        return periods

    def _extract_cdata_json(self, xml_text: str, suffix: str) -> dict:
        """Extract key fields from the JSON-like CDATA blob in the AJAX response."""
        result = {}
        # The CDATA contains JS object literal (not valid JSON, uses single quotes)
        # Extract specific fields via regex
        patterns = {
            "meterId": rf"'meterId':'(\d+)'",
            "servicePointId": rf"'servicePointId':'(\d+)'",
            "premiseAddr": rf"'premiseAddr':'([^']+)'",
            "rateId": rf"'rateId':'(\w+)'",
            "hourlyDataAvailable": rf"'hourlyDataAvailable':(true|false)",
        }
        for key, pattern in patterns.items():
            m = re.search(pattern, xml_text)
            if m:
                val = m.group(1)
                if val == "true":
                    val = True
                elif val == "false":
                    val = False
                result[key] = val
        return result


def _parse_date(s: str) -> date:
    """Parse DD/MM/YYYY date string."""
    s = s.strip()
    day, month, year = s.split("/")
    return date(int(year), int(month), int(day))
