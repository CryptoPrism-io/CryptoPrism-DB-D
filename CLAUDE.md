# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

CryptoPrism-DB is a production-grade cryptocurrency technical analysis system featuring a multi-database architecture for live trading, AI analysis, and backtesting. The system processes 1000+ cryptocurrencies using 100+ technical indicators across momentum, oscillators, ratios, metrics, and volume/value analysis.

**Note**: Database utilities (schema analysis, benchmarking, optimization tools) have been extracted to a separate repository: `CryptoPrism-DB-Utils`. This repository now focuses exclusively on production database management and technical analysis pipelines.

## Architecture

### Three-Database System
- **`dbcp`** - Primary production database for live market data
- **`cp_ai`** - AI-enhanced analysis database with processed indicators
- **`cp_backtest` / `cp_backtest_h`** - Historical data for backtesting and strategy validation

### Core Module Structure
All main processing scripts are located in `gcp_postgres_sandbox/`:

**Data Ingestion:**
- `data_ingestion/cmc_listings.py` - CoinMarketCap API integration for top 1000 cryptocurrencies
- `data_ingestion/gcp_cc_info.py` - Cryptocurrency metadata fetcher
- `data_ingestion/gcp_fear_greed_cmc.py` - Market sentiment via Fear & Greed Index
- `data_ingestion/gcp_108k_1kcoins.R` - OHLCV historical data collection via R/crypto2

**Technical Analysis Pipeline (run in sequence):**
1. `technical_analysis/gcp_dmv_met.py` - Fundamental metrics (ATH/ATL, market cap, coin age)
2. `technical_analysis/gcp_dmv_tvv.py` - Volume/value analysis (OBV, VWAP, channels)
3. `technical_analysis/gcp_dmv_pct.py` - Risk metrics (VaR, CVaR, returns)
4. `technical_analysis/gcp_dmv_mom.py` - Momentum indicators (RSI, ROC, Williams %R, etc.)
5. `technical_analysis/gcp_dmv_osc.py` - Oscillators (MACD, CCI, ADX, etc.)
6. `technical_analysis/gcp_dmv_rat.py` - Financial ratios (Alpha/Beta, Sharpe, Sortino)
7. **`technical_analysis/gcp_dmv_core.py`** - Final signal aggregation (run last)

**Backtesting:**
- `backtesting/gcp_dmv_mom_backtest.py` - Historical momentum analysis
- `backtesting/test_backtest_mom_data.py` - Backtest data validation

**Quality Assurance:**
- `quality_assurance/prod_qa_dbcp.py` - Production database monitoring
- `quality_assurance/prod_qa_cp_ai.py` - AI database QA with Telegram alerts
- `quality_assurance/prod_qa_cp_ai_backtest.py` - AI backtest data validation
- `quality_assurance/prod_qa_dbcp_backtest.py` - Backtest database validation

## Common Commands

### Dependencies
```bash
# Python dependencies
pip install -r requirements.txt

# R dependencies (if using R scripts)
Rscript requirements.R
```

### Testing
```bash
# Offline tests (no database): indicator ranges and the PIT layer
pip install -r requirements-dev.txt
python -m pytest

# Run backtest data validation tests
python -m unittest gcp_postgres_sandbox/test_backtest_mom_data.py

# Test environment (used in CI/CD)
python gcp_postgres_sandbox/cmc_listings.py
```

### Data Pipeline Execution
```bash
# Data collection sequence
python gcp_postgres_sandbox/data_ingestion/cmc_listings.py
python gcp_postgres_sandbox/data_ingestion/gcp_cc_info.py
python gcp_postgres_sandbox/data_ingestion/gcp_fear_greed_cmc.py

# Technical analysis sequence (must run in order)
python gcp_postgres_sandbox/technical_analysis/gcp_dmv_met.py
python gcp_postgres_sandbox/technical_analysis/gcp_dmv_tvv.py
python gcp_postgres_sandbox/technical_analysis/gcp_dmv_pct.py
python gcp_postgres_sandbox/technical_analysis/gcp_dmv_mom.py
python gcp_postgres_sandbox/technical_analysis/gcp_dmv_osc.py
python gcp_postgres_sandbox/technical_analysis/gcp_dmv_rat.py
python gcp_postgres_sandbox/technical_analysis/gcp_dmv_core.py  # Always run last

# Backtesting
python gcp_postgres_sandbox/backtesting/gcp_dmv_mom_backtest.py

# Quality assurance
python gcp_postgres_sandbox/quality_assurance/prod_qa_dbcp.py
python gcp_postgres_sandbox/quality_assurance/prod_qa_cp_ai.py
```

