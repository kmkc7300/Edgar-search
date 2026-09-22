"""
EDGAR deal screener — Kilo Capital

Searches SEC EDGAR full-text search, rolls document hits up to the company
level, and enriches the shortlist with SEC-sourced size and contact data.

Design notes (why this differs from the June build):
  * EDGAR full-text search has NO boolean AND/OR and no parentheses. Each
    search term is run as its own query and the result sets are merged here.
  * EFTS no longer returns a `highlight` block. Mention counts now come from
    an explicit, opt-in filing scan rather than a field that is always empty.
  * All SEC traffic goes through one shared rate limiter (SEC ceiling is 10
    req/sec; breaching it is an IP block for ~10 minutes).
  * Results live in st.session_state, so changing a filter does not re-search.
"""

from __future__ import annotations

import inspect
import io
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import pandas as pd
import requests
import streamlit as st

try:
    import yfinance as yf
    HAVE_YF = True
except Exception:                                    # noqa: BLE001
    yf = None
    HAVE_YF = False


# ─────────────────────────────────────────────────────────── config ──────────

USER_AGENT = "Kilo Capital research@kilocapital.com"   # SEC requires a real contact

EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
FLOAT_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/dei/EntityPublicFloat.json"

PAGE_SIZE = 100          # EFTS ignores `size`; the page is always 100
MAX_OFFSET = 9900        # from + 100 must be <= 10000
REQS_PER_SEC = 6.0       # stay well inside SEC's ceiling of 10

OWNERSHIP_FORMS = {"3", "4", "5", "3/A", "4/A", "5/A"}

st.set_page_config(page_title="EDGAR deal screener", page_icon="⛏", layout="wide")

st.markdown(
    """
    <style>
      .block-container { padding-top: 2.2rem; max-width: 1500px; }
      [data-testid="stMetricValue"] { font-size: 1.5rem; }
      section[data-testid="stSidebar"] { width: 355px !important; }
    </style>
    """,
    unsafe_allow_html=True,
)


# Streamlit deprecated `use_container_width` in favour of `width`. `st.button` never
# had a `width` parameter before that change, so it is a safe probe for the new API.
_MODERN_WIDTH = "width" in inspect.signature(st.button).parameters
FILL = {"width": "stretch"} if _MODERN_WIDTH else {"use_container_width": True}


# ────────────────────────────────────────────────────── SIC reference ────────

SIC_MAP = [
    (range(100, 1000), "Basic Materials", "Agriculture"),
    (range(1000, 1040), "Basic Materials", "Metal Mining"),
    (range(1040, 1090), "Basic Materials", "Gold Mining"),
    (range(1090, 1100), "Basic Materials", "Silver & Other Mining"),
    (range(1200, 1300), "Energy", "Coal Mining"),
    (range(1311, 1382), "Energy", "Crude Petroleum & Natural Gas"),
    (range(1382, 1390), "Energy", "Oil & Gas Field Services"),
    (range(1400, 1500), "Basic Materials", "Mining & Quarrying"),
    (range(1500, 1800), "Industrials", "Construction"),
    (range(2000, 2100), "Consumer Defensive", "Food Processing"),
    (range(2100, 2200), "Consumer Defensive", "Tobacco"),
    (range(2600, 2700), "Basic Materials", "Paper & Forest Products"),
    (range(2800, 2900), "Basic Materials", "Chemicals"),
    (range(2900, 3000), "Energy", "Petroleum Refining"),
    (range(3300, 3310), "Basic Materials", "Steel Works"),
    (range(3310, 3330), "Basic Materials", "Iron & Steel Foundries"),
    (range(3330, 3334), "Basic Materials", "Primary Nonferrous Metals"),
    (range(3334, 3335), "Basic Materials", "Aluminum Smelting"),
    (range(3335, 3356), "Basic Materials", "Nonferrous Rolling & Drawing"),
    (range(3356, 3358), "Basic Materials", "Copper Rolling & Drawing"),
    (range(3358, 3360), "Basic Materials", "Nonferrous Foundries"),
    (range(3360, 3400), "Basic Materials", "Metal Services"),
    (range(3400, 3500), "Industrials", "Fabricated Metal Products"),
    (range(3500, 3600), "Industrials", "Industrial Machinery"),
    (range(3600, 3674), "Technology", "Electronic Equipment"),
    (range(3674, 3675), "Technology", "Semiconductors"),
    (range(3675, 3700), "Technology", "Electronic Components"),
    (range(3700, 3760), "Consumer Cyclical", "Auto & Parts"),
    (range(3760, 3813), "Industrials", "Aerospace & Defense"),
    (range(3813, 3900), "Industrials", "Instruments & Related"),
    (range(3910, 3920), "Consumer Cyclical", "Jewelry & Precious Metal"),
    (range(4000, 4200), "Industrials", "Railroad Transportation"),
    (range(4200, 4400), "Industrials", "Trucking & Warehousing"),
    (range(4400, 4500), "Industrials", "Water Transportation"),
    (range(4500, 4600), "Industrials", "Air Transportation"),
    (range(4810, 4900), "Communication Services", "Telecommunications"),
    (range(4900, 4912), "Utilities", "Electric Services"),
    (range(4920, 4925), "Utilities", "Natural Gas Distribution"),
    (range(4940, 4942), "Utilities", "Water Supply"),
    (range(4942, 5000), "Utilities", "Sanitary Services"),
    (range(5050, 5052), "Industrials", "Wholesale — Metals Service Centers"),
    (range(5052, 5053), "Industrials", "Wholesale — Coal & Minerals"),
    (range(5093, 5094), "Industrials", "Wholesale — Scrap & Waste"),
    (range(5000, 5200), "Industrials", "Wholesale — Durable Goods"),
    (range(5200, 5600), "Consumer Cyclical", "Retail — General"),
    (range(5400, 5500), "Consumer Defensive", "Retail — Food & Drug"),
    (range(6000, 6100), "Financial Services", "Commercial Banking"),
    (range(6100, 6200), "Financial Services", "Credit Institutions"),
    (range(6200, 6300), "Financial Services", "Security Brokers"),
    (range(6300, 6400), "Financial Services", "Insurance"),
    (range(6500, 6600), "Real Estate", "Real Estate"),
    (range(6700, 6800), "Financial Services", "Investment Offices"),
    (range(7000, 7300), "Consumer Cyclical", "Hotels & Personal Services"),
    (range(7372, 7373), "Technology", "Prepackaged Software"),
    (range(7370, 7375), "Technology", "Computer Services"),
    (range(7300, 7400), "Technology", "Business Services"),
    (range(7500, 7900), "Consumer Cyclical", "Auto & Amusement Services"),
    (range(8060, 8070), "Healthcare", "Hospitals"),
    (range(8000, 8100), "Healthcare", "Health Services"),
    (range(8731, 8735), "Healthcare", "Biotech & Pharma Research"),
    (range(8700, 8800), "Industrials", "Engineering Services"),
    (range(9000, 9999), "Government", "Public Administration"),
]

