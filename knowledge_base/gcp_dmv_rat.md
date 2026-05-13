# gcp_dmv_rat.py - Financial Ratios & Risk Metrics Calculator

## Overview
This script is the **advanced financial analysis module** of the CryptoPrism-DB system, calculating sophisticated risk-adjusted performance ratios including Alpha, Beta, Sharpe, Sortino, Treynor, Omega, Information Ratio, Win/Loss ratios, and Risk of Ruin metrics using Bitcoin as the benchmark for comparative analysis.

## Detailed Functionality

### **Core Financial Ratios**

#### **1. Alpha (Market-Adjusted Returns)**
```python
def calculate_alpha(group, benchmark_avg_return):
    avg_return = group['m_pct_1d'].mean()
    return pd.Series({'m_rat_alpha': avg_return - benchmark_avg_return})
```
- **Definition**: Excess return over Bitcoin benchmark
- **Application**: Measures outperformance vs. market leader
- **Signal**: Positive alpha = outperforming Bitcoin

#### **2. Beta (Market Sensitivity)**
```python  
def calculate_beta(group, benchmark_returns):
    covariance = group['m_pct_1d'].cov(aligned_benchmark)
    benchmark_variance = benchmark_returns.var()
    beta = covariance / benchmark_variance
```
- **Definition**: Price sensitivity relative to Bitcoin movements
- **Range**: Beta > 1 = more volatile, Beta < 1 = less volatile
- **Risk Assessment**: Higher beta = higher systematic risk

#### **3. Sharpe Ratio (Risk-Adjusted Returns)**
```python
def calculate_sharpe_ratio(group, risk_free_rate=0.0):
    excess_returns = returns - risk_free_rate
    sharpe_ratio = excess_returns.mean() / excess_returns.std()
```
- **Purpose**: Return per unit of total risk
- **Benchmark**: Higher Sharpe = better risk-adjusted performance
- **Application**: Portfolio optimization and comparison

#### **4. Sortino Ratio (Downside Risk-Adjusted)**
```python
def calculate_sortino_ratio(group, risk_free_rate=0.0):
    excess_returns = returns - risk_free_rate
    downside_returns = excess_returns[excess_returns < 0]
    downside_deviation = downside_returns.std()
    sortino_ratio = excess_returns.mean() / downside_deviation
```
- **Focus**: Only penalizes downside volatility
- **Advantage**: Better for asymmetric return distributions
- **Usage**: Preferred over Sharpe for crypto assets

#### **5. Treynor Ratio (Systematic Risk-Adjusted)**
```python
def calculate_treynor_ratio(group, beta_values):
    avg_return = returns.mean()
    treynor_ratio = (avg_return - risk_free_rate) / beta
```
- **Definition**: Return per unit of systematic risk (beta)
- **Application**: Measures efficiency of systematic risk taking
- **Comparison**: Higher Treynor = better systematic risk management

### **Advanced Risk Metrics**

#### **6. Omega Ratio (Threshold-Based Performance)**
```python
def calculate_omega_ratio(group, benchmark_returns):
    excess_returns = returns - aligned_benchmark
    avg_gain = excess_returns[excess_returns > 0].mean()
    avg_loss = abs(excess_returns[excess_returns < 0].mean())
    omega_ratio = avg_gain / avg_loss
```
- **Calculation**: Average gains divided by average losses vs. Bitcoin
- **Threshold**: Uses Bitcoin returns as performance threshold
- **Interpretation**: Omega > 1 = more gains than losses relative to benchmark

#### **7. Information Ratio (Active Management Efficiency)**
```python
def calculate_information_ratio(group, benchmark_returns):
    active_returns = returns - aligned_benchmark
    tracking_error = active_returns.std()
    information_ratio = active_returns.mean() / tracking_error
```
- **Purpose**: Risk-adjusted active return vs. Bitcoin
- **Application**: Measures consistency of outperformance
- **Usage**: Active portfolio management evaluation

#### **8. Win/Loss & Win Rate Analysis**
```python
def calculate_winloss_ratio(group):
    wins = (returns > 0).sum()
    losses = (returns < 0).sum()
    winloss_ratio = wins / losses

def calculate_win_rate(group):
    wins = (returns > 0).sum()
    total = len(returns)
    win_rate = wins / total
```
- **Win/Loss Ratio**: Number of winning periods vs. losing periods
- **Win Rate**: Percentage of profitable periods
- **Trading Insight**: Higher ratios indicate consistent performance

