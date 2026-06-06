# /// script
# dependencies = [
#   "pandas",
#   "yfinance",
#   "requests",
#   "matplotlib",
#   "tabulate",
# ]
# ///

import os
import sqlite3
import argparse
from datetime import datetime, timedelta
import pandas as pd
import yfinance as yf
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

DB_FILE = "stock_data.db"

def init_historical_prices_table(conn):
    """Create a table to cache historical stock prices."""
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS historical_prices (
        symbol TEXT,
        date TEXT,
        close REAL,
        PRIMARY KEY (symbol, date)
    );
    """)
    conn.commit()

def fetch_and_cache_historical_prices(tickers, conn, start_date="2015-01-01", force_refresh=False):
    """Download historical weekly closing prices in a single batch and cache in DB."""
    init_historical_prices_table(conn)
    cursor = conn.cursor()
    
    # Check which tickers are missing
    if force_refresh:
        tickers_to_download = tickers
    else:
        cursor.execute("SELECT DISTINCT symbol FROM historical_prices")
        cached_tickers = set([row[0] for row in cursor.fetchall()])
        tickers_to_download = [t for t in tickers if t not in cached_tickers]
        
    if not tickers_to_download:
        print("All requested historical prices are already cached in database.")
        return
        
    print(f"Downloading historical weekly prices for {len(tickers_to_download)} tickers since {start_date}...")
    
    # yfinance download in batch
    try:
        # Download weekly adjusted close prices
        df_prices = yf.download(tickers_to_download, start=start_date, interval="1wk")["Close"]
        
        # Handle single ticker case
        if isinstance(df_prices, pd.Series):
            df_prices = df_prices.to_frame(name=tickers_to_download[0])
            
        if df_prices.empty:
            print("Warning: Downloaded data is empty. No historical prices could be retrieved.")
            return
            
        # Melt dataframe to long format
        df_long = df_prices.reset_index().melt(id_vars=["Date"], var_name="symbol", value_name="close")
        df_long = df_long.dropna(subset=["close"])
        df_long["date"] = df_long["Date"].dt.strftime("%Y-%m-%d")
        
        # Batch insert/update into SQLite
        print("Caching historical prices in database...")
        placeholders = ",".join(["?"] * len(tickers_to_download))
        cursor.execute(f"DELETE FROM historical_prices WHERE symbol IN ({placeholders})", tickers_to_download)
        
        records = df_long[["symbol", "date", "close"]].to_records(index=False)
        cursor.executemany("""
        INSERT OR REPLACE INTO historical_prices (symbol, date, close)
        VALUES (?, ?, ?)
        """, list(records))
        conn.commit()
        print(f"Successfully cached {len(df_long)} price records in database.")
    except Exception as e:
        print(f"Error downloading historical prices: {e}")
        import traceback
        traceback.print_exc()

def load_historical_prices(conn, tickers):
    """Load cached historical prices from database for specified tickers."""
    placeholders = ",".join(["?"] * len(tickers))
    query = f"SELECT symbol, date, close FROM historical_prices WHERE symbol IN ({placeholders})"
    df = pd.read_sql_query(query, conn, params=tickers, parse_dates=["date"])
    # Pivot back to wide format
    df_pivot = df.pivot(index="date", columns="symbol", values="close")
    return df_pivot

def run_backtest(conn, tickers, index_name="sp500", holding_period_months=12, start_year=2015):
    """Run forward holding return analysis grouped by historical macro regimes."""
    print(f"Running backtest with {holding_period_months}-month holding period since {start_year}...")
    
    # 1. Load macro data
    df_macro = pd.read_sql_query("SELECT date, walcl, fedfunds, regime FROM fed_macro", conn, parse_dates=["date"])
    df_macro = df_macro[df_macro["date"].dt.year >= start_year].sort_values("date")
    
    # 2. Get today's classified stock profiles for active tickers only
    placeholders = ",".join(["?"] * len(tickers))
    query = f"""
    SELECT s.symbol, s.sector, m.revenue_growth, m.earnings_growth, m.forward_pe, m.debt_to_ebitda, m.market_cap
    FROM stocks s
    JOIN stock_metrics m ON s.symbol = m.symbol
    WHERE m.date = (SELECT MAX(date) FROM stock_metrics)
      AND s.symbol IN ({placeholders})
    """
    df_stocks = pd.read_sql_query(query, conn, params=tickers)
    
    if df_stocks.empty:
        print("Error: No stock screening data found in DB. Run stock_screen.py first.")
        return
        
    # Recalculate medians for classification
    pe_filter = df_stocks["forward_pe"] > 0
    med_pe = df_stocks.loc[pe_filter, "forward_pe"].median() if not df_stocks[pe_filter].empty else 15.0
    debt_filter = df_stocks["debt_to_ebitda"].notna()
    med_debt = df_stocks.loc[debt_filter, "debt_to_ebitda"].median() if not df_stocks[debt_filter].empty else 2.0
    
    # Classify current stocks
    def classify(row):
        pe = row["forward_pe"]
        debt = row["debt_to_ebitda"]
        if pd.isna(pe) or pd.isna(debt):
            return None
        high_pe = pe > med_pe
        high_debt = debt > med_debt
        if not high_pe and not high_debt: return "Q4: Defensive"
        elif high_pe and not high_debt: return "Q1: Aggressive"
        elif high_pe and high_debt: return "Q2: Moderate"
        else: return "Q3: Value"
        
    df_stocks["profile"] = df_stocks.apply(classify, axis=1)
    df_stocks = df_stocks.dropna(subset=["profile"])
    
    # Group tickers by profile
    cohorts = {}
    for profile in ["Q1: Aggressive", "Q2: Moderate", "Q3: Value", "Q4: Defensive"]:
        cohorts[profile] = df_stocks[df_stocks["profile"] == profile]["symbol"].tolist()
        print(f"Cohort {profile}: {len(cohorts[profile])} stocks.")
        
    # 3. Load historical prices
    df_prices = load_historical_prices(conn, tickers)
    if df_prices.empty:
        print("Error: No historical prices found. Download them first.")
        return
        
    # 4. Perform backtest calculations
    # Align dates between macro and prices
    dates = df_macro["date"].tolist()
    
    results = []
    
    for idx, date in enumerate(dates):
        # Find price at date
        # Use ffill to find closest available date in prices dataframe
        closest_price_idx = df_prices.index.get_indexer([date], method="pad")[0]
        if closest_price_idx == -1:
            continue
            
        start_date_actual = df_prices.index[closest_price_idx]
        
        # Calculate target end date
        end_date_target = start_date_actual + timedelta(days=holding_period_months * 30.5)
        closest_end_idx = df_prices.index.get_indexer([end_date_target], method="pad")[0]
        if closest_end_idx == -1 or closest_end_idx <= closest_price_idx:
            continue
            
        end_date_actual = df_prices.index[closest_end_idx]
        
        # Ensure we have enough data (i.e. we are not looking into the future)
        if end_date_actual > df_prices.index[-1] - timedelta(days=7):
            continue
            
        # Get active macro regime
        regime = df_macro.iloc[idx]["regime"]
        
        # Compute returns for each cohort
        row_res = {"date": start_date_actual, "regime": regime}
        
        for profile, tickers_list in cohorts.items():
            valid_tickers = [t for t in tickers_list if t in df_prices.columns]
            if not valid_tickers:
                continue
                
            p_start = df_prices.loc[start_date_actual, valid_tickers]
            p_end = df_prices.loc[end_date_actual, valid_tickers]
            
            # Calculate individual stock returns
            stock_returns = (p_end - p_start) / p_start
            # Drop NaNs
            stock_returns = stock_returns.dropna()
            
            if not stock_returns.empty:
                # Average return of this cohort
                row_res[profile] = stock_returns.mean()
            else:
                row_res[profile] = None
                
        results.append(row_res)
        
    df_results = pd.DataFrame(results).dropna()
    
    if df_results.empty:
        print("No backtest results could be calculated. Check date alignments.")
        return
        
    # 5. Group by Regime and analyze performance
    summary_data = []
    regimes_list = [
        "Q1: Low Rate / QE (Aggressive)",
        "Q2: High Rate / QE (Selective)",
        "Q3: Low Rate / QT (Selective)",
        "Q4: High Rate / QT (Defensive)"
    ]
    
    # We want to display average holding period return
    print("\n--- Backtest Results (Average Forward Holding Period Returns) ---")
    for r in regimes_list:
        sub_df = df_results[df_results["regime"] == r]
        if sub_df.empty:
            continue
            
        q1_ret = sub_df["Q1: Aggressive"].mean() * 100
        q2_ret = sub_df["Q2: Moderate"].mean() * 100
        q3_ret = sub_df["Q3: Value"].mean() * 100
        q4_ret = sub_df["Q4: Defensive"].mean() * 100
        
        count = len(sub_df)
        summary_data.append({
            "Regime": r.split(" (")[0], # Short name
            "Observations (Weeks)": count,
            "Q1 Cohort (Aggressive)": f"{q1_ret:.2f}%",
            "Q2 Cohort (Moderate)": f"{q2_ret:.2f}%",
            "Q3 Cohort (Value)": f"{q3_ret:.2f}%",
            "Q4 Cohort (Defensive)": f"{q4_ret:.2f}%"
        })
        
    df_summary = pd.DataFrame(summary_data)
    print(df_summary.to_markdown(index=False))
    
    index_names_map = {
        "sp500": "S&P 500",
        "nasdaq100": "Nasdaq-100",
        "smi": "Swiss Market Index (SMI)",
        "eurostoxx50": "Euro Stoxx 50",
        "custom": "Custom Ticker List"
    }
    index_display = index_names_map.get(index_name, index_name.upper())
    
    report_file = "backtest_report.md" if index_name == "sp500" else f"backtest_report_{index_name}.md"
    regimes_img = "regimes_history.png" if index_name == "sp500" else f"regimes_history_{index_name}.png"
    perf_img = "backtest_performance.png" if index_name == "sp500" else f"backtest_performance_{index_name}.png"
    
    # Save backtest report to text
    with open(report_file, "w") as f:
        f.write(f"""# {index_display} Macro Alignment Backtesting Report

