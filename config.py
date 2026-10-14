"""Static configuration for the trading agent: guardrail thresholds, model choice,
and the tradable universe.

WATCHLIST is the S&P 1500 (500 + MidCap 400 + SmallCap 600) unioned with the
Nasdaq-100, plus SPY/QQQ, snapshotted 2026-08-05. Every symbol was verified
against Alpaca's asset list as active, tradable, and fractionable at snapshot
time — orders are placed as notional dollar amounts, which whole-share-only
assets reject. Index membership changes over time; refresh periodically and
re-run that validation, because a symbol that leaves the list can still be
held (sells are exempt from the whitelist check for exactly that reason).
"""

# --- Guardrails (README.md#guardrails) ---------------------------------------
MAX_POSITION_PCT = 0.15        # max 15% of portfolio per ticker
MIN_CASH_BUFFER_PCT = 0.05     # keep at least 5% of equity in cash
MAX_TRADES_PER_CYCLE = 5       # reject proposed trades beyond this count
# Alpaca rejects notional orders under $1. Catching it here keeps a trivially
# small trade from becoming a broker exception mid-execution.
MIN_TRADE_NOTIONAL_USD = 1.0

# Every sell must name which of these fired. Keeps exits auditable after the
# fact and stops positions being closed on price drift alone.
VALID_EXIT_TRIGGERS = frozenset({
    "thesis_broken",    # the written case is no longer true
    "target_hit",       # the case played out
    "bad_news",         # new information materially damages the case
    "no_longer_fits",   # better use of capital / no longer earns its place
    "risk",             # position or portfolio risk management
})

# --- Live-trading lock --------------------------------------------------------
# Phase 1 is paper-only until the operator decides otherwise (README.md#rollout).
# `--mode live` is not enough on its own: a typo, a stale shell alias, or a
# copy-pasted command would otherwise trade real money. Live mode additionally
# requires this environment variable to be set to exactly LIVE_UNLOCK_VALUE, so
# reaching a live account is always a deliberate, separate act.
LIVE_UNLOCK_ENV = "TRADING_AGENT_ALLOW_LIVE"
LIVE_UNLOCK_VALUE = "yes-trade-real-money"


def assert_mode_allowed(mode: str) -> None:
    """Refuse to run in live mode unless it has been explicitly unlocked.

    Raises SystemExit rather than returning a flag — callers must not be able to
    ignore this the way they could a boolean.
    """
    if mode != "live":
        return
    import os

    if os.environ.get(LIVE_UNLOCK_ENV) != LIVE_UNLOCK_VALUE:
        raise SystemExit(
            "refusing to run in live mode: this project is locked to paper trading.\n"
            f"To unlock deliberately, set {LIVE_UNLOCK_ENV}={LIVE_UNLOCK_VALUE} "
            "in the environment (not in .env) for that one command."
        )


# --- Claude decision engine ---------------------------------------------------
CLAUDE_MODEL = "claude-opus-5-5"
# Thinking depth and overall token spend: low | medium | high | xhigh | max.
# Set explicitly: Opus 5.5 defaults to `medium`, one level below Opus 5's
# `high`, and thinking cannot be disabled — effort is the only depth control.
# Cycles up to 47 ran Opus 5 at `xhigh`; compare performance across the switch
# by the model/effort recorded on each decision_cycles row.
CLAUDE_EFFORT = "medium"
# Each cycle returns a full review per open position plus any trades, so this
# scales with position count — 2048 truncated mid-JSON at only 5 positions.
# Thinking tokens count against this too, so raising CLAUDE_EFFORT eats into
# the same budget: at xhigh, 8192 is not enough headroom to be safe.
CLAUDE_MAX_TOKENS = 32000

# How many of this agent's own prior trades in a symbol are replayed back into
# the prompt. The stored thesis says what it believed; this says what it did.
# Capped because it grows without bound: the newest actions carry the intent a
# current decision could contradict, and older ones are already reflected in
# the position weight the model can see.
ACTION_HISTORY_LIMIT = 5

