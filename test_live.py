#!/usr/bin/env python3
"""Live integration test against real FortisBC portal.
Run: python3 test_live.py
"""
import json
import logging
import os
import sys

logging.basicConfig(level=logging.DEBUG, stream=sys.stdout)

from bs4 import BeautifulSoup
from curl_cffi.requests import Session as CurlSession
from fortisbc import FortisbcClient

USERNAME = os.environ.get("FORTISBC_USERNAME", "miczimm")
PASSWORD = os.environ.get("FORTISBC_PASSWORD")
if not PASSWORD:
    print("Set env vars before running:")
    print("  export FORTISBC_USERNAME='your@email.com'")
    print("  export FORTISBC_PASSWORD='yourpassword'")
    sys.exit(1)

print("\n--- Fetching login page / smagentname ---")
client = FortisbcClient(USERNAME, PASSWORD)

# Debug: show CIAM init and dump the actual login form
_debug_session = CurlSession(impersonate="chrome110")
resp_login = _debug_session.get("https://accounts.fortisbc.com/", allow_redirects=True)
login_url = resp_login.url
print(f"CIAM login form URL: {login_url}")

soup_login = BeautifulSoup(resp_login.text, "html.parser")

# Dump all form fields
print("\n--- All form inputs on the login page ---")
for form in soup_login.find_all("form"):
    print(f"Form action: {form.get('action')}  method: {form.get('method')}")
    for inp in form.find_all("input"):
        name = inp.get("name", "")
        typ = inp.get("type", "text")
        val = inp.get("value", "")
        if name.lower() in ("password",):
            val = "***"
        print(f"  [{typ}] name={name!r}  value={val[:60]!r}")

# Build form data from ALL hidden fields + credentials
print("\n--- Submitting login form ---")
form = soup_login.find("form")
form_data = {}
for inp in form.find_all("input"):
    name = inp.get("name")
    if name:
        form_data[name] = inp.get("value", "")

# Detect username/password field names
user_field = next((k for k in form_data if k.lower() in ("user", "username", "userid", "login")), "User")
pass_field = next((k for k in form_data if k.lower() == "password"), "Password")
print(f"Username field: {user_field!r}, Password field: {pass_field!r}")

form_data[user_field] = USERNAME
form_data[pass_field] = PASSWORD

action = form.get("action") or login_url
if not action.startswith("http"):
    from urllib.parse import urljoin
    action = urljoin(login_url, action)
print(f"Posting to: {action}")

resp = _debug_session.post(action, data=form_data, allow_redirects=True)
print(f"Final URL: {resp.url}")
print(f"Status: {resp.status_code}")

# Look for error messages in the response
err_soup = BeautifulSoup(resp.text, "html.parser")
for el in err_soup.find_all(class_=lambda c: c and any(x in c for x in ["error", "alert", "msg", "warn"])):
    print(f"  ERROR ELEMENT [{el.name}.{el.get('class')}]: {el.get_text(strip=True)[:200]}")

# Also print SMAUTHREASON from redirect URL if present
import re
reason = re.search(r'SMAUTHREASON=(\d+)', resp.url)
if reason:
    print(f"  SMAUTHREASON: {reason.group(1)}")

print(f"Response body (first 1000 chars):\n{resp.text[:1000]}")

print("\n--- Logging in (via client.login()) ---")
client2 = FortisbcClient(USERNAME, PASSWORD)
try:
    client2.login()
    print("Login OK\n")
except Exception as e:
    print(f"Login FAILED: {e}")
    sys.exit(1)

client = client2

print("--- Fetching account summary ---")
try:
    summary_html = client._get_account_summary()
    print(f"account_summary.xhtml: {len(summary_html)} bytes")

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(summary_html, "html.parser")
    vs = client._extract_view_state(soup)
    print(f"ViewState: {vs}")

    gas_link = client._find_account_link(soup, "GAS")
    elec_links = client._find_all_electric_links(soup)
    print(f"Gas link ID:      {gas_link}")
    print(f"Electric link IDs: {elec_links}")

    # Dump all input names/ids to help debug if links not found
    if not gas_link and not elec_links:
        print("\n[DEBUG] All inputs on account_summary page:")
        for inp in soup.find_all("input"):
            print(f"  id={inp.get('id')} name={inp.get('name')} type={inp.get('type')} value={str(inp.get('value',''))[:40]}")

except Exception as e:
    print(f"FAILED: {e}")
    import traceback; traceback.print_exc()

print("\n--- fetch_all() ---")
try:
    result = client.fetch_all()
    print(json.dumps(
        {
            "gas": (
                {
                    "sa_id": result["gas"].sa_id,
                    "billing_periods": [
                        {
                            "start": p.start_date.isoformat(),
                            "end": p.end_date.isoformat(),
                            "days": p.days,
                            "usage": p.usage,
                            "unit": p.usage_unit,
                            "avg_temp": p.avg_temperature,
                        }
                        for p in result["gas"].billing_periods
                    ],
                }
                if result["gas"] else None
            ),
            "electric": [
                {
                    "sa_id": acct.sa_id,
                    "premise": acct.premise_address,
                    "rate": acct.rate_id,
                    "billing_periods": [
                        {
                            "start": p.start_date.isoformat(),
                            "end": p.end_date.isoformat(),
                            "days": p.days,
                            "usage": p.usage,
                            "unit": p.usage_unit,
                        }
                        for p in acct.billing_periods
                    ],
                }
                for acct in result["electric"]
            ],
        },
        indent=2,
    ))
except Exception as e:
    print(f"fetch_all FAILED: {e}")
    import traceback; traceback.print_exc()
