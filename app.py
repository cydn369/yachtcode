import streamlit as st
import yfinance as yf
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timedelta
import requests
import ast
import operator as op
import smtplib
import time
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
else:
    file_name = None

if file_name:
    try:
        with open(file_name, "r") as f:
            content = f.read()
        tickers = [t.strip().upper() for t in content.split(",") if t.strip()]
    except FileNotFoundError:
        st.error(f"{file_name} not found.")
        st.stop()
elif source_option == "Upload ticker file":
    uploaded_file = st.file_uploader(
        "Upload ticker file (comma separated)",
        type=["txt"]
    )
    if uploaded_file:
        content = uploaded_file.read().decode("utf-8")
        tickers = [t.strip().upper() for t in content.split(",") if t.strip()]
    else:
        st.warning("Upload a ticker file to begin.")
        st.stop()

# =========================
# Trigger Input
# =========================
trigger_col, input_col = st.columns([1, 2])
with trigger_col:
    st.markdown("**Trigger:**")
with input_col:
    trigger_condition = st.text_input("", value="((abs(Open - High) / Open)*100 >= 0.333 or (abs(Open - Low) / Open)*100 >= 0.333) and (4*abs(Open-Close)<abs(High-Low))")

# =========================
# Sidebar Controls
# =========================
timeframe = st.sidebar.selectbox(
    "Timeframe",
    ["1m", "5m", "15m", "1h"],
    index=1
)

alerts_active = st.sidebar.checkbox("Activate Alerts", value=False)
if alerts_active:
    st.sidebar.success("Alerts are ACTIVE")
else:
    st.sidebar.info("Alerts are OFF")

if st.sidebar.button("Test Alerts"):
    # Simple test
    requests.post(f"https://api.telegram.org/bot{telegram_token}/sendMessage", data={
        "chat_id": telegram_chat_id,
        "text": "Test Telegram from Yacht Code"
    })
    msg = MIMEMultipart()
    msg["From"] = gmail_user
    msg["To"] = ", ".join(alert_emails)
    msg["Subject"] = "Test Email"
    msg.attach(MIMEText("This is a test email from Yacht Code.", "plain"))
    server = smtplib.SMTP("smtp.gmail.com", 587)
    server.starttls()
    server.login(gmail_user, gmail_password)
    server.sendmail(gmail_user, alert_emails, msg.as_string())
    server.quit()
    st.sidebar.success("Test alerts sent!")

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

# =========================
# Safe Expression Engine
# =========================
operators = {
    ast.Gt: op.gt, ast.Lt: op.lt, ast.GtE: op.ge, ast.LtE: op.le,
    ast.Eq: op.eq, ast.NotEq: op.ne, ast.Add: op.add, ast.Sub: op.sub,
    ast.Mult: op.mul, ast.Div: op.truediv, ast.And: lambda a, b: a and b,
    ast.Or: lambda a, b: a or b
}
allowed_functions = {"abs": abs, "max": max, "min": min}

def check_trigger(df, condition):
    try:
        if len(df) < 3: return False
        def get_value(name, index=-1):
            if name not in ["Open","High","Low","Close","Volume"]: raise ValueError("Invalid field")
            return float(df.iloc[index][name])
        def _eval(node):
            if isinstance(node, ast.Expression): return _eval(node.body)
            elif isinstance(node, ast.BoolOp):
                res = _eval(node.values[0])
                for v in node.values[1:]:
                    res = operators[type(node.op)](res,_eval(v))
                return res
            elif isinstance(node, ast.UnaryOp):
                if isinstance(node.op, ast.USub): return -_eval(node.operand)
                if isinstance(node.op, ast.Not): return not _eval(node.operand)
            elif isinstance(node, ast.Compare):
                return operators[type(node.ops[0])](_eval(node.left), _eval(node.comparators[0]))
            elif isinstance(node, ast.BinOp):
                return operators[type(node.op)](_eval(node.left), _eval(node.right))
            elif isinstance(node, ast.Subscript):
                field = node.value.id
                index = _eval(node.slice) if not isinstance(node.slice, ast.Constant) else node.slice.value
                return get_value(field, index)
            elif isinstance(node, ast.Call):
                func_name = node.func.id
                if func_name not in allowed_functions: raise ValueError("Function not allowed")
                return allowed_functions[func_name](*[_eval(arg) for arg in node.args])
            elif isinstance(node, ast.Name): return get_value(node.id, -1)
            elif isinstance(node, ast.Constant): return node.value
            else: raise TypeError(node)
        return bool(_eval(ast.parse(condition, mode='eval')))
    except Exception:
        return False