# --- Tradable universe (long-only, whitelisted US equities/ETFs) -------------
WATCHLIST = [
    "A", "AA", "AAL", "AAMI", "AAON", "AAP", "AAPL", "AAT", "ABBV", "ABCB",
    "ABG", "ABM", "ABNB", "ABR", "ABT", "ACA", "ACAD", "ACGL", "ACHC", "ACI",
    "ACIW", "ACLS", "ACM", "ACMR", "ACN", "ACT", "ADAM", "ADBE", "ADC", "ADEA",
    "ADI", "ADM", "ADMA", "ADNT", "ADP", "ADSK", "ADT", "ADUS", "AEE", "AEIS",
    "AEO", "AEP", "AES", "AESI", "AFG", "AFL", "AGCO", "AGNT", "AGO", "AGX",
    "AGYS", "AHCO", "AHR", "AIG", "AIN", "AIR", "AIT", "AIZ", "AJG", "AKAM",
    "AKR", "ALAB", "ALB", "ALG", "ALGM", "ALGN", "ALGT", "ALHC", "ALK", "ALKS",
    "ALL", "ALLE", "ALLY", "ALNY", "ALRM", "ALSN", "ALV", "AM", "AMAT", "AMCR",
    "AMD", "AME", "AMG", "AMGN", "AMH", "AMKR", "AMN", "AMP", "AMPH", "AMR",
    "AMRX", "AMSF", "AMT", "AMTM", "AMZN", "AN", "ANDE", "ANET", "ANF", "ANIP",
    "AON", "AORT", "AOS", "AOSL", "APA", "APAM", "APD", "APG", "APH", "APLE",
    "APO", "APOG", "APP", "APPF", "APTV", "AR", "ARCB", "ARE", "ARES", "ARLO",
    "ARM", "ARMK", "AROC", "ARR", "ARW", "ARWR", "ASB", "ASH", "ASML", "ASO",
    "ASTE", "ASTH", "ATEN", "ATI", "ATMU", "ATO", "ATR", "AUB", "AVA", "AVAV",
    "AVB", "AVGO", "AVNT", "AVT", "AVTR", "AVY", "AWI", "AWK", "AWR", "AX",
    "AXON", "AXP", "AXTA", "AYI", "AZO", "AZTA", "AZZ", "BA", "BAC", "BAH",
    "BALL", "BANC", "BANF", "BANR", "BAX", "BBT", "BBWI", "BBY", "BC", "BCC",
    "BCO", "BCPC", "BDC", "BDX", "BEN", "BF.B", "BFAM", "BFH", "BFS", "BG",
    "BGC", "BHE", "BHF", "BIIB", "BILL", "BIO", "BJ", "BJRI", "BKE", "BKH",
    "BKNG", "BKR", "BKU", "BL", "BLDR", "BLFS", "BLK", "BLKB", "BMI", "BMRN",
    "BMY", "BNL", "BNY", "BOH", "BOOT", "BOX", "BR", "BRC", "BRK.B", "BRKR",
    "BRO", "BROS", "BRX", "BSX", "BSY", "BTSG", "BTU", "BURL", "BWA", "BWXT",
    "BX", "BXMT", "BXP", "BYD", "C", "CACC", "CACI", "CAG", "CAH", "CAKE",
    "CALM", "CALX", "CALY", "CAR", "CARG", "CARR", "CART", "CASH", "CASY", "CAT",
    "CATY", "CAVA", "CB", "CBOE", "CBRE", "CBRL", "CBSH", "CBT", "CBU", "CC",
    "CCEP", "CCI", "CCK", "CCL", "CCOI", "CCS", "CDE", "CDNS", "CDP", "CDW",
    "CE", "CEG", "CELH", "CENT", "CENTA", "CENX", "CERT", "CF", "CFFN", "CFG",
    "CFR", "CG", "CGNX", "CHCO", "CHD", "CHDN", "CHE", "CHEF", "CHH", "CHRD",
    "CHRW", "CHTR", "CHWY", "CI", "CIEN", "CINF", "CL", "CLF", "CLH", "CLSK",
    "CLX", "CMC", "CMCSA", "CME", "CMG", "CMI", "CMS", "CNC", "CNH", "CNK",
    "CNM", "CNMD", "CNO", "CNP", "CNR", "CNS", "CNX", "CNXC", "CNXN", "COCO",
    "COF", "COHR", "COHU", "COIN", "COKE", "COLB", "COLL", "COLM", "CON", "COO",
    "COP", "COR", "CORT", "COST", "COTY", "CPAY", "CPB", "CPF", "CPK", "CPRI",
    "CPRT", "CPT", "CR", "CRBG", "CRC", "CRGY", "CRH", "CRI", "CRK", "CRL",
    "CRM", "CROX", "CRS", "CRSR", "CRUS", "CRVL", "CRWD", "CRWV", "CSCO", "CSGP",
    "CSL", "CSR", "CSW", "CSX", "CTAS", "CTRE", "CTS", "CTSH", "CTVA", "CUBE",
    "CUBI", "CURB", "CUZ", "CVBF", "CVCO", "CVI", "CVLT", "CVNA", "CVS", "CVSA",
    "CVX", "CW", "CWEN", "CWK", "CWST", "CWT", "CXM", "CXT", "CXW", "CYTK",
    "CZR", "D", "DAL", "DAN", "DAR", "DASH", "DAVE", "DBD", "DBX", "DCH",
    "DCI", "DCOM", "DD", "DDOG", "DE", "DEA", "DECK", "DEI", "DELL", "DFH",
    "DFIN", "DG", "DGII", "DGX", "DHI", "DHR", "DINO", "DIOD", "DIS", "DKS",
    "DLB", "DLR", "DLTR", "DLX", "DNOW", "DOC", "DOCN", "DOCS", "DOCU", "DORM",
    "DOV", "DOW", "DPZ", "DRH", "DRI", "DT", "DTE", "DTM", "DUK", "DUOL",
    "DV", "DVA", "DVN", "DXC", "DXCM", "DXPE", "DY", "EAT", "EBAY", "EBC",
    "ECG", "ECHO", "ECL", "ECPG", "ED", "EEFT", "EFC", "EFX", "EG", "EGBN",
    "EGP", "EHC", "EIG", "EIX", "EL", "ELAN", "ELF", "ELS", "ELV", "EME",
    "EMN", "EMR", "ENOV", "ENPH", "ENR", "ENS", "ENSG", "ENTG", "ENVA", "EOG",
    "EPAC", "EPAM", "EPC", "EPR", "EPRT", "EQH", "EQIX", "EQR", "EQT", "ERIE",
    "ES", "ESAB", "ESE", "ESI", "ESNT", "ESS", "ETN", "ETR", "ETSY", "EVR",
    "EVRG", "EVTC", "EW", "EWBC", "EXC", "EXE", "EXEL", "EXLS", "EXP", "EXPD",
    "EXPE", "EXPO", "EXR", "EXTR", "EYE", "EZPW", "F", "FA", "FAF", "FANG",
    "FAST", "FBIN", "FBK", "FBNC", "FBP", "FBRT", "FCF", "FCFS", "FCN", "FCPT",
    "FCX", "FDS", "FDX", "FDXF", "FE", "FELE", "FER", "FFBC", "FFIN", "FFIV",
    "FG", "FHB", "FHI", "FHN", "FIBK", "FICO", "FIS", "FISV", "FITB", "FIVE",
    "FIVN", "FIX", "FIZZ", "FLEX", "FLG", "FLO", "FLR", "FLS", "FMC", "FN",
    "FNB", "FND", "FNF", "FORM", "FOUR", "FOX", "FOXA", "FOXF", "FR", "FRPT",
    "FRT", "FSLR", "FSS", "FTDR", "FTI", "FTNT", "FTRE", "FTV", "FUL", "FULT",
    "FUN", "G", "GAP", "GATX", "GBCI", "GBX", "GD", "GDDY", "GE", "GEF",
    "GEHC", "GEN", "GEO", "GEV", "GFF", "GGG", "GHC", "GIII", "GILD", "GIS",
    "GKOS", "GL", "GLPI", "GLW", "GM", "GME", "GMED", "GNL", "GNRC", "GNTX",
    "GNW", "GO", "GOLF", "GOOG", "GOOGL", "GPC", "GPI", "GPK", "GPN", "GPOR",
    "GRBK", "GRMN", "GS", "GSHD", "GT", "GTES", "GTM", "GTY", "GVA", "GWRE",
    "GWW", "GXO", "H", "HAE", "HAFC", "HAL", "HALO", "HAS", "HASI", "HAYW",
    "HBAN", "HCA", "HCC", "HCI", "HCSG", "HD", "HE", "HFWA", "HGV", "HIG",
    "HII", "HIMS", "HIW", "HL", "HLI", "HLIT", "HLNE", "HLT", "HLX", "HMN",
    "HNI", "HOG", "HOMB", "HON", "HONA", "HOOD", "HOPE", "HP", "HPE", "HPQ",
    "HQY", "HR", "HRB", "HRL", "HRMY", "HSIC", "HST", "HSTM", "HSY", "HTH",
    "HTLD", "HTO", "HTZ", "HUBB", "HUBG", "HUM", "HWC", "HWKN", "HWM", "HXL",
    "HZO", "IART", "IBKR", "IBM", "IBOC", "IBP", "ICE", "ICHR", "ICUI", "IDA",
    "IDCC", "IDXX", "IESC", "IEX", "IFF", "IIPR", "IJR", "ILMN", "INCY", "INDB",
    "INDV", "INGR", "INSP", "INSW", "INTC", "INTU", "INVA", "INVH", "INVX", "IOSP",
    "IP", "IPAR", "IPGP", "IQV", "IR", "IRDM", "IRM", "IRT", "ISRG", "IT",
    "ITGR", "ITRI", "ITT", "ITW", "IVT", "IVZ", "J", "JAZZ", "JBGS", "JBHT",
    "JBL", "JBLU", "JBSS", "JBTM", "JCI", "JEF", "JJSF", "JKHY", "JLL", "JNJ",
    "JOE", "JPM", "JXN", "KAI", "KALU", "KBH", "KBR", "KD", "KDP", "KEX",
    "KEY", "KEYS", "KFY", "KGS", "KHC", "KIM", "KKR", "KLAC", "KLIC", "KMB",
    "KMI", "KMPR", "KMT", "KMX", "KN", "KNF", "KNSL", "KNTK", "KNX", "KO",
    "KOP", "KR", "KRC", "KRG", "KRMN", "KRYS", "KSS", "KTB", "KTOS", "KVUE",
    "KWR", "L", "LAD", "LAMR", "LAUR", "LAZ", "LBRT", "LCII", "LDOS", "LEA",
    "LECO", "LEG", "LEN", "LEU", "LFST", "LFUS", "LGIH", "LGND", "LH", "LHX",
    "LIF", "LII", "LIN", "LITE", "LIVN", "LKFN", "LKQ", "LLY", "LMAT", "LMT",
    "LNC", "LNN", "LNT", "LNTH", "LOPE", "LOW", "LPG", "LPX", "LQDA", "LQDT",
    "LRCX", "LRN", "LSTR", "LTC", "LTH", "LULU", "LUMN", "LUV", "LVS", "LW",
    "LXP", "LYB", "LYFT", "LYV", "LZ", "LZB", "M", "MA", "MAA", "MAC",
    "MAN", "MANH", "MAR", "MARA", "MAS", "MAT", "MATW", "MATX", "MBC", "MBGL",
    "MBIN", "MC", "MCD", "MCHP", "MCK", "MCO", "MCRI", "MCY", "MD", "MDLZ",
    "MDT", "MDU", "MEDP", "MELI", "MET", "META", "MFP", "MGEE", "MGM", "MGY",
    "MHK", "MHO", "MIDD", "MIR", "MKC", "MKSI", "MKTX", "MLI", "MLKN", "MLM",
    "MMI", "MMM", "MMS", "MMSI", "MNST", "MO", "MOG.A", "MOH", "MORN", "MOS",
    "MP", "MPC", "MPT", "MPWR", "MRCY", "MRK", "MRNA", "MRP", "MRSH", "MRTN",
    "MRVL", "MS", "MSA", "MSCI", "MSEX", "MSFT", "MSGS", "MSI", "MSM", "MSTR",
    "MTB", "MTCH", "MTD", "MTDR", "MTG", "MTH", "MTN", "MTRN", "MTSI", "MTUS",
    "MTX", "MTZ", "MU", "MUR", "MUSA", "MWA", "MXL", "MYRG", "MZTI", "NABL",
    "NATL", "NAVI", "NBHC", "NBIS", "NBIX", "NBTB", "NCLH", "NDAQ", "NDSN", "NE",
    "NEE", "NEM", "NEO", "NEOG", "NEU", "NFG", "NFLX", "NGVT", "NHC", "NHI",
    "NI", "NIC", "NJR", "NKE", "NLY", "NMIH", "NNN", "NOC", "NOG", "NOV",
    "NOVT", "NOW", "NPK", "NPO", "NRG", "NSC", "NSIT", "NSP", "NSSC", "NTAP",
    "NTCT", "NTNX", "NTRS", "NTST", "NUE", "NVDA", "NVR", "NVST", "NVT", "NWBI",
    "NWE", "NWL", "NWN", "NWS", "NWSA", "NX", "NXPI", "NXRT", "NXST", "NXT",
    "NYT", "O", "OC", "ODFL", "OFG", "OGE", "OGN", "OGS", "OHI", "OI",
    "OII", "OKE", "OKTA", "OLED", "OLLI", "OLN", "OMC", "OMCL", "ON", "ONB",
    "ONTO", "OPCH", "OPLN", "ORA", "ORCL", "ORI", "ORLY", "OSIS", "OSK", "OSW",
    "OTIS", "OTTR", "OUT", "OVV", "OXY", "OZK", "P", "PAG", "PAHC", "PANW",
    "PARR", "PATH", "PATK", "PAYC", "PAYO", "PAYX", "PB", "PBF", "PBH", "PBI",
    "PCAR", "PCG", "PCRX", "PCTY", "PDD", "PDFS", "PEB", "PECO", "PEG", "PEGA",
    "PEN", "PENG", "PENN", "PEP", "PFBC", "PFE", "PFG", "PFGC", "PFS", "PG",
    "PGNY", "PGR", "PH", "PHIN", "PHM", "PI", "PII", "PINS", "PIPR", "PJT",
    "PK", "PKG", "PLAB", "PLD", "PLMR", "PLNT", "PLTR", "PLUS", "PLXS", "PM",
    "PMT", "PNC", "PNFP", "PNR", "PNW", "PODD", "POOL", "POR", "POST", "POWI",
    "POWL", "PPC", "PPG", "PPL", "PPLI", "PR", "PRDO", "PRG", "PRGO", "PRGS",
    "PRI", "PRIM", "PRKS", "PRLB", "PRSU", "PRU", "PRVA", "PSA", "PSKY", "PSMT",
    "PSN", "PSX", "PTC", "PTCT", "PTEN", "PTGX", "PTON", "PVH", "PWR", "PYPL",
    "PZZA", "Q", "QCOM", "QDEL", "QLYS", "QNST", "QQQ", "QRVO", "QTWO", "R",
    "RAL", "RAMP", "RBA", "RBC", "RCL", "RCUS", "RDN", "RDNT", "REG", "REGN",
    "RELY", "RES", "REX", "REXR", "REYN", "REZI", "RF", "RGA", "RGEN", "RGLD",
    "RH", "RHI", "RHP", "RITM", "RJF", "RKLB", "RL", "RLI", "RMBS", "RMD",
    "RNG", "RNR", "RNST", "ROAD", "ROCK", "ROG", "ROIV", "ROK", "ROKU", "ROL",
    "ROP", "ROST", "RPM", "RRC", "RRR", "RRX", "RS", "RSG", "RSI", "RTX",
    "RUN", "RUSHA", "RVTY", "RXO", "RYAN", "RYN", "SABR", "SAFE", "SAFT", "SAH",
    "SAIA", "SAIC", "SAM", "SANM", "SARO", "SBAC", "SBCF", "SBH", "SBRA", "SBSI",
    "SBUX", "SCHL", "SCHW", "SCI", "SCL", "SCSC", "SDGR", "SEDG", "SEI", "SEIC",
    "SEZL", "SF", "SFBS", "SFM", "SFNC", "SGI", "SHAK", "SHC", "SHEN", "SHO",
    "SHOO", "SHOP", "SHW", "SIG", "SIGI", "SIRI", "SITM", "SJM", "SKT", "SKY",
    "SKYW", "SLAB", "SLB", "SLG", "SLGN", "SLM", "SLVM", "SM", "SMCI", "SMG",
    "SMP", "SMPL", "SMTC", "SN", "SNA", "SNDK", "SNDR", "SNEX", "SNPS", "SNX",
    "SO", "SOLS", "SOLV", "SON", "SONO", "SPCX", "SPG", "SPGI", "SPHR", "SPNT",
    "SPSC", "SPSM", "SPXC", "SPY", "SR", "SRE", "SRPT", "SSB", "SSD", "ST",
    "STAA", "STAG", "STBA", "STC", "STE", "STEP", "STLD", "STRA", "STRL", "STT",
    "STWD", "STX", "STZ", "SUPN", "SW", "SWK", "SWKS", "SWX", "SXI", "SXT",
    "SYF", "SYK", "SYNA", "SYY", "T", "TALO", "TAP", "TBBK", "TCBI", "TDC",
    "TDG", "TDS", "TDW", "TDY", "TECH", "TEL", "TER", "TEX", "TFC", "TFIN",
    "TFX", "TGT", "TGTX", "THC", "THG", "THO", "THRM", "TILE", "TJX", "TKO",
    "TKR", "TLN", "TMDX", "TMO", "TMUS", "TNC", "TNDM", "TNL", "TOL", "TOST",
    "TPC", "TPL", "TPR", "TR", "TREX", "TRGP", "TRI", "TRIP", "TRMB", "TRMK",
    "TRN", "TRNO", "TROW", "TRST", "TRU", "TRUP", "TRV", "TSCO", "TSLA", "TSN",
    "TT", "TTC", "TTD", "TTEK", "TTMI", "TTWO", "TWLO", "TXN", "TXNM", "TXRH",
    "TXT", "TYL", "UA", "UAA", "UAL", "UBER", "UBSI", "UCB", "UCTT", "UDR",
    "UE", "UFCS", "UFPI", "UFPT", "UGI", "UHS", "ULS", "ULTA", "UMBF", "UNF",
    "UNFI", "UNH", "UNIT", "UNM", "UNP", "UPBD", "UPS", "UPWK", "URBN", "URI",
    "USB", "USFD", "USLM", "USPH", "UTHR", "UTI", "UTL", "UVV", "V", "VAC",
    "VAL", "VC", "VCEL", "VCTR", "VCYT", "VECO", "VEEV", "VFC", "VGNT", "VIAV",
    "VICI", "VICR", "VIOO", "VIR", "VIRT", "VLO", "VLTO", "VLY", "VMC", "VMI",
    "VNO", "VNOM", "VNT", "VOYA", "VRRM", "VRSK", "VRSN", "VRT", "VRTS", "VRTX",
    "VSAT", "VSEC", "VSH", "VSNT", "VST", "VSTS", "VSXY", "VTOL", "VTR", "VTRS",
    "VVV", "VVX", "VYX", "VZ", "WAB", "WABC", "WAFD", "WAL", "WAT", "WAY",
    "WBD", "WBS", "WCC", "WD", "WDAY", "WDC", "WDFC", "WEC", "WELL", "WEN",
    "WERN", "WEX", "WFC", "WFRD", "WGO", "WH", "WHD", "WHR", "WINA", "WING",
    "WKC", "WLK", "WLY", "WM", "WMB", "WMG", "WMS", "WMT", "WOR", "WPC",
    "WRB", "WRBY", "WRLD", "WS", "WSBC", "WSC", "WSFS", "WSM", "WSO", "WST",
    "WT", "WTFC", "WTRG", "WTS", "WTW", "WU", "WWD", "WWW", "WY", "WYNN",
    "XEL", "XHR", "XNCR", "XOM", "XPEL", "XPO", "XRAY", "XYL", "XYZ", "YELP",
    "YETI", "YOU", "YUM", "ZBH", "ZBRA", "ZD", "ZION", "ZTS", "ZWS",
]

