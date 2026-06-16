"""NEVAREP global configuration."""

import os
import random
from pathlib import Path
from datetime import date
from dotenv import load_dotenv

load_dotenv()

# ── Paths ──
PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
BHAV_DIR = Path(os.getenv("BHAV_DIR", DATA_DIR / "raw_csv" / "fo_bhav"))
CM_BHAV_DIR = Path(os.getenv("CM_BHAV_DIR", DATA_DIR / "raw_csv" / "cm_bhav"))
PARTICIPANT_OI_DIR = Path(os.getenv("PARTICIPANT_OI_DIR", DATA_DIR / "raw_csv" / "participant_oi"))
ARTICLES_DIR = Path(os.getenv("ARTICLES_DIR", DATA_DIR / "articles"))
DB_PATH = Path(os.getenv("DB_PATH", DATA_DIR / "nevarep.duckdb"))

# Create directories
for d in [BHAV_DIR, CM_BHAV_DIR, PARTICIPANT_OI_DIR, ARTICLES_DIR, PROJECT_ROOT / "logs"]:
    d.mkdir(parents=True, exist_ok=True)

# ── Date range ──
HIST_START = date.fromisoformat(os.getenv("HIST_START", "2022-01-03"))
HIST_END = date.fromisoformat(os.getenv("HIST_END", "2025-03-21"))

# ── API Keys ──
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
FRED_API_KEY = os.getenv("FRED_API_KEY", "")

# ── NSE Configuration ──
NSE_MAX_ERRORS = int(os.getenv("NSE_MAX_ERRORS", "5"))
NSE_SESSION_REFRESH_WAIT = int(os.getenv("NSE_SESSION_REFRESH_WAIT", "10"))
NSE_REQUEST_DELAY = float(os.getenv("NSE_REQUEST_DELAY", "1.5"))
MAX_SESSION_REFRESHES = 10

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.nseindia.com/",
}

# User-Agent rotation pool
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
]


def get_random_ua() -> str:
    """Get a random User-Agent string."""
    return random.choice(USER_AGENTS)


# ── NSE URL Templates ──

# Old format (pre July 8, 2024)
NSE_FO_BHAV_URL_OLD = (
    "https://archives.nseindia.com/content/historical/DERIVATIVES"
    "/{year}/{month_upper}/fo{day}{month_upper}{year}bhav.csv.zip"
)

# New UDiFF format (post July 8, 2024)
NSE_FO_BHAV_URL_UDIFF = (
    "https://nsearchives.nseindia.com/content/fo"
    "/BhavCopy_NSE_FO_0_0_0_{date_yyyymmdd}_F_0000.csv.zip"
)

# NSE equity bhav copy (UDiFF)
NSE_CM_BHAV_URL = (
    "https://nsearchives.nseindia.com/content/cm"
    "/BhavCopy_NSE_CM_0_0_0_{date_yyyymmdd}_F_0000.csv.zip"
)

# Participant-wise OI
NSE_PARTICIPANT_OI_URL = (
    "https://archives.nseindia.com/content/nsccl"
    "/fao_participant_oi_{date_ddMMyyyy}.csv"
)

# Participant-wise trading volumes
NSE_PARTICIPANT_VOL_URL = (
    "https://archives.nseindia.com/content/nsccl"
    "/fao_participant_vol_{date_ddMMyyyy}.csv"
)

# UDiFF format switchover date
UDIFF_START_DATE = date(2024, 7, 8)


# ── Instrument scope ──
# Tier-1 pipeline covers these three instruments only.
# CRUDEOIL: price + futures + global tokens only (no MCX options).
INSTRUMENTS = ["NIFTY50", "BANKNIFTY", "CRUDEOIL"]

# Lot sizes for GEX computation
LOT_SIZES = {"NIFTY": 75, "BANKNIFTY": 30}

# Risk-free rate (RBI repo proxy; wire to FRED later via FRED_API_KEY)
RISK_FREE_RATE = 0.065

# ── Symbol Filters ──
# These are the ONLY symbols we extract from the bhav copy

# NSE F&O symbols (what to filter from bhav copy)
NSE_FO_SYMBOLS = ["NIFTY", "BANKNIFTY"]

# Old format instrument types
OLD_INSTRUMENT_TYPES = ["FUTIDX", "OPTIDX"]