# =========================
# Alerts
# =========================
def send_telegram(message):
    requests.post(f"https://api.telegram.org/bot{telegram_token}/sendMessage", data={
        "chat_id": telegram_chat_id,
        "text": message
    })

def send_email(subject, message):
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

# =========================
# Candle-aligned Refresh Logic
# =========================
def wait_for_next_candle(tf):
    now = datetime.now()
    if tf == "1m":
        wait_sec = 50 - now.second if now.second < 50 else 110 - now.second
    elif tf == "5m":
        next_min = (now.minute//5)*5 + 4
        wait_sec = ((next_min - now.minute)%60)*60 + (30 - now.second)
    elif tf == "15m":
        next_min = (now.minute//15)*15 + 14
        wait_sec = ((next_min - now.minute)%60)*60 + (0 - now.second)
    elif tf == "1h":
        wait_sec = ((59 - now.minute)%60)*60 + (0 - now.second)
    else:
        wait_sec = 60
    if wait_sec < 0: wait_sec += 3600
    time.sleep(wait_sec)

# =========================
# Main Loop
# =========================
while True:
    wait_for_next_candle(timeframe)
    raw = fetch_data(tickers, timeframe)
    if raw.empty: 
        st.warning("No data received.")
        continue
    if not isinstance(raw.columns, pd.MultiIndex):
        raw = pd.concat({tickers[0]: raw}, axis=1)
    results = []
    for ticker in tickers:
        if ticker not in raw: continue
        df = raw[ticker].dropna().tail(10)
        if df.empty: continue
        triggered = check_trigger(df, trigger_condition)
        results.append((ticker, df, triggered))
    results.sort(key=lambda x: x[2], reverse=True)
    triggered_list = [t[0] for t in results if t[2]]

    # =========================
    # Consolidated Alerts
    # =========================
    if alerts_active and triggered_list:
        condition_str = trigger_condition
        subject = f"Here's the YachtCode {timeframe} Alert Brief"
        body = f"{condition_str}\nTriggered Tickers: {', '.join(triggered_list)}"
        send_email(subject, body)
        send_telegram(body)

    # =========================
    # Display Grid
    # =========================
    cards_per_row = 4
    for i in range(0, len(results), cards_per_row):
        row = results[i:i+cards_per_row]
        cols = st.columns(cards_per_row)
        for col, (ticker, df, triggered) in zip(cols, row):
            latest, prev = float(df["Close"].iloc[-1]), float(df["Close"].iloc[-2])
            change, pct = latest - prev, (latest - prev)/prev*100
            color = "#16a34a" if change>=0 else "#dc2626"
            with col:
                st.markdown(f"**{ticker}**")
                if triggered: st.markdown(f"<span style='background-color:yellow;padding:3px;'>TRIGGERED</span>", unsafe_allow_html=True)
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
                fig.update_layout(
                    height=220,
                    margin=dict(l=5,r=5,t=5,b=5),
                    xaxis_rangeslider_visible=False,
                    xaxis_type="category",
                    showlegend=False,
                    template="plotly_white"
                )
                fig.update_xaxes(showgrid=False)
                fig.update_yaxes(showgrid=False)
                st.plotly_chart(fig, use_container_width=True)