# --- News lookup and archive ---------------------------------------------------
# Every article fetched is kept in the `news_articles` table, keyed by Alpaca's
# article id, and each held position's news is read back from that archive
# rather than from the day's fetch. Three headlines with no dates, rebuilt from
# scratch daily, meant the agent could not tell a story it had already reacted
# to from one that broke this afternoon, and nothing older than the last three
# items existed for it at all.
#
# Articles fetched per HELD position, one request each covering the last
# NEWS_HISTORY_DAYS, so every open thesis is reviewed against its own news
# rather than whatever a shared cap happened to cover. Fetching the whole window
# every cycle is also what keeps the archive self-healing: a missed or failed
# day is backfilled by the next one.
NEWS_FETCH_PER_SYMBOL = 20
NEWS_HISTORY_DAYS = 30
# Shown per holding. Articles published since the previous cycle carry their
# summary; older ones are headline-only, since the model has seen them before
# and only needs to recognise them. Summaries are clipped — Benzinga's run long.
NEWS_NEW_PER_SYMBOL = 5
NEWS_EARLIER_PER_SYMBOL = 8
NEWS_SUMMARY_CHARS = 240
# Candidates (the screened shortlist of names not held) get a lighter lookup:
# fetched over the same window so the archive fills in, but only the newest few
# headlines shown. ~30 candidates x 3 headlines is ~6k prompt tokens.
NEWS_FETCH_PER_CANDIDATE = 10
NEWS_PER_CANDIDATE = 3
# Broad market sweep across the watchlist. This is a sample, not per-symbol
# coverage — at watchlist scale no article budget makes it complete.
MARKET_NEWS_ARTICLES = 40