# SIC codes where a physical-metals inventory facility is plausible.
METALS_SIC = set()
for _lo, _hi in [
    (1000, 1100), (1400, 1500), (2810, 2900), (3300, 3400),
    (3400, 3500), (3600, 3700), (3910, 3920), (5050, 5052),
    (5052, 5053), (5093, 5094), (5094, 5095),
]:
    METALS_SIC.update(range(_lo, _hi))


def sic_lookup(sic_code) -> tuple[str, str]:
    """Return (sector, industry) for a SIC code, narrowest range wins."""
    if not sic_code:
        return "Unclassified", "Unclassified"
    try:
        code = int(sic_code)
    except (TypeError, ValueError):
        return "Unclassified", "Unclassified"
    best = ("Unclassified", "Unclassified", 99999)
    for rng, sector, industry in SIC_MAP:
        if code in rng and len(rng) < best[2]:
            best = (sector, industry, len(rng))
    return best[0], best[1]


# ────────────────────────────────────────────────── shared HTTP plumbing ─────

class RateLimiter:
    """Process-wide throttle shared by every worker thread."""

    def __init__(self, per_second: float):
        self._interval = 1.0 / per_second
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if self._next > now:
                time.sleep(self._next - now)
                self._next += self._interval
            else:
                self._next = now + self._interval


@st.cache_resource
def get_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"})
    return s


@st.cache_resource
def get_limiter() -> RateLimiter:
    return RateLimiter(REQS_PER_SEC)


@st.cache_resource
def get_memo() -> tuple[dict, threading.Lock]:
    """Cache for calls made inside worker threads (st.cache_data is not thread-safe there)."""
    return {}, threading.Lock()


# Resolved here, on the main thread, once per rerun. Worker threads use these
# globals directly — calling an st.cache_resource function from a thread has no
# ScriptRunContext and logs a warning on every call.
SESSION = get_session()
LIMITER = get_limiter()
MEMO_STORE, MEMO_LOCK = get_memo()


def memoized(key, producer):
    with MEMO_LOCK:
        if key in MEMO_STORE:
            return MEMO_STORE[key]
    value = producer()
    with MEMO_LOCK:
        MEMO_STORE[key] = value
    return value


def sec_get(url: str, params: dict | None = None, timeout: int = 25):
    """Rate-limited GET returning parsed JSON, or None on any failure."""
    LIMITER.wait()
    try:
        r = SESSION.get(url, params=params, timeout=timeout)
        if r.status_code in (403, 429):
            time.sleep(2.0)
            LIMITER.wait()
            r = SESSION.get(url, params=params, timeout=timeout)
        if not r.ok:
            return None
        return r.json()
    except Exception:                                # noqa: BLE001
        return None


