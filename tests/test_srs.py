import os
import shutil
import tempfile

import pytest

from main import ProgressStore, SRSDeck, SessionState, WordState


@pytest.fixture()
def progress_store(tmp_path):
    store_path = tmp_path / "progress.json"
    return ProgressStore(str(store_path))


def test_fast_correct_increases_mastery(progress_store):
    deck = SRSDeck(progress_store)
    words = ["kat"]
    deck.load(words, mode="common")
    word = deck.next_word()
    assert word == "kat"
    state = deck.record("kat", is_correct=True, elapsed=1.5)
    assert state.mastery > 0.0
    assert state.interval > 2.0


def test_slow_correct_penalizes_interval(progress_store):
    deck = SRSDeck(progress_store)
    words = ["boom"]
    deck.load(words, mode="common")
    deck.record("boom", is_correct=True, elapsed=6.0)
    state = progress_store.ensure("boom")
    assert state.mastery < 0.05
    assert state.interval <= 2.5


def test_incorrect_resets_streak(progress_store):
    deck = SRSDeck(progress_store)
    words = ["boom"]
    deck.load(words, mode="common")
    deck.record("boom", is_correct=True, elapsed=1.2)
    state = deck.record("boom", is_correct=False, elapsed=4.0)
    assert state.streak == 0
    assert state.mastery <= 0.1
    assert pytest.approx(state.interval, rel=0.1) == 5.0
