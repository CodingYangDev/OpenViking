# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

from __future__ import annotations

from types import SimpleNamespace

from openviking.storage.vectordb.collection import local_collection as lc
from openviking.storage.vectordb.store.data import DeltaRecord
from openviking.storage.vectordb.store.store import OpType


class _FakeIndex:
    def __init__(self, **kwargs):
        self.upsert_batches = []
        self.delete_batches = []

    def get_newest_version(self):
        return 0

    def upsert_data(self, records):
        self.upsert_batches.append(len(records))

    def delete_data(self, records):
        self.delete_batches.append(len(records))

    def _start_dense_rebuild_worker(self):
        pass


class _FakeStore:
    def __init__(self, records):
        self.records = records

    def iter_all_cands_data(self):
        return iter(())

    def get_delta_data_after_ts(self, timestamp):
        return list(self.records)


def _build_collection(tmp_path, records, monkeypatch):
    index_dir = tmp_path / "indexes"
    (index_dir / "default").mkdir(parents=True)
    (index_dir / "default" / "index_meta.json").write_text("{}", encoding="utf-8")

    collection = object.__new__(lc.PersistCollection)
    collection.index_dir = str(index_dir)
    collection.meta = SimpleNamespace()
    collection.store_mgr = _FakeStore(records)
    collection.dense_search_config = {}
    collection.indexes = SimpleNamespace(
        set=lambda name, index: setattr(collection, "index", index)
    )
    monkeypatch.setattr(lc, "create_index_meta", lambda meta, path: SimpleNamespace())
    monkeypatch.setattr(lc, "PersistentIndex", _FakeIndex)

    collection._recover()
    return collection


def test_recovery_replays_put_delta_in_bounded_batches(tmp_path, monkeypatch):
    batch_size = lc._RECOVERY_REPLAY_BATCH_SIZE
    total = batch_size * 2 + 3
    records = [DeltaRecord(type=OpType.PUT.value, label=index) for index in range(total)]

    collection = _build_collection(tmp_path, records, monkeypatch)

    assert sum(collection.index.upsert_batches) == total
    assert max(collection.index.upsert_batches) <= batch_size
    assert len(collection.index.upsert_batches) == 3


def test_recovery_replays_delete_delta_in_bounded_batches(tmp_path, monkeypatch):
    batch_size = lc._RECOVERY_REPLAY_BATCH_SIZE
    total = batch_size + 5
    records = [DeltaRecord(type=OpType.DEL.value, label=index) for index in range(total)]

    collection = _build_collection(tmp_path, records, monkeypatch)

    assert sum(collection.index.delete_batches) == total
    assert max(collection.index.delete_batches) <= batch_size
    assert len(collection.index.delete_batches) == 2
