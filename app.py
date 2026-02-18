import streamlit as st
import yfinance as yf
import pandas as pd
import plotly.graph_objects as go
from streamlit_autorefresh import st_autorefresh
from datetime import datetime
import requests
import ast
import operator as op
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import time
import json

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
# LOAD TRIGGER FORMULAS
# =========================
try:
    with open("trigger.json", "r") as f:
        trigger_formulas = json.load(f)
except FileNotFoundError:
    trigger_formulas = {
        "Default": "((abs(Open - High) / Open)*100 >= 0.333 or (abs(Open - Low) / Open)*100 >= 0.333) and (4*abs(Open-Close)<abs(High-Low))"
    }

default_formula = trigger_formulas.get("Default")

# =========================
# TITLE
# =========================
st.title("Yacht Code")

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
    st.error(f"{file_map[source_option]} not found in repository.")
    st.stop()

# =========================
# Trigger Input (Combo Box)
# =========================
trigger_condition = st.selectbox(
    "Select or enter Trigger Condition:",
    options=list(trigger_formulas.values()),
    index=list(trigger_formulas.values()).index(default_formula),
    editable=True
)

# =========================
# Sidebar Controls
# =========================
timeframe = st.sidebar.selectbox(
    "Timeframe",
    ["1m", "5m", "15m", "1h"],
    index=1
)

alerts_active = st.sidebar.checkbox(
    "Activate Alerts",
    value=False
)

if alerts_active:
    st.sidebar.success("Alerts are ACTIVE")
else:
    st.sidebar.info("Alerts are OFF")

# =========================
# Candle-aligned auto-refresh logic
# =========================
import schedule, threading, time as ttime

def get_refresh_interval_sec(tf):
    if tf == "1m":
        return 50
    elif tf == "5m":
        return 4*60 + 30
    elif tf == "15m":
        return 14*60
    elif tf == "1h":
        return 59*60
    return 300

refresh_interval = get_refresh_interval_sec(timeframe)
st_autorefresh(interval=refresh_interval*1000, key="refresh")

st.caption(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

# =========================
# Fetch Data
# =========================
@st.cache_data(ttl=10)
def fetch_data(tickers, timeframe):
    period_map = {
        "1m": "2d",
        "5m": "2d",
        "15m": "2d",
        "1h": "2d"
    }
    return yf.download(
        tickers=tickers,
        period=period_map.get(timeframe, "2d"),
        interval=timeframe,
        group_by="ticker",
        progress=False,
        threads=True
    )

raw = fetch_data(tickers, timeframe)
if raw.empty:
    st.warning("No data received from Yahoo Finance.")
    st.stop()

if not isinstance(raw.columns, pd.MultiIndex):
    raw = pd.concat({tickers[0]: raw}, axis=1)

# =========================
# Safe Expression Engine
# =========================
operators = {
    ast.Gt: op.gt,
    ast.Lt: op.lt,
    ast.GtE: op.ge,
    ast.LtE: op.le,
    ast.Eq: op.eq,
    ast.NotEq: op.ne,
    ast.Add: op.add,
    ast.Sub: op.sub,
    ast.Mult: op.mul,
    ast.Div: op.truediv,
    ast.And: lambda a, b: a and b,
    ast.Or: lambda a, b: a or b,
}

allowed_functions = {
    "abs": abs,
    "max": max,
    "min": min,
}

def check_trigger(df, condition):
    try:
        if len(df) < 3:
            return False
        def get_value(name, index=-1):
            if name not in ["Open", "High", "Low", "Close", "Volume"]:
                raise ValueError("Invalid field")
            return float(df.iloc[index][name])
        def _eval(node):
            if isinstance(node, ast.Expression):
                return _eval(node.body)
            elif isinstance(node, ast.BoolOp):
                result = _eval(node.values[0])
                for v in node.values[1:]:
                    result = operators[type(node.op)](result, _eval(v))
                return result
            elif isinstance(node, ast.UnaryOp):
                if isinstance(node.op, ast.USub):
                    return -_eval(node.operand)
                if isinstance(node.op, ast.Not):
                    return not _eval(node.operand)
            elif isinstance(node, ast.Compare):
                left = _eval(node.left)
                right = _eval(node.comparators[0])
                return operators[type(node.ops[0])](left, right)
            elif isinstance(node, ast.BinOp):
                return operators[type(node.op)](
                    _eval(node.left),
                    _eval(node.right)
                )
            elif isinstance(node, ast.Subscript):
                field = node.value.id
                if isinstance(node.slice, ast.Constant):
                    index = node.slice.value
                else:
                    index = _eval(node.slice)
                return get_value(field, index)
            elif isinstance(node, ast.Call):
                func_name = node.func.id
                if func_name not in allowed_functions:
                    raise ValueError("Function not allowed")
                args = [_eval(arg) for arg in node.args]
                return allowed_functions[func_name](*args)
            elif isinstance(node, ast.Name):
                return get_value(node.id, -1)
            elif isinstance(node, ast.Constant):
                return node.value
            else:
                raise TypeError(node)
        parsed = ast.parse(condition, mode='eval')
        return bool(_eval(parsed))
    except Exception:
        return False

# =========================
# Telegram Alert
# =========================
def send_telegram(message):
    url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
    requests.post(url, data={"chat_id": telegram_chat_id, "text": message})

# =========================
# Email Alert
# =========================
def send_email(subject, message):
    try:
        msg = MIMEMultipart()
        msg["From"] = gmail_user
        msg["To"] = ", ".join(alert_emails)
        msg["Subject"] = subject
        msg.attach(MIMEText(message, "plain"))
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(gmail_user, gmail_password)
        server.sendmail(gmail_user, alert_emails, msg.as_string())
        server.quit()
    except Exception as e:
        st.sidebar.error(f"Email error: {e}")

# =========================
# TEST BUTTON
# =========================
if st.sidebar.button("Test Alerts"):
    send_telegram("Test Telegram from Yacht Code")
    send_email("Test Email", "This is a test email from Yacht Code.")
    st.sidebar.success("Test alerts sent!")

# =========================
# Process Tickers and Consolidated Alerts
# =========================
results = []

for ticker in tickers:
    if ticker not in raw:
        continue
    df = raw[ticker].dropna().tail(10)
    if df.empty:
        continue
    triggered = check_trigger(df, trigger_condition)
    results.append((ticker, df, triggered))

results.sort(key=lambda x: x[2], reverse=True)
triggered_count = sum(1 for r in results if r[2])

st.markdown(f"### 🔔 Triggered: {triggered_count} / {len(results)}")

# =========================
# Consolidated Alerts
# =========================
if alerts_active:
    triggered_tickers = [ticker for ticker, df, triggered in results if triggered]
    if triggered_tickers:
        tickers_list = "\n".join(triggered_tickers)
        subject = f"Here's the YachtCode {timeframe} Alert Brief"
        body = f"Condition: {trigger_condition}\nTriggered Tickers:\n{tickers_list}"
        send_telegram(f"{subject}\n{body}")
        send_email(subject, body)

# =========================
# Display Grid
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
            if triggered:
                st.markdown(f"<span style='background-color:yellow; color:black; font-weight:bold;'>TRIGGERED</span>", unsafe_allow_html=True)
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
            fig.update_xaxes(showgrid=False)
            fig.update_yaxes(showgrid=False)
            st.plotly_chart(fig, use_container_width=True)