# ──────────────────────────────────────────────────────── EDGAR search ───────

class EdgarUnavailable(RuntimeError):
    """Raised so a failed query is not written to the cache."""


def efts_search(term: str, forms: tuple, start: str, end: str,
                states: tuple, max_pages: int) -> dict:
    """Cached wrapper. Successes are cached for an hour; failures are not."""
    try:
        return _efts_query(term, forms, start, end, states, max_pages)
    except EdgarUnavailable as exc:
        return {"hits": [], "total": 0, "capped": False, "error": str(exc)}


@st.cache_data(ttl=3600, show_spinner=False)
def _efts_query(term: str, forms: tuple, start: str, end: str,
                states: tuple, max_pages: int) -> dict:
    """One EFTS query, paginated. Returns {hits, total, capped, error}."""
    params = {"q": term, "startdt": start, "enddt": end, "dateRange": "custom"}
    if forms:
        params["forms"] = ",".join(forms)
    if states:
        params["locationCodes"] = ",".join(states)     # plural — singular is ignored

    hits, total, capped, error = [], 0, False, None

    for page in range(max_pages):
        offset = page * PAGE_SIZE
        if offset > MAX_OFFSET:
            break
        data = sec_get(EFTS_URL, {**params, "from": offset})
        if data is None:
            error = "EDGAR did not respond. You may be rate limited — wait a minute and retry."
            break
        if "hits" not in data:
            error = data.get("errorMessage", "Unexpected response from EDGAR.")
            break
        block = data["hits"]
        if page == 0:
            total = block.get("total", {}).get("value", 0)
            capped = block.get("total", {}).get("relation") == "gte"
        page_hits = block.get("hits", [])
        if not page_hits:
            break
        hits.extend(page_hits)
        if offset + PAGE_SIZE >= total:
            break

    if error and not hits:
        raise EdgarUnavailable(error)
    return {"hits": hits, "total": total, "capped": capped, "error": error}


def date_windows(start: date, end: date, slice_by_year: bool) -> list[tuple[str, str]]:
    """Split a range into calendar years so no single query hits the 10,000 cap."""
    if not slice_by_year or (end - start).days <= 366:
        return [(start.isoformat(), end.isoformat())]
    windows = []
    year = start.year
    while year <= end.year:
        lo = max(start, date(year, 1, 1))
        hi = min(end, date(year, 12, 31))
        if lo <= hi:
            windows.append((lo.isoformat(), hi.isoformat()))
        year += 1
    return windows


NAME_RE = re.compile(r"^(?P<name>.*?)\s*(?:\((?P<ticker>[A-Z0-9.\-]{1,8})\)\s*)?\(CIK\s*(?P<cik>\d{10})\)\s*$")


def parse_filer(display_name: str) -> tuple[str, str]:
    """'NETLIST INC  (NLST)  (CIK 0001282631)' -> ('NETLIST INC', 'NLST')."""
    m = NAME_RE.match(str(display_name).strip())
    if m:
        return m.group("name").strip(), (m.group("ticker") or "")
    cleaned = re.sub(r"\s*\(CIK\s*\d+\)\s*$", "", str(display_name)).strip()
    return cleaned or str(display_name), ""


def collect(term_results: dict[str, dict]) -> tuple[dict, list[dict]]:
    """Roll document-level hits up to one row per company."""
    companies: dict[str, dict] = {}
    filings: list[dict] = []
    seen_docs: set[tuple[str, str]] = set()

    for term, payload in term_results.items():
        for hit in payload["hits"]:
            src = hit.get("_source", {})
            ciks = src.get("ciks") or []
            names = src.get("display_names") or []
            if not ciks or not names:
                continue

            form = src.get("form", "")
            # Ownership forms list the reporting person first, issuer second.
            idx = 1 if (form in OWNERSHIP_FORMS and len(ciks) > 1) else 0
            cik10 = str(ciks[idx]).zfill(10)
            name, ticker = parse_filer(names[min(idx, len(names) - 1)])

            adsh = src.get("adsh", "")
            doc_id = hit.get("_id", "")
            filename = doc_id.split(":", 1)[1] if ":" in doc_id else ""
            cik_bare = str(int(cik10))
            acc = adsh.replace("-", "")
            doc_url = (f"https://www.sec.gov/Archives/edgar/data/{cik_bare}/{acc}/{filename}"
                       if acc and filename else "")
            index_url = (f"https://www.sec.gov/Archives/edgar/data/{cik_bare}/{acc}/{adsh}-index.htm"
                         if acc else "")

            sic = (src.get("sics") or [None])[0]
            sector, industry = sic_lookup(sic)
            filed = src.get("file_date", "")

            rec = companies.setdefault(cik10, {
                "cik": cik10,
                "Company": name,
                "Ticker": ticker,
                "Location": (src.get("biz_locations") or [""])[0],
                "State": (src.get("biz_states") or [""])[0],
                "Sector": sector,
                "Industry": industry,
                "SIC": str(sic) if sic else "",
                "Metals-adjacent": bool(sic and str(sic).isdigit() and int(sic) in METALS_SIC),
                "Docs": 0,
                "Filings": set(),
                "Latest filing": "",
                "_terms": set(),
                "_best_score": -1.0,
                "Filing": index_url,
                "_doc_url": doc_url,
                "_adsh": adsh,
                "_filename": filename,
                "EDGAR": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
                         f"&CIK={cik10}&type=10-K&dateb=&owner=include&count=40",
            })
            if ticker and not rec["Ticker"]:
                rec["Ticker"] = ticker

            rec["_terms"].add(term)
            rec["Filings"].add(adsh)
            if filed > rec["Latest filing"]:
                rec["Latest filing"] = filed

            score = float(hit.get("_score") or 0)
            if score > rec["_best_score"] and doc_url:
                rec["_best_score"] = score
                rec["Filing"] = index_url
                rec["_doc_url"] = doc_url
                rec["_adsh"] = adsh
                rec["_filename"] = filename

            key = (adsh, filename)
            if key not in seen_docs:
                seen_docs.add(key)
                rec["Docs"] += 1
                filings.append({
                    "Company": name, "Ticker": ticker, "Form": form,
                    "Filed": filed, "Term": term, "Document": doc_url,
                    "Filing index": index_url,
                })

    for rec in companies.values():
        rec["Filings"] = len(rec["Filings"])
        rec["Terms"] = ", ".join(sorted(rec.pop("_terms")))
    return companies, filings


