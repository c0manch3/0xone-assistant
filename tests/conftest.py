"""Shared pytest fixtures for the 0xone-assistant test suite."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_installer_ctx() -> None:
    """Reset the module-level ``configure_installer`` one-shot guard
    between tests (S11 wave-3).

    ``configure_installer`` became idempotent + strict to block silent
    re-configuration with different ``(project_root, data_dir)`` pairs.
    Each test uses its own ``tmp_path`` so without this reset the second
    test to call ``configure_installer`` would raise ``RuntimeError``.
    """
    from assistant.tools_sdk.installer import reset_installer_for_tests

    reset_installer_for_tests()
    yield
    reset_installer_for_tests()
