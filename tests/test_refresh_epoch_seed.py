"""Refresh epoch must not repeat across process lifetimes.

Search/catalog ETags fold in `RefreshStatus.epoch()`. When it started at 0
on every boot, an API restart (or `down -v` + fresh DB) re-issued the same
ETags as the previous process, so browsers got 304s for result sets the
new process no longer had: a fresh empty DB showed 32k cached results.
"""
from __future__ import annotations

from biblioplex.ingest.refresh import RefreshStatus


def test_epoch_differs_between_instances() -> None:
    assert RefreshStatus().epoch() != RefreshStatus().epoch()
