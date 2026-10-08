"""Streamlit dashboard: key metrics, a chart and a chat interface to the data agent.

Run with:
    streamlit run app.py
"""

from __future__ import annotations

import numbers

import altair as alt
import pandas as pd
import streamlit as st

import agent

PROJECT_TITLE = "Course Analytics Agent"
PROJECT_SUBTITLE = "PostgreSQL → Agent → Analysis → Dashboard"

# --------------------------------------------------------------------------- #
# Dashboard queries
#
# Domain KPIs for the online-course database: learners, enrollments, the
# certification funnel and subscription revenue.
#
# Note on dates: `enrollments.enrolled_at` is stored as TEXT in two different
# formats, so it is parsed explicitly instead of being cast. See agent.SCHEMA_NOTES.
# --------------------------------------------------------------------------- #

# Parses both of the formats actually present in enrolled_at.
ENROLLED_DATE = """
    CASE WHEN enrolled_at ~ '^\\d{4}-\\d{2}-\\d{2}$' THEN to_date(enrolled_at, 'YYYY-MM-DD')
         WHEN enrolled_at ~ '^\\d{2}\\.\\d{2}\\.\\d{4}$' THEN to_date(enrolled_at, 'DD.MM.YYYY')
    END
"""

KPI_SQL = """
SELECT (SELECT count(*) FROM users)                                         AS learners,
       (SELECT count(*) FROM enrollments)                                   AS enrollments,
       (SELECT round(100.0 * sum(is_certified) / nullif(count(*), 0), 1)
          FROM enrollments)                                                 AS certification_rate,
       (SELECT round(sum(amount_usd) FILTER (WHERE is_refunded = 0), 0)
          FROM payments)                                                    AS net_revenue,
       (SELECT round(100.0 * count(*) FILTER (WHERE is_refunded = 1)
                           / nullif(count(*), 0), 2) FROM payments)         AS refund_rate
"""

TREND_SQL = f"""
WITH enrolled AS (
    SELECT date_trunc('month', {ENROLLED_DATE})::date AS month,
           count(*)          AS enrollments,
           sum(is_certified) AS certified
    FROM enrollments
    GROUP BY 1
),
revenue AS (
    SELECT date_trunc('month', paid_at)::date AS month,
           sum(amount_usd) FILTER (WHERE is_refunded = 0) AS net_revenue
    FROM payments
    GROUP BY 1
)
SELECT to_char(e.month, 'YYYY-MM')               AS month,
       e.enrollments,
       e.certified,
       round(100.0 * e.certified / nullif(e.enrollments, 0), 2) AS cert_rate_pct,
       round(coalesce(r.net_revenue, 0), 0)      AS net_revenue
FROM enrolled AS e
LEFT JOIN revenue AS r ON r.month = e.month
WHERE e.month IS NOT NULL
ORDER BY e.month
"""

DOMAINS_SQL = """
SELECT d.domain                                                        AS domain,
       count(*)                                                        AS enrollments,
       sum(e.is_certified)                                             AS certified,
       round(100.0 * sum(e.is_certified) / nullif(count(*), 0), 1)     AS cert_rate_pct,
       round(avg(e.progress_pct), 1)                                   AS avg_progress_pct,
       round(avg(d.review_score), 2)                                   AS avg_rating
FROM enrollments AS e
JOIN dim_course AS d ON d.course_id = e.course_id
WHERE d.domain IS NOT NULL AND d.domain <> 'Unknown'
GROUP BY 1
ORDER BY enrollments DESC
LIMIT 12
"""


# --------------------------------------------------------------------------- #


@st.cache_data(ttl=300, show_spinner=False)
def load(sql: str) -> pd.DataFrame:
    """Run a dashboard query, cached for 5 minutes so reruns do not hit the database."""
    return agent.run_query(sql)


def format_number(value: object) -> str:
    """Render a number with thin-space thousands separators.

    Accepts numpy and Decimal values too — the database returns both.
    """
    if isinstance(value, numbers.Number) and not isinstance(value, bool):
        return f"{float(value):,.0f}".replace(",", "\u00a0")
    return str(value)


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #

st.set_page_config(page_title=PROJECT_TITLE, page_icon="📊", layout="wide")
st.title(PROJECT_TITLE)
st.caption(PROJECT_SUBTITLE)

# The dashboard only needs the database; the chat additionally needs Gemini, so a
# missing API key disables the chat instead of blocking the whole page.
missing = agent.missing_config()
if "DATABASE_URL" in missing:
    st.error(
        "Missing `DATABASE_URL`. Copy `.env.example` to `.env` and fill it in, "
        "then restart the app."
    )
    st.stop()
