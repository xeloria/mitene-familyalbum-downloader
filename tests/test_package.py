"""The package-level API and lazy re-exports."""

from __future__ import annotations

import pytest

import mitene_download


def test_version():
    assert mitene_download.__version__ == "7.1.0"


def test_lazy_exports_are_importable():
    from mitene_download import (
        DatabaseCache,
        Options,
        Summary,
        process_album,
        repair_directory,
        sync_albums,
    )

    assert Options().concurrency == 4
    assert Summary().to_dict()["downloaded"] == 0
    assert DatabaseCache("x.db").db_path == "x.db"
    assert callable(process_album)
    assert callable(repair_directory)
    assert callable(sync_albums)


def test_lazy_export_is_cached_and_unknown_names_raise():
    first = mitene_download.Options
    assert mitene_download.Options is first
    with pytest.raises(AttributeError):
        _ = mitene_download.does_not_exist