# ──────────────────────────────────────────────────────── enrichment ─────────

def fetch_profile(cik10: str) -> dict:
    """Submissions API: phone, SIC description, tickers, address. One call."""
    def _go():
        data = sec_get(SUBMISSIONS_URL.format(cik=cik10), timeout=20)
        if not data:
            return {}
        addr = (data.get("addresses") or {}).get("business") or {}
        tickers = data.get("tickers") or []
        return {
            "Phone": data.get("phone") or "",
            "SIC description": data.get("sicDescription") or "",
            "Ticker": tickers[0] if tickers else "",
            "City": addr.get("city") or "",
            "State": addr.get("stateOrCountry") or "",
            "Entity type": data.get("entityType") or "",
            "FY end": data.get("fiscalYearEnd") or "",
        }
    return memoized(("profile", cik10), _go)


def fetch_float(cik10: str) -> dict:
    """dei:EntityPublicFloat — the SEC's own non-affiliate equity value."""
    def _go():
        data = sec_get(FLOAT_URL.format(cik=cik10), timeout=20)
        if not data:
            return {}
        usd = (data.get("units") or {}).get("USD") or []
        if not usd:
            return {}
        latest = max(usd, key=lambda x: x.get("end", ""))
        return {"float_usd": latest.get("val"), "float_date": latest.get("end", "")}
    return memoized(("float", cik10), _go)


def fetch_live_cap(ticker: str) -> float | None:
    """yfinance fast_info only — never .info, which scrapes a full profile page."""
    if not HAVE_YF or not ticker:
        return None

    def _go():
        try:
            fi = yf.Ticker(ticker).fast_info
            return fi.get("market_cap") if hasattr(fi, "get") else getattr(fi, "market_cap", None)
        except Exception:                            # noqa: BLE001
            return None
    return memoized(("cap", ticker), _go)


CFO_TITLE = re.compile(
    r"(?:Chief Financial Officer|Principal Financial Officer|"
    r"Chief Financial and Accounting Officer|Interim Chief Financial Officer)", re.I)

# Names sit immediately before the title in a signature block. Anchor to the end of
# a short window and read backwards rather than scanning forward from "/s/".
NAME_BEFORE = re.compile(
    r"((?:[A-Z][A-Za-z'\-]+|[A-Z]\.)"
    r"(?:\s+(?:[A-Z][A-Za-z'\-]+|[A-Z]\.|de|del|la|las|van|von|der|di|da|dos|bin|al)){1,9})"
    r"\s*[,\-–]?\s*$")

BAD_NAME_TOKENS = {
    "the", "and", "company", "corporation", "corp", "inc", "llc", "our", "we",
    "signature", "signatures", "by", "its", "title", "name", "date", "director",
    "officer", "president", "chairman", "duly", "undersigned", "pursuant", "as",
}


def _collapse_repeat(tokens: list[str]) -> list[str]:
    """'SIGNATURES Tracy G. Smith Tracy G. Smith' -> 'Tracy G. Smith'.

    Filings print the name once after /s/ and again above the title; tag-stripping
    glues the two together, often behind a heading word. Scan suffixes longest-first
    for an exact doubling, since that doubling is the name.
    """
    n = len(tokens)
    for start in range(n):
        suffix = tokens[start:]
        half = len(suffix) // 2
        if len(suffix) >= 4 and len(suffix) % 2 == 0 and suffix[:half] == suffix[half:]:
            return suffix[:half]
    return tokens


