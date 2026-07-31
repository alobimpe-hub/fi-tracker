import streamlit as st
import numpy_financial as npf
import numpy as np
import pandas as pd
import sqlite3
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime, date
from dateutil.relativedelta import relativedelta
import requests
import os

# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="FI Tracker",
    page_icon="💸",
    layout="wide",
    initial_sidebar_state="auto",
)

# ── Authentication ───────────────────────────────────────────────────────────
def check_password():
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False

    if st.session_state.authenticated:
        return True

    with st.form("login_form"):
        st.title("💸 FI Tracker")
        st.caption("Enter your password to unlock")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Unlock")

        if submitted:
            expected = st.secrets.get("APP_PASSWORD", "demo1234")
            if password == expected:
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("Wrong password")
    return False


if not check_password():
    st.stop()

# ── Database ─────────────────────────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(__file__), "finance_tracker.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS mortgage_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            loan_amount REAL NOT NULL,
            annual_rate REAL NOT NULL,
            term_months INTEGER NOT NULL,
            start_date TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS planned_extras (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id INTEGER NOT NULL,
            extra_date TEXT NOT NULL,
            amount REAL NOT NULL,
            description TEXT,
            FOREIGN KEY (plan_id) REFERENCES mortgage_plans(id)
        );

        CREATE TABLE IF NOT EXISTS payment_status (
            plan_id INTEGER NOT NULL,
            payment_number INTEGER NOT NULL,
            is_paid INTEGER DEFAULT 0,
            paid_date TEXT,
            actual_extra REAL DEFAULT 0,
            PRIMARY KEY (plan_id, payment_number),
            FOREIGN KEY (plan_id) REFERENCES mortgage_plans(id)
        );

        CREATE TABLE IF NOT EXISTS balance_checkpoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id INTEGER NOT NULL,
            checkpoint_date TEXT NOT NULL,
            bank_balance REAL NOT NULL,
            notes TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (plan_id) REFERENCES mortgage_plans(id)
        );

        CREATE TABLE IF NOT EXISTS investment_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_date TEXT NOT NULL,
            contribution REAL DEFAULT 0,
            total_balance REAL NOT NULL,
            notes TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS fi_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            monthly_expenses REAL NOT NULL DEFAULT 0,
            withdrawal_rate REAL NOT NULL DEFAULT 4.0,
            updated_at TEXT DEFAULT (datetime('now'))
        );
    """
    )
    conn.commit()
    conn.close()


# ── Brazilian formatting ─────────────────────────────────────────────────────
def fmt_brl(val):
    if val is None or pd.isna(val):
        return ""
    return f"R$ {val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def parse_brl(s):
    if not s:
        return 0.0
    cleaned = s.replace("R$", "").replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


# ── Amortization engine ──────────────────────────────────────────────────────
def generate_schedule(loan_amount, annual_rate, term_months, start_date, extras=None):
    """
    Generate full amortization schedule with optional planned extra payments.
    extras: list of (date_str, amount) for planned extra payments
    """
    monthly_rate = annual_rate / 100.0 / 12.0
    monthly_payment = npf.pmt(monthly_rate, term_months, -loan_amount)

    if extras is None:
        extras = []

    schedule = []
    balance = loan_amount
    extra_map = {}
    for extra_date, extra_amt in extras:
        extra_map[extra_date] = extra_map.get(extra_date, 0) + extra_amt

    for i in range(1, term_months + 1):
        payment_date = start_date + relativedelta(months=i - 1)
        date_key = payment_date.strftime("%Y-%m-%d")

        interest = balance * monthly_rate
        principal = monthly_payment - interest

        if principal > balance:
            principal = balance
            monthly_payment_actual = principal + interest
        else:
            monthly_payment_actual = monthly_payment

        planned_extra = extra_map.get(date_key, 0)
        total_principal = principal + planned_extra
        if total_principal > balance:
            total_principal = balance
            planned_extra = total_principal - principal

        balance -= total_principal

        schedule.append(
            {
                "payment_number": i,
                "due_date": payment_date,
                "date_key": date_key,
                "scheduled_payment": round(monthly_payment_actual, 2),
                "scheduled_principal": round(principal, 2),
                "scheduled_interest": round(interest, 2),
                "planned_extra": round(planned_extra, 2),
                "scheduled_balance": round(max(balance, 0), 2),
                "total_payment": round(monthly_payment_actual + planned_extra, 2),
            }
        )

        if balance <= 0:
            break

    return schedule


def find_original_payoff(schedule):
    for row in schedule:
        if row["scheduled_balance"] <= 0:
            return row["due_date"], row["payment_number"]
    return schedule[-1]["due_date"], schedule[-1]["payment_number"]


def recalc_remaining(schedule, paid_statuses, loan_amount, checkpoints=None):
    """
    Recalculate actual remaining balance based on which payments have been made,
    extra amounts paid, and any bank-reported balance checkpoints (for TR/index adjustments).
    Returns (actual_balance, last_paid, tr_adjustment).
    """
    if checkpoints is None:
        checkpoints = []

    cp_sorted = sorted(checkpoints, key=lambda c: c["checkpoint_date"])

    if not cp_sorted:
        actual_balance = loan_amount
        last_paid = None
        for row in schedule:
            pn = row["payment_number"]
            paid_info = paid_statuses.get(pn, {"is_paid": 0, "actual_extra": 0})
            if paid_info["is_paid"]:
                actual_balance -= row["scheduled_principal"] + paid_info["actual_extra"]
                last_paid = pn
            if row["scheduled_balance"] <= 0 and actual_balance <= 0:
                break
        return round(max(actual_balance, 0), 2), last_paid, 0.0

    latest_cp = cp_sorted[-1]
    actual_balance = latest_cp["bank_balance"]

    theoretical = loan_amount
    for row in schedule:
        pn = row["payment_number"]
        paid_info = paid_statuses.get(pn, {"is_paid": 0, "actual_extra": 0})
        if row["date_key"] < latest_cp["checkpoint_date"]:
            if paid_info["is_paid"]:
                theoretical -= row["scheduled_principal"] + paid_info["actual_extra"]
            if theoretical <= 0:
                theoretical = 0
                break
        else:
            break

    tr_adjustment = actual_balance - max(theoretical, 0)

    last_paid = None
    for row in schedule:
        pn = row["payment_number"]
        if row["date_key"] < latest_cp["checkpoint_date"]:
            continue
        paid_info = paid_statuses.get(pn, {"is_paid": 0, "actual_extra": 0})
        if paid_info["is_paid"]:
            actual_balance -= row["scheduled_principal"] + paid_info["actual_extra"]
            last_paid = pn
        if actual_balance <= 0:
            break

    return round(max(actual_balance, 0), 2), last_paid, round(tr_adjustment, 2)


def projected_payoff(actual_balance, monthly_rate, scheduled_payment, future_extras=None):
    """
    Calculate projected payoff date based on actual remaining balance,
    future planned extras.
    future_extras: list of (months_from_now, amount)
    """
    if actual_balance <= 0:
        return 0, None
    remaining_months = npf.nper(monthly_rate, scheduled_payment, -actual_balance)
    return max(0, int(np.ceil(remaining_months)))


# ── BCB API ──────────────────────────────────────────────────────────────────
@st.cache_data(ttl=86400)
def fetch_bcb_series(series_code, name):
    """
    Fetch a time series from Banco Central do Brasil SGS API.
    Returns DataFrame with 'data' and 'valor' columns.
    """
    url = f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{series_code}/dados?formato=json"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        df = pd.DataFrame(data)
        df["data"] = pd.to_datetime(df["data"], format="%d/%m/%Y")
        df["valor"] = pd.to_numeric(df["valor"], errors="coerce")
        df = df.sort_values("data")
        return df
    except Exception as e:
        return pd.DataFrame()


@st.cache_data(ttl=86400)
def get_selic():
    """SELIC annual rate (série 4189) - already annualized"""
    df = fetch_bcb_series(4189, "SELIC")
    if not df.empty:
        df["annualized"] = df["valor"]
    return df


@st.cache_data(ttl=86400)
def get_ipca():
    """IPCA monthly % (série 433)"""
    return fetch_bcb_series(433, "IPCA")


@st.cache_data(ttl=86400)
def get_cdi():
    """CDI monthly rate (série 4391), annualized"""
    df = fetch_bcb_series(4391, "CDI")
    if not df.empty:
        df["annualized"] = ((1 + df["valor"] / 100) ** 12 - 1) * 100
    return df


# ── Navigation ───────────────────────────────────────────────────────────────
st.title("💸 FI Tracker")
st.caption("Mortgage Payoff · Investment Growth · Brazilian Indicators")

page = st.sidebar.radio(
    "Menu",
    ["Dashboard", "FI Tracker", "Mortgage Tracker", "Investment Tracker", "BCB Indicators"],
)

if st.sidebar.button("🔒 Lock App"):
    st.session_state.authenticated = False
    st.rerun()

# ── Initialize DB ────────────────────────────────────────────────────────────
init_db()

# ══════════════════════════════════════════════════════════════════════════════
# DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════
if page == "Dashboard":
    st.header("At a Glance")

    col1, col2, col3 = st.columns(3)

    # ── Mortgage summary ─────────────────────────────────────────────────
    with col1:
        st.subheader("🏠 Mortgage")
        conn = get_db()
        plans = conn.execute(
            "SELECT id, name, loan_amount, annual_rate, start_date, term_months "
            "FROM mortgage_plans ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if plans:
            plan = plans
            paid = conn.execute(
                "SELECT SUM(actual_extra) as total_extras, COUNT(*) as paid_count "
                "FROM payment_status WHERE plan_id = ? AND is_paid = 1",
                (plan["id"],),
            ).fetchone()
            schedule = generate_schedule(
                plan["loan_amount"],
                plan["annual_rate"],
                plan["term_months"],
                datetime.strptime(plan["start_date"], "%Y-%m-%d"),
            )
            orig_payoff, orig_n = find_original_payoff(schedule)
            paid_map = {}
            for row in conn.execute(
                "SELECT payment_number, is_paid, actual_extra FROM payment_status WHERE plan_id = ?",
                (plan["id"],),
            ):
                paid_map[row["payment_number"]] = {
                    "is_paid": row["is_paid"],
                    "actual_extra": row["actual_extra"],
                }

            checkpoints = []
            for row in conn.execute(
                "SELECT checkpoint_date, bank_balance FROM balance_checkpoints WHERE plan_id = ? ORDER BY checkpoint_date",
                (plan["id"],),
            ):
                checkpoints.append(dict(row))

            actual_balance, last_paid, tr_adj = recalc_remaining(
                schedule, paid_map, plan["loan_amount"], checkpoints
            )
            monthly_rate = plan["annual_rate"] / 100.0 / 12.0
            rem_months = (
                projected_payoff(
                    actual_balance,
                    monthly_rate,
                    schedule[0]["scheduled_payment"] if schedule else 0,
                )
                if actual_balance > 0
                else 0
            )

            st.metric("Original Loan", fmt_brl(plan["loan_amount"]))
            st.metric(
                "Remaining Balance",
                fmt_brl(actual_balance),
            )
            if tr_adj != 0:
                st.caption(f"TR / Index adjustment: {fmt_brl(tr_adj)}")
            if actual_balance > 0 and rem_months > 0:
                proj_date = datetime.now() + relativedelta(months=rem_months)
                st.metric("Est. Payoff", proj_date.strftime("%b %Y"))
        else:
            st.info("No mortgage plan yet. Add one in Mortgage Tracker.")
        conn.close()

    # ── Investment summary ───────────────────────────────────────────────
    with col2:
        st.subheader("📈 Investments")
        conn = get_db()
        investments = conn.execute(
            "SELECT entry_date, contribution, total_balance FROM investment_log ORDER BY entry_date"
        ).fetchall()
        if investments:
            total_contrib = sum(r["contribution"] for r in investments)
            latest_balance = investments[-1]["total_balance"]
            growth = latest_balance - total_contrib
            st.metric("Total Contributed", fmt_brl(total_contrib))
            st.metric("Current Balance", fmt_brl(latest_balance))
            st.metric(
                "Growth",
                fmt_brl(growth),
                delta=f"{(growth / total_contrib * 100):.1f}%" if total_contrib > 0 else "",
            )
        else:
            st.info("No investment logs yet. Add them in Investment Tracker.")
        conn.close()

    # ── BCB snapshot ─────────────────────────────────────────────────────
    with col3:
        st.subheader("🇧🇷 Brazil Rates")
        selic_df = get_selic()
        ipca_df = get_ipca()
        cdi_df = get_cdi()

        if not selic_df.empty:
            latest_selic = selic_df["annualized"].iloc[-1]
            st.metric("SELIC (annual)", f"{latest_selic:.2f}%")
        if not ipca_df.empty:
            latest_ipca = ipca_df["valor"].iloc[-1]
            st.metric("IPCA (monthly)", f"{latest_ipca:.2f}%")
        if not cdi_df.empty:
            latest_cdi = cdi_df["annualized"].iloc[-1]
            st.metric("CDI (annual)", f"{latest_cdi:.2f}%")

        if selic_df.empty and ipca_df.empty:
            st.warning("Could not fetch BCB data.")

    # ── FI progress ─────────────────────────────────────────────────────
    st.markdown("---")
    fi_conn = get_db()
    fi_row = fi_conn.execute(
        "SELECT monthly_expenses, withdrawal_rate FROM fi_settings WHERE id = 1"
    ).fetchone()
    if fi_row and fi_row["monthly_expenses"] > 0:
        inv_bal = fi_conn.execute(
            "SELECT total_balance FROM investment_log ORDER BY entry_date DESC LIMIT 1"
        ).fetchone()
        portfolio = inv_bal["total_balance"] if inv_bal else 0
        fi_number = fi_row["monthly_expenses"] * 12 * (100 / fi_row["withdrawal_rate"])
        fi_pct = (portfolio / fi_number * 100) if fi_number > 0 else 0
        monthly_fi_income = portfolio * (fi_row["withdrawal_rate"] / 100) / 12

        c1, c2, c3, c4 = st.columns(4)
        with c1:
            st.metric("🔥 FI Number", fmt_brl(fi_number))
        with c2:
            st.metric("Progress", f"{fi_pct:.1f}%")
        with c3:
            st.metric("Monthly FI income now", fmt_brl(monthly_fi_income))
        with c4:
            st.metric(
                "Coverage",
                f"{(monthly_fi_income / fi_row['monthly_expenses'] * 100):.1f}%",
            )
        st.progress(min(fi_pct / 100, 1.0))
    fi_conn.close()

# ══════════════════════════════════════════════════════════════════════════════
# FI TRACKER
# ══════════════════════════════════════════════════════════════════════════════
elif page == "FI Tracker":
    st.header("🔥 Financial Independence Tracker")

    conn = get_db()

    # Load existing settings
    fi_row = conn.execute("SELECT monthly_expenses, withdrawal_rate FROM fi_settings WHERE id = 1").fetchone()
    current_monthly = fi_row["monthly_expenses"] if fi_row else 0
    current_wr = fi_row["withdrawal_rate"] if fi_row else 4.0

    # Get latest investment balance
    latest_inv = conn.execute(
        "SELECT total_balance FROM investment_log ORDER BY entry_date DESC LIMIT 1"
    ).fetchone()
    current_balance = latest_inv["total_balance"] if latest_inv else 0

    # ── Settings form ──────────────────────────────────────────────────
    with st.form("fi_settings_form"):
        c1, c2 = st.columns(2)
        with c1:
            expenses_raw = st.text_input(
                "Monthly expenses (R$)",
                value=fmt_brl(current_monthly).replace("R$ ", "") if current_monthly else "",
                placeholder="e.g. 10.000,00",
            )
        with c2:
            withdrawal_rate = st.number_input(
                "Safe Withdrawal Rate (%)",
                min_value=1.0,
                max_value=10.0,
                value=float(current_wr),
                step=0.1,
                help="4% is the traditional rule from the Trinity Study.",
            )
        if st.form_submit_button("Save Goal", use_container_width=True):
            expenses = parse_brl(expenses_raw) if expenses_raw else 0
            conn.execute(
                "INSERT OR REPLACE INTO fi_settings (id, monthly_expenses, withdrawal_rate, updated_at) "
                "VALUES (1, ?, ?, datetime('now'))",
                (expenses, withdrawal_rate),
            )
            conn.commit()
            current_monthly = expenses
            current_wr = withdrawal_rate
            st.rerun()

    if current_monthly <= 0:
        st.info("Set your monthly expenses above to see your FI number.")
    else:
        fi_number = current_monthly * 12 * (100 / current_wr)
        progress_pct = (current_balance / fi_number * 100) if fi_number > 0 else 0
        monthly_income_now = current_balance * (current_wr / 100) / 12
        coverage_pct = (monthly_income_now / current_monthly * 100) if current_monthly > 0 else 0

        st.markdown("---")

        # ── Big FI number ──────────────────────────────────────────────
        c1, c2, c3 = st.columns(3)
        with c1:
            st.metric("Your FI Number", fmt_brl(fi_number))
        with c2:
            st.metric("Current Portfolio", fmt_brl(current_balance))
        with c3:
            st.metric(
                "Progress to FI",
                f"{progress_pct:.1f}%",
                delta=f"{fmt_brl(fi_number - current_balance)} to go",
            )

        # ── Progress bar ───────────────────────────────────────────────
        st.progress(
            min(progress_pct / 100, 1.0),
            text=f"You own {progress_pct:.1f}% of your freedom",
        )

        # ── What you can already withdraw ──────────────────────────────
        c1, c2 = st.columns(2)
        with c1:
            st.metric(
                "Monthly income your portfolio can sustain",
                fmt_brl(monthly_income_now),
                delta=f"Covers {coverage_pct:.1f}% of your expenses",
            )
        with c2:
            annual_withdrawal = current_balance * (current_wr / 100)
            st.metric(
                "Annual withdrawal at {:.1f}%".format(current_wr),
                fmt_brl(annual_withdrawal),
            )

        # ── Projection: when will you reach FI? ────────────────────────
        st.markdown("---")
        st.subheader("Projection: When Will You Reach FI?")

        # Get monthly contribution from investment log
        entries = conn.execute(
            "SELECT contribution, entry_date FROM investment_log ORDER BY entry_date"
        ).fetchall()
        monthly_contrib = 0
        if len(entries) >= 2:
            total_contrib = sum(e["contribution"] for e in entries)
            first_d = datetime.strptime(entries[0]["entry_date"], "%Y-%m-%d")
            last_d = datetime.strptime(entries[-1]["entry_date"], "%Y-%m-%d")
            months = max((last_d - first_d).days / 30.44, 1)
            monthly_contrib = total_contrib / months

        # Use CDI rate as default expected return
        cdi_df = get_cdi()
        expected_return = 12.0  # fallback
        if not cdi_df.empty:
            expected_return = cdi_df["annualized"].iloc[-1]

        col1, col2 = st.columns(2)
        with col1:
            monthly_savings_raw = st.text_input(
                "Monthly savings (R$)",
                value=fmt_brl(monthly_contrib).replace("R$ ", "") if monthly_contrib else "",
                placeholder="e.g. 6.500,00",
                key="fi_monthly_savings",
            )
        with col2:
            expected_rate = st.number_input(
                "Expected annual return (%)",
                min_value=0.0,
                max_value=50.0,
                value=round(expected_return, 1),
                step=0.1,
                help=f"Current CDI: {expected_return:.1f}%",
            )

        monthly_savings = parse_brl(monthly_savings_raw) if monthly_savings_raw else monthly_contrib
        monthly_rate = expected_rate / 100 / 12

        if monthly_savings > 0 and current_balance < fi_number:
            try:
                months_to_fi = npf.nper(monthly_rate, -monthly_savings, -current_balance, fi_number)
                months_to_fi = max(0, int(np.ceil(months_to_fi)))
                fi_date = datetime.now() + relativedelta(months=months_to_fi)
                years_to_fi = months_to_fi / 12

                st.metric(
                    "Estimated FI Date",
                    fi_date.strftime("%B %Y"),
                    delta=f"{years_to_fi:.1f} years ({months_to_fi} months)" if months_to_fi > 0 else "",
                )

                # Year-by-year projection table
                proj_data = []
                bal = current_balance
                for yr in range(1, min(int(years_to_fi) + 3, 31)):
                    for m in range(12):
                        bal = bal * (1 + monthly_rate) + monthly_savings
                        if bal >= fi_number:
                            break
                    proj_data.append(
                        {
                            "Year": yr,
                            "Projected Balance": fmt_brl(bal),
                            "% to FI": f"{(bal / fi_number * 100):.1f}%",
                        }
                    )
                    if bal >= fi_number:
                        break

                if proj_data:
                    st.dataframe(
                        pd.DataFrame(proj_data),
                        use_container_width=True,
                        hide_index=True,
                    )
            except Exception:
                st.caption("Could not compute projection.")

        else:
            if current_balance >= fi_number:
                st.success("🎉 Congratulations! You've already reached your FI number!")
            else:
                st.info("Enter your monthly savings to see a projection.")

    conn.close()

# ══════════════════════════════════════════════════════════════════════════════
# MORTGAGE TRACKER
# ══════════════════════════════════════════════════════════════════════════════
elif page == "Mortgage Tracker":
    st.header("🏠 Mortgage Tracker")

    conn = get_db()
    existing_plans = conn.execute(
        "SELECT id, name, loan_amount, annual_rate, term_months, start_date FROM mortgage_plans ORDER BY created_at"
    ).fetchall()
    existing_ids = [p["id"] for p in existing_plans]

    tab1, tab2 = st.tabs(["📋 My Plan", "➕ New Plan"])

    # ── Tab 1: View / track payments ──────────────────────────────────────
    with tab1:
        if not existing_plans:
            st.info("No mortgage plan yet. Create one in the 'New Plan' tab.")
        else:
            plan_names = {f"{p['name']} ({p['id']})": p for p in existing_plans}
            selected_name = st.selectbox(
                "Select plan", list(plan_names.keys()), key="plan_select"
            )
            plan = plan_names[selected_name]
            plan_id = plan["id"]

            # Load extras
            planned_extras = conn.execute(
                "SELECT extra_date, amount, description FROM planned_extras WHERE plan_id = ?",
                (plan_id,),
            ).fetchall()
            extras_list = [(pe["extra_date"], pe["amount"]) for pe in planned_extras]

            start_dt = datetime.strptime(plan["start_date"], "%Y-%m-%d")
            schedule = generate_schedule(
                plan["loan_amount"],
                plan["annual_rate"],
                plan["term_months"],
                start_dt,
                extras_list,
            )

            orig_payoff_date, orig_payoff_n = find_original_payoff(schedule)

            # Load payment statuses
            paid_map = {}
            for row in conn.execute(
                "SELECT payment_number, is_paid, paid_date, actual_extra FROM payment_status WHERE plan_id = ?",
                (plan_id,),
            ):
                paid_map[row["payment_number"]] = {
                    "is_paid": row["is_paid"],
                    "paid_date": row["paid_date"],
                    "actual_extra": row["actual_extra"],
                }

            # Load checkpoints
            checkpoints = []
            for row in conn.execute(
                "SELECT checkpoint_date, bank_balance FROM balance_checkpoints WHERE plan_id = ? ORDER BY checkpoint_date",
                (plan_id,),
            ):
                checkpoints.append(dict(row))

            actual_balance, last_paid_n, tr_adj = recalc_remaining(
                schedule, paid_map, plan["loan_amount"], checkpoints
            )
            monthly_rate = plan["annual_rate"] / 100.0 / 12.0
            sched_pmt = schedule[0]["scheduled_payment"] if schedule else 0

            # Projected payoff with future planned extras
            # For simplicity, use nper on actual_balance
            raw_nper = npf.nper(monthly_rate, sched_pmt, -actual_balance) if actual_balance > 0 else 0
            proj_remain_months = max(0, int(np.ceil(raw_nper)))
            proj_payoff_date = (
                datetime.now() + relativedelta(months=proj_remain_months)
                if actual_balance > 0
                else datetime.now()
            )

            # ── Summary cards ────────────────────────────────────────────
            c1, c2, c3, c4 = st.columns(4)
            paid_count = sum(1 for v in paid_map.values() if v["is_paid"])

            orig_term = plan["term_months"]
            actual_remaining_months = proj_remain_months if actual_balance > 0 else 0
            actual_total_term = paid_count + actual_remaining_months
            saved_months = orig_term - actual_total_term

            with c1:
                st.metric(
                    "Original Term",
                    f"{orig_term} months",
                    delta=f"Payoff {orig_payoff_date.strftime('%b %Y')}",
                )
            with c2:
                if saved_months > 0:
                    st.metric(
                        "Actual Term (with extras)",
                        f"{actual_total_term} months",
                        delta=f"Saved {saved_months} months!",
                    )
                elif actual_balance <= 0:
                    st.metric("Actual Term", "PAID OFF!", delta="Congratulations!")
                else:
                    st.metric(
                        "Actual Term",
                        f"{actual_total_term} months",
                        delta=f"{actual_remaining_months} months left",
                    )
            with c3:
                st.metric("Remaining Balance", fmt_brl(actual_balance))
                if tr_adj != 0:
                    st.caption(f"TR: {fmt_brl(tr_adj)}")
            with c4:
                st.metric(
                    "Progress",
                    f"{paid_count} / {actual_total_term} paid",
                    delta=f"{paid_count / max(actual_total_term, 1) * 100:.0f}%",
                )

            # ── Progress bar ─────────────────────────────────────────────
            progress = paid_count / max(actual_total_term, 1) if actual_total_term > 0 else 1
            if saved_months > 0:
                bar_text = f"Paid {paid_count} of ~{actual_total_term} payments ({saved_months} months saved with extras)"
            elif actual_balance <= 0:
                bar_text = "PAID OFF! 🎉"
            else:
                bar_text = f"Paid {paid_count} of ~{actual_total_term} payments"
            st.progress(min(progress, 1.0), text=bar_text)

            # ── Schedule table ───────────────────────────────────────────
            st.subheader("Payment Schedule")

            # Generate remaining schedule from actual balance
            remaining_schedule = []
            bal = actual_balance
            current_dt = datetime.now().replace(day=1) + relativedelta(months=1)
            last_payment_num = paid_count
            for i in range(1, actual_remaining_months + 1):
                interest = bal * monthly_rate
                principal = sched_pmt - interest
                if principal > bal:
                    principal = bal
                bal -= principal
                pn = last_payment_num + i
                remaining_schedule.append({
                    "payment_number": pn,
                    "due_date": current_dt,
                    "scheduled_payment": round(sched_pmt, 2),
                    "scheduled_principal": round(principal, 2),
                    "scheduled_interest": round(interest, 2),
                    "scheduled_balance": round(max(bal, 0), 2),
                })
                current_dt += relativedelta(months=1)
                if bal <= 0:
                    break

            # Paid payments history
            paid_rows = [
                row for row in schedule
                if paid_map.get(row["payment_number"], {}).get("is_paid", 0)
            ]
            if paid_rows:
                with st.expander(f"View {len(paid_rows)} paid payments"):
                    for row in paid_rows:
                        pn = row["payment_number"]
                        pi = paid_map[pn]
                        ex = pi.get("actual_extra", 0)
                        st.text(
                            f"#{pn}  {row['due_date'].strftime('%b %Y')}  |  "
                            f"Payment: {fmt_brl(row['scheduled_payment'])}  |  "
                            f"Principal: {fmt_brl(row['scheduled_principal'])}  |  "
                            f"Interest: {fmt_brl(row['scheduled_interest'])}  |  "
                            f"Balance after: {fmt_brl(row['scheduled_balance'])}"
                            + (f"  |  Extra: {fmt_brl(ex)}" if ex else "")
                        )

            # Remaining schedule with headers
            if remaining_schedule:
                st.caption(
                    f"Showing {len(remaining_schedule)} remaining payments "
                    f"(term reduced from {orig_term} months)"
                )
                with st.form("payment_form"):
                    # Header row
                    hc1, hc2, hc3, hc4, hc5, hc6, hc7, hc8 = st.columns(
                        [0.5, 1.2, 1.2, 1.2, 1.2, 1.2, 0.8, 1.2]
                    )
                    with hc1:
                        st.caption("**#**")
                    with hc2:
                        st.caption("**Due**")
                    with hc3:
                        st.caption("**Payment**")
                    with hc4:
                        st.caption("**Principal**")
                    with hc5:
                        st.caption("**Interest**")
                    with hc6:
                        st.caption("**Balance**")
                    with hc7:
                        st.caption("**Paid?**")
                    with hc8:
                        st.caption("**Extra**")

                    rows_data = []
                    for row in remaining_schedule:
                        pn = row["payment_number"]
                        paid_info = paid_map.get(pn, {"is_paid": 0, "paid_date": "", "actual_extra": 0})

                        ca, cb, cc, cd, ce, cf, cg, ch = st.columns(
                            [0.5, 1.2, 1.2, 1.2, 1.2, 1.2, 0.8, 1.2]
                        )
                        with ca:
                            st.caption(f"#{pn}")
                        with cb:
                            st.caption(row["due_date"].strftime("%b %Y"))
                        with cc:
                            st.caption(fmt_brl(row["scheduled_payment"]))
                        with cd:
                            st.caption(fmt_brl(row["scheduled_principal"]))
                        with ce:
                            st.caption(fmt_brl(row["scheduled_interest"]))
                        with cf:
                            st.caption(fmt_brl(row["scheduled_balance"]))
                        with cg:
                            is_checked = st.checkbox(
                                "Paid",
                                value=bool(paid_info["is_paid"]),
                                key=f"paid_{plan_id}_{pn}",
                            )
                        with ch:
                            extra_val = st.text_input(
                                "Extra",
                                value=str(paid_info["actual_extra"]) if paid_info["actual_extra"] else "",
                                key=f"extra_{plan_id}_{pn}",
                                placeholder="0",
                            )
                        rows_data.append((pn, is_checked, extra_val))

                    if st.form_submit_button("💾 Save Payment Updates", use_container_width=True):
                        for pn, is_checked, extra_val in rows_data:
                            try:
                                ex = float(extra_val) if extra_val else 0.0
                            except ValueError:
                                ex = 0.0
                            conn.execute(
                                "INSERT OR REPLACE INTO payment_status (plan_id, payment_number, is_paid, paid_date, actual_extra) "
                                "VALUES (?, ?, ?, ?, ?)",
                                (
                                    plan_id,
                                    pn,
                                    1 if is_checked else 0,
                                    datetime.now().strftime("%Y-%m-%d") if is_checked else None,
                                    ex,
                                ),
                            )
                        conn.commit()
                        st.success("Saved!")
                        st.rerun()
            elif actual_balance <= 0:
                st.success("All payments complete! 🎉")
            else:
                st.info("No remaining payments to show.")

            # ── Balance over time chart ─────────────────────────────────
            st.subheader("Balance Over Time")
            chart_dates = [r["due_date"] for r in schedule]
            chart_scheduled = [r["scheduled_balance"] for r in schedule]

            # Calculate actual balance trajectory based on paid status + checkpoints
            running_balance = plan["loan_amount"]
            chart_actual = []
            cp_dates = {c["checkpoint_date"]: c["bank_balance"] for c in checkpoints}
            for row in schedule:
                pn = row["payment_number"]
                if row["date_key"] in cp_dates:
                    running_balance = cp_dates[row["date_key"]]
                pi = paid_map.get(pn, {"is_paid": 0, "actual_extra": 0})
                if pi["is_paid"]:
                    running_balance -= row["scheduled_principal"] + pi["actual_extra"]
                chart_actual.append(max(running_balance, 0))

            fig = go.Figure()
            fig.add_trace(
                go.Scatter(
                    x=chart_dates,
                    y=chart_scheduled,
                    name="Original Plan",
                    line=dict(color="gray", dash="dash"),
                )
            )
            fig.add_trace(
                go.Scatter(
                    x=chart_dates,
                    y=chart_actual,
                    name="Actual",
                    line=dict(color="green", width=3),
                    fill="tozeroy",
                    fillcolor="rgba(0,128,0,0.1)",
                )
            )
            fig.update_layout(
                height=350,
                margin=dict(l=20, r=20, t=20, b=20),
                legend=dict(orientation="h", yanchor="bottom", y=1.02),
                hovermode="x unified",
            )
            fig.update_yaxes(title="Balance (R$)", tickprefix="R$")
            st.plotly_chart(fig, use_container_width=True)

            # ── Balance checkpoint (TR / index adjustment) ────────────────
            st.subheader("Update Balance from Bank")
            st.caption(
                "If your bank adjusts your balance (e.g. TR / Taxa Referencial), "
                "enter the new balance your bank shows so projections stay accurate."
            )
            with st.form("checkpoint_form"):
                c1, c2, c3 = st.columns([1, 1, 1])
                with c1:
                    cp_date = st.date_input("As of date", value=date.today(), key="cp_date")
                with c2:
                    cp_balance_raw = st.text_input(
                        "Bank balance (R$)", placeholder="e.g. 44.500,00", key="cp_balance"
                    )
                with c3:
                    cp_notes = st.text_input("Notes (optional)", placeholder="e.g. TR adjustment", key="cp_notes")
                if st.form_submit_button("💾 Save Balance Checkpoint", use_container_width=True):
                    cp_balance = parse_brl(cp_balance_raw) if cp_balance_raw else 0
                    if cp_balance > 0:
                        conn.execute(
                            "INSERT INTO balance_checkpoints (plan_id, checkpoint_date, bank_balance, notes) "
                            "VALUES (?, ?, ?, ?)",
                            (plan_id, cp_date.isoformat(), cp_balance, cp_notes),
                        )
                        conn.commit()
                        st.success("Balance updated!")
                        st.rerun()
                    else:
                        st.error("Please enter a valid balance.")

            # Show checkpoint history
            cp_history = conn.execute(
                "SELECT checkpoint_date, bank_balance, notes FROM balance_checkpoints WHERE plan_id = ? ORDER BY checkpoint_date DESC",
                (plan_id,),
            ).fetchall()
            if cp_history:
                with st.expander("View Balance History"):
                    for cp in cp_history:
                        st.text(
                            f"{cp['checkpoint_date']} — {fmt_brl(cp['bank_balance'])}"
                            + (f" ({cp['notes']})" if cp["notes"] else "")
                        )

            # ── Delete plan ──────────────────────────────────────────────
            with st.expander("Delete Plan"):
                if st.button("🗑️ Delete This Plan", type="secondary"):
                    conn.execute("DELETE FROM balance_checkpoints WHERE plan_id = ?", (plan_id,))
                    conn.execute("DELETE FROM payment_status WHERE plan_id = ?", (plan_id,))
                    conn.execute("DELETE FROM planned_extras WHERE plan_id = ?", (plan_id,))
                    conn.execute("DELETE FROM mortgage_plans WHERE id = ?", (plan_id,))
                    conn.commit()
                    st.success("Deleted.")
                    st.rerun()

    # ── Tab 2: Create new plan ────────────────────────────────────────────
    with tab2:
        st.subheader("Create New Mortgage Plan")
        with st.form("new_plan_form"):
            c1, c2 = st.columns(2)
            with c1:
                name = st.text_input("Plan Name", value="My Mortgage")
                loan_amount_raw = st.text_input(
                    "Loan Amount", value="45.290,45", placeholder="e.g. 45.290,45"
                )
                annual_rate = st.number_input(
                    "Annual Interest Rate (%)",
                    min_value=0.0,
                    max_value=50.0,
                    value=7.66,
                    step=0.01,
                )
            with c2:
                term_months = st.number_input(
                    "Term (months)", min_value=1, max_value=600, value=84
                )
                start_date = st.date_input("Start Date", value=date(2026, 8, 1))

            loan_amount = parse_brl(loan_amount_raw) if loan_amount_raw else 0

            st.markdown("---")
            st.caption("Planned Extra Payments (optional)")
            c1, c2 = st.columns(2)
            with c1:
                extra_monthly = st.text_input(
                    "Extra Monthly Payment", value="5.200,00", placeholder="e.g. 5.200,00"
                )
                extra_monthly_start = st.date_input(
                    "Extra Monthly Starts", value=date(2026, 8, 1)
                )
            with c2:
                extra_onetime = st.text_input(
                    "One-Time Extra Payment", value="17.000,00", placeholder="e.g. 17.000,00"
                )
                extra_onetime_date = st.date_input(
                    "One-Time Extra Date", value=date(2026, 12, 1)
                )

            submitted = st.form_submit_button("Create Plan", use_container_width=True)

        if submitted:
            if loan_amount <= 0:
                st.error("Please enter a valid loan amount.")
            else:
                conn.execute(
                    "INSERT INTO mortgage_plans (name, loan_amount, annual_rate, term_months, start_date) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (name, loan_amount, annual_rate, term_months, start_date.isoformat()),
                )
                plan_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

                # Save planned extras
                em = parse_brl(extra_monthly) if extra_monthly else 0
                eo = parse_brl(extra_onetime) if extra_onetime else 0

                if em > 0:
                    current = extra_monthly_start
                    end_date = start_date + relativedelta(months=term_months - 1)
                    while current <= end_date:
                        conn.execute(
                            "INSERT INTO planned_extras (plan_id, extra_date, amount, description) VALUES (?, ?, ?, ?)",
                            (plan_id, current.isoformat(), em, "Monthly extra"),
                        )
                        current += relativedelta(months=1)

                if eo > 0:
                    conn.execute(
                        "INSERT INTO planned_extras (plan_id, extra_date, amount, description) VALUES (?, ?, ?, ?)",
                        (
                            plan_id,
                            extra_onetime_date.isoformat(),
                            eo,
                            "One-time extra",
                        ),
                    )

                conn.commit()
                st.success(f"Plan '{name}' created!")
                st.rerun()

    conn.close()

# ══════════════════════════════════════════════════════════════════════════════
# INVESTMENT TRACKER
# ══════════════════════════════════════════════════════════════════════════════
elif page == "Investment Tracker":
    st.header("📈 Investment Tracker")

    conn = get_db()

    col_form, col_chart = st.columns([1, 2])

    with col_form:
        st.subheader("Log Entry")
        with st.form("investment_form"):
            entry_date = st.date_input("Date", value=date.today())
            contribution_raw = st.text_input("Amount Contributed", placeholder="e.g. 6.500,00")
            balance_raw = st.text_input(
                "Total Balance (what your bank shows)", placeholder="e.g. 25.000,00"
            )
            notes = st.text_input("Notes (optional)")
            submitted = st.form_submit_button("Add Entry", use_container_width=True)

        if submitted:
            contribution = parse_brl(contribution_raw) if contribution_raw else 0
            balance = parse_brl(balance_raw) if balance_raw else 0
            if balance <= 0:
                st.error("Please enter a valid total balance.")
            else:
                conn.execute(
                    "INSERT INTO investment_log (entry_date, contribution, total_balance, notes) "
                    "VALUES (?, ?, ?, ?)",
                    (entry_date.isoformat(), contribution, balance, notes),
                )
                conn.commit()
                st.success("Entry added!")
                st.rerun()

    with col_chart:
        entries = conn.execute(
            "SELECT id, entry_date, contribution, total_balance, notes "
            "FROM investment_log ORDER BY entry_date"
        ).fetchall()

        if entries:
            df = pd.DataFrame(
                [
                    {
                        "Date": e["entry_date"],
                        "Contributed": e["contribution"],
                        "Total Balance": e["total_balance"],
                        "Growth": e["total_balance"]
                        - sum(
                            r["contribution"]
                            for r in entries
                            if r["entry_date"] <= e["entry_date"]
                        ),
                        "Cumulative Contributions": sum(
                            r["contribution"]
                            for r in entries
                            if r["entry_date"] <= e["entry_date"]
                        ),
                        "Notes": e["notes"] or "",
                    }
                    for e in entries
                ]
            )

            # Chart
            fig = go.Figure()
            fig.add_trace(
                go.Scatter(
                    x=df["Date"],
                    y=df["Total Balance"],
                    name="Your Balance",
                    line=dict(color="green", width=3),
                    mode="lines+markers",
                )
            )
            fig.add_trace(
                go.Scatter(
                    x=df["Date"],
                    y=df["Cumulative Contributions"],
                    name="Total Contributed",
                    line=dict(color="blue", dash="dash"),
                    mode="lines",
                )
            )
            fig.add_trace(
                go.Scatter(
                    x=df["Date"],
                    y=df["Growth"],
                    name="Growth (Interest)",
                    line=dict(color="gold"),
                    fill="tozeroy",
                    fillcolor="rgba(255,215,0,0.15)",
                    mode="lines",
                )
            )

            # ── Inflation-adjusted balance ──────────────────────────────
            ipca_df = get_ipca()
            inflation_factor = 1.0
            real_balances = []
            if not ipca_df.empty:
                ipca_df = ipca_df.copy()
                ipca_df["data"] = pd.to_datetime(ipca_df["data"])
                ipca_df = ipca_df.sort_values("data")

                first_entry_dt = pd.to_datetime(df["Date"].iloc[0])
                base_month = first_entry_dt.replace(day=1)

                for _, row in df.iterrows():
                    entry_dt = pd.to_datetime(row["Date"])
                    entry_month = entry_dt.replace(day=1)

                    ipca_between = ipca_df[
                        (ipca_df["data"] > base_month)
                        & (ipca_df["data"] <= entry_month)
                    ]
                    cum_inflation = 1.0
                    for v in ipca_between["valor"]:
                        cum_inflation *= 1 + v / 100
                    real_balances.append(row["Total Balance"] / cum_inflation)

                df["Real Balance"] = real_balances

                fig.add_trace(
                    go.Scatter(
                        x=df["Date"],
                        y=df["Real Balance"],
                        name="Inflation-Adjusted Balance",
                        line=dict(color="red", dash="dash", width=2),
                        mode="lines+markers",
                    )
                )

                fig.add_trace(
                    go.Scatter(
                        x=df["Date"],
                        y=[df["Cumulative Contributions"].iloc[0]] * len(df),
                        name="Initial Contribution (real)",
                        line=dict(color="gray", dash="dot", width=1),
                        mode="lines",
                    )
                )

            fig.update_layout(
                height=400,
                margin=dict(l=20, r=20, t=20, b=20),
                legend=dict(orientation="h", yanchor="bottom", y=1.02),
                hovermode="x unified",
            )
            fig.update_yaxes(title="R$", tickprefix="R$")
            st.plotly_chart(fig, use_container_width=True)

            # ── Stats ───────────────────────────────────────────────────
            st.subheader("Summary")
            total_contrib = df["Cumulative Contributions"].iloc[-1]
            latest_balance = df["Total Balance"].iloc[-1]
            total_growth = latest_balance - total_contrib
            return_pct = (total_growth / total_contrib * 100) if total_contrib > 0 else 0

            first_date = pd.to_datetime(df["Date"].iloc[0])
            last_date = pd.to_datetime(df["Date"].iloc[-1])
            years = max((last_date - first_date).days / 365.25, 0.01)

            # Inflation metrics
            real_balance = df["Real Balance"].iloc[-1] if "Real Balance" in df.columns else latest_balance
            real_growth = real_balance - df["Cumulative Contributions"].iloc[0]
            real_return_pct = (
                (real_growth / df["Cumulative Contributions"].iloc[0] * 100)
                if df["Cumulative Contributions"].iloc[0] > 0
                else 0
            )

            latest_ipca = 0
            ipca_12m = 0
            if not ipca_df.empty:
                latest_ipca = ipca_df["valor"].iloc[-1]
                ipca_last_12 = ipca_df.tail(12)
                if len(ipca_last_12) > 0:
                    ipca_12m = ((1 + ipca_last_12["valor"] / 100).prod() - 1) * 100

            c1, c2, c3, c4, c5 = st.columns(5)
            with c1:
                st.metric("Total Contributed", fmt_brl(total_contrib))
            with c2:
                st.metric("Current Balance", fmt_brl(latest_balance))
            with c3:
                st.metric(
                    "Total Growth",
                    fmt_brl(total_growth),
                    delta=f"{return_pct:.1f}% nominal",
                )
            with c4:
                st.metric(
                    f"Real Return (vs IPCA)",
                    fmt_brl(real_growth),
                    delta=f"{real_return_pct:.1f}% real",
                )
            with c5:
                st.metric(
                    "IPCA (month / 12m)",
                    f"{latest_ipca:.2f}%",
                    delta=f"{ipca_12m:.1f}% 12m" if ipca_12m else "",
                )

            # ── Data table ──────────────────────────────────────────────
            st.subheader("History")
            display_df = df[["Date", "Contributed", "Total Balance", "Growth", "Notes"]].copy()
            display_df["Contributed"] = display_df["Contributed"].apply(fmt_brl)
            display_df["Total Balance"] = display_df["Total Balance"].apply(fmt_brl)
            display_df["Growth"] = display_df["Growth"].apply(fmt_brl)
            st.dataframe(display_df, use_container_width=True, hide_index=True)

            # Delete entries
            with st.expander("Delete Entries"):
                to_delete = st.multiselect(
                    "Select entries to delete",
                    [f"{e['id']}: {e['entry_date']}" for e in entries],
                )
                if to_delete and st.button("🗑️ Delete Selected"):
                    for item in to_delete:
                        entry_id = int(item.split(":")[0])
                        conn.execute("DELETE FROM investment_log WHERE id = ?", (entry_id,))
                    conn.commit()
                    st.rerun()
        else:
            st.info("No entries yet. Log your first contribution!")

    conn.close()

# ══════════════════════════════════════════════════════════════════════════════
# BCB INDICATORS
# ══════════════════════════════════════════════════════════════════════════════
elif page == "BCB Indicators":
    st.header("🇧🇷 Banco Central do Brasil - Economic Indicators")

    with st.spinner("Fetching data from BCB API..."):
        selic_df = get_selic()
        ipca_df = get_ipca()
        cdi_df = get_cdi()

    # ── Current rates ─────────────────────────────────────────────────────
    st.subheader("Current Rates")
    c1, c2, c3 = st.columns(3)

    with c1:
        if not selic_df.empty:
            latest = selic_df.iloc[-1]
            prev = selic_df.iloc[-22] if len(selic_df) >= 22 else selic_df.iloc[0]
            st.metric(
                "SELIC (annualized)",
                f"{latest['annualized']:.2f}%",
                delta=f"{(latest['annualized'] - prev['annualized']):.2f}% vs ~1 month ago",
            )
            st.caption(f"Source: BCB SGS série 4189 · Updated: {latest['data'].strftime('%d/%m/%Y')}")
        else:
            st.warning("SELIC data unavailable")

    with c2:
        if not ipca_df.empty:
            latest = ipca_df.iloc[-1]
            prev = ipca_df.iloc[-2] if len(ipca_df) >= 2 else ipca_df.iloc[0]
            st.metric(
                "IPCA (monthly %)",
                f"{latest['valor']:.2f}%",
                delta=f"{(latest['valor'] - prev['valor']):.2f}% vs previous month",
            )
            st.caption(f"Source: BCB SGS série 433 · Updated: {latest['data'].strftime('%d/%m/%Y')}")
        else:
            st.warning("IPCA data unavailable")

    with c3:
        if not cdi_df.empty:
            latest = cdi_df.iloc[-1]
            prev = cdi_df.iloc[-22] if len(cdi_df) >= 22 else cdi_df.iloc[0]
            st.metric(
                "CDI (annualized)",
                f"{latest['annualized']:.2f}%",
                delta=f"{(latest['annualized'] - prev['annualized']):.2f}% vs ~1 month ago",
            )
            st.caption(f"Source: BCB SGS série 12 · Updated: {latest['data'].strftime('%d/%m/%Y')}")
        else:
            st.warning("CDI data unavailable")

    # ── Charts ──────────────────────────────────────────────────────────────
    st.subheader("Trends")

    chart_sel = st.selectbox("Select indicator", ["SELIC (annualized)", "IPCA (monthly)", "CDI (annualized)"])

    if chart_sel == "SELIC (annualized)" and not selic_df.empty:
        fig = px.line(
            selic_df.tail(252),
            x="data",
            y="annualized",
            title="SELIC (Annualized %) - Last ~1 Year",
        )
        fig.update_layout(height=350, margin=dict(l=20, r=20, t=40, b=20))
        fig.update_yaxes(title="%")
        st.plotly_chart(fig, use_container_width=True)

    elif chart_sel == "IPCA (monthly)" and not ipca_df.empty:
        fig = px.bar(
            ipca_df.tail(24),
            x="data",
            y="valor",
            title="IPCA (Monthly %) - Last 24 Months",
        )
        fig.update_layout(height=350, margin=dict(l=20, r=20, t=40, b=20))
        fig.update_yaxes(title="%")
        st.plotly_chart(fig, use_container_width=True)

    elif chart_sel == "CDI (annualized)" and not cdi_df.empty:
        fig = px.line(
            cdi_df.tail(252),
            x="data",
            y="annualized",
            title="CDI (Annualized %) - Last ~1 Year",
        )
        fig.update_layout(height=350, margin=dict(l=20, r=20, t=40, b=20))
        fig.update_yaxes(title="%")
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No data available for selected indicator.")

    st.caption("Data source: Banco Central do Brasil SGS API · Updates cached for 24 hours")
