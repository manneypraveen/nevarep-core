"""NEVAREP global configuration."""

import os
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
HIST_START = date.fromisoformat(os.getenv("HIST_START", "2022-01-01"))
HIST_END = date.fromisoformat(os.getenv("HIST_END", "2024-12-31"))

# ── API Keys ──
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
FRED_API_KEY = os.getenv("FRED_API_KEY", "")

# ── NSE Configuration ──
NSE_MAX_ERRORS = int(os.getenv("NSE_MAX_ERRORS", "5"))
NSE_SESSION_REFRESH_WAIT = int(os.getenv("NSE_SESSION_REFRESH_WAIT", "10"))
NSE_REQUEST_DELAY = float(os.getenv("NSE_REQUEST_DELAY", "1.5"))

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.nseindia.com/",
}

# ── NSE URL Templates ──
# Old format (pre July 2024)
NSE_FO_BHAV_URL_OLD = (
    "https://archives.nseindia.com/content/historical/DERIVATIVES"
    "/{year}/{month_upper}/fo{day}{month_upper}{year}bhav.csv.zip"
)
# New UDiFF format (post July 2024)
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
# UDiFF format switchover date
UDIFF_START_DATE = date(2024, 7, 8)

# ── Symbols to track ──
NSE_INDEX_SYMBOLS = ["NIFTY", "BANKNIFTY", "FINNIFTY"]
BSE_INDEX_SYMBOLS = ["SENSEX", "BANKEX"]
ALL_INDEX_SYMBOLS = NSE_INDEX_SYMBOLS + BSE_INDEX_SYMBOLS

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

# FRED series codes
FRED_SERIES = {
    "US_10Y": "DGS10",
    "US_2Y": "DGS2",
}
