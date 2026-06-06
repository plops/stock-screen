[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/plops/stock-screen)

# S&P 500 Stock Screening & Macro Alignment Tool

This project provides a quantitative stock screening and backtesting pipeline that aligns equity investments with Federal Reserve monetary policy. The framework segments market conditions into four quadrants based on two core indicators: interest rates and central bank liquidity (balance sheet size). It then screens and categorizes S&P 500 companies into four matching risk/growth profiles.

## Installation & Requirements

We use `uv` for python dependency management. It automatically creates isolated environments and installs requirements (`pandas`, `yfinance`, `requests`, `matplotlib`, `tabulate`) using PEP 723 metadata.

If you don't have `uv` installed, get it here: https://github.com/astral-sh/uv

## How to Run

### 1. Run the Stock Screener
This script downloads Wikipedia S&P 500 data, downloads the Fed balance sheet (`WALCL`) and interest rates (`FEDFUNDS`) from FRED, and fetches yfinance fundamentals (Forward P/E, Debt-to-EBITDA, Revenue & EPS Growth) to classify the companies. All data is cached in a local SQLite database (`stock_data.db`) to avoid repeated API requests and rate limits.

```bash
# Run the screener (cached mode, polite rate-limiting, skips today's already fetched data)
uv run stock_screen.py

# Force refresh stock metrics (overwrites cached data for all stocks)
uv run stock_screen.py --refresh

# Force refresh FED macroeconomic data from FRED
uv run stock_screen.py --refresh-fed

# Run a quick test on the first 20 tickers
uv run stock_screen.py --limit 20
```

#### Resuming Failed Runs & Handling Rate Limits
If a run fails or gets interrupted (for instance, Yahoo Finance rate limits your IP after several hundred requests, causing some stocks to fail), **simply run the script again without flags**:
```bash
uv run stock_screen.py
```
Because the SQLite database caches successful daily metrics, the script will automatically detect and skip all successfully downloaded stocks for today, and **only fetch the missing ones**. 

*Tip: If you hit a rate limit block, wait 10–15 minutes for Yahoo's block to expire, then run the command to fill in the missing data. Repeat as necessary until all stocks are cached.*

**Outputs:**
- `stock_data.db` (SQLite Database cache)
- `screener_results.csv` (CSV of current S&P 500 company classifications)
- `report.md` (A beautiful Markdown report of the current regime, thresholds, and top candidates)

---

### 2. Run the Backtester
This script runs historical backtests to verify the video's investment theory. It downloads weekly closing prices for S&P 500 stocks since 2015 (cached in SQLite), maps historical FRED regimes, calculates forward-holding returns for the four stock cohorts, and generates comparative metrics.

```bash
# Run backtest with default parameters (12-month holding period, since 2015)
uv run backtest.py

# Run backtest with a 24-month holding period since 2018
uv run backtest.py --holding-months 24 --start-year 2018

# Force refresh historical stock price downloads
uv run backtest.py --refresh-prices
```

**Outputs:**
- `backtest_report.md` (Detailed Markdown report of historical holding returns by regime)
- `regimes_history.png` (Chart showing historical Fed Funds rate and balance sheet size, color-shaded by policy regime)
- `backtest_performance.png` (Bar chart comparing stock cohort returns across different Fed regimes)

---

## The Macro Quadrant Framework

We define the four regimes based on the Federal Funds Rate (Leitzins) and the 13-week change of Fed Assets (WALCL):

| Quadrant | Interest Rate Level | Balance Sheet Trend | Policy Regime | Recommended Stock Cohort |
| :--- | :--- | :--- | :--- | :--- |
| **Q1** | **Low** (Below historical median) | **QE** (Expanding WALCL) | Aggressive | **Q1 Profile (Aggressive)**: High growth, low debt, high valuation |
| **Q2** | **High** (Above historical median) | **QE** (Expanding WALCL) | Selective | **Q2 Profile (Moderate)**: Moderate growth, moderate debt |
| **Q3** | **Low** (Below historical median) | **QT** (Contracting WALCL) | Selective | **Q3 Profile (Value)**: Low P/E, high debt (leveraged but cheap) |
| **Q4** | **High** (Above historical median) | **QT** (Contracting WALCL) | Defensive | **Q4 Profile (Defensive)**: Stable, low debt, low valuation |

## Cohort Sorting and Candidate Selection

To find the companies expected to perform best in the future, the candidates are ranked by the **optimal metric matching the macro thesis** of their specific quadrant:

| Cohort | Target Macro Regime | Optimal Sorting Metric | Rationale & Selection Criteria |
| :--- | :--- | :--- | :--- |
| **Q1 (Aggressive)** | Low Interest / QE | **Revenue Growth** (descending) | In expansionary periods, the market rewards absolute market share expansion and top-line growth. |
| **Q2 (Moderate)** | High Interest / QE | **Earnings Growth** (descending) | High interest rates compress overall multiples. We select companies showing strong bottom-line growth to prove their resilience to capital costs. |
| **Q3 (Value)** | Low Interest / QT | **Revenue Growth** (descending) | Low rates aid leveraged companies. We prioritize those showing high top-line expansion capacity. |
| **Q4 (Defensive)** | High Interest / QT | **Lowest Forward P/E** (ascending) | In contracting/high-rate environments, valuation safety is paramount. We filter out unprofitable companies (Forward P/E ≤ 0) and sort from the cheapest positive P/E. |