This report evaluates the **Macro-Sensitive Stock Screening Framework** by analyzing the historical returns of the four current {index_display} stock cohorts under different monetary policy regimes.

* **Analysis Period:** {start_year} to present
* **Holding Period:** {holding_period_months} Months

---

## Core Concepts & Methodology

### 1. What is a Cohort?
A **cohort** is a group of companies that share similar financial characteristics. In this framework, the {index_display} companies are classified into four distinct investment profiles (cohorts) based on their relative **Forward P/E (valuation)** and **Debt-to-EBITDA (leverage)**:
- **Q1 Cohort (Aggressive):** High valuation (above market median), Low leverage (below market median).
- **Q2 Cohort (Moderate):** High valuation, High leverage.
- **Q3 Cohort (Value):** Low valuation, High leverage.
- **Q4 Cohort (Defensive):** Low valuation, Low leverage.

### 2. Backtest Execution: When are Stocks Bought and Sold?
To verify the macro theory, the backtester simulates a **rolling entry and holding strategy**:
- **Buying:** For **every week** in history since {start_year}, the backtester determines which Federal Reserve monetary policy regime was active on that week (based on interest rates and balance sheet trends). It simulates "buying" an equal-weighted basket of stocks in each cohort on that specific date.
- **Holding:** The backtester holds the stocks for a fixed period of **{holding_period_months} months** (1 year by default).
- **Selling:** The stocks are "sold" exactly at the end of the {holding_period_months}-month holding period.
- **Aggregation:** Finally, the backtester groups all simulated trades by the Fed policy regime that was active at the *buy date* and calculates the average forward return for each cohort. This tells us: *"If I buy a specific stock cohort on a day when the Fed is in Regime X, what is my average expected return after holding for {holding_period_months} months?"*

