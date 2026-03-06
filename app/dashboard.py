import ast
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.express as px
import streamlit as st

DATA_PATH = Path("parsed_scaled/levels.csv")


@st.cache_data(show_spinner=True)
def load_data(path: str) -> pd.DataFrame:
    usecols = [
        "tick_id",
        "timestamp",
        "side",
        "future_strike",
        "spx_strike",
        "current_es_price",
        "current_es_price_scaled",
        "spx_price",
        "mbo",
        "mbo_pulling_stacking",
        "call_gamma",
        "put_gamma",
        "call_vega",
        "put_vega",
        "call_delta",
        "cans ll_delta",
        "put_delta",
        "t",
    ]

    df = pd.read_csv(path, usecols=lambda c: c in set(usecols), low_memory=False)

    if "call_delta" not in df.columns and "cans ll_delta" in df.columns:
        df = df.rename(columns={"cans ll_delta": "call_delta"})

    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["timestamp", "side", "future_strike"])

    def parse_mbo(value: str) -> tuple[float, int]:
        if pd.isna(value):
            return 0.0, 0
        try:
            parsed = ast.literal_eval(str(value))
            if isinstance(parsed, list):
                numeric = [float(x) for x in parsed]
                return float(sum(numeric)), int(len(numeric))
        except (ValueError, SyntaxError):
            pass
        return 0.0, 0

    mbo_parsed = df["mbo"].apply(parse_mbo)
    df["mbo_total_size"] = mbo_parsed.apply(lambda x: x[0])
    df["mbo_order_count"] = mbo_parsed.apply(lambda x: x[1])

    df["abs_pull_stack"] = df["mbo_pulling_stacking"].abs()
    df["gamma_total"] = df["call_gamma"].abs() + df["put_gamma"].abs()
    df["vega_total"] = df["call_vega"].abs() + df["put_vega"].abs()
    df["price_gap"] = df["current_es_price_scaled"] - df["spx_price"]

    return df


def minmax(series: pd.Series) -> pd.Series:
    smin = series.min()
    smax = series.max()
    if pd.isna(smin) or pd.isna(smax) or smax == smin:
        return pd.Series(np.zeros(len(series)), index=series.index)
    return (series - smin) / (smax - smin)


def aggregate_timeseries(df: pd.DataFrame) -> pd.DataFrame:
    ts = (
        df.groupby("timestamp", as_index=False)
        .agg(
            es_price=("current_es_price_scaled", "mean"),
            spx_price=("spx_price", "mean"),
            price_gap=("price_gap", "mean"),
            liquidity=("mbo_total_size", "mean"),
            manipulation=("abs_pull_stack", "mean"),
            gamma=("gamma_total", "mean"),
            vega=("vega_total", "mean"),
            rows=("tick_id", "count"),
        )
        .sort_values("timestamp")
    )

    ts["kinetic"] = ts["es_price"].diff().abs().fillna(0.0)

    liquidity_n = minmax(ts["liquidity"])
    manipulation_n = minmax(ts["manipulation"])
    gamma_n = minmax(ts["gamma"])
    kinetic_n = minmax(ts["kinetic"])

    ts["stress_index"] = (
        0.35 * manipulation_n
        + 0.30 * gamma_n
        + 0.20 * kinetic_n
        + 0.15 * (1.0 - liquidity_n)
    ).clip(0.0, 1.0)
    ts["health_score"] = (1.0 - ts["stress_index"]).clip(0.0, 1.0)

    return ts


