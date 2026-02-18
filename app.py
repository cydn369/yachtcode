import streamlit as st
import yfinance as yf
import pandas as pd
import plotly.graph_objects as go
from streamlit_autorefresh import st_autorefresh
from datetime import datetime, timedelta
import requests
import ast
import operator as op
import smtplib
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import json

# =========================
# PAGE CONFIG
# =========================
st.set_page_config(layout="wide")
st.title("Yacht Code")

# =========================
# SESSION STATE FOR ALERTS
# =========================
if "last_alert" not in st.session_state:
    st.session_state.last_alert = {}

# =========================
# LOAD SECRETS
# =========================
gmail_user = st.secrets["gmail_user"]
gmail_password = st.secrets["gmail_password"]
alert_emails = st.secrets["alert_emails"]

telegram_token = st.secrets["telegram_token"]
telegram_chat_id = st.secrets["telegram_chat_id"]

# =========================
# FUNCTION: CANDLE-ALIGNED REFRESH
# =========================
def seconds_until_next_refresh(timeframe):
    now = datetime.now()
    if timeframe == "1m":
        target = now.replace(second=50, microsecond=0)
        if now.second >= 50:
            target += timedelta(minutes=1)
    elif timeframe == "5m":
        minute_block = (now.minute // 5) * 5
        target = now.replace(minute=minute_block + 4, second=30, microsecond=0)
        if now >= target:
            target += timedelta(minutes=5)
    elif timeframe == "15m":
        minute_block = (now.minute // 15) * 15
        target = now.replace(minute=minute_block + 14, second=0, microsecond=0)
        if now >= target:
            target += timedelta(minutes=15)
    elif timeframe == "1h":
        target = now.replace(minute=59, second=0, microsecond=0)
        if now >= target:
            target += timedelta(hours=1)
    else:
        return 60
    return max(1, int((target - now).total_seconds()))

# =========================
# TICKER SOURCES
# =========================
source_option = st.radio(
    "Select Ticker Source:",
    ["Nifty50", "Nifty500", "Forex Pairs", "Upload ticker file"]
)

tickers = []

if source_option in ["Nifty50", "Nifty500", "Forex Pairs"]:
    file_map = {
        "Nifty50": "Nifty50.txt",
        "Nifty500": "Nifty500.txt",
        "Forex Pairs": "Forex_Pairs.txt"
    }
    file_name = file_map[source_option]
    try:
        with open(file_name, "r") as f:
            content = f.read()
        tickers = [t.strip().upper() for t in content.split(",") if t.strip()]
    except FileNotFoundError:
        st.error(f"{file_name} not found in repository.")
        st.stop()
elif source_option == "Upload ticker file":
    uploaded_file = st.file_uploader(
        "Upload ticker file (comma separated)",
        type=["txt"]
    )
    if uploaded_file is not None:
        content = uploaded_file.read().decode("utf-8")
        tickers = [t.strip().upper() for t in content.split(",") if t.strip()]
    else:
        st.warning("Upload a ticker file to begin.")
        st.stop()

# =========================
# LOAD TRIGGER FORMULAS JSON
# =========================
try:
    with open("triggers.json") as f:
        trigger_dict = json.load(f)
except FileNotFoundError:
    trigger_dict = {"Default": "((abs(Open - High) / Open)*100 >= 0.333 or (abs(Open - Low) / Open)*100 >= 0.333) and (4*abs(Open-Close)<abs(High-Low))"}

# =========================
# TRIGGER FORMULA COMBO BOX + EDITABLE
# =========================
trigger_name = st.selectbox("Select Trigger Preset", ["Default"] + list(trigger_dict.keys()))
if trigger_name in trigger_dict:
    default_formula = trigger_dict[trigger_name]
trigger_condition = st.text_input("Trigger Condition", value=default_formula)

# =========================
# SIDEBAR: TIMEFRAME & ALERTS
# =========================
timeframe = st.sidebar.selectbox(
    "Timeframe",
    ["1m", "5m", "15m", "1h"],
    index=2
)

alerts_active = st.sidebar.checkbox("Activate Alerts", value=False)
if alerts_active:
    st.sidebar.success("Alerts ACTIVE")
else:
    st.sidebar.info("Alerts OFF")

# =========================
# AUTO REFRESH
# =========================
seconds_to_wait = seconds_until_next_refresh(timeframe)
st_autorefresh(interval=seconds_to_wait * 1000, key="refresh")
st.caption(f"Next refresh in {seconds_to_wait} seconds")
st.caption(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

# =========================
# FETCH DATA
# =========================
@st.cache_data(ttl=10)
def fetch_data(tickers, timeframe):
    period_map = {"1m": "2d", "5m": "2d", "15m": "2d", "1h": "2d"}
    period = period_map[timeframe]
    return yf.download(
        tickers=tickers,
        period=period,
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
# SAFE AST EVALUATION
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

allowed_functions = {"abs": abs, "max": max, "min": min}

def check_trigger(df, condition):
    try:
        if len(df) < 3:
            return False
        def get_value(name, index=-1):
            if name not in ["Open","High","Low","Close","Volume"]:
                raise ValueError("Invalid field")
            if abs(index) > len(df):
                raise ValueError("Index out of range")
            return float(df.iloc[index][name])
        def _eval(node):
            if isinstance(node, ast.Expression):
                return _eval(node.body)
            elif isinstance(node, ast.BoolOp):
                result = _eval(node.values[0])
                for v in node.values[1:]:
                    result = operators[type(node.op)](result,_eval(v))
                return result
            elif isinstance(node, ast.UnaryOp):
                if isinstance(node.op, ast.USub): return -_eval(node.operand)
                if isinstance(node.op, ast.Not): return not _eval(node.operand)
            elif isinstance(node, ast.Compare):
                left = _eval(node.left)
                right = _eval(node.comparators[0])
                return operators[type(node.ops[0])](left,right)
            elif isinstance(node, ast.BinOp):
                return operators[type(node.op)](_eval(node.left),_eval(node.right))
            elif isinstance(node, ast.Subscript):
                field = node.value.id
                index = node.slice.value if isinstance(node.slice, ast.Constant) else _eval(node.slice)
                return get_value(field,index)
            elif isinstance(node, ast.Call):
                func_name = node.func.id
                if func_name not in allowed_functions: raise ValueError("Function not allowed")
                args = [_eval(arg) for arg in node.args]
                return allowed_functions[func_name](*args)
            elif isinstance(node, ast.Name):
                return get_value(node.id)
            elif isinstance(node, ast.Constant):
                return node.value
            else:
                raise TypeError(node)
        parsed = ast.parse(condition, mode='eval')
        return bool(_eval(parsed))
    except Exception:
        return False

# =========================
# TELEGRAM ALERT
# =========================
def send_telegram(message):
    url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
    requests.post(url, data={"chat_id": telegram_chat_id, "text": message})

# =========================
# PROCESS TICKERS & ALERTS
# =========================
results = []
for ticker in tickers:
    if ticker not in raw: continue
    df = raw[ticker].dropna().tail(10)
    if df.empty: continue
    triggered = check_trigger(df, trigger_condition)
    results.append((ticker, df, triggered))

results.sort(key=lambda x:x[2], reverse=True)
triggered_count = sum(1 for r in results if r[2])
st.markdown(f"### 🔔 Triggered: {triggered_count} / {len(results)}")

# -------------------------
# ALERTS (SINGLE PER CANDLE)
# -------------------------
if alerts_active:
    try:
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(gmail_user, gmail_password)
    except Exception as e:
        server = None
        st.sidebar.error(f"SMTP error: {e}")

    for ticker, df, triggered in results:
        if not triggered: continue
        last_candle_time = df.index[-1]
        if ticker not in st.session_state.last_alert:
            st.session_state.last_alert[ticker] = None
        if st.session_state.last_alert[ticker] != last_candle_time:
            message = f"{ticker} triggered condition: {trigger_condition}"
            send_telegram(message)
            if server:
                try:
                    msg = MIMEMultipart()
                    msg["From"] = gmail_user
                    msg["To"] = ", ".join(alert_emails)
                    msg["Subject"] = f"Yacht Code Alert: {ticker}"
                    msg.attach(MIMEText(message, "plain"))
                    server.sendmail(gmail_user, alert_emails, msg.as_string())
                except Exception as e:
                    st.sidebar.error(f"Email error for {ticker}: {e}")
            st.session_state.last_alert[ticker] = last_candle_time
            time.sleep(0.3)
    if server:
        server.quit()

# =========================
# DISPLAY GRID
# =========================
cards_per_row = 4
for i in range(0,len(results),cards_per_row):
    row = results[i:i+cards_per_row]
    cols = st.columns(cards_per_row)
    for col, (ticker, df, triggered) in zip(cols,row):
        latest = float(df["Close"].iloc[-1])
        prev = float(df["Close"].iloc[-2])
        change = latest-prev
        pct = (change/prev)*100
        color = "#16a34a" if change>=0 else "#dc2626"
        with col:
            st.markdown(f"**{ticker}**")
            if triggered:
                st.warning("TRIGGERED")  # Yellow badge
            st.markdown(f"### {latest:.4f}")
            st.markdown(f"<span style='color:{color}; font-size:14px;'>{change:+.4f} ({pct:+.2f}%)</span>", unsafe_allow_html=True)
            fig = go.Figure(data=[go.Candlestick(
                x=df.index,
                open=df["Open"],
                high=df["High"],
                low=df["Low"],
                close=df["Close"],
                increasing_line_color="#16a34a",
                decreasing_line_color="#dc2626"
            )])
            fig.update_layout(height=220, margin=dict(l=5,r=5,t=5,b=5), xaxis_rangeslider_visible=False, showlegend=False, template="plotly_white")
            fig.update_xaxes(showgrid=False)
            fig.update_yaxes(showgrid=False)
            st.plotly_chart(fig, use_container_width=True)



