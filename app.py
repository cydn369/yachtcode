import streamlit as st
import yfinance as yf
import pandas as pd
import json
import ast
import operator as op
import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# =========================
# PAGE CONFIG AND BACKGROUND
# =========================
st.set_page_config(layout="wide")
st.title("Yacht Code")

# Set PNG background
st.markdown(
    """
    <style>
    .stApp {
        background-image: url('background.png');  /* Use local PNG file */
        background-size: cover;
        background-position: center;
    }
    </style>
    """,
    unsafe_allow_html=True
)

# =========================
# SESSION STATE INIT
# =========================
defaults = {
    "active_trigger": None,
    "uploaded_tickers": [],
    "timeframe": "1d",
    "source_option": "Nifty50",
    "alerts_active": False,
    "alerted_tickers": set()
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
# LAYOUT (LEFT + RIGHT)
# =========================
left_col, right_col = st.columns([1, 2])

# =========================
# LEFT PANEL (CONTROLS)
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

    st.subheader("Trigger")
    trigger_condition = st.selectbox("Trigger Condition", list(trigger_formulas.keys()))
    trigger_text = st.text_input("Edit Trigger", value=trigger_formulas[trigger_condition])

    scan_clicked = st.button("Scan Market", type="primary")

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

# =========================
# AST TRIGGER ENGINE
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
            return operators[type(node.ops[0])](_eval(node.left), _eval(node.comparators[0]))
        elif isinstance(node, ast.BinOp):
            return operators[type(node.op)](_eval(node.left), _eval(node.right))
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
# RIGHT PANEL (RESULTS)
# =========================
with right_col:
    st.header("Results")

    if scan_clicked:
        if not tickers:
            st.warning("No tickers loaded.")
            st.stop()

        with st.spinner("Scanning..."):
            raw = yf.download(
                tickers=tickers,
                period="5d" if st.session_state.timeframe=="15m" else "1mo",
                interval=st.session_state.timeframe,
                group_by="ticker",
                progress=False,
                threads=True
            )

            if raw.empty:
                st.warning("No data received.")
                st.stop()

            if not isinstance(raw.columns, pd.MultiIndex):
                raw = pd.concat({tickers[0]: raw}, axis=1)

            results = []

            for ticker in tickers:
                if ticker not in raw:
                    continue

                df = raw[ticker].dropna().tail(15)
                if df.empty:
                    continue

                triggered = check_trigger(df, trigger_text)
                current_price = float(df["Close"].iloc[-1])
                yahoo_finance_link = f"https://finance.yahoo.com/chart/{ticker}"

                results.append({
                    "Ticker": f"🚨 {ticker}" if triggered else ticker,
                    "RawTicker": ticker,
                    "Current Price": round(current_price, 2),
                    "Triggered": triggered,
                    "Chart Link": f'<a href="{yahoo_finance_link}" target="_blank">View Chart</a>'
                })

            result_df = pd.DataFrame(results)
            result_df = result_df.sort_values(by="Triggered", ascending=False)
            display_df = result_df[["Ticker", "Current Price", "Chart Link"]]

            triggered_count = result_df["Triggered"].sum()
            total_processed = len(result_df)
            st.success(f"{triggered_count} of {total_processed} Stocks Triggered")

            st.markdown(display_df.to_html(escape=False), unsafe_allow_html=True)

            # =========================
            # ALERT LOGIC
            # =========================
            if st.session_state.alerts_active:
                triggered_tickers = result_df[result_df["Triggered"]]["RawTicker"].tolist()
                new_triggers = [t for t in triggered_tickers if t not in st.session_state.alerted_tickers]

                if new_triggers:
                    message = f"{trigger_text}\nTriggered: {', '.join(new_triggers)}"

                    # Telegram alert
                    requests.post(
                        f"https://api.telegram.org/bot{telegram_token}/sendMessage",
                        data={"chat_id": telegram_chat_id, "text": message}
                    )

                    # Email alert
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

                st.session_state.alerted_tickers.intersection_update(triggered_tickers)
