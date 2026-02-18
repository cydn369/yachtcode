import streamlit as st
import yfinance as yf
import pandas as pd
import plotly.graph_objects as go
from streamlit_autorefresh import st_autorefresh
from datetime import datetime
import requests
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
# TITLE
# =========================
st.title("Yacht Code")

# =========================
# Ticker Source Selector
# =========================
source_option = st.radio(
    "Select Ticker Source:",
    ["Nifty50", "Nifty500", "Forex Pairs", "Upload ticker file"]
)

tickers = []

if source_option == "Nifty50":
    file_name = "Nifty50.txt"
elif source_option == "Nifty500":
    file_name = "Nifty500.txt"
elif source_option == "Forex Pairs":
    file_name = "Forex_Pairs.txt"
elif source_option == "Upload ticker file":
    uploaded_file = st.file_uploader("Upload ticker file (comma separated)", type=["txt"])
    if uploaded_file is not None:
        content = uploaded_file.read().decode("utf-8")
        tickers = [t.strip().upper() for t in content.split(",") if t.strip()]
    else:
        st.warning("Upload a ticker file to begin.")
        st.stop()

if source_option in ["Nifty50", "Nifty500", "Forex Pairs"]:
    try:
        with open(file_name, "r") as f:
            content = f.read()
        tickers = [t.strip().upper() for t in content.split(",") if t.strip()]
    except FileNotFoundError:
        st.error(f"{file_name} not found.")
        st.stop()

# =========================
# Load trigger formulas from JSON
# =========================
with open("triggers.json", "r") as f:
    triggers_json = json.load(f)

default_formula = triggers_json.get("Default", "Close > Open")
trigger_options = list(triggers_json.keys())

selected_trigger_name = st.selectbox("Select Trigger Condition:", trigger_options, index=0)
trigger_condition = triggers_json[selected_trigger_name]

# =========================
# Sidebar Controls
# =========================
timeframe = st.sidebar.selectbox("Timeframe", ["1m", "5m", "15m", "1h"], index=1)

# Auto-refresh based on candle interval
def get_refresh_interval(timeframe):
    if timeframe == "1m":
        return 50  # refresh at 50th second
    elif timeframe == "5m":
        return 270  # 4 min 30 sec
    elif timeframe == "15m":
        return 840  # 14th minute
    elif timeframe == "1h":
        return 3540  # 59th minute
    else:
        return 60

st_autorefresh(interval=get_refresh_interval(timeframe) * 1000, key="refresh")