def extract_cfo(text: str) -> str:
    for m in CFO_TITLE.finditer(text):
        window = text[max(0, m.start() - 120):m.start()]
        # Signature blocks often read "By: /s/ X  Name: X  Title: Chief Financial Officer".
        window = re.sub(r"/s/", " ", window)
        window = re.sub(r"\b(?:Title|Name|By|Its)\s*:", " ", window, flags=re.I)
        hit = NAME_BEFORE.search(window)
        if not hit:
            continue
        tokens = _collapse_repeat(hit.group(1).split())
        while tokens and tokens[0].lower().strip(".,") in BAD_NAME_TOKENS:
            tokens.pop(0)                          # "SIGNATURES", "By", "Director"
        if len(tokens) > 4:
            tokens = tokens[-4:]
        while tokens and tokens[0].islower():      # dangling "de", "van" etc.
            tokens.pop(0)
        if len(tokens) < 2:
            continue
        if any(t.lower().strip(".,") in BAD_NAME_TOKENS for t in tokens):
            continue
        name = " ".join(tokens)
        if 5 < len(name) < 55:
            return name
    return ""


def scan_filing(doc_url: str, terms: tuple[str, ...], byte_cap: int = 6_000_000) -> dict:
    """Download one filing document, count term occurrences, pull the CFO name."""
    def _go():
        get_limiter().wait()
        try:
            r = get_session().get(doc_url, timeout=30, stream=True)
            if not r.ok:
                return {}
            chunks, size = [], 0
            for chunk in r.iter_content(chunk_size=131072):
                chunks.append(chunk)
                size += len(chunk)
                if size > byte_cap:
                    break
            raw = b"".join(chunks).decode("utf-8", errors="ignore")
        except Exception:                            # noqa: BLE001
            return {}

        raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
        text = " ".join(re.sub(r"<[^>]+>", " ", raw).split())
        low = text.lower()
        counts = {t: low.count(t.strip('"').strip("'").lower()) for t in terms}
        return {"mentions": sum(counts.values()), "by_term": counts, "cfo": extract_cfo(text)}

    return memoized(("scan", doc_url, terms), _go)


def run_pool(items, worker, label: str, max_workers: int = 5):
    """Run worker over items with a progress bar. Returns {item: result}."""
    out, done = {}, 0
    bar = st.progress(0.0, text=f"{label} 0 / {len(items)}")
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(worker, it): it for it in items}
        for fut in as_completed(futures):
            it = futures[fut]
            try:
                out[it] = fut.result()
            except Exception:                        # noqa: BLE001
                out[it] = {}
            done += 1
            bar.progress(done / len(items), text=f"{label} {done} / {len(items)}")
    bar.empty()
    return out


# ───────────────────────────────────────────────────────── formatting ────────

def fmt_usd(val) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "—"
    val = float(val)
    if val >= 1e12:
        return f"${val / 1e12:.2f}T"
    if val >= 1e9:
        return f"${val / 1e9:.2f}B"
    if val >= 1e6:
        return f"${val / 1e6:.0f}M"
    return f"${val:,.0f}"


def to_excel(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Screen")
        ws = writer.sheets["Screen"]
        for col in ws.columns:
            width = max((len(str(c.value or "")) for c in col), default=10)
            ws.column_dimensions[col[0].column_letter].width = min(width + 4, 60)
    return buf.getvalue()


US_STATES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO",
    "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA",
    "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
]

DATE_PRESETS = {
    "Last 90 days": 90,
    "Last 12 months": 365,
    "Last 2 years": 730,
    "Last 5 years": 1825,
    "Since 2001": None,
}


# ──────────────────────────────────────────────────────────── sidebar ────────

st.title("EDGAR deal screener")
st.caption(
    "Company-level rollup of SEC full-text search. Size data comes from the SEC's own "
    "public-float tag; filing scans run only when you ask for them."
)

PRESETS = {
    "Copper inventory": '"copper cathode"\n"copper concentrate"\n"copper scrap"',
    "Already borrowing on inventory": '"borrowing base"\n"raw materials inventory"',
    "Tolling and refining": '"toll processing"\n"tolling agreement"\n"refining agreement"',
    "Consignment and leased metal": '"consignment inventory"\n"precious metals lease"\n"metal consignment"',
    "Distress signals": '"going concern"\n"forbearance agreement"\n"covenant default"',
    "Blank": "",
}


def _apply_preset():
    text = PRESETS.get(st.session_state.get("preset_choice"))
    if text is not None:
        st.session_state["terms_text"] = text