---

## Historical Holding Period Returns by Regime

The table below shows the average **{holding_period_months}-month forward return** for each stock cohort, grouped by the Federal Reserve regime in place at the time of purchase.

{df_summary.to_markdown(index=False)}

---

## Key Findings & Theory Validation

According to the macro-alignment investment theory:
1. **Q1 (Low Rate / QE) - Aggressive Phase:** Q1: Aggressive stock profile (high growth, low debt, high valuation) should perform best.
2. **Q2 (High Rate / QE) - Selective Phase:** Q2: Moderate stock profile (moderate growth, moderate debt) should perform best.
3. **Q3 (Low Rate / QT) - Selective Phase:** Q3: Value stock profile (high growth, high debt) should perform best due to low interest rates aiding leverage.
4. **Q4 (High Rate / QT) - Defensive Phase:** Q4: Defensive stock profile (stable, low debt, low valuation) should perform best (or experience the lowest drawdowns).

*Refer to the generated charts `{perf_img}` and `{regimes_img}` for visual verification.*
""")
    print(f"Backtest report saved to {report_file}")
    
    # 6. Generate Plots
    generate_charts(df_macro, df_results, regimes_list, index_name=index_name)

def generate_charts(df_macro, df_results, regimes_list, index_name="sp500"):
    """Generate Matplotlib charts for Fed regimes and backtest returns."""
    print("Generating performance charts...")
    
    # Chart 1: Fed Macro Regime History
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
    
    # Plot Fed Funds Rate
    ax1.plot(df_macro["date"], df_macro["fedfunds"], color="blue", linewidth=2, label="Fed Funds Rate")
    ax1.set_ylabel("Fed Funds Rate (%)", color="blue")
    ax1.tick_params(axis="y", labelcolor="blue")
    ax1.set_title("Federal Reserve Policy Regimes History (2015-Present)")
    
    # Plot Balance Sheet (WALCL) in Trillions
    ax2.plot(df_macro["date"], df_macro["walcl"] / 1000000.0, color="green", linewidth=2, label="Fed Assets (Trillion)")
    ax2.set_ylabel("Fed Assets (Trillions of $)", color="green")
    ax2.tick_params(axis="y", labelcolor="green")
    
    # Color background based on regime
    # Q1: Light green, Q2: Light blue, Q3: Light yellow, Q4: Light red
    colors = {
        "Q1: Low Rate / QE (Aggressive)": "#e2f0d9", # soft green
        "Q2: High Rate / QE (Selective)": "#ddebf7", # soft blue
        "Q3: Low Rate / QT (Selective)": "#fff2cc",  # soft yellow
        "Q4: High Rate / QT (Defensive)": "#fce4d6"  # soft orange/red
    }
    
    # Shade background periods
    # Group contiguous dates with same regime
    df_macro["regime_group"] = (df_macro["regime"] != df_macro["regime"].shift()).cumsum()
    groups = df_macro.groupby(["regime_group", "regime"])
    
    first_legend = {}
    for (group_num, regime), group_df in groups:
        start_date = group_df["date"].min()
        end_date = group_df["date"].max()
        color = colors.get(regime, "#ffffff")
        
        # Plot span in both axes
        ax1.axvspan(start_date, end_date, color=color, alpha=0.6, label=regime if regime not in first_legend else "")
        ax2.axvspan(start_date, end_date, color=color, alpha=0.6)
        first_legend[regime] = True
        
    ax1.legend(loc="upper left", framealpha=0.9, fontsize=9)
    plt.xlabel("Date")
    plt.tight_layout()
    regimes_img = "regimes_history.png" if index_name == "sp500" else f"regimes_history_{index_name}.png"
    plt.savefig(regimes_img, dpi=150)
    plt.close()
    
    # Chart 2: Average Return by Regime (Bar Chart)
    # Summarize mean returns
    regime_short_names = []
    q1_means, q2_means, q3_means, q4_means = [], [], [], []
    
    for r in regimes_list:
        sub_df = df_results[df_results["regime"] == r]
        if sub_df.empty:
            continue
        regime_short_names.append(r.split(":")[0])  # E.g. "Q1"
        q1_means.append(sub_df["Q1: Aggressive"].mean() * 100)
        q2_means.append(sub_df["Q2: Moderate"].mean() * 100)
        q3_means.append(sub_df["Q3: Value"].mean() * 100)
        q4_means.append(sub_df["Q4: Defensive"].mean() * 100)
        
    if not regime_short_names:
        return
        
    import numpy as np
    x = np.arange(len(regime_short_names))
    width = 0.18
    
    fig, ax = plt.subplots(figsize=(10, 6))
    ax.bar(x - 1.5*width, q1_means, width, label="Q1: Aggressive Cohort", color="#1f4e78")
    ax.bar(x - 0.5*width, q2_means, width, label="Q2: Moderate Cohort", color="#2f5597")
    ax.bar(x + 0.5*width, q3_means, width, label="Q3: Value Cohort", color="#8faadc")
    ax.bar(x + 1.5*width, q4_means, width, label="Q4: Defensive Cohort", color="#c5e0b4")
    
    ax.set_ylabel("Average Forward 12-Month Return (%)")
    ax.set_title("Stock Cohort Performance by Federal Reserve Regime")
    ax.set_xticks(x)
    ax.set_xticklabels(regime_short_names)
    ax.axhline(0, color="black", linewidth=0.8, linestyle="--")
    ax.legend(framealpha=0.9)
    ax.grid(axis="y", linestyle=":", alpha=0.6)
    
    # Add labels on bars
    for p in ax.patches:
        height = p.get_height()
        ax.annotate(f"{height:.1f}%",
                    xy=(p.get_x() + p.get_width() / 2, height),
                    xytext=(0, 3 if height >= 0 else -12),  # 3 points vertical offset
                    textcoords="offset points",
                    ha='center', va='bottom', fontsize=7)
                    
    plt.tight_layout()
    perf_img = "backtest_performance.png" if index_name == "sp500" else f"backtest_performance_{index_name}.png"
    plt.savefig(perf_img, dpi=150)
    plt.close()
    print(f"Charts successfully saved as {regimes_img} and {perf_img}")

def main():
    parser = argparse.ArgumentParser(description="Stock Screen Theory Backtester")
    parser.add_argument("--index", type=str, default="sp500", choices=["sp500", "nasdaq100", "smi", "eurostoxx50"], help="Wikipedia stock index to backtest")
    parser.add_argument("--tickers", type=str, default=None, help="Comma-separated custom tickers list (ignores --index)")
    parser.add_argument("--refresh-prices", action="store_true", help="Force download of historical prices")
    parser.add_argument("--start-year", type=int, default=2015, help="Start year for backtest (default: 2015)")
    parser.add_argument("--holding-months", type=int, default=12, help="Holding period in months (default: 12)")
    args = parser.parse_args()
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Get Tickers List
    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        index_name = "custom"
        # Register custom tickers in database if they don't exist
        for t in tickers:
            cursor.execute("""
            INSERT OR IGNORE INTO stocks (symbol, name, sector, industry, market_index)
            VALUES (?, ?, 'Custom', 'Custom', 'custom')
            """, (t, f"Custom Stock {t}"))
        conn.commit()
        print(f"Using {len(tickers)} custom tickers.")
    else:
        index_name = args.index
        cursor.execute("SELECT symbol FROM stocks WHERE market_index = ?", (index_name,))
        tickers = [row[0] for row in cursor.fetchall()]
        
    if not tickers:
        print(f"Error: No tickers found for index '{index_name}' in stocks table. Please run stock_screen.py first to fetch them.")
        conn.close()
        return
        
    # Download and cache historical weekly prices
    fetch_and_cache_historical_prices(tickers, conn, force_refresh=args.refresh_prices)
    
    # Run backtest
    run_backtest(conn, tickers, index_name=index_name, holding_period_months=args.holding_months, start_year=args.start_year)
    
    conn.close()
    print("Backtesting completed successfully!")

if __name__ == "__main__":
    main()
