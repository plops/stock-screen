# /// script
# dependencies = [
#   "pandas",
#   "yfinance",
#   "requests",
#   "lxml",
#   "tabulate",
# ]
# ///

import os
import sys
import io
import sqlite3
import argparse
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import yfinance as yf
import requests

DB_FILE = "stock_data.db"

def init_db():
    """Initialize SQLite database with required tables."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Enable foreign keys
    cursor.execute("PRAGMA foreign_keys = ON;")
    
    # Table for S&P 500 stock metadata
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS stocks (
        symbol TEXT PRIMARY KEY,
        name TEXT,
        sector TEXT,
        industry TEXT
    );
    """)
    
    # Table for daily stock metrics (cached)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS stock_metrics (
        symbol TEXT,
        date TEXT,
        revenue_growth REAL,
        earnings_growth REAL,
        forward_pe REAL,
        debt_to_ebitda REAL,
        market_cap REAL,
        PRIMARY KEY (symbol, date),
        FOREIGN KEY (symbol) REFERENCES stocks (symbol)
    );
    """)
    
    # Table for FED macro economic series
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS fed_macro (
        date TEXT PRIMARY KEY,
        walcl REAL,       -- Fed Assets (Total Assets)
        fedfunds REAL,    -- Fed Funds Rate (Effective)
        regime TEXT       -- Calculated Regime (Q1, Q2, Q3, Q4)
    );
    """)
    
    conn.commit()
    return conn

def fetch_sp500_tickers(conn):
    """Fetch S&P 500 tickers from Wikipedia and cache in DB."""
    print("Fetching S&P 500 tickers from Wikipedia...")
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    try:
        r = requests.get(url, headers=headers, timeout=10)
        tables = pd.read_html(io.StringIO(r.text))
        df = tables[0]
        
        # Rename columns to match database
        df = df.rename(columns={
            "Symbol": "symbol",
            "Security": "name",
            "GICS Sector": "sector",
            "GICS Sub-Industry": "industry"
        })
        
        # Clean symbols (yfinance uses - instead of . for classes, e.g. BRK.B -> BRK-B)
        df["symbol"] = df["symbol"].str.replace(".", "-", regex=False)
        
        # Insert/Update stocks table
        cursor = conn.cursor()
        for _, row in df.iterrows():
            cursor.execute("""
            INSERT OR REPLACE INTO stocks (symbol, name, sector, industry)
            VALUES (?, ?, ?, ?)
            """, (row["symbol"], row["name"], row["sector"], row["industry"]))
        conn.commit()
        
        print(f"Successfully cached {len(df)} tickers in database.")
        return df["symbol"].tolist()
    except Exception as e:
        print(f"Error fetching tickers from Wikipedia: {e}")
        # Fallback to already cached tickers if any
        cursor = conn.cursor()
        cursor.execute("SELECT symbol FROM stocks")
        tickers = [row[0] for row in cursor.fetchall()]
        if tickers:
            print(f"Using {len(tickers)} cached tickers from database.")
            return tickers
        raise e