with st.sidebar:
    st.subheader("Search")

    st.session_state.setdefault("terms_text", PRESETS["Copper inventory"])
    st.selectbox("Saved screens", list(PRESETS), key="preset_choice",
                 on_change=_apply_preset,
                 help="Loads a set of terms into the box below. Edit them before searching.")

    with st.form("search_form"):
        terms_raw = st.text_area(
            "Search terms — one per line",
            key="terms_text",
            height=110,
            help="EDGAR has no OR operator, so each line runs as its own query and the "
                 "results are merged here. Quote a phrase to require adjacent words.",
        )
        match_mode = st.radio(
            "Combine terms",
            ["Any term (union)", "Every term (intersection)"],
            help="Intersection keeps only companies whose filings matched every line.",
        )
        forms = st.multiselect(
            "Filing types",
            ["10-K", "10-K/A", "10-Q", "8-K", "S-1", "20-F", "40-F", "DEF 14A", "1-K"],
            default=["10-K"],
        )
        preset = st.selectbox("Filed", list(DATE_PRESETS) + ["Custom range"], index=1)
        c1, c2 = st.columns(2)
        custom_from = c1.date_input("From", value=date.today() - timedelta(days=365))
        custom_to = c2.date_input("To", value=date.today())
        states = st.multiselect("Headquartered in", US_STATES, default=[])
        slice_years = st.checkbox(
            "Split by calendar year", value=True,
            help="EDGAR refuses to page past 10,000 results. Splitting the range gives "
                 "each year its own allowance.",
        )
        max_pages = st.slider("Pages per query (100 docs each)", 1, 10, 5)
        submitted = st.form_submit_button("Search EDGAR", type="primary",
                                          **FILL)

    st.divider()
    st.caption(
        f"User-Agent sent to SEC: `{USER_AGENT}`  \n"
        f"yfinance available: {'yes' if HAVE_YF else 'no'}"
    )


# ───────────────────────────────────────────────────────── run a search ──────

if submitted:
    terms = tuple(t.strip() for t in terms_raw.splitlines() if t.strip())
    if not terms:
        st.warning("Enter at least one search term.")
    else:
        if preset == "Custom range":
            start, end = custom_from, custom_to
        elif DATE_PRESETS[preset] is None:
            start, end = date(2001, 1, 1), date.today()
        else:
            start, end = date.today() - timedelta(days=DATE_PRESETS[preset]), date.today()

        windows = date_windows(start, end, slice_years)
        jobs = [(t, w) for t in terms for w in windows]

        term_results = {t: {"hits": [], "total": 0, "capped": False} for t in terms}
        problems = []
        bar = st.progress(0.0, text="Querying EDGAR…")

        for i, (term, (w_start, w_end)) in enumerate(jobs, start=1):
            bar.progress((i - 1) / len(jobs), text=f"Querying EDGAR — {term} ({w_start[:4]})")
            res = efts_search(term, tuple(forms), w_start, w_end, tuple(states), max_pages)
            if res["error"]:
                problems.append(f"{term} ({w_start[:4]}): {res['error']}")
            term_results[term]["hits"].extend(res["hits"])
            term_results[term]["total"] += res["total"]
            term_results[term]["capped"] |= res["capped"]
        bar.empty()

        companies, filings = collect(term_results)

        if match_mode.startswith("Every"):
            need = len(terms)
            companies = {k: v for k, v in companies.items()
                         if len(v["Terms"].split(", ")) == need}

        st.session_state["companies"] = companies
        st.session_state["filings"] = filings
        st.session_state["terms"] = terms
        st.session_state["totals"] = {t: term_results[t]["total"] for t in terms}
        st.session_state["capped"] = [t for t in terms if term_results[t]["capped"]]
        st.session_state["problems"] = problems
        st.session_state.setdefault("enriched", {})
        st.session_state.setdefault("scans", {})


# ─────────────────────────────────────────────────────────── results ─────────

companies = st.session_state.get("companies")
problems = st.session_state.get("problems", [])

for problem in problems:
    st.warning(problem)
if st.session_state.get("capped"):
    st.warning(
        "These terms hit EDGAR's 10,000-result ceiling: "
        + ", ".join(st.session_state["capped"])
        + ". Narrow the date range or add a second term to see the full set."
    )

if not companies and problems:
    st.error(
        "Nothing came back — every query failed. EDGAR blocks bursts of traffic for "
        "about ten minutes. Wait a minute and search again."
    )
    st.stop()

if not companies and "terms" in st.session_state:
    st.info(
        "No filings matched. Widen the date range, drop a term, or switch from "
        "intersection back to union."
    )
    st.stop()

if not companies:
    st.info(
        "Set your terms in the sidebar and run a search. Each line is a separate EDGAR "
        "query — EDGAR has no OR operator, so this is how you cover synonyms."
    )
    st.markdown(
        "**Screens worth keeping around**\n\n"
        "- `\"copper cathode\"` / `\"copper concentrate\"` / `\"copper scrap\"` — 10-K, last 12 months\n"
        "- `\"borrowing base\"` + `\"raw materials inventory\"` with intersection on — companies "
        "already borrowing against inventory\n"
        "- `\"toll processing\"` / `\"tolling agreement\"` — refiners and fabricators who never own the metal\n"
        "- `\"consignment inventory\"` + `\"precious metals\"` — existing lease structures up for renewal"
    )
    st.stop()

