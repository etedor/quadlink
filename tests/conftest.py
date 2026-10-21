import tempfile

import pytest


@pytest.fixture(autouse=True)
def _private_tempdir(tmp_path, monkeypatch):
    # Daemon() clears its relay workroot under the temp dir; keep tests off the real one
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