chat_enabled = "GEMINI_API_KEY" not in missing

with st.sidebar:
    st.subheader("Connection")
    try:
        schema = agent.load_schema()
        table_count = len(schema.splitlines())
        st.success(f"Connected · {table_count} tables")
        with st.expander("Schema"):
            st.code(schema, language="text")
    except agent.AgentError as exc:
        st.error(str(exc))
        st.stop()

    st.caption(f"Model: `{agent.model_name()}`")
    if st.button("Refresh data", width="stretch"):
        st.cache_data.clear()
        agent.load_schema.cache_clear()
        st.rerun()

# --- Metrics -------------------------------------------------------------- #

st.subheader("Key metrics")
try:
    kpi = load(KPI_SQL).iloc[0]
    columns = st.columns(5)
    columns[0].metric("Learners", format_number(kpi["learners"]))
    columns[1].metric("Enrollments", format_number(kpi["enrollments"]))
    columns[2].metric("Certification rate", f"{kpi['certification_rate']:.1f}%")
    columns[3].metric("Net revenue", f"${float(kpi['net_revenue']) / 1e6:.2f}M")
    columns[4].metric("Refund rate", f"{kpi['refund_rate']:.2f}%")
except agent.AgentError as exc:
    st.error(str(exc))
    st.stop()

# --- Trend ---------------------------------------------------------------- #

st.subheader("Monthly trend")
trend = load(TREND_SQL)
if trend.empty:
    st.info("No data to plot.")
else:
    # Separate tabs because the series differ by orders of magnitude
    # (~4 500 enrollments vs ~200 certifications a month).
    growth, certification, money = st.tabs(["Enrollments", "Certification", "Revenue"])
    with growth:
        st.line_chart(trend, x="month", y="enrollments", height=320)
    with certification:
        st.line_chart(trend, x="month", y="cert_rate_pct", height=320)
    with money:
        st.line_chart(trend, x="month", y="net_revenue", height=320)

# --- Domains -------------------------------------------------------------- #

st.subheader("Top domains by enrollment")
domains = load(DOMAINS_SQL)
left, right = st.columns([1, 1])
with left:
    if domains.empty:
        st.info("No data to plot.")
    else:
        st.altair_chart(
            alt.Chart(domains)
            .mark_bar()
            .encode(
                x=alt.X("enrollments:Q", title="Enrollments"),
                y=alt.Y("domain:N", sort="-x", title=None),
                tooltip=["domain", "enrollments", "certified", "cert_rate_pct"],
            )
            .properties(height=360),
            use_container_width=True,
        )
with right:
    st.dataframe(
        domains,
        width="stretch",
        hide_index=True,
        height=360,
        column_config={
            "domain": st.column_config.TextColumn("Domain"),
            "enrollments": st.column_config.NumberColumn("Enroll.", format="%d"),
            "certified": st.column_config.NumberColumn("Cert.", format="%d"),
            "cert_rate_pct": st.column_config.NumberColumn("Cert %", format="%.1f"),
            "avg_progress_pct": st.column_config.NumberColumn("Progress %", format="%.1f"),
            "avg_rating": st.column_config.NumberColumn("Rating", format="%.2f"),
        },
    )

st.divider()

# --- Agent chat ----------------------------------------------------------- #

st.subheader("Ask the agent")
st.caption("Запитуй про дані звичайною мовою — агент сам напише SQL і пояснить результат.")

if not chat_enabled:
    st.info(
        "Chat is disabled: `GEMINI_API_KEY` is not set. Add it to `.env` and restart "
        "the app. The dashboard above works without it."
    )

if "messages" not in st.session_state:
    st.session_state.messages = []


def render_turn(turn: dict) -> None:
    """Render one stored assistant turn (answer, SQL, result table)."""
    if turn.get("error"):
        st.error(turn["error"])
    else:
        st.markdown(turn["answer"])
    if turn.get("sql"):
        with st.expander("Show SQL"):
            st.code(turn["sql"], language="sql")
    if turn.get("data") is not None and not turn["data"].empty:
        with st.expander(f"Show result ({len(turn['data'])} rows)"):
            st.dataframe(turn["data"], width="stretch", hide_index=True)


for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        if message["role"] == "user":
            st.markdown(message["content"])
        else:
            render_turn(message)

if question := st.chat_input(
    "Наприклад: яка частка сертифікованих за доменами?", disabled=not chat_enabled
):
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        with st.spinner("Агент аналізує дані..."):
            result = agent.ask(question)
        turn = {
            "role": "assistant",
            "answer": result.answer,
            "sql": result.sql,
            "data": result.dataframe,
            "error": result.error,
        }
        render_turn(turn)
    st.session_state.messages.append(turn)
