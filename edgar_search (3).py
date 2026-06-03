
import streamlit as st
import requests
import yfinance as yf
import pandas as pd
from datetime import date, timedelta
import time
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
import io

st.set_page_config(page_title="EDGAR Filing Search", layout="wide")
st.title("EDGAR Filing Keyword Search")
st.caption("Paginates through SEC EDGAR • SIC-based instant sector classification • Parallel market cap fetching")

# ── SIC → Sector/Industry ──────────────────────────────────────────────────────
SIC_MAP = [
    (range(100,   1000),  "Basic Materials",       "Agriculture"),
    (range(1000,  1040),  "Basic Materials",       "Metal Mining"),
    (range(1040,  1090),  "Basic Materials",       "Gold Mining"),
    (range(1090,  1100),  "Basic Materials",       "Silver & Other Mining"),
    (range(1094,  1095),  "Basic Materials",       "Uranium Mining"),
    (range(1200,  1300),  "Energy",                "Coal Mining"),
    (range(1311,  1382),  "Energy",                "Crude Petroleum & Natural Gas"),
    (range(1382,  1390),  "Energy",                "Oil & Gas Field Services"),
    (range(1400,  1500),  "Basic Materials",       "Mining & Quarrying"),
    (range(1500,  1800),  "Industrials",           "Construction"),
    (range(2000,  2100),  "Consumer Defensive",    "Food Processing"),
    (range(2100,  2200),  "Consumer Defensive",    "Tobacco"),
    (range(2600,  2700),  "Basic Materials",       "Paper & Forest Products"),
    (range(2800,  2900),  "Basic Materials",       "Chemicals"),
    (range(2900,  3000),  "Energy",                "Petroleum Refining"),
    (range(3300,  3310),  "Basic Materials",       "Steel Works"),
    (range(3310,  3330),  "Basic Materials",       "Iron & Steel Foundries"),
    (range(3330,  3334),  "Basic Materials",       "Primary Nonferrous Metals"),
    (range(3334,  3335),  "Basic Materials",       "Aluminum Smelting"),
    (range(3335,  3356),  "Basic Materials",       "Nonferrous Rolling & Drawing"),
    (range(3356,  3358),  "Basic Materials",       "Copper Rolling & Drawing"),
    (range(3358,  3360),  "Basic Materials",       "Nonferrous Foundries"),
    (range(3360,  3400),  "Basic Materials",       "Metal Services"),
    (range(3400,  3500),  "Industrials",           "Fabricated Metal Products"),
    (range(3500,  3600),  "Industrials",           "Industrial Machinery"),
    (range(3600,  3674),  "Technology",            "Electronic Equipment"),
    (range(3674,  3675),  "Technology",            "Semiconductors"),
    (range(3675,  3700),  "Technology",            "Electronic Components"),
    (range(3700,  3760),  "Consumer Cyclical",     "Auto & Parts"),
    (range(3760,  3813),  "Industrials",           "Aerospace & Defense"),
    (range(3813,  3900),  "Industrials",           "Instruments & Related"),
    (range(4000,  4200),  "Industrials",           "Railroad Transportation"),
    (range(4200,  4400),  "Industrials",           "Trucking & Warehousing"),
    (range(4400,  4500),  "Industrials",           "Water Transportation"),
    (range(4500,  4600),  "Industrials",           "Air Transportation"),
    (range(4810,  4900),  "Communication Services","Telecommunications"),
    (range(4900,  4912),  "Utilities",             "Electric Services"),
    (range(4920,  4925),  "Utilities",             "Natural Gas Distribution"),
    (range(4940,  4942),  "Utilities",             "Water Supply"),
    (range(4942,  5000),  "Utilities",             "Sanitary Services"),
    (range(5000,  5200),  "Industrials",           "Wholesale — Durable Goods"),
    (range(5200,  5600),  "Consumer Cyclical",     "Retail — General"),
    (range(5400,  5500),  "Consumer Defensive",    "Retail — Food & Drug"),
    (range(6000,  6100),  "Financial Services",    "Commercial Banking"),
    (range(6100,  6200),  "Financial Services",    "Credit Institutions"),
    (range(6200,  6300),  "Financial Services",    "Security Brokers"),
    (range(6300,  6400),  "Financial Services",    "Insurance"),
    (range(6500,  6600),  "Real Estate",           "Real Estate"),
    (range(6700,  6800),  "Financial Services",    "Investment Offices"),
    (range(7000,  7300),  "Consumer Cyclical",     "Hotels & Personal Services"),
    (range(7370,  7375),  "Technology",            "Computer Services"),
    (range(7372,  7373),  "Technology",            "Prepackaged Software"),
    (range(7300,  7400),  "Technology",            "Business Services"),
    (range(7500,  7900),  "Consumer Cyclical",     "Auto & Amusement Services"),
    (range(8000,  8100),  "Healthcare",            "Health Services"),
    (range(8060,  8070),  "Healthcare",            "Hospitals"),
    (range(8700,  8800),  "Industrials",           "Engineering Services"),
    (range(8731,  8735),  "Healthcare",            "Biotech & Pharma Research"),
    (range(9000,  9999),  "Government",            "Public Administration"),
]