terms = st.session_state.get("terms", ())
enriched = st.session_state.setdefault("enriched", {})
scans = st.session_state.setdefault("scans", {})

# Build the working frame, merging in whatever enrichment exists.
rows = []
for cik, rec in companies.items():
    row = dict(rec)
    prof = enriched.get(cik, {})
    row["Phone"] = prof.get("Phone", "")
    row["CFO"] = scans.get(cik, {}).get("cfo", "")
    row["Mentions"] = scans.get(cik, {}).get("mentions")
    if prof.get("SIC description"):
        row["Industry"] = prof["SIC description"]
    if prof.get("Ticker") and not row["Ticker"]:
        row["Ticker"] = prof["Ticker"]
    row["Float"] = prof.get("float_usd")
    row["Float date"] = prof.get("float_date", "")
    row["Market cap"] = prof.get("market_cap")
    row["Size"] = row["Market cap"] if row["Market cap"] is not None else row["Float"]
    row["LinkedIn"] = (
        "https://www.linkedin.com/search/results/people/?keywords="
        + (row["CFO"] + " " + row["Company"]).replace(" ", "%20")
    ) if row["CFO"] else ""
    rows.append(row)

df = pd.DataFrame(rows)

# ── filters (instant — they never re-query EDGAR) ────────────────────────────

with st.expander("Filters", expanded=True):
    f1, f2, f3, f4 = st.columns([1.1, 1.1, 1.4, 1.4])
    size_min = f1.number_input("Min size ($M)", min_value=0.0, value=0.0, step=25.0)
    size_max = f2.number_input("Max size ($M)", min_value=0.0, value=1000.0, step=25.0)
    sectors = f3.multiselect("Sector", sorted(df["Sector"].unique()))
    state_filter = f4.multiselect("State", sorted(x for x in df["State"].unique() if x))

    g1, g2, g3, g4 = st.columns([1.2, 1.2, 1.2, 1.4])
    keep_unknown = g1.checkbox("Keep unknown size", value=True,
                               help="Companies with no public-float tag. Turning this off "
                                    "silently drops most small filers.")
    metals_only = g2.checkbox("Metals-adjacent SIC only", value=False)
    min_docs = g3.number_input("Min matching documents", min_value=1, value=1, step=1)
    sort_by = g4.selectbox("Sort by", ["Matching documents", "Size (low to high)",
                                       "Size (high to low)", "Latest filing", "Company"])

view = df.copy()
if sectors:
    view = view[view["Sector"].isin(sectors)]
if state_filter:
    view = view[view["State"].isin(state_filter)]
if metals_only:
    view = view[view["Metals-adjacent"]]
view = view[view["Docs"] >= min_docs]

view["Size"] = pd.to_numeric(view["Size"], errors="coerce")
view["Float"] = pd.to_numeric(view["Float"], errors="coerce")
view["Market cap"] = pd.to_numeric(view["Market cap"], errors="coerce")
size_usd = view["Size"]
in_band = size_usd.between(size_min * 1e6, size_max * 1e6)
view = view[in_band | (size_usd.isna() & keep_unknown)]

sort_map = {
    "Matching documents": ("Docs", False),
    "Size (low to high)": ("Size", True),
    "Size (high to low)": ("Size", False),
    "Latest filing": ("Latest filing", False),
    "Company": ("Company", True),
}
col, asc = sort_map[sort_by]
view = view.sort_values(col, ascending=asc, na_position="last")

# ── headline numbers ─────────────────────────────────────────────────────────

m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Companies", f"{len(view):,}", delta=f"{len(view) - len(df):,}" if len(view) != len(df) else None)
m2.metric("Matching documents", f"{int(view['Docs'].sum()):,}")
m3.metric("With size data", f"{int(view['Size'].notna().sum()):,}")
sized = view["Size"].dropna()
m4.metric("Median size", fmt_usd(sized.median()) if len(sized) else "—")
m5.metric("Metals-adjacent", f"{int(view['Metals-adjacent'].sum()):,}")

# ── staged enrichment ────────────────────────────────────────────────────────

e1, e2, e3 = st.columns(3)
shortlist = view.head(200)

if e1.button(f"Get size and contact data ({len(shortlist)})", **FILL):
    ciks = [c for c in shortlist["cik"] if c not in enriched]
    if not ciks:
        st.toast("Already have data for everything on screen.")
    else:
        def _enrich(cik):
            out = fetch_profile(cik)
            out.update(fetch_float(cik))
            return out
        results = run_pool(ciks, _enrich, "Pulling SEC company data —")
        for cik, data in results.items():
            enriched.setdefault(cik, {}).update(data)
        st.rerun()

