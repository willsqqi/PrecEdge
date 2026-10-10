"""Read-only paper experiment view, usable in the existing monitor or on its own."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from prediction_market.research.replay import read_report


def report_tables(report: dict) -> tuple[list[dict], list[dict]]:
    if report.get("schema_version") != 1 or report.get("simulation_only") is not True:
        raise ValueError("unsupported paper experiment report")
    positions = []
    for position in report["positions"]:
        closed = all(leg["credit"] is not None for leg in position["legs"])
        for leg in position["legs"]:
            positions.append({"position_id": position["position_id"], "opened_at": position["opened_at"],
                              "pair_status": "settled" if closed else "unresolved", "quantity": position["quantity"],
                              "contract": leg["contract_key"], "outcome": leg["outcome"],
                              "entry_cost": leg["cost"], "entry_fee": leg["fee"],
                              "settlement_credit": leg["credit"], "settlement_event": leg["settlement_event"]})
    decisions = [{"event_id": d["event_id"], "status": d["status"], "reason": d.get("reason", ""),
                  "filled_quantity": d.get("position", {}).get("quantity"),
                  "entry_debit": d.get("position", {}).get("debit")} for d in report["decisions"]]
    return positions, decisions


def render(database: Path) -> None:
    import streamlit as st

    st.subheader("Paper experiment")
    st.caption("Simulation from recorded books. Realized P&L requires both venue legs to settle; remaining cost is not a market valuation.")
    if not database.is_file():
        st.info("Choose an existing local paper SQLite ledger. Run the fictional paper-demo or a reviewed saved-book paper-scan first.")
        return
    try:
        report = read_report(database)
        positions, decisions = report_tables(report)
    except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
        st.error(f"Cannot read paper experiment: {exc}")
        return
    if not report["reconciliation_passed"]:
        st.error("Cash reconciliation failed. Inspect the ledger before interpreting results.")
    if report["divergent_settlements"]:
        st.warning("Venue settlements diverged for: " + ", ".join(report["divergent_settlements"]))
    columns = st.columns(4)
    for column, label, value in zip(columns, ("Cash (USD)", "Realized P&L (USD)", "Unresolved cost (USD)", "Open pairs"),
                                    (report["cash"], report["realized_pnl"], report["unresolved_cost"], report["open_positions"]), strict=True):
        column.metric(label, value)
    st.caption(f"Kill switch: {'enabled' if report['kill_switch'] else 'disabled'} | Replay time: {report['last_replay_at']}")
    st.write("Positions and leg settlements")
    st.dataframe(positions, hide_index=True)
    st.write("Decisions and refusal reasons")
    st.dataframe(decisions, hide_index=True)
    with st.expander("Experiment inputs and provenance"):
        st.json({"experiment_fingerprint": report["experiment_fingerprint"],
                 "execution_assumption": report["execution_assumption"],
                 "config": report["config"], "provenance": report["provenance"]})


def main() -> None:
    import os

    import streamlit as st

    st.set_page_config(page_title="PrecEdge paper research", layout="wide")
    st.title("PrecEdge paper research")
    path = st.text_input("Local paper database", value=os.getenv("PRECEDGE_PAPER_DATABASE", "reports/paper-demo/paper.sqlite"))
    render(Path(path).expanduser())


def launch() -> None:
    import subprocess
    import sys

    raise SystemExit(subprocess.call([sys.executable, "-m", "streamlit", "run", str(Path(__file__).resolve()), *sys.argv[1:]]))


if __name__ == "__main__":
    main()