# --- Candidate screen -----------------------------------------------------------
# The prompt used to carry `watchlist_prices`: every watchlist symbol mapped to a
# bare last price. At 1,519 names that was a third of the whole prompt and it was
# unusable — a price with no reference point supports no judgment, so the only
# names the agent could form a view on were the ones it already held, and every
# trade it ever made was a tranche into an existing position. Worse, "no_longer_fits"
# is defined as "better use of the capital", which cannot fire without a better
# candidate to name, so the agent could not raise cash either. It sat at the 5% cash
# buffer for weeks returning nothing but `hold`.
#
# These parameters drive the replacement: a screen over daily bars that produces a
# small set of candidates carrying enough context to argue about.
SCREEN_LOOKBACK_DAYS = 400          # calendar days of daily bars (~275 trading days)
SCREEN_MIN_BARS = 40                # skip symbols too newly listed to measure
# Alpaca's free tier serves IEX bars, whose volume is only IEX's share of
# consolidated tape — roughly an order of magnitude below real dollar volume. This
# threshold is calibrated against that, not against true ADV: at $10M every current
# holding clears (the smallest, GD, is ~$17M) while ~450 of 1,519 names survive.
# Raising it to $20M would screen out a position the agent already owns.
SCREEN_MIN_DOLLAR_VOL_M = 10.0
# Candidates are drawn from four buckets rather than one ranking. A single
# momentum sort would hand the agent nothing but high-beta names, which is the
# wrong bias when the mandate is risk-adjusted return — the low-volatility bucket
# exists specifically to counterweight that.
SCREEN_PER_BUCKET = 10
# The bar fetch is split into chunks, each retried after these waits (seconds).
# As one ~1,500-symbol paginated request, a single connection reset anywhere in
# the pagination threw away every page fetched so far, and the immediate retry
# hit the same thing: the screen was lost on 2026-09-28, 10-07, 10-09 and 10-12.
SCREEN_BAR_CHUNK = 200
SCREEN_RETRY_WAITS = (5, 20, 60)

# --- Storage --------------------------------------------------------------------
DEFAULT_DB_PATH = "trading_agent.db"

# --- Fundamentals (fundamentals.py) -----------------------------------------
# Finnhub's free tier allows 60 calls/min; 55 leaves headroom for clock skew.
FINNHUB_CALLS_PER_MIN = 55
# The SEC asks for no more than 10 requests/second.
EDGAR_CALLS_PER_SEC = 5
EDGAR_CACHE_DIR = "cache/edgar"
# Cached companyfacts files unused this long are deleted (~3.6 MB each).
EDGAR_CACHE_MAX_AGE_DAYS = 30
FILINGS_LOOKBACK_DAYS = 90
# 20 quarters = the "5y" in pe_ttm_5y_range; fewer than 8 points shows no range.
FUNDAMENTALS_HISTORY_QUARTERS = 20
FUNDAMENTALS_MIN_HISTORY_POINTS = 8
EARNINGS_LOOKAHEAD_DAYS = 120
