from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import streamlit as st

from prediction_market.research.dashboard import render as render_paper_experiment
from prediction_market.cloud_db import load_approved_mappings_from_db
from prediction_market.dashboard_data import (
    TableLoadResult,
    filter_signals,
    load_dashboard_tables,
    pair_history,
    summarize_dashboard,
)
from prediction_market.review_store import (
    build_event_pair_review_row,
    build_manual_mapping_row,
    candidate_rows_for_review_event,
    default_event_pair_review_path,
    default_review_mapping_path,
    event_pair_review_errors,
    event_pair_review_status_by_source_event,
    filter_review_candidates,
    filter_review_events,
    load_event_pair_reviews,
    load_review_mappings,
    review_row_errors,
    review_event_for_event_top_match,
    save_review_mapping,
    save_event_pair_review,
    suggested_event_pairs_for_event,
    suggestions_for_event_pair,
    top_event_matches_for_review_event,
    utc_now_iso,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = PROJECT_ROOT / "data" / "cross_sports_arbitrage"
DEFAULT_GCS_SOURCE = "gs://poly-x-kalshi-dev-poly-x-kalshi-scanner/cross_sports_arbitrage"


def main() -> None:
    st.set_page_config(page_title="Poly x Kalshi Monitor", layout="wide")
    st.title("Poly x Kalshi Market Monitor")
    st.caption("Research dashboard for cross-venue sports prediction market snapshots. Alerts are informational only.")

    with st.sidebar:
        st.header("Data")
        default_source = os.getenv("POLY_X_KALSHI_DASHBOARD_SOURCE", str(DEFAULT_SOURCE))
        source = st.text_input("Output source", value=default_source)
        st.caption(f"GCS example: `{DEFAULT_GCS_SOURCE}`")
        default_mapping_store = os.getenv("POLY_X_KALSHI_REVIEW_MAPPING_PATH", default_review_mapping_path(source))
        review_mapping_path = st.text_input("Review mapping store", value=default_mapping_store)
        default_event_pair_store = os.getenv("POLY_X_KALSHI_EVENT_PAIR_REVIEW_PATH", default_event_pair_review_path(source))
        event_pair_review_path = st.text_input("Event-pair review store", value=default_event_pair_store)
        use_cloud_db = st.checkbox("Use Cloud SQL approvals", value=_cloud_db_enabled_default())
        prefer_history = st.toggle("Use full history when available", value=True)
        if st.button("Reload"):
            st.cache_data.clear()
            _clear_review_session_state()
            st.rerun()

        paper_database = st.text_input("Local paper database", value=os.getenv("PRECEDGE_PAPER_DATABASE", str(PROJECT_ROOT / "reports" / "paper-demo" / "paper.sqlite")))
        st.header("Signal Filters")
        search = st.text_input("Search event/pair", value="")
        min_edge = st.number_input("Minimum net edge", value=-1.0, step=0.01, format="%.3f")
        alerts_only = st.checkbox("Alerts only", value=False)

    tables = _load(source, prefer_history)

    missing = [name for name, result in tables.items() if result.error and name in {"scanner_runs", "strategy_signals"}]
    if missing:
        st.warning("Missing core table(s): " + ", ".join(missing))

    runs = tables["scanner_runs"].frame
    mappings = tables["manual_mappings_snapshot"].frame
    orderbooks = tables["orderbook_snapshots"].frame
    signals = tables["strategy_signals"].frame
    alerts = tables["arbitrage_alerts"].frame
    candidates = tables["approval_candidates"].frame
    suggestions = tables["suggested_mappings"].frame
    event_matches = tables["event_top_matches_gemini2"].frame
    review_mappings, review_mapping_error = _load_review_mappings(review_mapping_path)
    db_mappings, db_mapping_error = _load_db_review_mappings(use_cloud_db)
    event_pair_reviews, event_pair_review_error = _load_event_pair_reviews(event_pair_review_path)
    mapping_filter = db_mappings if not db_mappings.empty else review_mappings if not review_mappings.empty else mappings
    summary_tables = _tables_with_mapping_filter(tables, mapping_filter, "cloud_sql" if not db_mappings.empty else None)
    summary = summarize_dashboard(summary_tables)

    latest_run = summary["latest_run"]
    metric_cols = st.columns(6)
    metric_cols[0].metric("Runs", summary["run_count"])
    metric_cols[1].metric("Approved Pairs", summary["approved_mapping_count"])
    metric_cols[2].metric("Orderbooks", summary["orderbook_count"])
    metric_cols[3].metric("Signals", summary["signal_count"])
    metric_cols[4].metric("Alerts", summary["alert_count"])
    metric_cols[5].metric("Candidates", summary["candidate_count"])

    if latest_run:
        st.info(
            f"Latest run `{latest_run.get('run_id', 'unknown')}` finished as "
            f"`{latest_run.get('status', 'unknown')}` with "
            f"{latest_run.get('approved_mapping_count', 'n/a')} approved mappings and "
            f"{latest_run.get('alert_count', 'n/a')} executable alerts."
        )

    review_tab, overview_tab, viability_tab, coverage_tab, signals_tab, pairs_tab, orderbook_tab, discovery_tab, runs_tab, paper_tab = st.tabs(
        ["Review Queue", "Overview", "Viability", "Coverage", "Signals", "Pairs", "Orderbooks", "Discovery", "Runs", "Paper Research"]
    )

    with paper_tab:
        render_paper_experiment(Path(paper_database).expanduser())

    review_sources = {
        "approval_candidates": tables["approval_candidates"].source_path or tables["approval_candidates"].error or "not loaded",
        "suggested_mappings": tables["suggested_mappings"].source_path or tables["suggested_mappings"].error or "not loaded",
        "event_top_matches_gemini2": tables["event_top_matches_gemini2"].source_path or tables["event_top_matches_gemini2"].error or "not loaded",
    }
    with review_tab:
        _render_review_queue(
            candidates,
            suggestions,
            event_matches,
            event_pair_reviews,
            mapping_filter,
            event_pair_review_path,
            review_mapping_path,
            event_pair_review_error,
            db_mapping_error or review_mapping_error,
            review_sources,
        )

    with overview_tab:
        left, right = st.columns(2)
        with left:
            st.subheader("Best Current Edges")
            st.dataframe(_display_columns(summary["near_misses"], _signal_columns()), use_container_width=True, hide_index=True)
        with right:
            st.subheader("Why Signals Are Filtered")
            exclusions = summary["exclusion_counts"]
            if exclusions.empty:
                st.write("No exclusion data yet.")
            else:
                st.bar_chart(exclusions.set_index("exclusion_reason"))

        if not alerts.empty:
            st.subheader("Executable Alert Rows")
            st.dataframe(_display_columns(alerts, _alert_columns()), use_container_width=True, hide_index=True)

    with viability_tab:
        st.subheader("Historical Viability")
        viability = summary["viability_summary"]
        viability_cols = st.columns(6)
        viability_cols[0].metric("Snapshots", viability["snapshot_count"])
        viability_cols[1].metric("Pairs Seen", viability["pair_count"])
        viability_cols[2].metric("Alert Rate", _percent(viability["alert_rate"]))
        viability_cols[3].metric("Positive Edge Rate", _percent(viability["positive_edge_rate"]))
        viability_cols[4].metric("Best Net Edge", _decimal(viability["best_net_edge"]))
        viability_cols[5].metric("P95 Net Edge", _decimal(viability["p95_net_edge"]))

        st.caption(
            "A viable monitoring universe should show repeated positive or near-positive net edges, enough depth, "
            "and stable pair mappings. No-alert periods are useful evidence when the spread is consistently too tight."
        )

        recommendations = summary["pair_recommendations"]
        st.subheader("Pair Recommendations")
        st.dataframe(_display_columns(recommendations, _recommendation_columns()), use_container_width=True, hide_index=True)

        if not recommendations.empty and "recommendation" in recommendations.columns:
            counts = recommendations["recommendation"].fillna("missing").value_counts().reset_index()
            counts.columns = ["recommendation", "count"]
            st.bar_chart(counts.set_index("recommendation"))

        pair_viability = summary["pair_viability"]
        st.subheader("Pair Ranking")
        st.dataframe(_display_columns(pair_viability, _pair_viability_columns()), use_container_width=True, hide_index=True)

        if not pair_viability.empty and "best_net_edge" in pair_viability.columns:
            chart = pair_viability[["mapping_id", "best_net_edge", "positive_edge_rate", "alert_rate"]].head(25).set_index("mapping_id")
            st.bar_chart(chart[["best_net_edge", "positive_edge_rate", "alert_rate"]])

        st.subheader("Buffer / Threshold Sensitivity")
        sensitivity = summary["sensitivity_grid"]
        st.caption(
            "`total_buffer` means two-leg slippage plus fees. `eligible_signals` is the number of historical "
            "signals that would pass under that hypothetical configuration."
        )
        st.dataframe(_display_columns(sensitivity, _sensitivity_columns()), use_container_width=True, hide_index=True)

    with coverage_tab:
        st.subheader("Discovery Coverage Funnel")
        coverage = summary["coverage_funnel"]
        st.caption(
            "This shows where the universe narrows: discovered venue candidates -> suggested pairs -> manually approved "
            "pairs -> monitor/watch/pause recommendations. Low suggestion or approval rates point to matching gaps."
        )
        st.dataframe(_display_columns(coverage, _coverage_columns()), use_container_width=True, hide_index=True)
        if not coverage.empty:
            chart_columns = [
                column
                for column in ("candidate_count", "suggested_mapping_count", "approved_mapping_count")
                if column in coverage.columns
            ]
            if chart_columns:
                st.bar_chart(coverage.set_index("market_type")[chart_columns])

        st.subheader("Keyword Coverage")
        keyword_coverage = summary["keyword_coverage"]
        st.caption(
            "Keyword coverage highlights sports/leagues where we have raw venue inventory but little approved monitoring."
        )
        st.dataframe(_display_columns(keyword_coverage, _keyword_coverage_columns()), use_container_width=True, hide_index=True)
        if not keyword_coverage.empty:
            st.bar_chart(keyword_coverage.head(25).set_index("keyword")["candidate_count"])

    with signals_tab:
        st.subheader("All Scored Complementary-Buy Signals")
        filtered = filter_signals(signals, search=search, min_net_edge=min_edge, alerts_only=alerts_only)
        st.dataframe(_display_columns(filtered, _signal_columns()), use_container_width=True, hide_index=True)

        if not filtered.empty and "net_edge" in filtered.columns and "detected_at" in filtered.columns:
            chart = filtered.copy()
            chart["detected_at"] = pd.to_datetime(chart["detected_at"], errors="coerce")
            chart["net_edge"] = pd.to_numeric(chart["net_edge"], errors="coerce")
            chart = chart.dropna(subset=["detected_at", "net_edge"]).sort_values("detected_at")
            if not chart.empty:
                st.line_chart(chart, x="detected_at", y="net_edge", color="direction")

    with pairs_tab:
        st.subheader("Approved Mapping Gate")
        st.dataframe(_display_columns(mapping_filter, _mapping_columns()), use_container_width=True, hide_index=True)

        pair_options = sorted(signals["mapping_id"].dropna().astype(str).unique()) if "mapping_id" in signals.columns else []
        selected_pair = st.selectbox("Pair history", options=[""] + pair_options)
        history = pair_history(signals, selected_pair)
        if not history.empty:
            st.line_chart(history.dropna(subset=["detected_at", "net_edge"]), x="detected_at", y="net_edge", color="direction")
            st.dataframe(_display_columns(history, _signal_columns()), use_container_width=True, hide_index=True)

    with orderbook_tab:
        st.subheader("Latest YES/NO Orderbook Normalization")
        st.dataframe(_display_columns(orderbooks, _orderbook_columns()), use_container_width=True, hide_index=True)
        venue_counts = summary["venue_counts"]
        if not venue_counts.empty:
            st.bar_chart(venue_counts.set_index("venue"))

    with discovery_tab:
        st.subheader("Review Candidates")
        st.dataframe(_display_columns(candidates, _candidate_columns()), use_container_width=True, hide_index=True)
        st.subheader("Suggested Mappings")
        st.caption("Hidden from the manual review workflow for now. Pair approval is intentionally manual-only.")
        with st.expander("Show generated suggestions for diagnostics"):
            st.dataframe(_display_columns(suggestions, _suggestion_columns()), use_container_width=True, hide_index=True)

    with runs_tab:
        st.subheader("Scanner Run Log")
        st.dataframe(runs, use_container_width=True, hide_index=True)
        run_trend = summary["run_trend"]
        if not run_trend.empty and "finished_at" in run_trend.columns:
            chart_columns = [
                column
                for column in ("candidate_count", "approved_mapping_count", "orderbook_count", "alert_count")
                if column in run_trend.columns
            ]
            if chart_columns:
                st.subheader("Run Trend")
                st.line_chart(run_trend.dropna(subset=["finished_at"]), x="finished_at", y=chart_columns)
        with st.expander("Loaded table sources"):
            for name, result in tables.items():
                status = result.source_path or result.error or "not loaded"
                st.write(f"`{name}`: {status}")


@st.cache_data(ttl=30)
def _load(source: str, prefer_history: bool):
    return load_dashboard_tables(source, prefer_history=prefer_history)


def _cloud_db_enabled_default() -> bool:
    return os.getenv("POLY_X_KALSHI_DB_ENABLED", "").strip().lower() in {"1", "true", "yes"} or bool(
        os.getenv("POLY_X_KALSHI_DB_DSN")
    )


@st.cache_data(ttl=30)
def _load_db_review_mappings(enabled: bool) -> tuple[pd.DataFrame, str]:
    if not enabled:
        return pd.DataFrame(), ""
    try:
        return load_approved_mappings_from_db(), ""
    except Exception as exc:  # pragma: no cover - Streamlit displays environment/auth issues
        return pd.DataFrame(), str(exc)


def _tables_with_mapping_filter(
    tables: dict[str, TableLoadResult], mapping_filter: pd.DataFrame, source_path: str | None
) -> dict[str, TableLoadResult]:
    if mapping_filter.empty:
        return tables
    output = dict(tables)
    previous = tables["manual_mappings_snapshot"]
    output["manual_mappings_snapshot"] = TableLoadResult(
        name="manual_mappings_snapshot",
        frame=mapping_filter,
        source_path=source_path or previous.source_path,
        error=previous.error,
    )
    return output


def _load_review_mappings(path: str) -> tuple[pd.DataFrame, str]:
    try:
        return load_review_mappings(path), ""
    except Exception as exc:  # pragma: no cover - Streamlit displays environment/auth issues
        return pd.DataFrame(), str(exc)


def _load_event_pair_reviews(path: str) -> tuple[pd.DataFrame, str]:
    try:
        return load_event_pair_reviews(path), ""
    except Exception as exc:  # pragma: no cover - Streamlit displays environment/auth issues
        return pd.DataFrame(), str(exc)


def _clear_review_session_state() -> None:
    prefixes = (
        "assisted_",
        "suggestion_",
        "review_polymarket_",
        "review_kalshi_",
        "manual_pair_",
    )
    for key in list(st.session_state.keys()):
        if key.startswith(prefixes):
            del st.session_state[key]


def _render_review_queue(
    candidates: pd.DataFrame,
    suggestions: pd.DataFrame,
    event_matches: pd.DataFrame,
    event_pair_reviews: pd.DataFrame,
    mappings: pd.DataFrame,
    event_pair_review_path: str,
    review_mapping_path: str,
    event_pair_review_error: str,
    review_mapping_error: str,
    review_sources: dict[str, str],
) -> None:
    st.subheader("Manual Pair Review Queue")
    st.caption(
        "Only active venue events are shown. Search Polymarket and Kalshi separately, select one row from each side, "
        "then save your manual decision to the mapping store."
    )
    source_cols = st.columns(4)
    source_cols[0].metric("Review Candidates Loaded", len(candidates))
    source_cols[1].metric("Event Candidates Loaded", len(event_matches))
    source_cols[2].metric("Event Reviews Saved", len(event_pair_reviews))
    source_cols[3].metric("Suggestion Run", _latest_run_label(event_matches if not event_matches.empty else suggestions))
    with st.expander("Review queue data sources"):
        st.write(f"`approval_candidates`: {review_sources.get('approval_candidates', 'not loaded')}")
        st.write(f"`event_top_matches_gemini2`: {review_sources.get('event_top_matches_gemini2', 'not loaded')}")
        st.write(f"`suggested_mappings`: {review_sources.get('suggested_mappings', 'not loaded')}")
    if review_mapping_error:
        st.warning(f"Could not load review mapping store yet: {review_mapping_error}")
    if event_pair_review_error:
        st.warning(f"Could not load event-pair review store yet: {event_pair_review_error}")
    if candidates.empty:
        st.info("No discovery candidates loaded. Run a discovery-enabled sports snapshot first.")
        return

    _render_assisted_similarity_review(
        candidates,
        event_matches,
        event_pair_reviews,
        suggestions,
        mappings,
        event_pair_review_path,
    )

    controls = st.columns([1, 1, 1])
    with controls[0]:
        hide_approved = st.checkbox("Hide already approved events", value=True)
    with controls[1]:
        st.metric(
            "Active Polymarket Events",
            len(filter_review_events(candidates, "polymarket", mappings=mappings, hide_approved=hide_approved)),
        )
    with controls[2]:
        st.metric(
            "Active Kalshi Events",
            len(filter_review_events(candidates, "kalshi", mappings=mappings, hide_approved=hide_approved)),
        )

    left, right = st.columns(2)
    with left:
        st.markdown("**Polymarket Active Events**")
        poly_search = st.text_input("Search Polymarket", key="review_polymarket_search", placeholder="team, player, league, token, slug...")
        polymarket_events = filter_review_events(candidates, "polymarket", poly_search, mappings=mappings, hide_approved=hide_approved)
        st.dataframe(_display_columns(polymarket_events, _review_event_columns()), use_container_width=True, hide_index=True, height=300)
        selected_pm_event = _event_selectbox(polymarket_events, "Polymarket event", "review_polymarket_event_selection")
        polymarket_rows = (
            candidate_rows_for_review_event(candidates, selected_pm_event, mappings=mappings, hide_approved=hide_approved)
            if selected_pm_event is not None
            else pd.DataFrame()
        )
        st.dataframe(_display_columns(polymarket_rows, _review_candidate_columns()), use_container_width=True, hide_index=True, height=220)
        selected_pm = _candidate_selectbox(polymarket_rows, "Polymarket market/outcome", "review_polymarket_selection")

    with right:
        st.markdown("**Kalshi Active Events**")
        kalshi_search = st.text_input("Search Kalshi", key="review_kalshi_search", placeholder="team, player, league, ticker...")
        kalshi_events = filter_review_events(candidates, "kalshi", kalshi_search, mappings=mappings, hide_approved=hide_approved)
        st.dataframe(_display_columns(kalshi_events, _review_event_columns()), use_container_width=True, hide_index=True, height=300)
        selected_ks_event = _event_selectbox(kalshi_events, "Kalshi event", "review_kalshi_event_selection")
        kalshi_rows = (
            candidate_rows_for_review_event(candidates, selected_ks_event, mappings=mappings, hide_approved=hide_approved)
            if selected_ks_event is not None
            else pd.DataFrame()
        )
        st.dataframe(_display_columns(kalshi_rows, _review_candidate_columns()), use_container_width=True, hide_index=True, height=220)
        selected_ks = _candidate_selectbox(kalshi_rows, "Kalshi market/outcome", "review_kalshi_selection")

    if selected_pm is None or selected_ks is None:
        st.info("Select one event on each side, then one market/outcome row inside each event to create a manual pair.")
        return

    st.subheader("Selected Pair")
    pair_cols = st.columns(2)
    with pair_cols[0]:
        st.markdown("**Polymarket**")
        st.dataframe(_display_columns(pd.DataFrame([selected_pm]), _review_detail_columns()), use_container_width=True, hide_index=True)
        _market_link("Polymarket", _polymarket_url(selected_pm))
    with pair_cols[1]:
        st.markdown("**Kalshi**")
        st.dataframe(_display_columns(pd.DataFrame([selected_ks]), _review_detail_columns()), use_container_width=True, hide_index=True)
        _market_link("Kalshi", _kalshi_url(selected_ks))

    manual_key = _safe_widget_key(f"{selected_pm.get('market_id', '')}_{selected_pm.get('yes_token_id', '')}_{selected_ks.get('market_id', '')}")
    with st.form(f"manual_pair_review_form_{manual_key}"):
        default_event = str(selected_pm.get("event_title") or selected_ks.get("event_title") or selected_pm.get("title") or "")
        default_outcome = str(selected_pm.get("outcome_label") or selected_ks.get("outcome_label") or "")
        status = st.selectbox("Decision", options=["approved", "needs_review", "rejected"], index=0, key=f"manual_pair_status_{manual_key}")
        proposition = st.text_input(
            "Proposition",
            value=f"{default_outcome} to win / resolve Yes in {default_event}".strip(),
            key=f"manual_pair_proposition_{manual_key}",
        )
        draw_handling = st.text_input(
            "Draw handling",
            value=_default_review_note(selected_pm, selected_ks, "draw"),
            key=f"manual_pair_draw_{manual_key}",
        )
        extra_time_handling = st.text_input(
            "Extra-time / overtime handling",
            value=_default_review_note(selected_pm, selected_ks, "extra"),
            key=f"manual_pair_extra_time_{manual_key}",
        )
        penalties_handling = st.text_input(
            "Penalty / tiebreak handling",
            value=_default_review_note(selected_pm, selected_ks, "penalties"),
            key=f"manual_pair_penalties_{manual_key}",
        )
        settlement_notes = st.text_area(
            "Settlement notes",
            value=_default_settlement_notes(selected_pm, selected_ks),
            height=140,
            key=f"manual_pair_settlement_notes_{manual_key}",
        )
        reviewer = st.text_input("Reviewer", value=os.getenv("USER", ""), key=f"manual_pair_reviewer_{manual_key}")
        notes = st.text_area("Internal notes", value="", height=80, key=f"manual_pair_notes_{manual_key}")
        submitted = st.form_submit_button("Save manual pair decision")

    if not submitted:
        return

    row = build_manual_mapping_row(
        selected_pm,
        selected_ks,
        status=status,
        draw_handling=draw_handling,
        extra_time_handling=extra_time_handling,
        penalties_handling=penalties_handling,
        settlement_notes=settlement_notes,
        reviewer=reviewer,
        notes=notes,
        proposition=proposition,
    )
    errors = review_row_errors(row)
    if errors:
        st.error("Cannot save yet: " + "; ".join(errors))
        return
    try:
        written = save_review_mapping(review_mapping_path, row)
    except Exception as exc:  # pragma: no cover - Streamlit displays environment/auth issues
        st.error(f"Failed to save mapping: {exc}")
        return
    _clear_review_session_state()
    st.success(f"Saved `{row['mapping_id']}` to `{written['current']}`.")
    st.rerun()


def _render_assisted_similarity_review(
    candidates: pd.DataFrame,
    event_matches: pd.DataFrame,
    event_pair_reviews: pd.DataFrame,
    suggestions: pd.DataFrame,
    mappings: pd.DataFrame,
    event_pair_review_path: str,
) -> None:
    st.markdown("**Assisted Event Review**")
    st.caption(
        "Every active Polymarket event is queued here. Use the suggested Kalshi event and top 5 candidates as inputs, "
        "then approve, reject, mark needs-review, mark no-match, or override via search."
    )
    if event_matches.empty:
        st.info("No event candidate table loaded yet. Run the all-active event semantic step first.")
        if not suggestions.empty:
            _render_suggestion_based_similarity_review(candidates, suggestions, mappings, "")
        st.divider()
        return

    controls = st.columns([1.5, 0.7, 0.7])
    with controls[0]:
        search = st.text_input("Search Polymarket events", key="assisted_candidate_search", placeholder="team, player, league, ticker...")
    with controls[1]:
        min_score = st.number_input("Minimum event score", min_value=0.0, max_value=100.0, value=68.0, step=1.0, key="assisted_event_min_score")
    with controls[2]:
        hide_reviewed = st.checkbox("Hide reviewed", value=True, key="assisted_hide_reviewed")

    source_events = filter_review_events(
        candidates,
        "polymarket",
        search,
        mappings=None,
        hide_approved=False,
    )
    event_queue = _event_review_queue(source_events, event_matches, event_pair_reviews, min_score=float(min_score))
    if hide_reviewed and not event_queue.empty and "review_status" in event_queue.columns:
        event_queue = event_queue[event_queue["review_status"].eq("pending")].reset_index(drop=True)

    status_counts = (
        event_queue["review_status"].fillna("pending").value_counts().to_dict()
        if "review_status" in event_queue.columns and not event_queue.empty
        else {}
    )
    metric_cols = st.columns(4)
    metric_cols[0].metric("Polymarket events shown", len(event_queue))
    metric_cols[1].metric("Pending", int(status_counts.get("pending", 0)))
    metric_cols[2].metric("Approved", int(status_counts.get("approved", 0)))
    metric_cols[3].metric("No match", int(status_counts.get("no_match", 0)))
    st.dataframe(_display_columns(event_queue, _event_review_queue_columns()), use_container_width=True, hide_index=True, height=260)
    selected_event = _event_selectbox(event_queue, "Polymarket event to assess", "assisted_event_selection")
    if selected_event is None:
        st.info("Select one Polymarket event to assess.")
        st.divider()
        return

    event_pairs = top_event_matches_for_review_event(
        event_matches,
        selected_event,
        min_score=float(min_score),
        max_rows=5,
    )
    st.metric("Top Kalshi event candidates", len(event_pairs))
    selected_event_pair = None
    selected_ks_event = None
    if event_pairs.empty:
        st.warning("No top event candidates clear the current score threshold. Use the Kalshi search below.")
    else:
        st.dataframe(_display_columns(event_pairs, _event_top_pair_columns()), use_container_width=True, hide_index=True, height=220)
        selected_event_pair = _event_top_pair_selectbox(event_pairs, "Top Kalshi event candidate", "assisted_event_pair_selection")
        if selected_event_pair is not None:
            selected_ks_event = review_event_for_event_top_match(
                candidates,
                selected_event_pair,
                mappings=None,
                hide_approved=False,
            )
            if selected_ks_event is None:
                st.warning("The ranked event candidate is not present in the active Kalshi review rows. Use search below.")

    with st.expander("Search for another Kalshi event"):
        alternate_search = st.text_input("Search Kalshi events", key="assisted_alternate_kalshi_search", placeholder="team, player, league, ticker...")
        alternate_event = None
        if alternate_search.strip():
            alternate_events = filter_review_events(
                candidates,
                "kalshi",
                alternate_search,
                mappings=None,
                hide_approved=False,
            )
            st.dataframe(_display_columns(alternate_events, _review_event_columns()), use_container_width=True, hide_index=True, height=220)
            alternate_event = _event_selectbox(alternate_events, "Alternate Kalshi event", "assisted_alternate_kalshi_event")
        use_alternate = alternate_event is not None and st.checkbox("Use searched Kalshi event", value=event_pairs.empty, key="assisted_use_alternate_event")
        if use_alternate:
            selected_ks_event = alternate_event

    _render_event_pair_evidence(selected_event, selected_ks_event, selected_event_pair)
    selected_key = _safe_widget_key(f"{selected_event.get('event_key', '')}_{selected_ks_event.get('event_key', '') if selected_ks_event is not None else 'no_match'}")
    with st.form(f"assisted_event_review_form_{selected_key}"):
        status_options = ["approved", "needs_review", "rejected", "no_match"] if selected_ks_event is not None else ["no_match", "needs_review"]
        status = st.selectbox("Decision", options=status_options, index=0, key=f"assisted_event_decision_{selected_key}")
        reviewer = st.text_input("Reviewer", value=os.getenv("USER", ""), key=f"assisted_event_reviewer_{selected_key}")
        notes = st.text_area(
            "Internal notes",
            value=_event_top_review_note(selected_event_pair),
            height=80,
            key=f"assisted_event_notes_{selected_key}",
        )
        submitted = st.form_submit_button("Save event-pair decision")

    if not submitted:
        st.divider()
        return

    row = build_event_pair_review_row(
        selected_event,
        selected_ks_event,
        status=status,
        reviewer=reviewer,
        notes=notes,
        event_match=selected_event_pair,
    )
    errors = event_pair_review_errors(row)
    if errors:
        st.error("Cannot save yet: " + "; ".join(errors))
        st.divider()
        return
    try:
        written = save_event_pair_review(event_pair_review_path, row)
    except Exception as exc:  # pragma: no cover - Streamlit displays environment/auth issues
        st.error(f"Failed to save event-pair review: {exc}")
        st.divider()
        return
    _clear_review_session_state()
    st.success(f"Saved `{row['event_pair_id']}` to `{written['current']}`.")
    st.rerun()
    st.divider()


def _render_suggestion_based_similarity_review(
    candidates: pd.DataFrame,
    suggestions: pd.DataFrame,
    mappings: pd.DataFrame,
    review_mapping_path: str,
) -> None:
    st.markdown("**Legacy Suggested-Pair Review**")
    st.caption("Shown only when the event-top table is missing.")

    controls = st.columns([0.7, 1.5, 0.7, 0.7])
    with controls[0]:
        source_venue = st.selectbox("Start from", options=["polymarket", "kalshi"], format_func=str.title, key="assisted_source_venue")
    with controls[1]:
        search = st.text_input("Search selected-side events", key="assisted_legacy_candidate_search", placeholder="team, player, league, ticker...")
    with controls[2]:
        top_n = st.number_input("Top matches", min_value=1, max_value=50, value=10, step=1, key="assisted_legacy_top_n")
    with controls[3]:
        hide_reviewed = st.checkbox("Hide reviewed", value=True, key="assisted_legacy_hide_reviewed")

    source_events = filter_review_events(candidates, source_venue, search, mappings=mappings, hide_approved=hide_reviewed)
    st.dataframe(_display_columns(source_events, _review_event_columns()), use_container_width=True, hide_index=True, height=240)
    selected_event = _event_selectbox(source_events, f"{source_venue.title()} event", "assisted_legacy_event_selection")
    if selected_event is None:
        return

    event_pairs = suggested_event_pairs_for_event(suggestions, selected_event, mappings=mappings, hide_reviewed=hide_reviewed, max_rows=int(top_n))
    st.dataframe(_display_columns(event_pairs, _event_pair_columns()), use_container_width=True, hide_index=True, height=260)


def _render_suggestion_queue(suggestions: pd.DataFrame, mappings: pd.DataFrame, review_mapping_path: str) -> None:
    st.markdown("**Suggested Pairs for Review**")
    st.caption(
        "Vector-ranked and rules-ranked suggestions are only a bulk filter. Review settlement rules before approving."
    )
    if suggestions.empty:
        st.info("No suggested pairs generated yet.")
        return

    controls = st.columns([1.2, 0.8, 0.8, 0.8, 0.8])
    with controls[0]:
        search = st.text_input("Search suggestions", key="suggestion_search", placeholder="team, player, sport, ticker...")
    with controls[1]:
        min_score = st.number_input("Min suggestion score", min_value=0.0, max_value=100.0, value=60.0, step=1.0)
    with controls[2]:
        methods = ["all"] + sorted(suggestions.get("suggestion_method", pd.Series(dtype=str)).fillna("").astype(str).replace("", "rules").unique())
        method = st.selectbox("Method", options=methods, index=0)
    with controls[3]:
        ai_statuses = suggestions.get("ai_review_status", pd.Series(dtype=str)).fillna("").astype(str).replace("", "unreviewed")
        ai_status = st.selectbox("AI verdict", options=["all"] + sorted(ai_statuses.unique()), index=0)
    with controls[4]:
        hide_reviewed = st.checkbox("Hide reviewed suggestions", value=True)

    visible = _filter_suggestions(
        suggestions,
        mappings,
        search=search,
        min_score=min_score,
        method=method,
        ai_status=ai_status,
        hide_reviewed=hide_reviewed,
    )
    st.metric("Suggested pairs shown", len(visible))
    st.dataframe(_display_columns(visible, _suggestion_columns()), use_container_width=True, hide_index=True, height=260)

    selected = _suggestion_selectbox(visible, "Suggested pair selection", "suggestion_pair_selection")
    if selected is None:
        st.info("Select a suggested pair to approve, reject, or keep in review.")
        st.divider()
        return

    _render_suggestion_evidence(selected)

    selected_key = _safe_widget_key(selected.get("mapping_id") or selected.get("suggested_mapping_id") or "")
    with st.form(f"suggested_pair_review_form_{selected_key}"):
        status_options = ["approved", "needs_review", "rejected"]
        status = st.selectbox("Decision", options=status_options, index=0, key=f"suggestion_decision_{selected_key}")
        proposition = st.text_input("Proposition", value=str(selected.get("proposition") or ""), key=f"suggestion_proposition_{selected_key}")
        draw_handling = st.text_input("Draw handling", value=str(selected.get("draw_handling") or ""), key=f"suggestion_draw_{selected_key}")
        extra_time_handling = st.text_input(
            "Extra-time / overtime handling",
            value=str(selected.get("extra_time_handling") or ""),
            key=f"suggestion_extra_time_{selected_key}",
        )
        penalties_handling = st.text_input(
            "Penalty / tiebreak handling",
            value=str(selected.get("penalties_handling") or ""),
            key=f"suggestion_penalties_{selected_key}",
        )
        settlement_notes = st.text_area(
            "Settlement notes",
            value=str(selected.get("settlement_notes") or ""),
            height=130,
            key=f"suggestion_settlement_notes_{selected_key}",
        )
        reviewer = st.text_input("Reviewer", value=os.getenv("USER", ""), key=f"suggestion_reviewer_{selected_key}")
        notes = st.text_area(
            "Internal notes",
            value=f"suggestion_method={selected.get('suggestion_method', '')}; review_notes={selected.get('review_notes', '')}",
            height=80,
            key=f"suggestion_notes_{selected_key}",
        )
        submitted = st.form_submit_button("Save suggested pair decision")

    if not submitted:
        st.divider()
        return

    row = _mapping_row_from_suggestion(
        selected,
        status=status,
        proposition=proposition,
        draw_handling=draw_handling,
        extra_time_handling=extra_time_handling,
        penalties_handling=penalties_handling,
        settlement_notes=settlement_notes,
        reviewer=reviewer,
        notes=notes,
    )
    errors = review_row_errors(row)
    if errors:
        st.error("Cannot save yet: " + "; ".join(errors))
        st.divider()
        return
    try:
        written = save_review_mapping(review_mapping_path, row)
    except Exception as exc:  # pragma: no cover - Streamlit displays environment/auth issues
        st.error(f"Failed to save mapping: {exc}")
        st.divider()
        return
    _clear_review_session_state()
    st.success(f"Saved `{row['mapping_id']}` to `{written['current']}`.")
    st.rerun()
    st.divider()


def _render_suggestion_evidence(selected: dict) -> None:
    detail_cols = st.columns(2)
    with detail_cols[0]:
        st.markdown("**Suggested Polymarket Side**")
        st.write(f"Event: `{selected.get('polymarket_event_title', '')}`")
        st.write(f"Market: `{selected.get('polymarket_title', '')}`")
        st.write(f"Outcome: `{selected.get('polymarket_yes_outcome', '')}`")
        _market_link("Polymarket", _polymarket_url_from_suggestion(selected))
    with detail_cols[1]:
        st.markdown("**Suggested Kalshi Side**")
        st.write(f"Event: `{selected.get('kalshi_event_title', '')}`")
        st.write(f"Market: `{selected.get('kalshi_title', '')}`")
        st.write(f"Ticker: `{selected.get('kalshi_ticker', '')}`")
        _market_link("Kalshi", _kalshi_url_from_suggestion(selected))

    st.caption(
        f"run_id={selected.get('run_id', '')} | "
        f"method={selected.get('suggestion_method', 'rules')} | "
        f"score={selected.get('match_score', '')} | "
        f"embedding={selected.get('embedding_score', '')} | "
        f"semantic={selected.get('semantic_combined_score', '')} | "
        f"gemini={selected.get('gemini_embedding_score', '')} | "
        f"lexical={selected.get('lexical_score', '')} | "
        f"ai={selected.get('ai_review_status', '')}:{selected.get('ai_review_confidence', '')}"
    )
    if selected.get("ai_review_status"):
        ai_message = str(selected.get("ai_review_reason") or "")
        risk_flags = str(selected.get("ai_risk_flags") or "")
        if ai_message:
            st.info(f"AI review: {ai_message}")
        if risk_flags:
            st.caption(f"AI risk flags: `{risk_flags}`")
    if selected.get("review_notes"):
        st.warning(str(selected.get("review_notes")))


def _render_manual_pair_evidence(pm: pd.Series, ks: pd.Series, event_pair: dict | None = None) -> None:
    detail_cols = st.columns(2)
    with detail_cols[0]:
        st.markdown("**Selected Polymarket Side**")
        st.dataframe(_display_columns(pd.DataFrame([pm]), _review_detail_columns()), use_container_width=True, hide_index=True)
        _market_link("Polymarket", _polymarket_url(pm))
    with detail_cols[1]:
        st.markdown("**Selected Kalshi Side**")
        st.dataframe(_display_columns(pd.DataFrame([ks]), _review_detail_columns()), use_container_width=True, hide_index=True)
        _market_link("Kalshi", _kalshi_url(ks))
    if event_pair:
        st.caption(
            f"event_candidate_rank={event_pair.get('rank', '')} | "
            f"event_score={event_pair.get('event_score', event_pair.get('event_embedding_score', ''))} | "
            f"kalshi_event={event_pair.get('kalshi_event_title', event_pair.get('other_event_title', ''))}"
        )


def _render_event_pair_evidence(pm_event: pd.Series, ks_event: pd.Series | None, event_pair: dict | None = None) -> None:
    detail_cols = st.columns(2)
    with detail_cols[0]:
        st.markdown("**Polymarket Event**")
        st.dataframe(_display_columns(pd.DataFrame([pm_event]), _review_event_columns()), use_container_width=True, hide_index=True)
    with detail_cols[1]:
        st.markdown("**Selected Kalshi Event**")
        if ks_event is None:
            st.info("No Kalshi event selected. Save as no-match or search for an override.")
        else:
            st.dataframe(_display_columns(pd.DataFrame([ks_event]), _review_event_columns()), use_container_width=True, hide_index=True)
    if event_pair:
        st.caption(
            f"candidate_rank={event_pair.get('rank', '')} | "
            f"event_score={event_pair.get('event_score', event_pair.get('event_embedding_score', ''))} | "
            f"candidate_key={event_pair.get('kalshi_event_key', '')}"
        )


def _event_review_queue(
    source_events: pd.DataFrame,
    event_matches: pd.DataFrame,
    event_pair_reviews: pd.DataFrame,
    *,
    min_score: float,
) -> pd.DataFrame:
    if source_events.empty:
        return source_events
    review_statuses = event_pair_review_status_by_source_event(event_pair_reviews)
    candidate_groups = _event_top_candidate_groups(event_matches, min_score=min_score)
    rows: list[dict[str, object]] = []
    for _, event in source_events.iterrows():
        top = _top_event_candidates_from_groups(candidate_groups, event)
        best = top.iloc[0] if not top.empty else None
        row = event.to_dict()
        event_key = str(row.get("event_key") or "")
        row["review_status"] = review_statuses.get(event_key, "pending")
        row["suggested_kalshi_event"] = "" if best is None else str(best.get("kalshi_event_title") or "")
        row["suggested_event_score"] = "" if best is None else best.get("event_score", "")
        row["top5_candidate_count"] = int(len(top))
        row["top5_candidates"] = _format_top_event_candidates(top)
        rows.append(row)
    return pd.DataFrame(rows).sort_values(["review_status", "event_date", "event_title"], na_position="last").reset_index(drop=True)


def _event_top_candidate_groups(event_matches: pd.DataFrame, *, min_score: float) -> dict[str, pd.DataFrame]:
    if event_matches.empty:
        return {}
    required = {"pm_event_title", "pm_event_key", "rank", "event_embedding_score"}
    if not required.issubset(set(event_matches.columns)):
        return {}
    output = event_matches.copy()
    output["event_score"] = pd.to_numeric(output["event_embedding_score"], errors="coerce")
    output["rank"] = pd.to_numeric(output["rank"], errors="coerce")
    output = output[output["event_score"].fillna(0.0) >= float(min_score)]
    if output.empty:
        return {}
    output["_pm_event_norm"] = output["pm_event_title"].fillna("").astype(str).map(_normalize_dashboard_text)
    output = output.sort_values(["_pm_event_norm", "rank", "event_score"], ascending=[True, True, False], na_position="last")
    return {key: group.reset_index(drop=True) for key, group in output.groupby("_pm_event_norm", dropna=False, sort=False)}


def _top_event_candidates_from_groups(groups: dict[str, pd.DataFrame], event: pd.Series | dict[str, object]) -> pd.DataFrame:
    key = _normalize_dashboard_text(event.get("event_title") if isinstance(event, dict) else event.get("event_title"))
    group = groups.get(key)
    if group is None or group.empty:
        return pd.DataFrame()
    date = str((event.get("event_date") if isinstance(event, dict) else event.get("event_date")) or "").strip()
    if date:
        dated = group[group["pm_event_key"].fillna("").astype(str).str.contains(date, regex=False)]
        if not dated.empty:
            group = dated
    return group.head(5).reset_index(drop=True)


def _format_top_event_candidates(frame: pd.DataFrame) -> str:
    if frame.empty:
        return ""
    parts: list[str] = []
    for _, row in frame.iterrows():
        score = row.get("event_score", row.get("event_embedding_score", ""))
        title = str(row.get("kalshi_event_title") or "")
        rank = row.get("rank", "")
        parts.append(f"{rank}: {score} {title}".strip())
    return " | ".join(parts)


def _normalize_dashboard_text(value: object) -> str:
    text = str(value or "").casefold()
    text = "".join(character if character.isalnum() else " " for character in text)
    return " ".join(text.split())


def _latest_run_label(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "none"
    if "run_id" not in frame.columns:
        return "unknown"
    values = frame["run_id"].fillna("").astype(str).replace("", pd.NA).dropna().unique()
    if len(values) == 0:
        return "unknown"
    return str(values[-1])[:32]


def _safe_widget_key(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        text = "selected"
    safe = "".join(character if character.isalnum() else "_" for character in text)
    return safe[:180] or "selected"


def _display_columns(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    if frame.empty:
        return frame
    present = [column for column in columns if column in frame.columns]
    remaining = [column for column in frame.columns if column not in present]
    return frame[present + remaining[: max(0, 12 - len(present))]]


def _filter_suggestions(
    suggestions: pd.DataFrame,
    mappings: pd.DataFrame,
    *,
    search: str,
    min_score: float,
    method: str,
    ai_status: str = "all",
    hide_reviewed: bool = True,
) -> pd.DataFrame:
    if suggestions.empty:
        return suggestions
    output = suggestions.copy()
    if "match_score" in output.columns:
        output["match_score"] = pd.to_numeric(output["match_score"], errors="coerce").fillna(0.0)
        output = output[output["match_score"] >= min_score]
    if method != "all" and "suggestion_method" in output.columns:
        output = output[output["suggestion_method"].fillna("").astype(str) == method]
    if ai_status != "all" and "ai_review_status" in output.columns:
        statuses = output["ai_review_status"].fillna("").astype(str).replace("", "unreviewed")
        output = output[statuses == ai_status]
    if hide_reviewed and not mappings.empty and "mapping_id" in output.columns and "mapping_id" in mappings.columns:
        reviewed = set(mappings["mapping_id"].fillna("").astype(str))
        output = output[~output["mapping_id"].fillna("").astype(str).isin(reviewed)]
    if search.strip():
        terms = [term for term in search.casefold().split() if term]
        columns = [
            "event_name",
            "proposition",
            "polymarket_event_title",
            "polymarket_title",
            "polymarket_slug",
            "kalshi_event_title",
            "kalshi_title",
            "kalshi_ticker",
            "outcome_label",
            "market_type",
            "review_notes",
            "ai_review_status",
            "ai_review_reason",
            "ai_risk_flags",
        ]
        output = output[output.apply(lambda row: _row_matches_terms(row, columns, terms), axis=1)]
    sort_columns = [
        column
        for column in ("match_score", "semantic_combined_score", "gemini_embedding_score", "embedding_score", "lexical_score")
        if column in output.columns
    ]
    if sort_columns:
        for column in sort_columns:
            output[column] = pd.to_numeric(output[column], errors="coerce")
        output = output.sort_values(sort_columns, ascending=[False] * len(sort_columns), na_position="last")
    return output.reset_index(drop=True)


def _suggestion_selectbox(frame: pd.DataFrame, label: str, key: str) -> dict | None:
    if frame.empty:
        return None
    options = list(range(len(frame)))
    selected = st.selectbox(
        label,
        options=options,
        format_func=lambda index: _suggestion_label(frame.iloc[index]),
        key=key,
    )
    return frame.iloc[selected].to_dict()


def _suggestion_label(row: pd.Series) -> str:
    score = row.get("match_score", "")
    method = row.get("suggestion_method", "rules")
    ai_status = row.get("ai_review_status", "")
    ai_confidence = row.get("ai_review_confidence", "")
    ai_part = f" | ai={ai_status}:{ai_confidence}" if ai_status else ""
    event = row.get("event_name", "") or row.get("polymarket_event_title", "") or row.get("kalshi_event_title", "")
    outcome = row.get("outcome_label", "")
    ticker = row.get("kalshi_ticker", "")
    return f"{score} | {method}{ai_part} | {event} | {outcome} | {ticker}"


def _mapping_row_from_suggestion(
    suggestion: dict,
    *,
    status: str,
    proposition: str,
    draw_handling: str,
    extra_time_handling: str,
    penalties_handling: str,
    settlement_notes: str,
    reviewer: str,
    notes: str,
) -> dict[str, str]:
    return {
        "mapping_id": str(suggestion.get("mapping_id") or suggestion.get("suggested_mapping_id") or "").strip(),
        "status": status.strip().casefold(),
        "lifecycle_status": "active",
        "event_name": str(suggestion.get("event_name") or "").strip(),
        "proposition": proposition.strip(),
        "polymarket_market_id": str(suggestion.get("polymarket_market_id") or "").strip(),
        "polymarket_slug": str(suggestion.get("polymarket_slug") or "").strip(),
        "polymarket_yes_token_id": str(suggestion.get("polymarket_yes_token_id") or "").strip(),
        "polymarket_no_token_id": str(suggestion.get("polymarket_no_token_id") or "").strip(),
        "polymarket_yes_outcome": str(suggestion.get("polymarket_yes_outcome") or "").strip(),
        "polymarket_no_outcome": str(suggestion.get("polymarket_no_outcome") or "").strip(),
        "kalshi_ticker": str(suggestion.get("kalshi_ticker") or "").strip(),
        "draw_handling": draw_handling.strip(),
        "extra_time_handling": extra_time_handling.strip(),
        "penalties_handling": penalties_handling.strip(),
        "settlement_notes": settlement_notes.strip(),
        "reviewer": reviewer.strip(),
        "reviewed_at": utc_now_iso(),
        "notes": notes.strip(),
    }


def _row_matches_terms(row: pd.Series | dict, columns: list[str], terms: list[str]) -> bool:
    haystack = " ".join(str(row.get(column, "") or "") for column in columns).casefold()
    return all(term in haystack for term in terms)


def _signal_columns() -> list[str]:
    return [
        "detected_at",
        "event_name",
        "proposition",
        "direction",
        "is_alert",
        "net_edge",
        "gross_cost",
        "buffered_cost",
        "min_depth",
        "exclusion_reason",
        "leg1_venue",
        "leg1_outcome",
        "leg1_ask",
        "leg1_depth",
        "leg2_venue",
        "leg2_outcome",
        "leg2_ask",
        "leg2_depth",
    ]


def _alert_columns() -> list[str]:
    return [
        "detected_at",
        "event_name",
        "proposition",
        "direction",
        "net_edge",
        "gross_cost",
        "buffered_cost",
        "min_depth",
        "leg1_venue",
        "leg1_outcome",
        "leg1_ask",
        "leg2_venue",
        "leg2_outcome",
        "leg2_ask",
    ]


def _mapping_columns() -> list[str]:
    return [
        "mapping_id",
        "status",
        "event_name",
        "proposition",
        "kalshi_ticker",
        "polymarket_slug",
        "draw_handling",
        "extra_time_handling",
        "penalties_handling",
        "reviewed_at",
    ]


def _orderbook_columns() -> list[str]:
    return [
        "retrieved_at",
        "mapping_id",
        "venue",
        "market_id",
        "yes_bid",
        "yes_ask",
        "no_bid",
        "no_ask",
        "yes_ask_depth",
        "no_ask_depth",
        "error",
    ]


def _candidate_columns() -> list[str]:
    return [
        "venue",
        "market_type",
        "event_title",
        "outcome_label",
        "title",
        "event_date",
        "status",
        "liquidity_hint",
        "approval_notes",
    ]


def _review_candidate_columns() -> list[str]:
    return [
        "event_date",
        "market_type",
        "event_title",
        "outcome_label",
        "title",
        "ticker_or_slug",
        "market_id",
        "status",
        "close_time",
        "liquidity_hint",
    ]


def _review_event_columns() -> list[str]:
    return [
        "event_date",
        "event_title",
        "market_count",
        "outcome_count",
        "market_types",
        "outcomes_sample",
        "titles_sample",
        "tickers_or_slugs",
        "status",
        "close_time",
    ]


def _event_review_queue_columns() -> list[str]:
    return [
        "review_status",
        "event_date",
        "event_title",
        "market_count",
        "market_types",
        "suggested_event_score",
        "suggested_kalshi_event",
        "top5_candidate_count",
        "top5_candidates",
        "outcomes_sample",
    ]


def _review_detail_columns() -> list[str]:
    return [
        "venue",
        "event_title",
        "outcome_label",
        "title",
        "market_type",
        "market_id",
        "ticker_or_slug",
        "yes_token_id",
        "no_token_id",
        "outcomes",
        "settlement_summary",
        "rules_text",
    ]


def _suggestion_columns() -> list[str]:
    return [
        "suggestion_status",
        "match_score",
        "suggestion_method",
        "ai_review_status",
        "ai_review_confidence",
        "ai_event_match",
        "ai_market_match",
        "ai_outcome_match",
        "ai_settlement_match",
        "ai_recommendation",
        "semantic_provider",
        "semantic_combined_score",
        "gemini_embedding_score",
        "embedding_score",
        "lexical_score",
        "embedding_model",
        "market_type",
        "event_name",
        "outcome_label",
        "proposition",
        "polymarket_title",
        "kalshi_title",
        "kalshi_ticker",
        "draw_handling",
        "ai_review_reason",
        "ai_risk_flags",
        "review_notes",
        "settlement_notes",
    ]


def _event_pair_columns() -> list[str]:
    return [
        "max_match_score",
        "max_semantic_score",
        "suggestion_count",
        "other_venue",
        "other_event_title",
        "market_types",
        "outcomes_sample",
        "methods",
        "other_market_ids",
        "review_notes",
    ]


def _event_top_pair_columns() -> list[str]:
    return [
        "rank",
        "event_score",
        "kalshi_event_title",
        "kalshi_market_types",
        "kalshi_sample_outcomes",
        "kalshi_row_count",
        "kalshi_event_key",
        "pm_event_title",
        "pm_market_types",
        "pm_sample_outcomes",
    ]


def _assisted_suggestion_columns() -> list[str]:
    return [
        "match_score",
        "suggestion_method",
        "ai_review_status",
        "ai_review_confidence",
        "ai_event_match",
        "ai_market_match",
        "ai_outcome_match",
        "ai_settlement_match",
        "ai_recommendation",
        "semantic_combined_score",
        "gemini_embedding_score",
        "embedding_score",
        "lexical_score",
        "market_type",
        "event_name",
        "outcome_label",
        "proposition",
        "polymarket_title",
        "kalshi_title",
        "kalshi_ticker",
        "ai_review_reason",
        "ai_risk_flags",
        "review_notes",
    ]


def _pair_viability_columns() -> list[str]:
    return [
        "mapping_id",
        "event_name",
        "proposition",
        "snapshots",
        "observations",
        "alert_count",
        "alert_rate",
        "positive_edge_count",
        "positive_edge_rate",
        "best_net_edge",
        "median_net_edge",
        "latest_net_edge",
        "latest_exclusion_reason",
        "worst_min_depth",
    ]


def _recommendation_columns() -> list[str]:
    return [
        "recommendation",
        "confidence",
        "reason",
        "event_name",
        "proposition",
        "snapshots",
        "alert_rate",
        "positive_edge_rate",
        "best_net_edge",
        "latest_net_edge",
        "worst_min_depth",
        "mapping_id",
    ]


def _coverage_columns() -> list[str]:
    return [
        "market_type",
        "candidate_count",
        "polymarket_candidates",
        "kalshi_candidates",
        "suggested_mapping_count",
        "approved_mapping_count",
        "monitor_count",
        "watch_count",
        "pause_count",
        "needs_more_data_count",
        "suggestion_rate",
        "approval_rate",
    ]


def _keyword_coverage_columns() -> list[str]:
    return [
        "keyword",
        "candidate_count",
        "polymarket_candidates",
        "kalshi_candidates",
        "match_winner_candidates",
        "total_candidates",
        "spread_candidates",
        "other_candidates",
    ]


def _sensitivity_columns() -> list[str]:
    return [
        "total_buffer",
        "min_net_edge",
        "min_depth",
        "eligible_signals",
        "eligible_rate",
        "eligible_pairs",
        "price_rows",
        "best_repriced_net_edge",
    ]


def _percent(value: object) -> str:
    if value is None or pd.isna(value):
        return "n/a"
    return f"{float(value):.1%}"


def _decimal(value: object) -> str:
    if value is None or pd.isna(value):
        return "n/a"
    return f"{float(value):.3f}"


def _event_selectbox(frame: pd.DataFrame, label: str, key: str) -> pd.Series | None:
    if frame.empty:
        st.selectbox(label, options=[""], disabled=True, key=key)
        return None
    options = list(range(len(frame)))
    selected = st.selectbox(label, options=options, format_func=lambda index: _event_label(frame.iloc[index]), key=key)
    return frame.iloc[int(selected)]


def _event_label(row: pd.Series) -> str:
    pieces = [
        str(row.get("event_date") or "").strip(),
        str(row.get("event_title") or "").strip(),
        f"markets={row.get('market_count', '')}",
        str(row.get("market_types") or "").strip(),
    ]
    return " | ".join(piece for piece in pieces if piece)[:260]


def _event_pair_selectbox(frame: pd.DataFrame, label: str, key: str) -> dict | None:
    if frame.empty:
        st.selectbox(label, options=[""], disabled=True, key=key)
        return None
    options = list(range(len(frame)))
    selected = st.selectbox(label, options=options, format_func=lambda index: _event_pair_label(frame.iloc[index]), key=key)
    return frame.iloc[int(selected)].to_dict()


def _event_pair_label(row: pd.Series) -> str:
    score = row.get("max_match_score", "")
    semantic = row.get("max_semantic_score", "")
    event = row.get("other_event_title", "")
    count = row.get("suggestion_count", "")
    venue = row.get("other_venue", "")
    return f"{score} | semantic={semantic} | {venue} | {event} | suggestions={count}"[:260]


def _event_top_pair_selectbox(frame: pd.DataFrame, label: str, key: str) -> dict | None:
    if frame.empty:
        st.selectbox(label, options=[""], disabled=True, key=key)
        return None
    options = list(range(len(frame)))
    selected = st.selectbox(label, options=options, format_func=lambda index: _event_top_pair_label(frame.iloc[index]), key=key)
    return frame.iloc[int(selected)].to_dict()


def _event_top_pair_label(row: pd.Series) -> str:
    rank = row.get("rank", "")
    score = row.get("event_score", row.get("event_embedding_score", ""))
    event = row.get("kalshi_event_title", row.get("other_event_title", ""))
    types = row.get("kalshi_market_types", "")
    return f"rank {rank} | score={score} | {event} | {types}"[:260]


def _candidate_selectbox(frame: pd.DataFrame, label: str, key: str) -> pd.Series | None:
    if frame.empty:
        st.selectbox(label, options=[""], disabled=True, key=key)
        return None
    options = list(range(len(frame)))
    selected = st.selectbox(label, options=options, format_func=lambda index: _candidate_label(frame.iloc[index]), key=key)
    return frame.iloc[int(selected)]


def _candidate_label(row: pd.Series) -> str:
    pieces = [
        str(row.get("event_date") or "").strip(),
        str(row.get("market_type") or "").strip(),
        str(row.get("event_title") or row.get("title") or "").strip(),
        str(row.get("outcome_label") or "").strip(),
        str(row.get("ticker_or_slug") or row.get("market_id") or "").strip(),
    ]
    return " | ".join(piece for piece in pieces if piece)[:240]


def _event_top_review_note(event_pair: dict | None) -> str:
    if not event_pair:
        return "assisted_event_review=true; selected_kalshi_event=manual_search"
    return (
        "assisted_event_review=true; "
        f"event_candidate_rank={event_pair.get('rank', '')}; "
        f"event_score={event_pair.get('event_score', event_pair.get('event_embedding_score', ''))}; "
        f"kalshi_event_key={event_pair.get('kalshi_event_key', '')}"
    )


def _market_link(label: str, url: str) -> None:
    if url:
        st.markdown(f"[Open {label} market]({url})")


def _polymarket_url(row: pd.Series) -> str:
    slug = str(row.get("ticker_or_slug") or "").strip()
    return f"https://polymarket.com/event/{slug}" if slug else ""


def _kalshi_url(row: pd.Series) -> str:
    ticker = str(row.get("market_id") or row.get("ticker_or_slug") or "").strip().lower()
    return f"https://kalshi.com/markets/{ticker}" if ticker else ""


def _polymarket_url_from_suggestion(row: dict) -> str:
    slug = str(row.get("polymarket_slug") or "").strip()
    return f"https://polymarket.com/event/{slug}" if slug else ""


def _kalshi_url_from_suggestion(row: dict) -> str:
    ticker = str(row.get("kalshi_ticker") or "").strip().lower()
    return f"https://kalshi.com/markets/{ticker}" if ticker else ""


def _default_review_note(pm: pd.Series, ks: pd.Series, note_type: str) -> str:
    text = " ".join(str(value or "").casefold() for value in (pm.get("market_type"), ks.get("market_type"), pm.get("title"), ks.get("title")))
    if "tennis" in text or "itf" in text or "atp" in text or "wta" in text:
        if note_type == "draw":
            return "not applicable; tennis match winner"
        if note_type == "extra":
            return "not applicable; match winner after play begins"
        return "not applicable"
    if "baseball" in text or "mlb" in text:
        if note_type == "draw":
            return "no standard draw; verify cancellation/suspended-game handling"
        if note_type == "extra":
            return "extra innings included for official game winner"
        return "not applicable"
    return "review required"


def _default_settlement_notes(pm: pd.Series, ks: pd.Series) -> str:
    pm_summary = str(pm.get("settlement_summary") or pm.get("rules_text") or pm.get("title") or "").strip()
    ks_summary = str(ks.get("settlement_summary") or ks.get("rules_text") or ks.get("title") or "").strip()
    return f"Polymarket: {pm_summary}\n\nKalshi: {ks_summary}".strip()


if __name__ == "__main__":
    main()