## Environment Configuration

### Required Environment Variables
Create a `.env` file or set environment variables:
```env
# Database connections
DB_HOST=your_postgresql_host
DB_NAME=dbcp                    # Primary database
DB_NAME_BT=cp_backtest         # Backtest database
DB_USER=your_username
DB_PASSWORD=your_password
DB_PORT=5432

# API credentials
CMC_API_KEY=your_cmc_api_key

# Telegram notifications (optional)
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id

# Google AI for QA system (optional)
GEMINI_API_KEY=your_google_gemini_api_key
```

## Key Database Tables

### Signal Tables (Output)
- `FE_MOMENTUM_SIGNALS` - Momentum-based trading signals
- `FE_OSCILLATORS_SIGNALS` - Technical oscillator signals
- `FE_RATIOS_SIGNALS` - Financial ratio signals
- `FE_METRICS_SIGNAL` - Fundamental analysis signals
- `FE_TVV_SIGNALS` - Volume/value technical signals
- `FE_DMV_ALL` - Aggregated signal matrix (primary output)
- `FE_DMV_SCORES` - Durability/Momentum/Valuation scores

### Input Tables
- `crypto_listings_latest_1000` - CoinMarketCap listings data
- `1K_coins_ohlcv` - OHLCV historical price data

## Development Patterns

### Database Connections
- All scripts use SQLAlchemy with PostgreSQL
- Connection credentials loaded from environment variables
- Scripts auto-detect GitHub Actions vs local environment
- Use `create_engine(f'postgresql+pg8000://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}')` pattern

### Error Handling & Logging
- All scripts use Python `logging` module
- Environment variable validation before execution
- Database connection testing in scripts
- Comprehensive error reporting for production monitoring

### Data Processing Patterns
- Batch processing for API rate limits (199 coins per CoinMarketCap call)
- Strict schema enforcement (30-field schema in listings)
- Feature tables prefixed with `FE_`
- Timestamp-based data versioning
- Duplicate handling and data quality checks

### Signal Generation
- Binary signals: bullish/bearish/neutral counts per cryptocurrency
- Multi-timeframe analysis (5, 9, 14, 21, 50, 200 periods)
- Bitcoin-relative performance metrics (Alpha/Beta vs BTC)
- Risk-adjusted ratios (Sharpe, Sortino, Information ratios)

## CI/CD Pipeline

### GitHub Actions Workflows
The system uses a sophisticated 4-stage automated pipeline:

1. **LISTINGS** (Daily 5:00 AM UTC) - Fetch cryptocurrency listings
2. **OHLCV** (Sequential) - Collect historical price data via R script
3. **DMV** (Sequential) - Execute complete technical analysis pipeline
4. **QA** (Manual) - AI-powered quality assurance with alerts

### Development Branch Testing
- `TEST_DEV` workflow runs on `dev_ai_code_branch` pushes
- Tests environment setup, database connections, and core functionality
- Validates data write operations

## Adding New Indicators

When adding technical indicators, follow the established pattern:
1. Add calculation logic to appropriate module (`gcp_dmv_mom.py`, `gcp_dmv_osc.py`, etc.)
2. Update DataFrame with new columns
3. Ensure proper database schema alignment
4. Test with backtest validation scripts
5. Update `gcp_dmv_core.py` to include new signals in aggregation

## Key Dependencies

### Python Stack
- `pandas>=2.2.2`, `numpy>=1.26.4` - Data processing
- `sqlalchemy>=2.0.32`, `psycopg2-binary>=2.9.0` - Database connectivity
- `requests>=2.32.3` - API calls
- `google-generativeai>=0.5.0` - AI-powered QA
- `python-telegram-bot>=13.15` - Alert notifications

