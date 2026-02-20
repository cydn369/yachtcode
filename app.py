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
# CSS FOR SCROLLABLE RESULTS
# =========================
st.markdown(
    """
    <style>
    .scrollable-results {
        height: 600px;  /* adjust as needed */
        overflow-y: auto;
        padding-right: 10px;
        border: 1px solid #eee;
        border-radius: 6px;
    }
    </style>
    """,
    unsafe_allow_html=True
)

# =========================
# SESSION STATE INIT
# =========================
defaults = {
    "scanner_running": False,
    "active_trigger": None,
    "alerted_tickers": set(),
    "selected_ticker": None,
    "uploaded_tickers": [],
    "alerts_active": False,
    "timeframe": "15m",
    "source_option": "Nifty50"
}

for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v

# =========================
# LOAD SECRETS
# =========================
gmail_user = st.secrets["gmail_user"]
gmail_password = st.secrets["gmail_password"]
alert_emails = st.secrets["alert_emails"]
telegram_token = st.secrets["telegram_token"]
telegram_chat_id = st.secrets["telegram_chat_id"]

st.title("Yacht Code v2 – Real Time Scanner")

# =========================
# LAYOUT
# =========================
left_col, center_col, right_col = st.columns([1, 2, 1])

# =========================
# LOAD TRIGGERS
# =========================
try:
    with open("triggers.json", "r") as f:
        trigger_formulas = json.load(f)
except FileNotFoundError:
    st.error("triggers.json not found")
    st.stop()

# =========================
# LEFT PANEL
# =========================
with left_col:
    st.header("Controls")

    st.session_state.timeframe = st.selectbox(
        "Timeframe",
        ["15m", "1d"],
        index=["15m", "1d"].index(st.session_state.timeframe)
    )

    st.session_state.source_option = st.radio(
        "Ticker Source",
        ["Nifty50", "Nifty500", "Forex Pairs", "Upload File"],
        index=["Nifty50","Nifty500","Forex Pairs","Upload File"]
        .index(st.session_state.source_option)
    )

    st.session_state.alerts_active = st.checkbox(
        "Activate Alerts",
        value=st.session_state.alerts_active
    )

    st.divider()
    st.subheader("Trigger")

    trigger_condition = st.selectbox(
        "Trigger Condition",
        list(trigger_formulas.keys())
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
        st.success("Scanner running (refreshes every 60 seconds)")
    else:
        st.info("Scanner stopped")

# =========================
# AUTO REFRESH (EVERY 60 SECONDS)
# =========================
if st.session_state.scanner_running:
    st_autorefresh(interval=60_000, key="scanner_refresh")
else:
    st.stop()

# =========================
# LOAD TICKERS
# =========================
tickers = []

if st.session_state.source_option in ["Nifty50","Nifty500","Forex Pairs"]:
    file_map = {
        "Nifty50": "Nifty50.txt",
        "Nifty500": "Nifty500.txt",
        "Forex Pairs": "Forex_Pairs.txt"
    }
    with open(file_map[st.session_state.source_option], "r") as f:
        content = f.read()
    tickers = [t.strip().upper() for t in content.split(",") if t.strip()]

elif st.session_state.source_option == "Upload File":
    uploaded = st.file_uploader("Upload tickers", type=["txt","csv"])
    if uploaded:
        content = uploaded.read().decode("utf-8")
        st.session_state.uploaded_tickers = [
            t.strip().upper()
            for t in content.replace("\n", ",").split(",")
            if t.strip()
        ]
    tickers = st.session_state.uploaded_tickers

if not tickers:
    st.stop()

# =========================
# CHUNKED DOWNLOAD
# =========================
def chunk_list(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i+size]

@st.cache_data(ttl=30)
def fetch_data(tickers, timeframe):
    frames = []

    for chunk in chunk_list(tickers, 50):
        df = yf.download(
            tickers=chunk,
            period="5d" if timeframe=="15m" else "1mo",
            interval=timeframe,
            group_by="ticker",
            progress=False,
            threads=True
        )
        frames.append(df)

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, axis=1)

    if not isinstance(df.columns, pd.MultiIndex):
        df = pd.concat({tickers[0]: df}, axis=1)

    return df

raw = fetch_data(tickers, st.session_state.timeframe)

if raw.empty:
    st.stop()