def fetch_fed_data(conn):
    """Fetch FED data (WALCL and FEDFUNDS) from FRED and cache in DB."""
    print("Fetching Fed Balance Sheet (WALCL) and Fed Funds Rate (FEDFUNDS) from FRED...")
    
    # FRED CSV URLs
    walcl_url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=WALCL"
    fedfunds_url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=FEDFUNDS"
    
    try:
        import io
        
        # Fetch WALCL (Weekly, Wednesday)
        print("Downloading WALCL...")
        r_walcl = requests.get(walcl_url, timeout=10)
        r_walcl.raise_for_status()
        
        date_col_walcl = "DATE" if "DATE" in r_walcl.text else "observation_date"
        df_walcl = pd.read_csv(io.StringIO(r_walcl.text), parse_dates=[date_col_walcl])
        df_walcl.columns = ["date", "walcl"]
        # Replace '.' (missing values) with NaN and convert to float
        df_walcl["walcl"] = pd.to_numeric(df_walcl["walcl"].replace(".", pd.NA), errors="coerce")
        
        # Fetch FEDFUNDS (Monthly, 1st of month)
        print("Downloading FEDFUNDS...")
        r_fedfunds = requests.get(fedfunds_url, timeout=10)
        r_fedfunds.raise_for_status()
        
        date_col_fed = "DATE" if "DATE" in r_fedfunds.text else "observation_date"
        df_fedfunds = pd.read_csv(io.StringIO(r_fedfunds.text), parse_dates=[date_col_fed])
        df_fedfunds.columns = ["date", "fedfunds"]
        df_fedfunds["fedfunds"] = pd.to_numeric(df_fedfunds["fedfunds"].replace(".", pd.NA), errors="coerce")
        
        # Merge datasets using outer join on date, then sort
        df_macro = pd.merge(df_walcl, df_fedfunds, on="date", how="outer").sort_values("date")
        
        # Interpolate/fill missing values since WALCL is weekly and FEDFUNDS is monthly
        df_macro["walcl"] = df_macro["walcl"].ffill()
        df_macro["fedfunds"] = df_macro["fedfunds"].ffill()
        
        # Drop rows where both are null (before data starts)
        df_macro = df_macro.dropna(subset=["walcl", "fedfunds"])
        
        # Calculate regimes
        # 1. QE vs QT: Change in balance sheet over a 13-week (approx 3 months) window
        df_macro = df_macro.set_index("date")
        # Find values from 13 weeks ago
        df_macro["walcl_13w_ago"] = df_macro["walcl"].shift(13)
        df_macro["qe_qt"] = df_macro.apply(
            lambda r: "QE" if pd.isna(r["walcl_13w_ago"]) or r["walcl"] > r["walcl_13w_ago"] else "QT",
            axis=1
        )
        
        # 2. Interest Rate Level: High vs Low
        # We compute the median interest rate since 2000 as a robust historical threshold
        rates_since_2000 = df_macro.loc[df_macro.index >= "2000-01-01", "fedfunds"]
        median_rate = rates_since_2000.median() if not rates_since_2000.empty else 2.5
        print(f"Historical median Fed Funds rate (since 2000) used as threshold: {median_rate:.2f}%")
        
        df_macro["rate_level"] = df_macro["fedfunds"].apply(
            lambda r: "High" if r >= median_rate else "Low"
        )
        
        # Determine Quadrant
        # Q1: Low Rate / QE (Aggressive)
        # Q2: High Rate / QE (Selective/Speculative)
        # Q3: Low Rate / QT (Selective/Value)
        # Q4: High Rate / QT (Defensive)
        def map_regime(row):
            if row["rate_level"] == "Low" and row["qe_qt"] == "QE":
                return "Q1: Low Rate / QE (Aggressive)"
            elif row["rate_level"] == "High" and row["qe_qt"] == "QE":
                return "Q2: High Rate / QE (Selective)"
            elif row["rate_level"] == "Low" and row["qe_qt"] == "QT":
                return "Q3: Low Rate / QT (Selective)"
            else:
                return "Q4: High Rate / QT (Defensive)"
                
        df_macro["regime"] = df_macro.apply(map_regime, axis=1)
        
        # Reset index to write to DB
        df_macro = df_macro.reset_index()
        
        cursor = conn.cursor()
        for _, row in df_macro.iterrows():
            date_str = row["date"].strftime("%Y-%m-%d")
            cursor.execute("""
            INSERT OR REPLACE INTO fed_macro (date, walcl, fedfunds, regime)
            VALUES (?, ?, ?, ?)
            """, (date_str, float(row["walcl"]), float(row["fedfunds"]), row["regime"]))
        conn.commit()
        print(f"Fed data successfully updated up to {df_macro['date'].max().strftime('%Y-%m-%d')}.")
        
    except Exception as e:
        print(f"Error fetching Fed data: {e}")
        import traceback
        traceback.print_exc()

def fetch_single_stock_metrics(symbol, date_str):
    """Fetch metrics for a single stock using yfinance with fallback logic."""
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info
        
        # Basic fundamental fields
        rev_growth = info.get("revenueGrowth")
        eps_growth = info.get("earningsGrowth")
        forward_pe = info.get("forwardPE")
        debt_to_ebitda = info.get("debtToEbitda")
        market_cap = info.get("marketCap")
        
        # Fallback for Debt-to-EBITDA: Total Debt / EBITDA
        if debt_to_ebitda is None or pd.isna(debt_to_ebitda):
            total_debt = info.get("totalDebt")
            ebitda = info.get("ebitda")
            if total_debt is not None and ebitda is not None and ebitda != 0:
                debt_to_ebitda = total_debt / ebitda
                
        return {
            "symbol": symbol,
            "date": date_str,
            "revenue_growth": rev_growth,
            "earnings_growth": eps_growth,
            "forward_pe": forward_pe,
            "debt_to_ebitda": debt_to_ebitda,
            "market_cap": market_cap
        }
    except Exception as e:
        # yfinance can fail due to rate limits or missing ticker info
        return None

