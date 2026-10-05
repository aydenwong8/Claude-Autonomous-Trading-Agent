"""The decision prompt stays byte-identical through the agent.py module split.

Skips, rather than fails, when the fixture has not been captured: it is
gitignored because it holds a copy of the trading database.
"""

import pickle
import shutil

import pytest

from tests import golden

pytestmark = pytest.mark.skipif(
    not golden.PROMPT.exists(),
    reason="golden fixture not captured (gitignored): run venv/bin/python tests/golden.py",
)


def test_decision_prompt_is_byte_identical(tmp_path):
    db = tmp_path / "golden.db"
    shutil.copy(golden.DB, db)
    inputs = pickle.loads(golden.INPUTS.read_bytes())
    assert golden.build_prompt(db, inputs) == golden.PROMPT.read_text()


def test_system_prompt_is_unchanged():
    assert golden.system_prompt_sha() == golden.SYSTEM_SHA.read_text()
