"""Fixtures for the prod_dreamer_model tests."""

from __future__ import annotations

import pytest

from prod_dreamer_model.config import DreamerConfig

from .helpers import week_frames_data


@pytest.fixture
def config() -> DreamerConfig:
    return DreamerConfig(
        graph_name="test_graph",
        session_id="tester",
        tz="Asia/Singapore",
        model_api_key="test-key",
    )


@pytest.fixture
def week_frames() -> list[dict]:
    return week_frames_data()
