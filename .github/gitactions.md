# GitHub Actions Workflows

This document provides an overview of the GitHub Actions workflows defined in the `.github/workflows/` directory. Each workflow is described with its purpose, triggers, and key steps.

---

## 1. **Rtest.yml**
- **Purpose**: Sets up an R environment and runs R scripts for testing.
- **Triggers**: Manual trigger (`workflow_dispatch`).
- **Key Steps**:
  - Sets up R environment using `r-lib/actions/setup-r`.
  - Installs R packages (`log4r`, `RMySQL`, `crypto2`, `dplyr`, `DBI`, `RPostgres`).
  - Runs the `R_testCode.R` script.
- **Status**: Active.

---

## 2. **weekly_backtest_momentum.yml**
- **Purpose**: Runs weekly backtesting for momentum data processing.
- **Triggers**:
  - Scheduled: Every Sunday at 2:00 AM UTC.
  - Manual trigger (`workflow_dispatch`).
- **Key Steps**:
  - Sets up Python environment and installs dependencies.
  - Runs `gcp_dmv_mom_backtest.py` for backtesting.
  - Executes validation tests using `test_backtest_mom_data.py`.
- **Status**: Active.

---

## 3. **QA.yml**
- **Purpose**: Runs QA scripts for database and API validation.
- **Triggers**: Manual trigger (`workflow_dispatch`).
- **Key Steps**:
  - Sets up Python environment and installs dependencies.
  - Executes QA scripts such as `prod_qa_dbcp.py`.
- **Status**: Active.

---

## 4. **pytest.yml**
- **Purpose**: Runs Python tests for the project.
- **Triggers**: Manual trigger (`workflow_dispatch`).
- **Key Steps**:
  - Sets up Python environment and installs dependencies.
  - Runs `gcp_dmv_rat.py` for testing.
- **Status**: Active.

---

## 5. **OHLCV.yml**
- **Purpose**: Processes OHLCV data after the `LISTINGS` workflow completes.
- **Triggers**:
  - Dependent on `LISTINGS` workflow completion.
  - Manual trigger (`workflow_dispatch`).
- **Key Steps**:
  - Sets up R environment and installs required packages.
  - Runs `gcp_108k_1kcoins.R`.
- **Status**: Active.

---

## 6. **LISTINGS.yml**
- **Purpose**: Fetches cryptocurrency listings data daily.
- **Triggers**:
  - Scheduled: Every day at **00:05 UTC** (cron: `05 0 * * *`).
  - Manual trigger (`workflow_dispatch`).
- **Key Steps**:
  - Sets up Python environment and installs dependencies.
  - Runs `cmc_listings.py` to fetch and process data.
- **Status**: Active.

---

## 7. **DMV.yml**
- **Purpose**: Runs various data processing scripts for DMV (Durability, Momentum, Valuation).
- **Triggers**:
  - Dependent on `OHLCV` workflow completion.
  - Manual trigger (`workflow_dispatch`).
- **Key Steps**:
  - Sets up Python environment and installs dependencies.
  - Executes multiple scripts:
    - `gcp_fear_greed_cmc.py`
    - `gcp_dmv_met.py`
    - `gcp_dmv_tvv.py`
    - `gcp_dmv_pct.py`
    - `gcp_dmv_mom.py`
    - `gcp_dmv_osc.py`
    - `gcp_dmv_rat.py`
    - `gcp_dmv_core.py`
- **Status**: Active.

---

### Notes
- Secrets such as `DB_HOST`, `DB_USER`, `DB_PASSWORD`, and API keys are used for secure access and must be configured in the repository settings.
- Ensure all dependencies are up-to-date to avoid workflow failures.

For any issues or updates, refer to the `.github/workflows/` directory.