def sic_to_sector(sic_code):
    if not sic_code:
        return "N/A", "N/A"
    try:
        code = int(sic_code)
    except (ValueError, TypeError):
        return "N/A", "N/A"
    best_sector, best_industry, best_size = "N/A", "N/A", 99999
    for r, sector, industry in SIC_MAP:
        if code in r and len(r) < best_size:
            best_sector, best_industry, best_size = sector, industry, len(r)
    return best_sector, best_industry


# ── Sidebar ────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Search")
    keyword = st.text_input("Primary keyword / phrase", placeholder='"copper cathode"')
    keyword2 = st.text_input("AND keyword (optional)", placeholder='"toll processing"')
    keyword_or = st.text_input("OR keyword (optional)", placeholder='"copper scrap"')
    filing_type = st.selectbox("Filing type", ["10-K", "10-Q", "8-K", "Any", "S-1", "20-F", "6-K"])
    col1, col2 = st.columns(2)
    with col1:
        date_from = st.date_input("From", value=date.today() - timedelta(days=365))
    with col2:
        date_to = st.date_input("To", value=date.today())

    st.markdown("---")
    max_pages = st.slider("Pages to fetch (100 results/page)", 1, 10, 3)
    st.caption(f"Will fetch up to {max_pages * 100} filings from EDGAR")
    dedup = st.checkbox("One result per company", value=True)
    listed_only = st.checkbox("Listed companies only", value=True)

    st.markdown("---")
    st.subheader("Market Cap")
    use_mcap = st.checkbox("Filter by market cap", value=True)
    if use_mcap:
        mcap_min = st.number_input("Min ($B)", min_value=0.0, value=0.0, step=0.1, format="%.2f")
        mcap_max = st.number_input("Max ($B)", min_value=0.0, value=1.0, step=0.1, format="%.2f")
    else:
        mcap_min = mcap_max = None

    st.markdown("---")
    st.subheader("Sector")
    sector_inc = st.text_input("Include", placeholder="Basic Materials")
    sector_exc = st.text_input("Exclude", placeholder="Utilities")

    st.markdown("---")
    st.subheader("Industry")
    industry_inc = st.text_input("Include ", placeholder="Copper")
    industry_exc = st.text_input("Exclude ", placeholder="Gold")

    st.markdown("---")
    sort_by = st.selectbox("Sort by", ["Market Cap ↓", "Market Cap ↑", "Filing Date ↓", "Company A–Z"])
    debug_mode = st.checkbox("Debug mode")
    run = st.button("Search", type="primary", use_container_width=True)


# ── Helpers ────────────────────────────────────────────────────────────────────
def parse_display_names(display_names):
    parsed = []
    for entry in display_names:
        entry = str(entry)
        ticker = ""
        for p in re.findall(r'\(([^)]+)\)', entry):
            p = p.strip()
            if re.match(r'^[A-Z]{1,7}$', p) and not p.startswith("CIK"):
                ticker = p
                break
        clean = re.sub(r'\s*\(.*', '', entry).strip()
        parsed.append((clean or entry, ticker))
    return parsed


def build_query(keyword, keyword2, keyword_or):
    q = keyword.strip()
    if keyword2.strip():
        q = f"{q} AND {keyword2.strip()}"
    if keyword_or.strip():
        q = f"({q}) OR {keyword_or.strip()}"
    return q


