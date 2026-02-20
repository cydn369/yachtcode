import streamlit as st
import yfinance as yf
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime
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
if "scanner_running" not in st.session_state:
    st.session_state.scanner_running = False

if "active_trigger" not in st.session_state:
    st.session_state.active_trigger = None

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

st.title("Yacht Code – Real Time Scanner")

# =========================
# LAYOUT
# =========================
left_col, center_col, right_col = st.columns([1, 2, 1])

# =========================
# LOAD TRIGGERS
# =========================
with open("triggers.json", "r") as f:
    trigger_formulas = json.load(f)

# =========================
# LEFT PANEL (CONTROLS)
# =========================
with left_col:
    st.header("Controls")

    timeframe = st.selectbox("Timeframe", ["15m", "1d"])

    source_option = st.radio(
        "Ticker Source",
        ["Nifty50", "Nifty500", "Forex Pairs", "Upload File"]
    )

    alerts_active = st.checkbox("Activate Alerts")

    st.divider()
    st.subheader("Trigger")

    trigger_condition = st.selectbox(
        "Trigger Condition",
        options=list(trigger_formulas.keys())
    )

    trigger_text = st.text_input(
        "Edit Trigger",
        value=trigger_formulas[trigger_condition]
    )

    if st.button("Apply & Start Scanner", type="primary"):
        st.session_state.active_trigger = trigger_text
        st.session_state.scanner_running = True
        st.session_state.alerted_tickers.clear()

    if st.button("Stop Scanner"):
        st.session_state.scanner_running = False

    if st.session_state.scanner_running:
        st.success("Scanner running (updates every 60 seconds)")
    else:
        st.info("Scanner stopped")

# =========================
# REAL-TIME REFRESH (1 MIN)
# =========================
if st.session_state.scanner_running:
    st_autorefresh(interval=60_000, key="scanner_refresh")
else:
    st.stop()

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

    with open(file_map[source_option], "r") as f:
        content = f.read()

    tickers = [t.strip().upper() for t in content.split(",") if t.strip()]

elif source_option == "Upload File":
    uploaded_file = st.file_uploader("Upload tickers", type=["txt", "csv"])
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
# CHUNKED DATA DOWNLOAD
# =========================
def chunk_list(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]

@st.cache_data(ttl=30)
def fetch_data(tickers, timeframe):
    all_data = []

    for chunk in chunk_list(tickers, 50):  # 50 tickers per request
        df = yf.download(
            tickers=chunk,
            period="5d" if timeframe == "15m" else "6mo",
            interval=timeframe,
            group_by="ticker",
            progress=False,
            threads=True
        )
        all_data.append(df)

    if not all_data:
        return pd.DataFrame()

    return pd.concat(all_data, axis=1)

raw = fetch_data(tickers, timeframe)

if raw.empty:
    st.warning("No data received")
    st.stop()

# =========================
# SAFE TRIGGER ENGINE
# =========================
operators = {
    ast.Gt: op.gt, ast.Lt: op.lt, ast.GtE: op.ge, ast.LtE: op.le,
    ast.Eq: op.eq, ast.NotEq: op.ne,
    ast.Add: op.add, ast.Sub: op.sub,
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

    triggered = check_trigger(df, st.session_state.active_trigger)
    results.append((ticker, df, triggered))

# Sort triggered first
results.sort(key=lambda x: not x[2])

triggered_count = sum(1 for r in results if r[2])

# =========================
# ALERTS
# =========================
if alerts_active and triggered_count > 0:

    triggered_tickers = [r[0] for r in results if r[2]]
    new_triggers = [
        t for t in triggered_tickers
        if t not in st.session_state.alerted_tickers
    ]

    if new_triggers:
        message_body = (
            f"{st.session_state.active_trigger}\n"
            f"Triggered: {', '.join(new_triggers)}"
        )

        # Telegram
        requests.post(
            f"https://api.telegram.org/bot{telegram_token}/sendMessage",
            data={
                "chat_id": telegram_chat_id,
                "text": message_body
            }
        )

        # Email
        try:
            msg = MIMEMultipart()
            msg["From"] = gmail_user
            msg["To"] = ",".join(alert_emails)
            msg["Subject"] = "YachtCode Alert"
            msg.attach(MIMEText(message_body, "plain"))

            server = smtplib.SMTP("smtp.gmail.com", 587)
            server.starttls()
            server.login(gmail_user, gmail_password)
            server.sendmail(gmail_user, alert_emails, msg.as_string())
            server.quit()
        except Exception as e:
            left_col.error(f"Email error: {e}")

        st.session_state.alerted_tickers.update(new_triggers)

# =========================
# RIGHT PANEL (RESULTS)
# =========================
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
                height=650,
                xaxis_rangeslider_visible=False,
                template="plotly_white"
            )

            st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Click a ticker on the right to load chart.")
