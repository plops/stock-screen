# Walkthrough: S&P 500 Stock Screening & Macro Alignment Tool

I have successfully created and verified the stock screening and backtesting system that aligns portfolio allocation with Federal Reserve policies. 

All scripts are written in Python using `uv` inline metadata for package management, and use a local SQLite database (`stock_data.db`) to cache data, preventing yfinance rate limits.

---

## 1. Accomplishments & Files Created

1. **[stock_screen.py](file:///home/kiel/stage/stock-screen/stock_screen.py)**: The screening script.
   - Fetches S&P 500 tickers from Wikipedia using a custom browser `User-Agent`.
   - Downloads Fed Balance Sheet (`WALCL`) and interest rates (`FEDFUNDS`) from FRED without needing an API key.
   - Classifies current stocks into four profiles (**Q1 Aggressive**, **Q2 Moderate**, **Q3 Value**, **Q4 Defensive**) based on S&P 500 relative medians of Forward P/E and Debt-to-EBITDA.
   - Fetches financial metrics in parallel (using `ThreadPoolExecutor`) to reduce download time from 10 minutes to under 1 minute.
   - Saves results to `screener_results.csv` and compiles a beautiful Markdown report `report.md`.
2. **[backtest.py](file:///home/kiel/stage/stock-screen/backtest.py)**: The backtesting engine.
   - Downloads and caches historical weekly closing prices since 2015 for the S&P 500.
   - Segments historical dates into QE vs. QT and High vs. Low interest rate regimes.
   - Performs a forward-holding return analysis for the four stock cohorts and saves the summary table to `backtest_report.md`.
   - Generates two Matplotlib charts (`regimes_history.png` and `backtest_performance.png`).
3. **[README.md](file:///home/kiel/stage/stock-screen/README.md)**: User guide explaining all CLI options, macro quadrants, and output files.
4. **[worklog.md](file:///home/kiel/stage/stock-screen/worklog.md)**: Development log tracking progress and resolving issues (Akamai blocks, Wikipedia 403s, yfinance column name updates, and NameErrors).

---

## 2. Verification & Test Results

### Current Screening Run
The screener successfully ran on **438 active S&P 500 companies** (with 65 companies skipped due to temporary Yahoo Finance rate limit blocks). It categorized the companies based on today's relative thresholds:
- **Median Forward P/E:** 16.79
- **Median Debt-to-EBITDA:** 2.57

The current Fed policy regime was calculated as **Q2: High Rate / QE (Selective)**.

### Historical Backtest Run (2015–Present)
The backtester successfully calculated the average **12-month forward returns** for each stock cohort under the four historical regimes:

| Regime | Observations (Weeks) | Q1 Cohort (Aggressive) | Q2 Cohort (Moderate) | Q3 Cohort (Value) | Q4 Cohort (Defensive) |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **Q1: Low Rate / QE** | 169 | 16.39% | 13.18% | 18.64% | **34.36%** |
| **Q2: High Rate / QE** | 48 | **96.27%** | 68.10% | 39.57% | 82.32% |
| **Q3: Low Rate / QT** | 43 | 37.54% | 31.69% | 36.07% | **65.76%** |
| **Q4: High Rate / QT** | 312 | **66.42%** | 30.86% | 6.71% | 35.63% |

---

## 3. Visualizations

Here is the historical mapping of Federal Reserve interest rates and balance sheet trends:

![Federal Reserve Policy Regimes History](/home/kiel/.gemini/antigravity/brain/d5ed401c-5e7a-41e5-b2a8-b14d2aaa3e02/regimes_history.png)

Here are the backtest results showing the performance of each stock cohort across the regimes:

![Stock Cohort Performance by Fed Regime](/home/kiel/.gemini/antigravity/brain/d5ed401c-5e7a-41e5-b2a8-b14d2aaa3e02/backtest_performance.png)