# UDiFF instrument types (may differ — verify with sample file)
UDIFF_INSTRUMENT_TYPES = ["IDF", "IDO"]  # Index Derivatives Future/Option


# ── Column Mapping: Old Format → Unified Schema ──
OLD_TO_UNIFIED = {
    "INSTRUMENT": "instrument",
    "SYMBOL": "symbol",
    "EXPIRY_DT": "expiry_date",
    "STRIKE_PR": "strike_price",
    "OPTION_TYP": "option_type",
    "OPEN": "open_price",
    "HIGH": "high_price",
    "LOW": "low_price",
    "CLOSE": "close_price",
    "SETTLE_PR": "settle_price",
    "CONTRACTS": "contracts",
    "VAL_INLAKH": "value_lakhs",
    "OPEN_INT": "open_interest",
    "CHG_IN_OI": "change_in_oi",
}

# ── Column Mapping: UDiFF Format → Unified Schema ──
UDIFF_TO_UNIFIED = {
    "FinInstrmTp": "instrument",
    "TckrSymb": "symbol",
    "XpryDt": "expiry_date",
    "StrkPric": "strike_price",
    "OptnTp": "option_type",
    "OpnPric": "open_price",
    "HghPric": "high_price",
    "LwPric": "low_price",
    "ClsPric": "close_price",
    "SttlmPric": "settle_price",
    "LastPric": "last_price",
    "PrvsClsgPric": "prev_close",
    "UndrlygPric": "underlying_price",
    "TtlTradgVol": "contracts",
    "TtlTrfVal": "value_lakhs",
    "OpnIntrst": "open_interest",
    "ChngInOpnIntrst": "change_in_oi",
    "TtlNbOfTxsExctd": "num_trades",
    "NewBrdLotQty": "lot_size",
}


# ── Yahoo Finance tickers ──
YF_TICKERS = {
    # Indian indices
    "NIFTY50": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "SENSEX": "^BSESN",
    "INDIA_VIX": "^INDIAVIX",
    # US markets
    "SP500": "^GSPC",
    "NASDAQ": "^IXIC",
    "DOW": "^DJI",
    "US_VIX": "^VIX",
    # Currencies
    "DXY": "DX-Y.NYB",
    "USDINR": "INR=X",
    # Commodities
    "BRENT": "BZ=F",
    "GOLD": "GC=F",
    "COPPER": "HG=F",
    # Asian markets
    "NIKKEI": "^N225",
    "HANGSENG": "^HSI",
    # European markets
    "DAX": "^GDAXI",
    "FTSE": "^FTSE",
}

# Grouping for clarity
YF_INDIAN_INDICES = ["NIFTY50", "BANKNIFTY", "SENSEX", "INDIA_VIX"]
YF_US_MARKETS = ["SP500", "NASDAQ", "DOW", "US_VIX"]
YF_CURRENCIES = ["DXY", "USDINR"]
YF_COMMODITIES = ["BRENT", "GOLD", "COPPER"]
YF_ASIAN = ["NIKKEI", "HANGSENG"]
YF_EUROPEAN = ["DAX", "FTSE"]

# ── FRED Series Codes ──
FRED_SERIES = {
    "US_10Y": "DGS10",
    "US_2Y": "DGS2",
}

# ── Derived Field Thresholds ──

# PCR classification
PCR_STRONGLY_BEARISH = 0.7
PCR_BEARISH = 0.9
PCR_BULLISH = 1.1
PCR_STRONGLY_BULLISH = 1.3

# VIX regime
VIX_LOW = 12.0
VIX_NORMAL = 16.0
VIX_ELEVATED = 20.0
VIX_HIGH = 25.0

# DXY regime
DXY_WEAK = 100.0
DXY_NEUTRAL = 105.0
DXY_STRONG = 110.0

# Crude regime (Brent USD)
CRUDE_VERY_POSITIVE = 70.0
CRUDE_COMFORTABLE = 80.0
CRUDE_PRESSURE = 90.0

# US 10Y yield regime
YIELD_COMFORTABLE = 4.0
YIELD_WATCHING = 4.5
YIELD_PRESSURE = 5.0

# US market tone (change %)
US_STRONG_UP = 1.0
US_MILD_UP = 0.3
US_MILD_DOWN = -0.3
US_STRONG_DOWN = -1.0

# Gap classification (%)
GAP_THRESHOLD = 0.1  # ±0.1% = flat open
