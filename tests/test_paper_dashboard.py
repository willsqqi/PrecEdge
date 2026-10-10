from pathlib import Path

import pytest

from prediction_market.research.demo import run_demo

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest


def test_standalone_dashboard_renders_settled_fixture_readonly(tmp_path, monkeypatch):
    import prediction_market.research.dashboard as module

    target = tmp_path / "demo"
    run_demo(target)
    db = target / "paper.sqlite"
    before = db.read_bytes()
    monkeypatch.setenv("PRECEDGE_PAPER_DATABASE", str(db))
    app = AppTest.from_file(str(Path(module.__file__))).run(timeout=15)
    assert not app.exception
    assert not app.error
    assert [m.label for m in app.metric] == ["Cash (USD)", "Realized P&L (USD)", "Unresolved cost (USD)", "Open pairs"]
    assert [m.value for m in app.metric] == ["1000.59", "0.59", "0", "0"]
    assert len(app.dataframe) == 2
    assert "kill_switch" in app.dataframe[1].value["reason"].tolist()
    assert db.read_bytes() == before


def test_missing_database_shows_guidance_without_creating_state(tmp_path, monkeypatch):
    import prediction_market.research.dashboard as module

    path = tmp_path / "missing.sqlite"
    monkeypatch.setenv("PRECEDGE_PAPER_DATABASE", str(path))
    app = AppTest.from_file(str(Path(module.__file__))).run(timeout=15)
    assert not app.exception
    assert len(app.info) == 1
    assert len(app.metric) == 0
    assert not path.exists()