### R Stack
- `DBI`, `RPostgres` - Database operations
- `crypto2` - Cryptocurrency data fetching
- `dotenv` - Environment management
- Always add the changes to a new file called change log and version it with version number time and date , changes made and the rational behid it
- 🕒 CRON SCHEDULE DOCUMENTATION MAINTENANCE

  Whenever GitHub Actions workflow files (.yml) in .github/workflows/ are modified, ALWAYS update CRON_SCHEDULE_README.md to maintain synchronization.

  Auto-trigger documentation updates when:
  1. CRON schedule changes - Update timing tables and UTC/IST conversions
  2. New/removed workflows - Add/remove from schedule reference table
  3. Script path changes - Update script-to-workflow mapping section
  4. Workflow dependencies modified - Update pipeline flow diagram
  5. Manual trigger changes - Update workflow_dispatch availability

  Files to monitor: .github/workflows/*.yml + any script relocations in gcp_postgres_sandbox/

  Required updates in CRON_SCHEDULE_README.md:
  - Schedule reference table
  - Pipeline flow diagram
  - Script-to-workflow mapping
  - Maintenance timestamps

  Process: Read workflow files → Identify changes → Update CRON_SCHEDULE_README.md → Update CHANGELOG.md with version increment

  This ensures CRON documentation stays current with actual workflow implementations automatically.
- │ 📋 CHANGELOG.MD MAINTENANCE PROTOCOL                                                                                                                                                     │ │
│ │                                                                                                                                                                                          │ │
│ │ For EVERY file modification, code change, or system update in the CryptoPrism-DB project, ALWAYS update CHANGELOG.md with proper versioning before committing changes.                   │ │
│ │                                                                                                                                                                                          │ │
│ │ Auto-trigger changelog updates when:                                                                                                                                                     │ │
│ │ 1. File modifications - Any script, config, or documentation changes                                                                                                                     │ │
│ │ 2. New features added - Scripts, workflows, database tools, etc.                                                                                                                         │ │
│ │ 3. Security improvements - Credential handling, vulnerability fixes                                                                                                                      │ │
│ │ 4. Infrastructure changes - GitHub Actions, database schema, folder organization                                                                                                         │ │
│ │ 5. Bug fixes - Error corrections, performance improvements                                                                                                                               │ │
│ │ 6. Documentation updates - README changes, new documentation files                                                                                                                       │ │
│ │                                                                                                                                                                                          │ │
│ │ Version increment rules:                                                                                                                                                                 │ │
│ │ - Major (X.0.0): Breaking changes, database schema modifications, API changes                                                                                                            │ │
│ │ - Minor (X.Y.0): New features, file reorganization, workflow additions                                                                                                                   │ │
│ │ - Patch (X.Y.Z): Bug fixes, documentation updates, minor configuration tweaks                                                                                                            │ │
│ │                                                                                                                                                                                          │ │
│ │ Required changelog entries:                                                                                                                                                              │ │
│ │ - Version number with UTC timestamp                                                                                                                                                      │ │
│ │ - Added/Changed/Fixed/Security/Removed categories                                                                                                                                        │ │
│ │ - Detailed rationale explaining business/technical justification                                                                                                                         │ │
│ │ - Commit hash reference after committing                                                                                                                                                 │ │
│ │ - Impact analysis and risk considerations                                                                                                                                                │ │
│ │                                                                                                                                                                                          │ │
│ │ Process:                                                                                                                                                                                 │ │
│ │ 1. Before changes: Plan version increment                                                                                                                                                │ │
│ │ 2. Make modifications: Document what's being changed                                                                                                                                     │ │
│ │ 3. Update CHANGELOG.md: Add comprehensive entry with rationale                                                                                                                           │ │
│ │ 4. Commit changes: Include descriptive commit message                                                                                                                                    │ │
│ │ 5. Add commit hash: Reference back to changelog entry                                                                                                                                    │ │
│ │                                                                                                                                                                                          │ │
│ │ Files to monitor: ALL files in project, especially:                                                                                                                                      │ │
│ │ - gcp_postgres_sandbox/ (all scripts)                                                                                                                                                    │ │
│ │ - .github/workflows/ (workflow files)                                                                                                                                                    │ │
│ │ - Root documentation files                                                                                                                                                               │ │
│ │ - Configuration files
- /clear
- Never use emojis in any function, code snippet, variable, comment, or line of Python (or any programming language).
Emojis break the code and cause execution errors.
Always provide clean, emoji-free code.\
Until explicityly the word emoji is used to give u direction