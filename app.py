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

# =========================
# LOAD SECRETS
# =========================
gmail_user = st.secrets["gmail_user"]
gmail_password = st.secrets["gmail_password"]
alert_emails = st.secrets["alert_emails"]

telegram_token = st.secrets["telegram_token"]
telegram_chat_id = st.secrets["telegram_chat_id"]

# =========================
# TITLE
# =========================
st.title("Yacht Code")

# =========================
# SIDEBAR CONTROLS
# =========================
timeframe = st.sidebar.selectbox(
    "Timeframe",
    ["1m", "5m", "15m", "1h"],
    index=["1m","5m","15m","1h"].index(st.session_state.timeframe),
    key="timeframe"
)

source_option = st.radio(
    "Select Ticker Source:",
    ["Nifty50", "Nifty500", "Forex Pairs", "Upload File"],
    index=["Nifty50","Nifty500","Forex Pairs","Upload File"].index(st.session_state.source_option),
    key="source_option"
)

alerts_active = st.sidebar.checkbox(
    "Activate Alerts",
    value=st.session_state.alerts_active,
    key="alerts_active"
)

# =========================
# AUTO-REFRESH USING st_autorefresh
# =========================
interval_map = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}

# Calculate approximate refresh 30s before next candle
def get_autorefresh_interval(timeframe):
    now = datetime.now()
    if timeframe == "1m":
        next_candle = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    elif timeframe == "5m":
        next_candle = now.replace(minute=(now.minute//5)*5, second=0, microsecond=0) + timedelta(minutes=5)
    elif timeframe == "15m":
        next_candle = now.replace(minute=(now.minute//15)*15, second=0, microsecond=0) + timedelta(minutes=15)
    elif timeframe == "1h":
        next_candle = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    refresh_time = next_candle - timedelta(seconds=30)
    interval = max((refresh_time - now).total_seconds(), 5)
    return int(interval*1000)  # milliseconds for st_autorefresh

st_autorefresh(interval=get_autorefresh_interval(timeframe), key="auto_refresh")

# =========================
# STATIC COUNTDOWN CAPTION
# =========================
def seconds_until_next_candle(timeframe):
    now = datetime.now()
    if timeframe == "1m":
        next_candle = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    elif timeframe == "5m":
        next_candle = now.replace(minute=(now.minute//5)*5, second=0, microsecond=0) + timedelta(minutes=5)
    elif timeframe == "15m":
        next_candle = now.replace(minute=(now.minute//15)*15, second=0, microsecond=0) + timedelta(minutes=15)
    elif timeframe == "1h":
        next_candle = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    refresh_time = next_candle - timedelta(seconds=30)
    return max(int((refresh_time - now).total_seconds()), 0)

st.caption(f"Next refresh in ~{seconds_until_next_candle(timeframe)} seconds (30s before candle)")

# =========================
# LOAD TICKERS
# =========================
tickers = []

if source_option in ["Nifty50", "Nifty500", "Forex Pairs"]:
    file_map = {
        "Nifty50": "Nifty50.txt",
        "Nifty500": "Nifty500.txt",
        "Forex Pairs": "Forex_Pairs.txt"
    }
    try:
        with open(file_map[source_option], "r") as f:
            content = f.read()
        tickers = [t.strip().upper() for t in content.split(",") if t.strip()]
    except FileNotFoundError:
        st.error(f"{file_map[source_option]} not found")
        st.stop()
elif source_option == "Upload File":
    uploaded_file = st.file_uploader("Upload tickers (txt/csv)", type=["txt", "csv"])
    if uploaded_file:
        content = uploaded_file.read().decode("utf-8")
        st.session_state.uploaded_tickers = [
            t.strip().upper()
            for t in content.replace("\n", ",").split(",")
            if t.strip()
        ]
    if "uploaded_tickers" in st.session_state:
        tickers = st.session_state.uploaded_tickers
    else:
        st.warning("Please upload a file")
        st.stop()

if not tickers:
    st.stop()

# =========================
# LOAD TRIGGERS
# =========================
try:
    with open("triggers.json", "r") as f:
        trigger_formulas = json.load(f)
except FileNotFoundError:
    st.error("triggers.json not found")
    st.stop()

trigger_condition = st.selectbox(
    "Trigger Condition:",
    options=list(trigger_formulas.keys())
)

trigger_text = st.text_input(
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
        period="5d",
        interval=timeframe,
        group_by="ticker",
        progress=False,
        threads=True
    )
    if not isinstance(df.columns, pd.MultiIndex):
        df = pd.concat({tickers[0]: df}, axis=1)
    return df

raw = fetch_data(tickers, timeframe)
if raw.empty:
    st.warning("No data received.")
    st.stop()

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
allowed_functions = {"abs": abs, "max": max, "min": min}

def check_trigger(df, condition):
    if len(df) < 3: return False
    def get_value(name, index=-1):
        return float(df.iloc[index][name])
    def _eval(node):
        if isinstance(node, ast.Expression): return _eval(node.body)
        elif isinstance(node, ast.BoolOp):
            result = _eval(node.values[0])
            for v in node.values[1:]:
                result = operators[type(node.op)](result, _eval(v))
            return result
        elif isinstance(node, ast.Compare):
            return operators[type(node.ops[0])](_eval(node.left), _eval(node.comparators[0]))
        elif isinstance(node, ast.BinOp):
            return operators[type(node.op)](_eval(node.left), _eval(node.right))
        elif isinstance(node, ast.Name): return get_value(node.id)
        elif isinstance(node, ast.Constant): return node.value
        else: return False
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
    if ticker not in raw: continue
    df = raw[ticker].dropna().tail(10)
    if df.empty: continue
    triggered = check_trigger(df, trigger_text)
    results.append((ticker, df, triggered))

# Sort triggered first
results.sort(key=lambda x: not x[2])
triggered_count = sum(1 for r in results if r[2])
st.markdown(f"### 🔔 Triggered: {triggered_count} / {len(results)}")

# =========================
# ALERTS
# =========================
if alerts_active and triggered_count > 0:
    triggered_tickers = [r[0] for r in results if r[2]]
    new_triggers = [t for t in triggered_tickers if t not in st.session_state.alerted_tickers]
    if new_triggers:
        message_body = f"{trigger_text}\nTriggered Tickers: {', '.join(new_triggers)}"
        # Telegram
        requests.post(
            f"https://api.telegram.org/bot{telegram_token}/sendMessage",
            data={"chat_id": telegram_chat_id, "text": f"YachtCode {timeframe} Alert\n{message_body}"}
        )
        # Email
        try:
            msg = MIMEMultipart()
            msg["From"] = gmail_user
            msg["To"] = ",".join(alert_emails)
            msg["Subject"] = f"YachtCode {timeframe} Alert"
            msg.attach(MIMEText(message_body, "plain"))
            server = smtplib.SMTP("smtp.gmail.com", 587)
            server.starttls()
            server.login(gmail_user, gmail_password)
            server.sendmail(gmail_user, alert_emails, msg.as_string())
            server.quit()
        except Exception as e:
            st.sidebar.error(f"Email error: {e}")
        st.session_state.alerted_tickers.update(new_triggers)

# =========================
# DISPLAY CARDS
# =========================
cards_per_row = 4
for i in range(0, len(results), cards_per_row):
    row = results[i:i+cards_per_row]
    cols = st.columns(cards_per_row)
    for col, (ticker, df, triggered) in zip(cols, row):
        latest = float(df["Close"].iloc[-1])
        prev = float(df["Close"].iloc[-2])
        change = latest - prev
        pct = (change / prev) * 100
        color = "#16a34a" if change >= 0 else "#dc2626"
        with col:
            st.markdown(f"**{ticker}**")
            if triggered: st.warning("TRIGGERED")
            st.markdown(f"### {latest:.4f}")
            st.markdown(
                f"<span style='color:{color}; font-size:14px;'>"
                f"{change:+.4f} ({pct:+.2f}%)</span>",
                unsafe_allow_html=True
            )
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
                height=220,
                margin=dict(l=5, r=5, t=5, b=5),
                xaxis_rangeslider_visible=False,
                showlegend=False,
                template="plotly_white"
            )
            st.plotly_chart(fig, use_container_width=True)