if e2.button("Refresh live market cap", **FILL,
             disabled=not HAVE_YF,
             help="Yahoo Finance, listed tickers only. The SEC float is the reliable number."):
    tickers = [(c, t) for c, t in zip(shortlist["cik"], shortlist["Ticker"]) if t]
    if not tickers:
        st.toast("No tickers on screen to look up.")
    else:
        results = run_pool([t for _, t in tickers],
                           lambda t: {"market_cap": fetch_live_cap(t)},
                           "Fetching market caps —", max_workers=6)
        for cik, ticker in tickers:
            enriched.setdefault(cik, {}).update(results.get(ticker, {}))
        st.rerun()

scan_n = min(len(view), 40)
if e3.button(f"Scan filings for mentions and CFO ({scan_n})", **FILL,
             help="Downloads the best-matching document per company. Slow by nature — "
                  "run it on a shortlist, not the whole screen."):
    targets = [(r["cik"], r["_doc_url"]) for _, r in view.head(scan_n).iterrows()
               if r["_doc_url"] and r["cik"] not in scans]
    if not targets:
        st.toast("Already scanned everything on screen.")
    else:
        results = run_pool([u for _, u in targets],
                           lambda u: scan_filing(u, terms),
                           "Reading filings —", max_workers=4)
        for cik, url in targets:
            scans[cik] = results.get(url, {})
        st.rerun()

# ── table ────────────────────────────────────────────────────────────────────

tab_co, tab_filings, tab_mix = st.tabs(["Companies", "Filings", "Industry mix"])

with tab_co:
    display = view.copy()
    display["Size ($M)"] = display["Size"] / 1e6
    display["Size basis"] = display.apply(
        lambda r: "Market cap" if pd.notna(r["Market cap"])
        else (f"Float {r['Float date']}" if pd.notna(r["Float"]) else ""), axis=1)

    cols = ["Company", "Ticker", "Location", "Industry", "Size ($M)", "Size basis",
            "Docs", "Filings", "Latest filing", "Terms", "Metals-adjacent"]
    if display["Phone"].any():
        cols += ["Phone"]
    if display["CFO"].any():
        cols += ["CFO", "LinkedIn"]
    if display["Mentions"].notna().any():
        cols.insert(7, "Mentions")
    cols += ["Filing", "EDGAR"]

    st.dataframe(
        display[cols],
        hide_index=True,
        **FILL,
        height=620,
        column_config={
            "Company": st.column_config.TextColumn(width="large"),
            "Size ($M)": st.column_config.NumberColumn(format="%.0f"),
            "Docs": st.column_config.NumberColumn("Docs", help="Indexed documents matching your terms"),
            "Mentions": st.column_config.NumberColumn(format="%d"),
            "Metals-adjacent": st.column_config.CheckboxColumn("Metals SIC"),
            "Filing": st.column_config.LinkColumn("Filing", display_text="Open"),
            "EDGAR": st.column_config.LinkColumn("EDGAR", display_text="History"),
            "LinkedIn": st.column_config.LinkColumn("LinkedIn", display_text="Find"),
        },
    )

    export = display[[c for c in cols if c not in ("Metals-adjacent",)]].copy()
    d1, d2 = st.columns(2)
    stem = re.sub(r"\W+", "_", terms[0] if terms else "edgar")[:30]
    d1.download_button("Download CSV", export.to_csv(index=False).encode("utf-8"),
                       file_name=f"edgar_{stem}.csv", mime="text/csv",
                       **FILL)
    d2.download_button("Download Excel", to_excel(export),
                       file_name=f"edgar_{stem}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       **FILL)

with tab_filings:
    fdf = pd.DataFrame(st.session_state.get("filings", []))
    if fdf.empty:
        st.info("No filings to show.")
    else:
        keep = set(view["Company"])
        fdf = fdf[fdf["Company"].isin(keep)].sort_values("Filed", ascending=False)
        st.dataframe(
            fdf, hide_index=True, **FILL, height=560,
            column_config={
                "Document": st.column_config.LinkColumn("Document", display_text="Open"),
                "Filing index": st.column_config.LinkColumn("All documents", display_text="Index"),
            },
        )

with tab_mix:
    c1, c2 = st.columns(2)
    with c1:
        st.caption("Companies by sector")
        st.bar_chart(view["Sector"].value_counts(), horizontal=True)
    with c2:
        st.caption("Companies by state")
        st.bar_chart(view["State"].replace("", "Unknown").value_counts().head(15), horizontal=True)

    st.caption("Total documents EDGAR reports per term, before any filtering")
    st.dataframe(
        pd.DataFrame([
            {"Term": t, "Documents in EDGAR": st.session_state["totals"].get(t, 0),
             "Companies retrieved": sum(1 for r in rows if t in r["Terms"])}
            for t in terms
        ]),
        hide_index=True, **FILL,
    )