def fetch_page(q, filing_type, date_from, date_to, page, page_size=100):
    params = {
        "q": q,
        "dateRange": "custom",
        "startdt": str(date_from),
        "enddt": str(date_to),
        "from": page * page_size,
        "size": page_size,
    }
    if filing_type != "Any":
        params["forms"] = filing_type
    try:
        r = requests.get(
            "https://efts.sec.gov/LATEST/search-index",
            params=params,
            headers={"User-Agent": "KiloCapital research@kilocapital.com"},
            timeout=20,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def parse_hit(h):
    src = h.get("_source", {})
    display_names = src.get("display_names", [])
    parsed = parse_display_names(display_names) if display_names else [("N/A", "")]
    primary_name = parsed[0][0] if parsed else "N/A"
    ticker = next((t for _, t in parsed if t), "")

    root_forms = src.get("root_forms", [])
    form = root_forms[0] if root_forms else src.get("form", "N/A")
    filed = src.get("file_date", "N/A")

    sics = src.get("sics", [])
    sic_sector, sic_industry = sic_to_sector(sics[0] if sics else None)

    adsh = src.get("adsh", "")
    ciks = src.get("ciks", [])
    cik = str(ciks[0]).lstrip("0") if ciks else ""
    if adsh and cik:
        acc_clean = adsh.replace("-", "")
        filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_clean}/{adsh}-index.htm"
    else:
        filing_url = "N/A"

    highlight = h.get("highlight", {})
    snippet_parts = (
        highlight.get("file_contents") or
        highlight.get("period_of_report") or
        highlight.get("entity_name") or
        []
    )
    # Use highlight fragment count as lower-bound mention estimate
    mention_count = len(snippet_parts) if snippet_parts else 0

    location = (src.get("biz_locations") or [""])[0]

    return {
        "Company": primary_name,
        "Ticker": ticker,
        "Location": location,
        "Sector": sic_sector,
        "Industry": sic_industry,
        "Filing Type": form,
        "Filing Date": filed,
        "Mentions": mention_count,
        "Filing URL": filing_url,
        "_mcap_raw": None,
        "Market Cap": "N/A",
        "_raw_src": src,
    }


def fetch_mention_count(adsh, cik, keyword, timeout=8):
    """Fetch filing text and count exact keyword occurrences."""
    try:
        if not adsh or not cik:
            return None
        acc_clean = adsh.replace("-", "")
        index_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_clean}/{adsh}-index.htm"
        r = requests.get(index_url, headers={"User-Agent": "KiloCapital research@kilocapital.com"}, timeout=timeout)
        if not r.ok:
            return None
        # Find primary document (not exhibits)
        links = re.findall(r'href="(/Archives/edgar/data/[^"]+\.htm)"', r.text, re.IGNORECASE)
        doc_links = [l for l in links if not l.endswith("-index.htm")]
        if not doc_links:
            return None
        doc_url = "https://www.sec.gov" + doc_links[0]
        dr = requests.get(doc_url, headers={"User-Agent": "KiloCapital research@kilocapital.com"}, timeout=timeout, stream=True)
        if not dr.ok:
            return None
        # Read up to 2MB to keep it fast
        chunks = []
        size = 0
        for chunk in dr.iter_content(chunk_size=65536):
            chunks.append(chunk)
            size += len(chunk)
            if size > 2_000_000:
                break
        text = b"".join(chunks).decode("utf-8", errors="ignore")
        text = re.sub(r"<[^>]+>", " ", text).lower()
        kw = keyword.strip('"').lower()
        return text.count(kw)
    except Exception:
        return None



def fetch_one_ticker(ticker):
    try:
        t = yf.Ticker(ticker)
        mc = getattr(t.fast_info, "market_cap", None)
        try:
            full = t.info
            sector = full.get("sector") or None
            industry = full.get("industry") or None
        except Exception:
            sector = industry = None
        return ticker, {"market_cap": mc, "sector": sector, "industry": industry}
    except Exception:
        return ticker, {"market_cap": None, "sector": None, "industry": None}


@st.cache_data(ttl=300, show_spinner=False)
def get_ticker_info(tickers: tuple):
    info = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(fetch_one_ticker, t): t for t in tickers}
        for f in as_completed(futures):
            ticker, data = f.result()
            info[ticker] = data
    return info


def fmt_mcap(val):
    if val is None:
        return "N/A"
    if val >= 1e12: return f"${val/1e12:.2f}T"
    if val >= 1e9:  return f"${val/1e9:.2f}B"
    if val >= 1e6:  return f"${val/1e6:.0f}M"
    return f"${val:,.0f}"


def to_excel(df):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Results")
        ws = writer.sheets["Results"]
        for col in ws.columns:
            max_len = max(len(str(cell.value or "")) for cell in col)
            ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 60)
    return buf.getvalue()


