import streamlit as st
import yfinance as yf
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timedelta
import requests
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import ast
import operator as op
from streamlit_autorefresh import st_autorefresh

st.set_page_config(layout="wide")

# =========================
# SESSION STATE INIT
# =========================
if "timeframe" not in st.session_state:
    st.session_state.timeframe = "15m"
if "source_option" not in st.session_state:
    st.session_state.source_option = "Nifty50"
if "alerts_active" not in st.session_state:
    st.session_state.alerts_active = False
if "alerted_tickers" not in st.session_state:
    st.session_state.alerted_tickers = set()
if "selected_ticker" not in st.session_state:
    st.session_state.selected_ticker = None

# =========================
# LOAD SECRETS
# =========================
gmail_user = st.secrets["gmail_user"]
gmail_password = st.secrets["gmail_password"]
alert_emails = st.secrets["alert_emails"]

telegram_token = st.secrets["telegram_token"]
telegram_chat_id = st.secrets["telegram_chat_id"]

st.title("Yacht Code")

# =========================
# CANDLE COUNTDOWN
# =========================
def seconds_until_next_candle(timeframe):
    now = datetime.now()

    if timeframe == "15m":
        next_candle = (
            now.replace(
                minute=(now.minute // 15) * 15,
                second=0,
                microsecond=0
            )
            + timedelta(minutes=15)
        )
        refresh_time = next_candle - timedelta(seconds=30)

    elif timeframe == "1d":
        next_candle = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        refresh_time = next_candle - timedelta(seconds=30)

    return max(int((refresh_time - now).total_seconds()), 0)

def get_autorefresh_interval(timeframe):
    seconds = seconds_until_next_candle(timeframe)
    return max(seconds, 5) * 1000

st.caption(f"Next refresh in ~{seconds_until_next_candle(st.session_state.timeframe)} seconds")
st_autorefresh(interval=get_autorefresh_interval(st.session_state.timeframe), key="auto_refresh")

# =========================
# LAYOUT
# =========================
left_col, center_col, right_col = st.columns([1, 2, 1])

# =========================
# LEFT PANEL (CONTROLS)
# =========================
with left_col:
    st.header("Controls")

    timeframe = st.selectbox(
        "Timeframe",
        ["15m", "1d"],
        key="timeframe"
    )

    source_option = st.radio(
        "Select Ticker Source:",
        ["Nifty50", "Nifty500", "Forex Pairs", "Upload File"],
        key="source_option"
    )

    alerts_active = st.checkbox(
        "Activate Alerts",
        key="alerts_active"
    )

# =========================
# LOAD TICKERS
# =========================
tickers = []

if st.session_state.source_option in ["Nifty50", "Nifty500", "Forex Pairs"]:
    file_map = {
        "Nifty50": "Nifty50.txt",
        "Nifty500": "Nifty500.txt",
        "Forex Pairs": "Forex_Pairs.txt"
    }
    try:
        with open(file_map[st.session_state.source_option], "r") as f:
            content = f.read()
        tickers = [t.strip().upper() for t in content.split(",") if t.strip()]
    except FileNotFoundError:
        st.error("Ticker file not found")
        st.stop()

elif st.session_state.source_option == "Upload File":
    uploaded_file = st.file_uploader("Upload tickers (txt/csv)", type=["txt", "csv"])
    if uploaded_file:
        content = uploaded_file.read().decode("utf-8")
        tickers = [
            t.strip().upper()
            for t in content.replace("\n", ",").split(",")
            if t.strip()
        ]

if not tickers:
    st.stop()

# =========================
# LOAD TRIGGERS
# =========================
with open("triggers.json", "r") as f:
    trigger_formulas = json.load(f)

trigger_condition = left_col.selectbox(
    "Trigger Condition:",
    options=list(trigger_formulas.keys())
)

trigger_text = left_col.text_input(
    "Custom / Edit Trigger:",
    value=trigger_formulas[trigger_condition]
)

# =========================
# FETCH DATA
# =========================
@st.cache_data(ttl=30)
def fetch_data(tickers, timeframe):
    df = yf.download(
        tickers=tickers,
        period="5d" if timeframe == "15m" else "6mo",
        interval=timeframe,
        group_by="ticker",
        progress=False,
        threads=True
    )
    if not isinstance(df.columns, pd.MultiIndex):
        df = pd.concat({tickers[0]: df}, axis=1)
    return df

raw = fetch_data(tickers, st.session_state.timeframe)

# =========================
# SAFE TRIGGER EVALUATOR
# =========================
operators = {
    ast.Gt: op.gt, ast.Lt: op.lt, ast.GtE: op.ge, ast.LtE: op.le,
    ast.Eq: op.eq, ast.NotEq: op.ne, ast.Add: op.add, ast.Sub: op.sub,
    ast.Mult: op.mul, ast.Div: op.truediv,
    ast.And: lambda a,b: a and b,
    ast.Or: lambda a,b: a or b
}

def check_trigger(df, condition):
    if len(df) < 5:
        return False

    def get_value(name, index=-1):
        return float(df.iloc[index][name])

    def _eval(node):
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        elif isinstance(node, ast.BoolOp):
            result = _eval(node.values[0])
            for v in node.values[1:]:
                result = operators[type(node.op)](result, _eval(v))
            return result
        elif isinstance(node, ast.Compare):
            return operators[type(node.ops[0])](
                _eval(node.left),
                _eval(node.comparators[0])
            )
        elif isinstance(node, ast.BinOp):
            return operators[type(node.op)](
                _eval(node.left),
                _eval(node.right)
            )
        elif isinstance(node, ast.Name):
            return get_value(node.id)
        elif isinstance(node, ast.Constant):
            return node.value
        else:
            raise TypeError(node)

    try:
        parsed = ast.parse(condition, mode="eval")
        return bool(_eval(parsed))
    except:
        return False

# =========================
# PROCESS TICKERS
# =========================
results = []

for ticker in tickers:
    if ticker not in raw:
        continue
    df = raw[ticker].dropna().tail(10)
    if df.empty:
        continue
    triggered = check_trigger(df, trigger_text)
    results.append((ticker, df, triggered))

results.sort(key=lambda x: not x[2])

triggered_count = sum(1 for r in results if r[2])

with right_col:
    st.header(f"Results ({triggered_count}/{len(results)})")

    for ticker, df, triggered in results:

        latest = float(df["Close"].iloc[-1])
        prev = float(df["Close"].iloc[-2])
        change = latest - prev
        pct = (change / prev) * 100

        display_name = f"🚨 {ticker}" if triggered else ticker

        if st.button(display_name, key=f"select_{ticker}"):
            st.session_state.selected_ticker = ticker

        st.caption(f"{latest:.2f} ({pct:+.2f}%)")
        st.divider()

# =========================
# CENTER PANEL (CHART)
# =========================
with center_col:
    st.header("Chart")

    if st.session_state.selected_ticker:

        selected = next(
            (r for r in results if r[0] == st.session_state.selected_ticker),
            None
        )

        if selected:
            ticker, df, triggered = selected

            fig = go.Figure(
                data=[go.Candlestick(
                    x=df.index,
                    open=df["Open"],
                    high=df["High"],
                    low=df["Low"],
                    close=df["Close"],
                    increasing_line_color="#16a34a",
                    decreasing_line_color="#dc2626"
                )]
            )

            fig.update_layout(
                height=600,
                xaxis_rangeslider_visible=False,
                template="plotly_white"
            )

            st.plotly_chart(fig, use_container_width=True)

    else:
        st.info("Select a ticker from the right panel.")
