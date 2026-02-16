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

st.set_page_config(layout="wide")

# =========================
# LOAD SECRETS (Streamlit Cloud)
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
    ["Nifty50", "Upload ticker file"]
)

tickers = []

if source_option == "Nifty50":
    try:
        with open("Nifty50.txt", "r") as f:
            content = f.read()
        tickers = [t.strip().upper() for t in content.split(",") if t.strip()]
    except FileNotFoundError:
        st.error("Nifty50.txt not found in the repository.")
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
# Trigger Input
# =========================
trigger_col, input_col = st.columns([1, 2])
with trigger_col:
    st.markdown("**Trigger:**")
with input_col:
    trigger_condition = st.text_input("", value="Close > Open")

st.caption(
    "Fields: Close, Open, High, Low, Volume | "
    "Previous candles: Close[-2], High[-3] | "
    "Functions: abs(), max(), min() | "
    "Operators: and, or, not"
)

# =========================
# Sidebar Controls
# =========================
timeframe = st.sidebar.selectbox(
    "Timeframe",
    ["5m", "15m", "1h"],
    index=1
)

refresh_sec = st.sidebar.number_input(
    "Refresh interval (seconds)",
    min_value=5,
    max_value=900,
    value=890,
    step=5
)

st_autorefresh(interval=refresh_sec * 1000, key="refresh")
st.caption(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

# =========================
# Fetch Data
# =========================
@st.cache_data(ttl=10)
def fetch_data(tickers, timeframe):
    period = "5d" if timeframe in ["5m", "15m"] else "30d"
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

# Fix single ticker structure
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
    requests.post(url, data={
        "chat_id": telegram_chat_id,
        "text": message
    })

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
triggered_count = sum(1 for r in results if r[2])

st.markdown(f"### 🔔 Triggered: {triggered_count} / {len(results)}")

if "previous_triggers" not in st.session_state:
    st.session_state.previous_triggers = set()

current_triggers = {r[0] for r in results if r[2]}
new_triggers = current_triggers - st.session_state.previous_triggers

for ticker in new_triggers:
    message = f"{ticker} triggered condition: {trigger_condition}"
    send_telegram(message)
    send_email(f"Yacht Code Alert: {ticker}", message)

st.session_state.previous_triggers = current_triggers

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
                st.success("TRIGGERED")

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