def main() -> None:
    st.set_page_config(
        page_title="Market Organism Dashboard",
        page_icon="📈",
        layout="wide",
    )

    st.title("Market Organism Dashboard")
    st.caption("Interactive prototype for understanding market state, stress, and liquidity structure.")

    if not DATA_PATH.exists():
        st.error(f"Missing data file: {DATA_PATH}")
        st.stop()

    df = load_data(str(DATA_PATH))

    st.sidebar.header("Filters")
    side_options = sorted(df["side"].dropna().unique().tolist())
    selected_sides = st.sidebar.multiselect("Side", side_options, default=side_options)

    strike_min = float(df["future_strike"].min())
    strike_max = float(df["future_strike"].max())
    strike_range = st.sidebar.slider(
        "Future strike range",
        min_value=strike_min,
        max_value=strike_max,
        value=(strike_min, strike_max),
        step=0.25,
    )

    min_time = df["timestamp"].min().to_pydatetime()
    max_time = df["timestamp"].max().to_pydatetime()
    time_range = st.sidebar.slider(
        "Time window",
        min_value=min_time,
        max_value=max_time,
        value=(min_time, max_time),
    )

    max_points = st.sidebar.slider("Max scatter points", min_value=500, max_value=20000, value=5000, step=500)

    filtered = df[
        (df["side"].isin(selected_sides))
        & (df["future_strike"].between(strike_range[0], strike_range[1]))
        & (df["timestamp"].between(pd.Timestamp(time_range[0]), pd.Timestamp(time_range[1])))
    ].copy()

    if filtered.empty:
        st.warning("No rows match the selected filters.")
        st.stop()

    ts = aggregate_timeseries(filtered)

    latest = ts.iloc[-1]
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Health Score", f"{latest['health_score']:.2f}")
    col2.metric("Stress Index", f"{latest['stress_index']:.2f}")
    col3.metric("Price Gap (ES-SPX)", f"{latest['price_gap']:.2f}")
    col4.metric("Visible Rows", f"{len(filtered):,}")

    left, right = st.columns(2)

    with left:
        fig_prices = px.line(
            ts,
            x="timestamp",
            y=["es_price", "spx_price"],
            labels={"value": "Price", "timestamp": "Time", "variable": "Series"},
            title="ES vs SPX Over Time",
        )
        st.plotly_chart(fig_prices, use_container_width=True)

    with right:
        fig_health = px.line(
            ts,
            x="timestamp",
            y=["health_score", "stress_index"],
            labels={"value": "Score", "timestamp": "Time", "variable": "Metric"},
            title="Health and Stress Timeline",
        )
        fig_health.update_yaxes(range=[0, 1])
        st.plotly_chart(fig_health, use_container_width=True)

    heatmap_source = (
        filtered.groupby(["timestamp", "future_strike"], as_index=False)
        .agg(mbo_total_size=("mbo_total_size", "mean"))
        .sort_values(["timestamp", "future_strike"])
    )

    fig_heat = px.density_heatmap(
        heatmap_source,
        x="timestamp",
        y="future_strike",
        z="mbo_total_size",
        histfunc="avg",
        title="Liquidity Heatmap (MBO Total Size)",
        labels={"mbo_total_size": "Liquidity"},
    )
    st.plotly_chart(fig_heat, use_container_width=True)

    sampled = filtered
    if len(sampled) > max_points:
        sampled = sampled.sample(max_points, random_state=42)

    fig_scatter = px.scatter(
        sampled,
        x="abs_pull_stack",
        y="gamma_total",
        color="side",
        size="mbo_total_size",
        hover_data=["timestamp", "future_strike", "price_gap", "call_delta", "put_delta"],
        title="Manipulation Pressure vs Gamma Reactivity",
        labels={
            "abs_pull_stack": "|Pulling/Stacking|",
            "gamma_total": "Total Gamma",
            "mbo_total_size": "Liquidity",
        },
    )
    st.plotly_chart(fig_scatter, use_container_width=True)

    with st.expander("How to read this dashboard"):
        st.markdown(
            """
- **Health Score**: `1 - stress_index` (higher is calmer market structure).
- **Stress Index**: weighted blend of manipulation pressure, gamma reactivity, short-term kinetic movement, and low liquidity.
- **Liquidity Heatmap**: brighter areas indicate thicker order-book structure at specific strikes and times.
- **Scatter**: each point is a strike-time state; larger points indicate larger visible queue size.
            """
        )


if __name__ == "__main__":
    main()