st.markdown(f"**Last updated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

# =========================
# Fetch Data
# =========================
@st.cache_data(ttl=10)
def fetch_data(tickers, timeframe):
    period = {
        "1m": "1d",
        "5m": "5d",
        "15m": "15d",
        "1h": "30d"
    }.get(timeframe, "5d")
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
# Safe Expression Engine
# =========================
import ast, operator as op

operators = {
    ast.Gt: op.gt, ast.Lt: op.lt, ast.GtE: op.ge, ast.LtE: op.le,
    ast.Eq: op.eq, ast.NotEq: op.ne,
    ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv,
    ast.And: lambda a,b: a and b, ast.Or: lambda a,b: a or b
}
allowed_functions = {"abs": abs, "max": max, "min": min}

def check_trigger(df, condition):
    try:
        if len(df) < 3: return False
        def get_value(name, index=-1):
            if name not in ["Open","High","Low","Close","Volume"]:
                raise ValueError("Invalid field")
            return float(df.iloc[index][name])
        def _eval(node):
            if isinstance(node, ast.Expression): return _eval(node.body)
            elif isinstance(node, ast.BoolOp):
                result = _eval(node.values[0])
                for v in node.values[1:]: result = operators[type(node.op)](result,_eval(v))
                return result
            elif isinstance(node, ast.UnaryOp):
                if isinstance(node.op, ast.USub): return -_eval(node.operand)
                if isinstance(node.op, ast.Not): return not _eval(node.operand)
            elif isinstance(node, ast.Compare):
                left = _eval(node.left)
                right = _eval(node.comparators[0])
                return operators[type(node.ops[0])](left,right)
            elif isinstance(node, ast.BinOp):
                return operators[type(node.op)](_eval(node.left), _eval(node.right))
            elif isinstance(node, ast.Subscript):
                field = node.value.id
                index = _eval(node.slice) if not isinstance(node.slice, ast.Constant) else node.slice.value
                return get_value(field,index)
            elif isinstance(node, ast.Call):
                func_name = node.func.id
                if func_name not in allowed_functions: raise ValueError("Function not allowed")
                args = [_eval(arg) for arg in node.args]
                return allowed_functions[func_name](*args)
            elif isinstance(node, ast.Name):
                return get_value(node.id,-1)
            elif isinstance(node, ast.Constant): return node.value
            else: raise TypeError(node)
        parsed = ast.parse(condition, mode='eval')
        return bool(_eval(parsed))
    except Exception:
        return False

# =========================
# Alerts
# =========================
def send_telegram(message):
    requests.post(f"https://api.telegram.org/bot{telegram_token}/sendMessage",
                  data={"chat_id": telegram_chat_id, "text": message})

def send_email(subject, message):
    try:
        msg = MIMEMultipart()
        msg["From"] = gmail_user
        msg["To"] = ", ".join(alert_emails)
        msg["Subject"] = subject
        msg.attach(MIMEText(message,"plain"))
        server = smtplib.SMTP("smtp.gmail.com",587)
        server.starttls()
        server.login(gmail_user,gmail_password)
        server.sendmail(gmail_user,alert_emails,msg.as_string())
        server.quit()
    except Exception as e:
        st.sidebar.error(f"Email error: {e}")

alerts_active = st.sidebar.checkbox("Activate Alerts", value=False)
if alerts_active: st.sidebar.success("Alerts are ACTIVE")

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

results.sort(key=lambda x: x[2], reverse=True)
triggered_tickers = [r[0] for r in results if r[2]]
triggered_count = len(triggered_tickers)
st.markdown(f"### 🔔 Triggered: {triggered_count} / {len(results)}")

# =========================
# Consolidated Alerts
# =========================
if alerts_active and triggered_tickers:
    msg_body = f"{trigger_condition}\n\nTriggered Tickers:\n" + ", ".join(triggered_tickers)
    telegram_msg = f"Here's the YachtCode {timeframe} Alert Brief\n{msg_body}"
    email_subject = f"Here's the YachtCode {timeframe} Alert Brief"
    send_telegram(telegram_msg)
    send_email(email_subject, msg_body)

# =========================
# Display Grid (Plotly reverted)
# =========================
cards_per_row = 4
for i in range(0,len(results),cards_per_row):
    row = results[i:i+cards_per_row]
    cols = st.columns(cards_per_row)
    for col, (ticker, df, triggered) in zip(cols,row):
        latest = float(df["Close"].iloc[-1])
        prev = float(df["Close"].iloc[-2])
        change = latest - prev
        pct = (change/prev)*100
        color = "#16a34a" if change>=0 else "#dc2626"
        with col:
            st.markdown(f"**{ticker}**")
            if triggered:
                st.warning("TRIGGERED")
            st.markdown(f"### {latest:.4f}")
            st.markdown(f"<span style='color:{color}; font-size:14px;'>{change:+.4f} ({pct:+.2f}%)</span>", unsafe_allow_html=True)
            fig = go.Figure(data=[go.Candlestick(
                x=df.index, open=df["Open"], high=df["High"], low=df["Low"], close=df["Close"],
                increasing_line_color="#16a34a", decreasing_line_color="#dc2626"
            )])
            fig.update_layout(height=220, margin=dict(l=5,r=5,t=5,b=5),
                              xaxis_rangeslider_visible=False, showlegend=False, template="plotly_white")
            fig.update_xaxes(showgrid=False)
            fig.update_yaxes(showgrid=False)
            st.plotly_chart(fig, use_container_width=True)

