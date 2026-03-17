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

Note: "consumtion" is FortisBC's own typo in the URL — preserved faithfully.
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
        client = FortisbcClient("username", "password")
        try:
            client.login()
            data = client.fetch_all()
        finally:
            client.close()
    """

    def __init__(self, username: str, password: str) -> None:
        self._username = username
        self._password = password
        # impersonate="chrome110" gives us a real Chrome TLS fingerprint —
        # SiteMinder rejects Python's default SSL ClientHello as a bot
        self._session = CurlSession(impersonate="chrome110")

    def close(self) -> None:
        """Close the underlying TLS session and release resources."""
        self._session.close()

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
            # Re-fetch the summary page to get a fresh ViewState before electric accounts.
            # JSF server-side state advances after any account selection POST, so reusing
            # the original ViewState for the next account risks a "View Expired" error.
            if electric_links:
                summary_html = self._get_account_summary()
                soup = BeautifulSoup(summary_html, "html.parser")
                view_state = self._extract_view_state(soup)

        for link_id in electric_links:
            result["electric"].extend(self._fetch_electric_account(link_id, view_state))

        return result

    # ------------------------------------------------------------------
    # Internal: auth
    # ------------------------------------------------------------------

    def _complete_saml_if_needed(self, resp) -> "requests.Response":
        """Complete any pending SAML handshake hops after an initial GET.

        Enterprise SSO sometimes chains multiple intermediate redirects, each
        with a JS auto-submit form carrying SAMLResponse + RelayState.
        Browsers handle these silently; we POST each hop manually.
        Capped at 3 iterations to prevent infinite loops if SSO is broken.

        After all hops, verifies we have actually landed inside the
        hcl-axon portal — raises FortisbcError if the session is
        still stuck on a CIAM or error page.
        """
        from urllib.parse import urljoin, urlparse
        for _ in range(3):
            soup = BeautifulSoup(resp.text, "html.parser")
            saml_input = soup.find("input", {"name": "SAMLResponse"})
            if not saml_input:
                break
            form = soup.find("form")
            action = form.get("action") if form else None
            if not action:
                _LOGGER.warning("SAML page found but no form action; cannot complete handshake")
                break
            action = urljoin(resp.url, action)
            form_data = {
                inp["name"]: inp.get("value", "")
                for inp in form.find_all("input")
                if inp.get("name")
            }
            _LOGGER.debug("Completing SAML hop → %s", action)
            resp = self._session.post(action, data=form_data, allow_redirects=True)

        # Verify we have landed inside the portal after all SAML hops
        final_host = urlparse(resp.url).hostname or ""
        if "accounts.fortisbc.com" not in final_host:
            raise FortisbcError(
                f"SAML handshake did not reach the portal — landed on: {resp.url}"
            )
        return resp

    # ------------------------------------------------------------------
    # Internal: navigation
    # ------------------------------------------------------------------

    def _get_account_summary(self) -> str:
        resp = self._session.get(ACCOUNT_SUMMARY_URL, allow_redirects=True)
        resp = self._complete_saml_if_needed(resp)
        html = resp.text
        # Dismiss "Link accounts now" registration dialogue if present
        soup = BeautifulSoup(html, "html.parser")
        if soup.find("input", {"name": "regnLink1Form"}):
            vs = self._extract_view_state(soup)
            _LOGGER.debug("Dismissing account-linking dialogue")
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
            meta = self._extract_account_details_meta(details_soup)

            self._navigate_to_consumption(details_vs, is_electric=False)
            consumption_html = self._get_consumption_page()
            consumption_soup = BeautifulSoup(consumption_html, "html.parser")
            consumption_vs = self._extract_view_state(consumption_soup)

            return self._parse_gas_account(consumption_soup, consumption_vs, meta)
        except Exception:
            _LOGGER.exception("Failed to fetch gas account")
            return None

    def _fetch_electric_account(self, link_id: str, summary_view_state: str) -> list[ElectricAccount]:
        """Full navigation flow for an electric account. Returns one ElectricAccount per SA."""
        try:
            details_html = self._select_account(link_id, summary_view_state)
            details_soup = BeautifulSoup(details_html, "html.parser")
            details_vs = self._extract_view_state(details_soup)

            self._navigate_to_consumption(details_vs, is_electric=True)
            consumption_html = self._get_consumption_page()
            consumption_soup = BeautifulSoup(consumption_html, "html.parser")
            consumption_vs = self._extract_view_state(consumption_soup)

            suffix = self._detect_consumption_suffix(consumption_soup)
            if not suffix:
                return []
            hidden = self._extract_electric_hidden_params(consumption_soup, suffix)
            trigger_id = self._find_ajax_trigger(consumption_soup, suffix)
            if not trigger_id:
                return []

            ajax_html = self._fetch_electric_ajax(consumption_soup, suffix, hidden, trigger_id, consumption_vs)
            if not ajax_html:
                return []

            return self._parse_cdata_billing(ajax_html)
        except Exception:
            _LOGGER.exception("Failed to fetch electric account %s", link_id)
            return []

    # ------------------------------------------------------------------
    # Internal: parsing
    # ------------------------------------------------------------------

    def _parse_gas_account(
        self, soup: BeautifulSoup, view_state: str, meta: dict
    ) -> Optional[GasAccount]:
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
        return GasAccount(
            sa_id=meta.get("sa_id", ""),
            account_id=meta.get("account_id", ""),
            customer_id=meta.get("customer_id", ""),
            premise_address=meta.get("premise_address", ""),
            billing_periods=periods,
        )

    def _parse_cdata_billing(self, ajax_html: str) -> list[ElectricAccount]:
        """Parse the _ajax:data CDATA blob into ElectricAccount objects, one per SA.

        The CDATA contains consDetListCurrGraph — one entry per billing segment with
        real start/end dates (DD/MM/YYYY), cost (totAmntDue), and full account metadata.
        Segments are grouped by saId so each SA becomes its own ElectricAccount.
        """
        m = re.search(
            r'<span id="_ajax:data"><!\[CDATA\[(.*?)\]\]></span>', ajax_html, re.DOTALL
        )
        if not m:
            _LOGGER.warning("No _ajax:data CDATA found in electric AJAX response")
            return []
        blob = m.group(1)

        # Each entry in consDetListCurrGraph has an electricConsRetDTO with billing details.
        # Fields appear in consistent order; use DOTALL to match across whitespace.
        entry_pattern = re.compile(
            r"'electricConsRetDTO':\{"
            r"'accountId':'(?P<accountId>[^']+)'.*?"
            r"'custId':'(?P<custId>[^']+)'.*?"
            r"(?:'meterId':'(?P<meterId>[^']+)'.*?)?"
            r"'premiseAddr':'(?P<premiseAddr>[^']+)'.*?"
            r"'rateId':'(?P<rateId>[^']+)'.*?"
            r"'saId':'(?P<saId>[^']+)'.*?"
            r"(?:'servicePointId':'(?P<servicePointId>[^']+)'.*?)?"
            r"'segEndDt':'(?P<segEndDt>[^']+)'.*?"
            r"'segStartDt':'(?P<segStartDt>[^']+)'.*?"
            r"'totAmntDue':'(?P<totAmntDue>[^']+)'.*?"
            r"'usageQuantNumber':'(?P<usage>[^']+)'",
            re.DOTALL,
        )
        # Also grab hourlyDataAvailable from intDatConsDTO
        hourly_pattern = re.compile(r"'hourlyDataAvailable':(true|false)")
        hourly_flags = hourly_pattern.findall(blob)

        sa_periods: dict[str, list[BillingPeriod]] = {}
        sa_meta: dict[str, dict] = {}

        for i, match in enumerate(entry_pattern.finditer(blob)):
            d = match.groupdict()
            sa_id = d["saId"]
            try:
                start = _parse_date(d["segStartDt"])
                end = _parse_date(d["segEndDt"])
                usage = float(d["usage"].replace(",", ""))
                cost = float(d["totAmntDue"])
                days = (end - start).days
            except (ValueError, KeyError):
                continue

            if sa_id not in sa_meta:
                sa_meta[sa_id] = {
                    "account_id": d.get("accountId", ""),
                    "customer_id": d.get("custId", ""),
                    "premise_address": d.get("premiseAddr", ""),
                    "rate_id": d.get("rateId", ""),
                    "meter_id": d.get("meterId") or "",
                    "service_point_id": d.get("servicePointId") or "",
                    "hourly_available": (hourly_flags[i] == "true") if i < len(hourly_flags) else False,
                }
                sa_periods[sa_id] = []

            sa_periods[sa_id].append(BillingPeriod(
                start_date=start,
                end_date=end,
                days=days,
                usage=usage,
                usage_unit="kWh",
                cost=cost,
            ))

        accounts = []
        for sa_id, periods in sa_periods.items():
            periods.sort(key=lambda p: p.start_date, reverse=True)
            meta = sa_meta[sa_id]
            accounts.append(ElectricAccount(
                sa_id=sa_id,
                account_id=meta["account_id"],
                customer_id=meta["customer_id"],
                meter_id=meta["meter_id"],
                service_point_id=meta["service_point_id"],
                premise_address=meta["premise_address"],
                rate_id=meta["rate_id"],
                hourly_available=meta["hourly_available"],
                billing_periods=periods,
            ))
        return accounts

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
        resp = self._session.post(
            CONSUMPTION_AJAX_URL,
            data=data,
            headers={"Referer": CONSUMPTION_URL},
        )
        if resp.status_code == 200 and resp.text:
            return resp.text
        return None

    # ------------------------------------------------------------------
    # Internal: HTML helpers
    # ------------------------------------------------------------------

    def _extract_account_details_meta(self, soup: BeautifulSoup) -> dict:
        """Extract SA ID and premise address from the account_details page.

        JSF portals typically embed these in read-only inputs or labelled table cells.
        Best-effort — returns empty strings if fields are not found so callers
        can proceed without failing hard.

        Note: field names confirmed for electric; gas field names need live
        verification against account_details.xhtml for a gas account.
        """
        meta = {"sa_id": "", "account_id": "", "customer_id": "", "premise_address": ""}

        # Common patterns: hidden inputs or display spans with predictable IDs
        for candidate in ["graph1:saId", "graph1:serviceAgreementId", "saId"]:
            el = soup.find(attrs={"id": candidate}) or soup.find(attrs={"name": candidate})
            if el:
                meta["sa_id"] = el.get("value") or el.get_text(strip=True)
                break

        for candidate in ["graph1:accountId", "accountId"]:
            el = soup.find(attrs={"id": candidate}) or soup.find(attrs={"name": candidate})
            if el:
                meta["account_id"] = el.get("value") or el.get_text(strip=True)
                break

        for candidate in ["graph1:premiseAddr", "graph1:address", "premiseAddress"]:
            el = soup.find(attrs={"id": candidate}) or soup.find(attrs={"name": candidate})
            if el:
                meta["premise_address"] = el.get("value") or el.get_text(strip=True)
                break

        if not meta["sa_id"]:
            _LOGGER.debug(
                "SA ID not found on account_details page — "
                "gas sensor will have empty sa_id until field names are confirmed"
            )
        return meta

    def _extract_view_state(self, soup: BeautifulSoup) -> str:
        field = soup.find("input", {"name": "javax.faces.ViewState"})
        if not field or not field.get("value"):
            raise FortisbcError("ViewState missing from page — session may have expired or navigation failed")
        return str(field["value"])

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
        resp = self._session.post(
            CONSUMPTION_AJAX_URL,
            data=data,
            headers={"Referer": CONSUMPTION_URL},
        )
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



def _parse_date(s: str) -> date:
    """Parse DD/MM/YYYY date string."""
    s = s.strip()
    day, month, year = s.split("/")
    return date(int(year), int(month), int(day))
