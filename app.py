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
from email.mime.base import MIMEBase
from email import encoders
import io

st.set_page_config(layout="wide")

# =========================
# Load secrets from Streamlit Cloud
# =========================
gmail_user = st.secrets["gmail_user"]
gmail_password = st.secrets["gmail_password"]
alert_emails = st.secrets["alert_emails"]

telegram_token = st.secrets["telegram_token"]
telegram_chat_id = st.secrets["telegram_chat_id"]

# =========================
# Title
# =========================
st.title("Yacht Code")

# =========================
# Ticker Source
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
                return operators[type(node.op)](_eval(node.left), _eval(node.right))
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
# Helper: Plotly Figure → PNG for email
# =========================
def fig_to_image(fig):
    buf = io.BytesIO()
    fig.write_image(buf, format="png")  # small PNG
    buf.seek(0)
    return buf

# =========================
# Email Alert with Chart
# =========================
def send_email(subject, message, fig=None):
    try:
        msg = MIMEMultipart()
        msg["From"] = gmail_user
        msg["To"] = ", ".join(alert_emails)
        msg["Subject"] = subject
        msg.attach(MIMEText(message, "plain"))

        if fig is not None:
            img_buf = fig_to_image(fig)
            part = MIMEBase("application", "octet-stream")
            part.set_payload(img_buf.read())
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                'attachment; filename="chart.png"',
            )
            msg.attach(part)

        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(gmail_user, gmail_password)
        server.sendmail(gmail_user, alert_emails, msg.as_string())
        server.quit()
    except Exception as e:
        st.sidebar.error(f"Email error: {e}")

# =========================
# Test Alerts Button
# =========================
if st.sidebar.button("Test Alerts"):
    import numpy as np
    test_df = pd.DataFrame({
        "Open": [10, 11, 12],
        "High": [11, 12, 13],
        "Low": [9, 10, 11],
        "Close": [10.5, 11.5, 12.5]
    })
    fig = go.Figure(data=[go.Candlestick(
        x=test_df.index,
        open=test_df["Open"],
        high=test_df["High"],
        low=test_df["Low"],
        close=test_df["Close"],
        increasing_line_color="#16a34a",
        decreasing_line_color="#dc2626"
    )])
    fig.update_layout(height=300, xaxis_rangeslider_visible=False, showlegend=False, template="plotly_white")
    send_email("Test Email with Chart", "This is a test email with chart", fig)
    send_telegram("Test Telegram from Yacht Code")
    st.sidebar.success("Test alerts with chart sent!")

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
    df = [r[1] for r in results if r[0]==ticker][0]

    # Plotly chart
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
    fig.update_layout(height=300, xaxis_rangeslider_visible=False, showlegend=False, template="plotly_white")

    send_telegram(message)
    send_email(f"Yacht Code Alert: {ticker}", message, fig)

st.session_state.previous_triggers = current_triggers

# =========================
# Display Grid with Yellow Triggered Badge
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
            border_color = "yellow" if triggered else "#e5e7eb"

            st.markdown(
                f"""
                <div style="
                    border:2px solid {border_color};
                    border-radius:10px;
                    padding:10px;
                ">
                <b>{ticker}</b>
                {'<span style="background:yellow;color:black;padding:2px 6px;border-radius:4px;margin-left:8px;">TRIGGERED</span>' if triggered else ''}
                </div>
                """,
                unsafe_allow_html=True
            )

            st.markdown(f"### {latest:.4f}")
            st.markdown(
                f"<span style='color:{color}; font-size:14px;'>"
                f"{change:+.4f} ({pct:+.2f}%)</span>",
                unsafe_allow_html=True
            )

            fig_display = go.Figure(
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
            fig_display.update_layout(
                height=220,
                margin=dict(l=5, r=5, t=5, b=5),
                xaxis_rangeslider_visible=False,
                showlegend=False,
                template="plotly_white"
            )
            fig_display.update_xaxes(showgrid=False)
            fig_display.update_yaxes(showgrid=False)

            st.plotly_chart(fig_display, use_container_width=True)
