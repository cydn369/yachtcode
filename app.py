import streamlit as st
import yfinance as yf
import pandas as pd
import plotly.graph_objects as go
from streamlit_autorefresh import st_autorefresh
from datetime import datetime, timedelta
import requests
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import time
import ast
import operator as op

st.set_page_config(layout="wide")

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
# Load trigger formulas
# =========================
try:
    with open("triggers.json", "r") as f:
        trigger_formulas = json.load(f)
except FileNotFoundError:
    st.error("triggers.json not found")
    st.stop()

# =========================
# Ticker Source Selector
# =========================
source_option = st.radio(
    "Select Ticker Source:",
    ["Nifty50", "Nifty500", "Forex Pairs"]
)

tickers = []
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

# =========================
# Sidebar Controls
# =========================
timeframe = st.sidebar.selectbox(
    "Timeframe",
    ["1m", "5m", "15m", "1h"],
    index=1
)

# =========================
# Last updated / next update
# =========================
def next_update_time(now, timeframe):
    if timeframe == "1m":
        next_ts = now.replace(second=50, microsecond=0)
        if now.second >= 50:
            next_ts += timedelta(minutes=1)
    elif timeframe == "5m":
        next_min = (now.minute // 5) * 5 + 4
        next_ts = now.replace(minute=next_min % 60, second=30, microsecond=0)
        if now.minute > next_min or (now.minute == next_min and now.second >= 30):
            next_ts += timedelta(minutes=5)
    elif timeframe == "15m":
        next_min = (now.minute // 15) * 15 + 14
        next_ts = now.replace(minute=next_min % 60, second=0, microsecond=0)
        if now.minute > next_min:
            next_ts += timedelta(minutes=15)
    elif timeframe == "1h":
        next_ts = now.replace(minute=59, second=0, microsecond=0)
        if now.minute >= 59:
            next_ts += timedelta(hours=1)
    return next_ts

now = datetime.now()
st.caption(f"Last updated: {now.strftime('%Y-%m-%d %H:%M:%S')}")
st.caption(f"Next update (approx): {next_update_time(now, timeframe).strftime('%Y-%m-%d %H:%M:%S')}")

# =========================
# Auto-refresh per candle interval
# =========================
interval_ms_map = {
    "1m": 60 * 1000,
    "5m": 5 * 60 * 1000,
    "15m": 15 * 60 * 1000,
    "1h": 60 * 60 * 1000
}
st_autorefresh(interval=interval_ms_map[timeframe], key="refresh")

# =========================
# Trigger condition combo box
# =========================
trigger_options = list(trigger_formulas.keys())
default_trigger = "Default"
trigger_condition = st.selectbox(
    "Trigger Condition:",
    options=trigger_options,
    index=trigger_options.index(default_trigger)
)
trigger_text = st.text_input(
    "Custom / Edit Trigger:",
    value=trigger_formulas[trigger_condition]
)

# =========================
# Alerts sidebar
# =========================
alerts_active = st.sidebar.checkbox("Activate Alerts", value=False)

if alerts_active:
    st.sidebar.success("Alerts are ACTIVE")
else:
    st.sidebar.info("Alerts are OFF")

if st.sidebar.button("Test Alerts"):
    requests.post(
        f"https://api.telegram.org/bot{telegram_token}/sendMessage",
        data={"chat_id": telegram_chat_id, "text": "Test Telegram from Yacht Code"}
    )
    try:
        msg = MIMEMultipart()
        msg["From"] = gmail_user
        msg["To"] = ",".join(alert_emails)
        msg["Subject"] = "Test Email"
        msg.attach(MIMEText("This is a test email from Yacht Code", "plain"))
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(gmail_user, gmail_password)
        server.sendmail(gmail_user, alert_emails, msg.as_string())
        server.quit()
    except Exception as e:
        st.sidebar.error(f"Email error: {e}")
    st.sidebar.success("Test alerts sent!")

# =========================
# Fetch Data
# =========================
@st.cache_data(ttl=10)
def fetch_data(tickers, timeframe):
    period_map = {"1m": "1d", "5m": "5d", "15m": "15d", "1h": "60d"}
    df = yf.download(
        tickers=tickers,
        period=period_map[timeframe],
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
    st.warning("No data received from Yahoo Finance.")
    st.stop()

# =========================
# Safe trigger evaluator
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
        elif isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.USub): return -_eval(node.operand)
            if isinstance(node.op, ast.Not): return not _eval(node.operand)
        elif isinstance(node, ast.Compare):
            return operators[type(node.ops[0])](_eval(node.left), _eval(node.comparators[0]))
        elif isinstance(node, ast.BinOp):
            return operators[type(node.op)](_eval(node.left), _eval(node.right))
        elif isinstance(node, ast.Subscript):
            field = node.value.id
            index = node.slice.value if isinstance(node.slice, ast.Constant) else _eval(node.slice)
            return get_value(field, index)
        elif isinstance(node, ast.Call):
            func_name = node.func.id
            if func_name not in allowed_functions: raise ValueError("Function not allowed")
            return allowed_functions[func_name](*[_eval(arg) for arg in node.args])
        elif isinstance(node, ast.Name): return get_value(node.id, -1)
        elif isinstance(node, ast.Constant): return node.value
        else: raise TypeError(node)
    parsed = ast.parse(condition, mode="eval")
    try: return bool(_eval(parsed))
    except: return False

# =========================
# Process tickers and trigger
# =========================
results = []
for ticker in tickers:
    if ticker not in raw: continue
    df = raw[ticker].dropna().tail(10)
    if df.empty: continue
    triggered = check_trigger(df, trigger_text)
    results.append((ticker, df, triggered))

triggered_count = sum(1 for r in results if r[2])
st.markdown(f"### 🔔 Triggered: {triggered_count} / {len(results)}")

# =========================
# Send consolidated alerts
# =========================
if alerts_active and triggered_count > 0:
    triggered_tickers = [r[0] for r in results if r[2]]
    message_body = f"{trigger_text}\nTriggered Tickers: {', '.join(triggered_tickers)}"

    # Telegram
    requests.post(
        f"https://api.telegram.org/bot{telegram_token}/sendMessage",
        data={"chat_id": telegram_chat_id, "text": f"Here's the YachtCode {timeframe} Alert Brief\n{message_body}"}
    )

    # Email
    try:
        msg = MIMEMultipart()
        msg["From"] = gmail_user
        msg["To"] = ",".join(alert_emails)
        msg["Subject"] = f"Here's the YachtCode {timeframe} Alert Brief"
        msg.attach(MIMEText(message_body, "plain"))
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(gmail_user, gmail_password)
        server.sendmail(gmail_user, alert_emails, msg.as_string())
        server.quit()
    except Exception as e:
        st.sidebar.error(f"Email error: {e}")

# =========================
# Display tickers in cards
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
            fig.update_xaxes(showgrid=False, zeroline=False)
            fig.update_yaxes(showgrid=False, zeroline=False)
            st.plotly_chart(fig, use_container_width=True)
