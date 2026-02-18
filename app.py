import streamlit as st
import yfinance as yf
import pandas as pd
import plotly.graph_objects as go
from streamlit_autorefresh import st_autorefresh
from datetime import datetime
import requests
import json
import time
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

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
# Load triggers.json
# =========================
try:
    with open("triggers.json", "r") as f:
        triggers_json = json.load(f)
except FileNotFoundError:
    triggers_json = {
        "Default": "((abs(Open - High) / Open)*100 >= 0.333 or (abs(Open - Low) / Open)*100 >= 0.333) and (4*abs(Open-Close)<abs(High-Low))"
    }

# =========================
# Ticker Source Selector
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
    try:
        with open(file_map[source_option], "r") as f:
            content = f.read()
        tickers = [t.strip().upper() for t in content.split(",") if t.strip()]
    except FileNotFoundError:
        st.error(f"{file_map[source_option]} not found.")
        st.stop()
elif source_option == "Upload ticker file":
    uploaded_file = st.file_uploader(
        "Upload ticker file (comma separated)", type=["txt"]
    )
    if uploaded_file is not None:
        content = uploaded_file.read().decode("utf-8")
        tickers = [t.strip().upper() for t in content.split(",") if t.strip()]
    else:
        st.warning("Upload a ticker file to begin.")
        st.stop()

# =========================
# Trigger Condition Input
# =========================
trigger_col, input_col = st.columns([1, 2])
with trigger_col:
    st.markdown("**Trigger:**")
with input_col:
    # Use default formula from triggers.json
    default_formula = triggers_json.get("Default")
    trigger_condition = st.text_input("", value=default_formula)

# =========================
# Sidebar Controls
# =========================
timeframe = st.sidebar.selectbox("Timeframe", ["1m", "5m", "15m", "1h"], index=1)

alerts_active = st.sidebar.checkbox("Activate Alerts", value=True)
if alerts_active:
    st.sidebar.success("Alerts are ACTIVE")
else:
    st.sidebar.info("Alerts are OFF")

if st.sidebar.button("Test Alerts"):
    send_telegram = lambda msg: requests.post(
        f"https://api.telegram.org/bot{telegram_token}/sendMessage",
        data={"chat_id": telegram_chat_id, "text": msg}
    )
    send_email = lambda subj, msg: None  # Simplified for test
    send_telegram("Test Telegram from Yacht Code")
    st.sidebar.success("Test Telegram sent!")

st_autorefresh(interval=60*1000, key="refresh")
st.caption(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

# =========================
# Fetch Data
# =========================
@st.cache_data(ttl=10)
def fetch_data(tickers, timeframe):
    period_map = {"1m": "1d", "5m": "5d", "15m": "15d", "1h": "30d"}
    return yf.download(
        tickers=tickers,
        period=period_map[timeframe],
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
# Simple Trigger Engine
# =========================
import ast, operator as op

operators = {
    ast.Gt: op.gt, ast.Lt: op.lt, ast.GtE: op.ge, ast.LtE: op.le,
    ast.Eq: op.eq, ast.NotEq: op.ne, ast.Add: op.add, ast.Sub: op.sub,
    ast.Mult: op.mul, ast.Div: op.truediv, ast.And: lambda a,b: a and b,
    ast.Or: lambda a,b: a or b,
}

allowed_functions = {"abs": abs, "max": max, "min": min}

def check_trigger(df, condition):
    if len(df) < 3: return False
    def get_value(name, index=-1):
        if name not in ["Open","High","Low","Close","Volume"]:
            raise ValueError("Invalid field")
        return float(df.iloc[index][name])
    def _eval(node):
        if isinstance(node, ast.Expression): return _eval(node.body)
        elif isinstance(node, ast.BoolOp):
            result = _eval(node.values[0])
            for v in node.values[1:]:
                result = operators[type(node.op)](result,_eval(v))
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
            return get_value(field,index)
        elif isinstance(node, ast.Call):
            func_name = node.func.id
            args = [_eval(a) for a in node.args]
            return allowed_functions[func_name](*args)
        elif isinstance(node, ast.Name):
            return get_value(node.id)
        elif isinstance(node, ast.Constant):
            return node.value
        else: raise TypeError(node)
    try: parsed = ast.parse(condition, mode='eval'); return bool(_eval(parsed))
    except: return False

# =========================
# Alerts Functions
# =========================
def send_telegram(message):
    requests.post(
        f"https://api.telegram.org/bot{telegram_token}/sendMessage",
        data={"chat_id": telegram_chat_id, "text": message}
    )

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
# Process Tickers
# =========================
results = []
for ticker in tickers:
    if ticker not in raw: continue
    df = raw[ticker].dropna().tail(10)
    if df.empty: continue
    triggered = check_trigger(df, trigger_condition)
    results.append((ticker, df, triggered))

# Sort by triggered
results.sort(key=lambda x: x[2], reverse=True)
triggered_tickers = [t[0] for t in results if t[2]]

# =========================
# Consolidated Alerts
# =========================
if alerts_active and triggered_tickers:
    msg_body = f"{trigger_condition}\n\nTriggered Tickers:\n" + "\n".join(triggered_tickers)
    send_telegram(f"Here's the YachtCode {timeframe} Alert Brief\n{msg_body}")
    send_email(f"Here's the YachtCode {timeframe} Alert Brief", msg_body)

# =========================
# Display Grid
# =========================
st.markdown(f"### 🔔 Triggered: {len(triggered_tickers)} / {len(results)}")
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
            st.markdown(f"<span style='color:{color}; font-size:14px;'>{change:+.4f} ({pct:+.2f}%)</span>", unsafe_allow_html=True)
            fig = go.Figure(data=[go.Candlestick(
                x=df.index, open=df["Open"], high=df["High"], low=df["Low"], close=df["Close"],
                increasing_line_color="#16a34a", decreasing_line_color="#dc2626"
            )])
            fig.update_layout(height=220, margin=dict(l=5,r=5,t=5,b=5),
                              xaxis_rangeslider_visible=False, showlegend=False, template="plotly_white")
            fig.update_xaxes(showgrid=False, zeroline=False)
            fig.update_yaxes(showgrid=False, zeroline=False)
            st.plotly_chart(fig, use_container_width=True)
