import streamlit as st
import yfinance as yf
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime
import requests
import ast
import operator as op
import smtplib
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
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

if source_option in ["Nifty50", "Nifty500", "Forex Pairs"]:
    file_map = {"Nifty50": "Nifty50.txt", "Nifty500": "Nifty500.txt", "Forex Pairs": "Forex_Pairs.txt"}
    try:
        with open(file_map[source_option], "r") as f:
            content = f.read()
        tickers = [t.strip().upper() for t in content.split(",") if t.strip()]
    except FileNotFoundError:
        st.error(f"{file_map[source_option]} not found.")
        st.stop()

elif source_option == "Upload ticker file":
    uploaded_file = st.file_uploader("Upload ticker file (comma separated)", type=["txt"])
    if uploaded_file is not None:
        content = uploaded_file.read().decode("utf-8")
        tickers = [t.strip().upper() for t in content.split(",") if t.strip()]
    else:
        st.warning("Upload a ticker file to begin.")
        st.stop()

# =========================
# Trigger Input with Combo Box (from trigger.json)
# =========================
trigger_col, input_col = st.columns([1, 2])
with trigger_col:
    st.markdown("**Trigger:**")
with input_col:
    # Load formulas from JSON
    try:
        with open("triggers.json", "r") as f:
            trigger_formulas = json.load(f)
    except FileNotFoundError:
        trigger_formulas = {
            "Default": "((abs(Open - High) / Open)*100 >= 0.333 or (abs(Open - Low) / Open)*100 >= 0.333) and (4*abs(Open-Close)<abs(High-Low))"
        }

    # Combo box to select a formula
    selected_formula = st.selectbox(
        "Select formula or type new:",
        options=list(trigger_formulas.keys()),
        index=0
    )

    # Text box to display/override the formula
    trigger_condition = st.text_input(
        "",
        value=trigger_formulas[selected_formula]
    )


# =========================
# Sidebar Controls
# =========================
timeframe = st.sidebar.selectbox(
    "Timeframe",
    ["1m", "5m", "15m", "1h"],
    index=1
)

# =========================
# Alerts Controls
# =========================
alerts_active = st.sidebar.checkbox(
    "Activate Alerts",
    value=False  # Default OFF
)

if alerts_active:
    st.sidebar.success("Alerts are ACTIVE")
else:
    st.sidebar.info("Alerts are OFF")

# Test Alerts button
if st.sidebar.button("Test Alerts"):
    msg_body = f"{trigger_condition}\n\nTriggered Tickers List: TestTicker1, TestTicker2"
    send_telegram(f"Here's the YachtCode {timeframe} Alert Brief\n{msg_body}")
    send_email(f"Here's the YachtCode {timeframe} Alert Brief", msg_body)
    st.sidebar.success("Test alert sent via Telegram & Email!")

# =========================
# Fetch Data
# =========================
@st.cache_data(ttl=10)
def fetch_data(tickers, timeframe):
    period_map = {"1m": "1d", "5m": "5d", "15m": "15d", "1h": "60d"}
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

allowed_functions = {"abs": abs, "max": max, "min": min}

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
                return operators[type(node.op)](_eval(node.left), _eval(node.right))
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
# Process Tickers
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
triggered_tickers = [r[0] for r in results if r[2]]

# =========================
# Consolidated Alerts
# =========================
if alerts_active and triggered_tickers:
    alert_msg = f"Here's the YachtCode {timeframe} Alert Brief\n{trigger_condition}\nTriggered Tickers: {', '.join(triggered_tickers)}"
    send_telegram(alert_msg)
    send_email(f"Here's the YachtCode {timeframe} Alert Brief", alert_msg)

st.markdown(f"### 🔔 Triggered: {len(triggered_tickers)} / {len(results)}")

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
                st.warning("TRIGGERED")  # yellow badge

            st.markdown(f"### {latest:.4f}")
            st.markdown(
                f"<span style='color:{color}; font-size:14px;'>"
                f"{change:+.4f} ({pct:+.2f}%)</span>",
                unsafe_allow_html=True
            )

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
                margin=dict(l=5, r=5, t=5, b=5),
                xaxis_rangeslider_visible=False,
                showlegend=False,
                template="plotly_white"
            )

            # Remove gaps
            fig.update_xaxes(type='category', showgrid=False)
            fig.update_yaxes(showgrid=False)

            st.plotly_chart(fig, use_container_width=True)