def fetch_all_stocks_data(tickers, conn, force_refresh=False):
    """Fetch fundamental data for S&P 500 tickers, caching results in database."""
    today_str = datetime.today().strftime("%Y-%m-%d")
    cursor = conn.cursor()
    
    # Find which tickers we already have data for today
    cursor.execute("SELECT symbol FROM stock_metrics WHERE date = ?", (today_str,))
    cached_symbols = set([row[0] for row in cursor.fetchall()])
    
    tickers_to_fetch = tickers if force_refresh else [t for t in tickers if t not in cached_symbols]
    
    if not tickers_to_fetch:
        print("All stock metrics are already cached for today.")
        return
        
    print(f"Need to fetch data for {len(tickers_to_fetch)} tickers (cached: {len(cached_symbols)}).")
    
    count = 0
    failures = 0
    max_workers = 10
    
    print(f"Starting parallel fetch using {max_workers} threads...")
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all tasks
        future_to_symbol = {
            executor.submit(fetch_single_stock_metrics, symbol, today_str): symbol 
            for symbol in tickers_to_fetch
        }
        
        for i, future in enumerate(as_completed(future_to_symbol)):
            symbol = future_to_symbol[future]
            try:
                metrics = future.result()
                if metrics:
                    cursor.execute("""
                    INSERT OR REPLACE INTO stock_metrics (
                        symbol, date, revenue_growth, earnings_growth, forward_pe, debt_to_ebitda, market_cap
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (
                        metrics["symbol"], metrics["date"], metrics["revenue_growth"], metrics["earnings_growth"],
                        metrics["forward_pe"], metrics["debt_to_ebitda"], metrics["market_cap"]
                    ))
                    conn.commit()
                    print(f"[{i+1}/{len(tickers_to_fetch)}] Fetched {symbol} successfully.")
                    count += 1
                else:
                    print(f"[{i+1}/{len(tickers_to_fetch)}] Failed to fetch metrics for {symbol}.")
                    failures += 1
            except Exception as exc:
                print(f"[{i+1}/{len(tickers_to_fetch)}] {symbol} generated an exception: {exc}")
                failures += 1
                
    print(f"Batch completed: {count} fetched, {failures} failed/skipped.")

def run_classification(conn, output_csv="screener_results.csv"):
    """Classify current stock database into quadrants using relative medians."""
    today_str = datetime.today().strftime("%Y-%m-%d")
    
    # Query stock metadata and latest metrics
    query = """
    SELECT s.symbol, s.name, s.sector, s.industry, 
           m.revenue_growth, m.earnings_growth, m.forward_pe, m.debt_to_ebitda, m.market_cap
    FROM stocks s
    JOIN stock_metrics m ON s.symbol = m.symbol
    WHERE m.date = (SELECT MAX(date) FROM stock_metrics)
    """
    df = pd.read_sql_query(query, conn)
    
    if df.empty:
        print("No stock metrics found in the database. Please fetch data first.")
        return None
        
    # Calculate market medians for categorization (for non-null and positive values where appropriate)
    # Forward P/E must be positive for standard multiple comparison
    pe_filter = df["forward_pe"] > 0
    med_pe = df.loc[pe_filter, "forward_pe"].median()
    
    # Debt to EBITDA
    debt_filter = df["debt_to_ebitda"].notna()
    med_debt = df.loc[debt_filter, "debt_to_ebitda"].median()
    
    print(f"Classification relative thresholds:")
    print(f"  - Median Forward P/E: {med_pe:.2f}")
    print(f"  - Median Debt/EBITDA: {med_debt:.2f}")
    
    def classify(row):
        pe = row["forward_pe"]
        debt = row["debt_to_ebitda"]
        
        if pd.isna(pe) or pd.isna(debt):
            return "Unclassified"
            
        high_pe = pe > med_pe
        high_debt = debt > med_debt
        
        # Quadrant Matching (from macro regimes profile recommendations):
        # Q4 Profile: Low P/E, Low Debt (Defensive)
        # Q1 Profile: High P/E, Low Debt (Aggressive/Speculative)
        # Q2 Profile: High P/E, High Debt (Selective/Leveraged Growth)
        # Q3 Profile: Low P/E, High Debt (Value/Established Leverage)
        if not high_pe and not high_debt:
            return "Q4 Profile (Defensive)"
        elif high_pe and not high_debt:
            return "Q1 Profile (Aggressive)"
        elif high_pe and high_debt:
            return "Q2 Profile (Moderate)"
        else:
            return "Q3 Profile (Value)"
            
    df["profile"] = df.apply(classify, axis=1)
    
    # Save results to CSV
    df.to_csv(output_csv, index=False)
    print(f"Screener results saved to {output_csv}")
    
    return df

def generate_report(conn, df_stocks, output_report="report.md"):
    """Generate a detailed Markdown report with macro regime and top stock candidates."""
    cursor = conn.cursor()
    
    # Calculate relative thresholds for display
    pe_filter = df_stocks["forward_pe"] > 0
    med_pe = df_stocks.loc[pe_filter, "forward_pe"].median() if not df_stocks[pe_filter].empty else 15.0
    
    debt_filter = df_stocks["debt_to_ebitda"].notna()
    med_debt = df_stocks.loc[debt_filter, "debt_to_ebitda"].median() if not df_stocks[debt_filter].empty else 2.0
    
    # Get latest Fed macro regime
    cursor.execute("SELECT date, walcl, fedfunds, regime FROM fed_macro ORDER BY date DESC LIMIT 1")
    latest_macro = cursor.fetchone()
    
    if not latest_macro:
        print("No Fed macro data available for report.")
        return
        
    macro_date, walcl, fedfunds, regime = latest_macro
    
    # Format Fed values
    # WALCL is in millions of dollars, convert to trillions for readability
    walcl_trillions = walcl / 1000000.0
    
    # Sector distribution across quadrants
    sector_quadrant = pd.crosstab(df_stocks["sector"], df_stocks["profile"])
    sector_table = sector_quadrant.to_markdown()
    
    # Get top 5 stocks for each profile (sorted by market cap or revenue growth)
    profile_details = ""
    for p in ["Q1 Profile (Aggressive)", "Q2 Profile (Moderate)", "Q3 Profile (Value)", "Q4 Profile (Defensive)"]:
        sub_df = df_stocks[df_stocks["profile"] == p].copy()
        # Sort by Market Cap descending
        sub_df = sub_df.sort_values("market_cap", ascending=False)
        top_5 = sub_df.head(5)
        
        profile_details += f"### {p} Candidates\n"
        profile_details += "Top 5 companies by Market Capitalization:\n\n"
        
        table_rows = []
        for _, row in top_5.iterrows():
            mcap_b = f"${row['market_cap'] / 1e9:.1f}B" if not pd.isna(row['market_cap']) else "N/A"
            pe_val = f"{row['forward_pe']:.1f}" if not pd.isna(row['forward_pe']) else "N/A"
            debt_val = f"{row['debt_to_ebitda']:.2f}" if not pd.isna(row['debt_to_ebitda']) else "N/A"
            rev_val = f"{row['revenue_growth']*100:.1f}%" if not pd.isna(row['revenue_growth']) else "N/A"
            eps_val = f"{row['earnings_growth']*100:.1f}%" if not pd.isna(row['earnings_growth']) else "N/A"
            
            table_rows.append(
                f"| **{row['symbol']}** | {row['name']} | {row['sector']} | {mcap_b} | {pe_val} | {debt_val} | {rev_val} | {eps_val} |"
            )
            
        profile_details += "| Ticker | Name | Sector | Market Cap | Forward P/E | Debt/EBITDA | Rev Growth | EPS Growth |\n"
        profile_details += "|---|---|---|---|---|---|---|---|\n"
        profile_details += "\n".join(table_rows) + "\n\n"
        
    report_content = f"""# S&P 500 Stock Screening & Macro Alignment Report

**Date of Report:** {datetime.today().strftime("%Y-%m-%d")}
**Database Last Updated:** {df_stocks['symbol'].count()} stocks screened

## 1. Current Federal Reserve Policy & Macro Regime

According to the latest Federal Reserve statistical releases, here is the current macroeconomic posture:

* **Observation Date:** {macro_date}
* **Federal Funds Rate:** {fedfunds:.2f}%
* **Balance Sheet Size (Assets):** ${walcl_trillions:.3f} Trillion
* **Current Macro Regime:** **{regime}**

### Action Recommendation
Based on the current macro regime (**{regime}**), the recommended strategic asset allocation is:
"""
    
    # Add custom recommendation text
    if "Q1" in regime:
        report_content += """* **Allocation Strategy:** **Aggressive (Maximize growth exposure)**
* **Target Profile:** **Q1 Profile (Aggressive)**
* **Rationale:** Low interest rates reduce cost of capital and boost valuations. QE injects liquidity. Speculative, high-growth, lower-debt companies tend to dramatically outperform as multiples expand.
"""
    elif "Q2" in regime:
        report_content += """* **Allocation Strategy:** **Selective (Moderate Growth / Moderate Leverage)**
* **Target Profile:** **Q2 Profile (Moderate)**
* **Rationale:** Interest rates are higher (compressing overall market valuations), but the Fed is still injecting liquidity (QE). Select companies with moderate growth and manageable leverage that can withstand higher interest costs while taking advantage of expansionary liquidity.
"""
    elif "Q3" in regime:
        report_content += """* **Allocation Strategy:** **Selective (High Growth / High Leverage)**
* **Target Profile:** **Q3 Profile (Value)**
* **Rationale:** Rates are low, which reduces the debt burden on leveraged companies, but the balance sheet is shrinking (QT), constraining broad market liquidity. Focus on established companies with higher leverage that benefit from low refinancing rates but possess stable revenues.
"""
    else: # Q4
        report_content += """* **Allocation Strategy:** **Defensive (Capital Preservation / Value)**
* **Target Profile:** **Q4 Profile (Defensive)**
* **Rationale:** High interest rates compress P/E multiples and raise refinancing risks, while QT drains market liquidity. Focus heavily on stable, low-debt, low-valuation companies (Stock A) to protect capital and avoid multiple compression. High cash allocation (up to 20%) is advised.
"""

    report_content += f"""
## 2. Relative Screening Thresholds

The screener classifies stocks using **relative thresholds** based on the medians of all active S&P 500 stocks. This ensures classifications adjust dynamically to the market environment.

* **Median Forward P/E:** {med_pe:.2f}
* **Median Debt-to-EBITDA:** {med_debt:.2f}

## 3. Sector Distribution Across Profiles

This matrix displays the count of companies in each GICS Sector categorized into the 4 investment profiles:

{sector_table}

## 4. Top Candidate Screen Results

Below are the top candidate companies matching each profile, ranked by Market Capitalization.

{profile_details}

***

*Disclaimer: This report is generated programmatically for educational and research purposes based on public market data. It does not constitute formal financial advice.*
"""

    with open(output_report, "w") as f:
        f.write(report_content)
    print(f"Report saved to {output_report}")

def main():
    parser = argparse.ArgumentParser(description="S&P 500 Macro Alignment Stock Screener")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of stocks to fetch (for testing)")
    parser.add_argument("--refresh", action="store_true", help="Force refresh of stock metrics from Yahoo Finance")
    parser.add_argument("--refresh-fed", action="store_true", help="Force refresh of Fed economic data")
    args = parser.parse_args()
    
    conn = init_db()
    
    # 1. Fetch Fed Data if needed
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM fed_macro")
    macro_count = cursor.fetchone()[0]
    if macro_count == 0 or args.refresh_fed:
        fetch_fed_data(conn)
    else:
        print("Fed economic data already exists in database. Use --refresh-fed to update.")
        
    # 2. Fetch S&P 500 Tickers
    tickers = fetch_sp500_tickers(conn)
    if args.limit:
        tickers = tickers[:args.limit]
        print(f"Testing mode enabled. Limited run to first {len(tickers)} tickers.")
        
    # 3. Fetch Stock Data
    fetch_all_stocks_data(tickers, conn, force_refresh=args.refresh)
    
    # 4. Classify Stocks
    df_stocks = run_classification(conn)
    
    # 5. Generate Report
    if df_stocks is not None:
        generate_report(conn, df_stocks)
        
    conn.close()
    print("Done!")

if __name__ == "__main__":
    main()