# =========================
# ADVANCED SAFE AST ENGINE
# =========================
operators = {
    ast.Gt: op.gt, ast.Lt: op.lt, ast.GtE: op.ge, ast.LtE: op.le,
    ast.Eq: op.eq, ast.NotEq: op.ne,
    ast.Add: op.add, ast.Sub: op.sub,
    ast.Mult: op.mul, ast.Div: op.truediv,
    ast.And: lambda a,b: a and b,
    ast.Or: lambda a,b: a or b
}

allowed_functions = {"abs": abs, "max": max, "min": min}

def check_trigger(df, condition):

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

        elif isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.USub):
                return -_eval(node.operand)
            if isinstance(node.op, ast.Not):
                return not _eval(node.operand)

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

        elif isinstance(node, ast.Subscript):
            field = node.value.id
            index = node.slice.value if isinstance(node.slice, ast.Constant) else _eval(node.slice)
            return get_value(field, index)

        elif isinstance(node, ast.Call):
            func_name = node.func.id
            if func_name not in allowed_functions:
                raise ValueError("Function not allowed")
            return allowed_functions[func_name](*[_eval(arg) for arg in node.args])

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

    df = raw[ticker].dropna().tail(15)
    if df.empty:
        continue

    triggered = check_trigger(df, st.session_state.active_trigger)
    results.append((ticker, df, triggered))

results.sort(key=lambda x: not x[2])
triggered_count = sum(1 for r in results if r[2])

# =========================
# ALERT LOGIC (WITH RETRIGGER)
# =========================
if st.session_state.alerts_active and triggered_count > 0:

    triggered_tickers = [r[0] for r in results if r[2]]
    new_triggers = [
        t for t in triggered_tickers
        if t not in st.session_state.alerted_tickers
    ]

    if new_triggers:
        message = (
            f"{st.session_state.active_trigger}\n"
            f"Triggered: {', '.join(new_triggers)}"
        )

        requests.post(
            f"https://api.telegram.org/bot{telegram_token}/sendMessage",
            data={"chat_id": telegram_chat_id, "text": message}
        )

        try:
            msg = MIMEMultipart()
            msg["From"] = gmail_user
            msg["To"] = ",".join(alert_emails)
            msg["Subject"] = "YachtCode Alert"
            msg.attach(MIMEText(message, "plain"))

            server = smtplib.SMTP("smtp.gmail.com", 587)
            server.starttls()
            server.login(gmail_user, gmail_password)
            server.sendmail(gmail_user, alert_emails, msg.as_string())
            server.quit()
        except:
            pass

        st.session_state.alerted_tickers.update(new_triggers)

# allow retrigger
currently_triggered = {r[0] for r in results if r[2]}
st.session_state.alerted_tickers.intersection_update(currently_triggered)

# =========================
# RIGHT PANEL (RESULTS) - Scrollable
# =========================
with right_col:
    st.header(f"Results ({triggered_count}/{len(results)})")

    st.markdown('<div class="scrollable-results">', unsafe_allow_html=True)

    for ticker, df, triggered in results:
        latest = float(df["Close"].iloc[-1])
        prev = float(df["Close"].iloc[-2])
        pct = ((latest - prev)/prev)*100

        label = f"🚨 {ticker}" if triggered else ticker

        if st.button(label, key=f"select_{ticker}"):
            st.session_state.selected_ticker = ticker

        st.caption(f"{latest:.2f} ({pct:+.2f}%)")
        st.divider()

    st.markdown('</div>', unsafe_allow_html=True)

# =========================
# CENTER PANEL (CHART & TICKER INFO)
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
            latest = float(df["Close"].iloc[-1])
            prev = float(df["Close"].iloc[-2])
            change = latest - prev
            pct = (change / prev) * 100
            color = "#16a34a" if change >= 0 else "#dc2626"
            siren = "🚨 " if triggered else ""

            st.markdown(
                f"<h3 style='margin-bottom:5px'>{siren}{ticker} "
                f"<span style='color:{color}; font-weight:normal; font-size:18px;'>"
                f"{latest:.2f} ({pct:+.2f}%)</span></h3>",
                unsafe_allow_html=True
            )

            fig = go.Figure(data=[go.Candlestick(
                x=df.index,
                open=df["Open"],
                high=df["High"],
                low=df["Low"],
                close=df["Close"]
            )])

            fig.update_layout(
                height=650,
                xaxis_rangeslider_visible=False,
                template="plotly_white"
            )

            st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Click a ticker on the right to load chart.")