#### **9. Risk of Ruin**
```python
def calculate_risk_of_ruin(group):
    ror = ((1 - win_rate) / win_rate) ** n
```
- **Definition**: Probability of total portfolio loss
- **Formula**: Based on win rate and number of periods
- **Risk Management**: Lower ROR = safer investment profile

#### **10. Gain to Pain Ratio**
```python
def calculate_gain_to_pain(group):
    total_gain = returns[returns > 0].sum()
    total_pain = abs(returns[returns < 0]).sum()
    gain_to_pain_ratio = total_gain / total_pain
```
- **Calculation**: Sum of positive returns / Sum of absolute negative returns
- **Interpretation**: Higher ratio = more reward relative to pain experienced
- **Behavioral Finance**: Accounts for loss aversion psychology

### **Binary Signal Generation Framework**

#### **Signal Logic**
```python
def generate_binary_signals_ratios(ratios_df):
    # Standard ratios: positive = buy (1), negative = sell (-1)
    for col in ['m_rat_alpha', 'd_rat_beta', 'v_rat_sharpe', 'v_rat_sortino']:
        ratios_df[col + '_bin'] = np.sign(ratios_df[col]).fillna(0).astype(int)
    
    # Custom thresholds for specific ratios
    ratios_df['v_rat_information_bin'] = np.select(
        [ratios_df['v_rat_information'] < -0.1, ratios_df['v_rat_information'] >= -0.1], 
        [-1, 1], default=0
    )
    
    ratios_df['m_rat_win_rate_bin'] = np.select(
        [ratios_df['m_rat_win_rate'] < 0.5, ratios_df['m_rat_win_rate'] >= 0.5], 
        [-1, 1], default=0
    )
```

### **Database Architecture**

#### **Data Processing Pipeline**
1. **Fetch 30-day OHLCV data** for top 1000 cryptocurrencies
2. **Calculate Bitcoin benchmark** returns and statistics  
3. **Process each cryptocurrency** through ratio calculation pipeline
4. **Generate binary signals** using standardized thresholds
5. **Upload to dual databases** (production + backtest)

#### **Output Tables**
- **FE_RATIOS**: Complete ratio calculations and intermediate values
- **FE_RATIOS_SIGNALS**: Binary signals optimized for DMV aggregation

### **Integration Points**
- **Upstream**: `1K_coins_ohlcv` and `crypto_listings_latest_1000`
- **Downstream**: `FE_RATIOS_SIGNALS` feeds into `gcp_dmv_core.py`
- **Benchmark**: Uses Bitcoin as market benchmark for all relative calculations
- **Pipeline Position**: **Stage 3.6** in DMV workflow

## Usage
```bash
python gcp_postgres_sandbox/gcp_dmv_rat.py
# Runtime: ~4-6 minutes for ratio calculations across 1000 cryptocurrencies
```

## Dependencies
- **pandas>=2.2.2** - Advanced data manipulation and financial calculations
- **numpy>=1.26.4** - Statistical operations and array processing
- **sqlalchemy>=2.0.32** - Database operations with transaction management
- **logging** - Performance monitoring and error tracking
- **time** - Execution time measurement

## Output Schema
```sql
-- FE_RATIOS_SIGNALS table
CREATE TABLE FE_RATIOS_SIGNALS (
    slug VARCHAR(255),
    timestamp TIMESTAMP,
    m_rat_alpha_bin INTEGER,          -- Alpha signal (-1,0,1)
    d_rat_beta_bin INTEGER,           -- Beta signal (-1,0,1)  
    v_rat_sharpe_bin INTEGER,         -- Sharpe ratio signal (-1,0,1)
    v_rat_sortino_bin INTEGER,        -- Sortino ratio signal (-1,0,1)
    v_rat_teynor_bin INTEGER,         -- Treynor ratio signal (-1,0,1)
    v_rat_common_sense_bin INTEGER,   -- Common sense ratio signal (-1,0,1)
    v_rat_information_bin INTEGER,    -- Information ratio signal (-1,0,1)
    v_rat_win_loss_bin INTEGER,       -- Win/loss ratio signal (-1,0,1)
    m_rat_win_rate_bin INTEGER,       -- Win rate signal (-1,0,1)
    m_rat_ror_bin INTEGER,            -- Risk of ruin signal (-1,0,1)
    d_rat_pain_bin INTEGER            -- Gain to pain ratio signal (-1,0,1)
);
```

## Recent Changes
- **v4.8.2** (2026-05-13): Both `to_sql` calls (FE_RATIOS_SIGNALS to dbcp + cp_backtest) now use `method='multi', chunksize=200` -- ~10x faster writes.