# ── Main ───────────────────────────────────────────────────────────────────────
if run:
    if not keyword.strip():
        st.warning("Enter a keyword to search.")
    else:
        q = build_query(keyword, keyword2, keyword_or)
        all_hits = []
        total_available = None
        progress = st.progress(0, text="Fetching page 1...")

        for page in range(max_pages):
            # Stop early if we've already fetched everything available
            if total_available is not None and len(all_hits) >= total_available:
                break

            progress.progress((page) / max_pages, text=f"Fetching page {page+1} of {max_pages}...")
            data = fetch_page(q, filing_type, date_from, date_to, page)

            if "error" in data:
                st.warning(f"EDGAR stopped at page {page+1} — fetched {len(all_hits)} filings total.")
                break

            hits = data.get("hits", {}).get("hits", [])
            if total_available is None:
                total_available = data.get("hits", {}).get("total", {}).get("value", 0)

            all_hits.extend(hits)

            if len(hits) < 100:
                break  # no more pages

            time.sleep(0.3)  # be polite to SEC

        progress.progress(1.0, text=f"Done — fetched {len(all_hits)} filings from EDGAR (total available: {total_available:,})" if total_available is not None else f"Done — fetched {len(all_hits)} filings from EDGAR")

        if debug_mode and all_hits:
            st.subheader("First raw hit")
            st.json(all_hits[0])

        results = [parse_hit(h) for h in all_hits]

        if not results:
            st.info(f"No filings found for '{q}'.")
        else:
            if listed_only:
                results = [r for r in results if r["Ticker"]]

            if dedup:
                seen = {}
                for r in sorted(results, key=lambda x: x["Filing Date"], reverse=True):
                    key = r["Ticker"] or r["Company"]
                    if key not in seen:
                        seen[key] = r
                results = list(seen.values())

            tickers = tuple(set(r["Ticker"] for r in results if r["Ticker"]))

            if tickers:
                with st.spinner(f"Fetching market data for {len(tickers)} companies..."):
                    ticker_info = get_ticker_info(tickers)
            else:
                ticker_info = {}

            for r in results:
                ti = ticker_info.get(r["Ticker"], {})
                raw = ti.get("market_cap")
                r["_mcap_raw"] = raw
                r["Market Cap"] = fmt_mcap(raw)
                if ti.get("sector"):
                    r["Sector"] = ti["sector"]
                if ti.get("industry"):
                    r["Industry"] = ti["industry"]

            # Parallel keyword mention count from actual filings
            with st.spinner("Counting keyword mentions in filings..."):
                kw_clean = keyword.strip('"')
                def _count(r):
                    src = r.get("_raw_src", {})
                    adsh = src.get("adsh", "")
                    ciks = src.get("ciks", [])
                    cik = str(ciks[0]).lstrip("0") if ciks else ""
                    count = fetch_mention_count(adsh, cik, kw_clean)
                    # Fall back to highlight count if fetch fails
                    return count if count is not None else r.get("Mentions", 0)

                with ThreadPoolExecutor(max_workers=6) as ex:
                    futures = {ex.submit(_count, r): i for i, r in enumerate(results)}
                    for f in as_completed(futures):
                        i = futures[f]
                        results[i]["Mentions"] = f.result()

            if use_mcap:
                results = [r for r in results if r["_mcap_raw"] is not None
                           and r["_mcap_raw"] / 1e9 >= mcap_min
                           and r["_mcap_raw"] / 1e9 <= mcap_max]
            if sector_inc.strip():
                results = [r for r in results if sector_inc.lower() in r["Sector"].lower()]
            if sector_exc.strip():
                results = [r for r in results if sector_exc.lower() not in r["Sector"].lower()]
            if industry_inc.strip():
                results = [r for r in results if industry_inc.lower() in r["Industry"].lower()]
            if industry_exc.strip():
                results = [r for r in results if industry_exc.lower() not in r["Industry"].lower()]

            if sort_by == "Market Cap ↓":
                results.sort(key=lambda x: x["_mcap_raw"] or 0, reverse=True)
            elif sort_by == "Market Cap ↑":
                results.sort(key=lambda x: x["_mcap_raw"] or float("inf"))
            elif sort_by == "Filing Date ↓":
                results.sort(key=lambda x: x["Filing Date"], reverse=True)
            elif sort_by == "Company A–Z":
                results.sort(key=lambda x: x["Company"])

            if not results:
                st.info("No results match your filters.")
            else:
                valid_caps = [r["_mcap_raw"] for r in results if r["_mcap_raw"]]
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Results", len(results))
                m2.metric("With Market Cap", len(valid_caps))
                m3.metric("Median Market Cap", fmt_mcap(sorted(valid_caps)[len(valid_caps)//2]) if valid_caps else "N/A")
                m4.metric("Unique Sectors", len(set(r["Sector"] for r in results if r["Sector"] != "N/A")))

                df = pd.DataFrame(results)[[
                    "Company", "Ticker", "Location", "Sector", "Industry",
                    "Market Cap", "Filing Type", "Filing Date", "Mentions", "Filing URL"
                ]]

                df_display = df.copy()
                df_display["Filing URL"] = df_display["Filing URL"].apply(
                    lambda x: f'<a href="{x}" target="_blank">View</a>' if x != "N/A" else "N/A"
                )
                st.write(df_display.to_html(escape=False, index=False), unsafe_allow_html=True)

                c1, c2 = st.columns(2)
                with c1:
                    st.download_button("Download CSV",
                        data=df.to_csv(index=False).encode("utf-8"),
                        file_name=f"edgar_{keyword[:25].replace(' ','_')}.csv",
                        mime="text/csv")
                with c2:
                    st.download_button("Download Excel",
                        data=to_excel(df),
                        file_name=f"edgar_{keyword[:25].replace(' ','_')}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
