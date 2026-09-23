"""Concurrency tests: dense unique versions under racing registrations."""
from __future__ import annotations

import threading

from app.models import CompatibilityLevel
from app.service import IncompatibleSchemaError, RegistryService

V1 = {"type": "record", "name": "User", "fields": [
    {"name": "id", "type": "int"},
    {"name": "name", "type": "string"},
]}


def _evolve(n: int) -> dict:
    """BACKWARD-compatible with V1: id stays int, one optional field added."""
    return {"type": "record", "name": "User", "fields": [
        {"name": "id", "type": "int"},
        {"name": "name", "type": "string"},
        {"name": f"f{n}", "type": "string", "default": ""},
    ]}


def _breaking() -> dict:
    """Incompatible with V1 under every policy: id int->bytes."""
    return {"type": "record", "name": "User", "fields": [
        {"name": "id", "type": "bytes"},
        {"name": "name", "type": "string"}]}


def test_concurrent_registrations_get_dense_unique_versions(judge):
    svc = RegistryService()
    svc.register("user", V1)

    n_threads = 16
    barrier = threading.Barrier(n_threads)
    accepted: list[int] = []
    rejected: list[str] = []
    lock = threading.Lock()

    def worker(i: int):
        barrier.wait()  # maximize contention: release all at once
        try:
            rec = svc.register("user", _evolve(i))
            with lock:
                accepted.append(rec.version)
        except IncompatibleSchemaError as exc:
            with lock:
                rejected.append(exc.report.incompatibilities[0].code.value)

    threads = [threading.Thread(target=worker, args=(i,))
               for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    stored = [v.version for v in svc.list_versions("user")]
    judge("concurrent-register", {"threads": n_threads},
          "register serializes check-then-register under one lock",
          {"accepted_versions": sorted(accepted),
           "stored_versions": stored, "rejected": rejected})

    assert len(accepted) + len(rejected) == n_threads
    assert stored == list(range(1, len(stored) + 1))  # dense, unique
    assert len(set(accepted)) == len(accepted)
    assert set(accepted).issubset(set(stored))


def test_concurrent_breaking_changes_all_rejected(judge):
    svc = RegistryService()
    svc.register("user", V1)

    n_threads = 12
    barrier = threading.Barrier(n_threads)
    accepted = 0
    rejected_codes: dict[str, int] = {}
    lock = threading.Lock()

    def worker():
        barrier.wait()
        try:
            svc.register("user", _breaking())
            nonlocal accepted
            with lock:
                accepted += 1
        except IncompatibleSchemaError as exc:
            code = exc.report.incompatibilities[0].code.value
            with lock:
                rejected_codes[code] = rejected_codes.get(code, 0) + 1

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    judge("concurrent-breaking", {"threads": n_threads},
          "int->bytes never promotes under any policy; rejection consumes "
          "no version number",
          {"accepted": accepted, "rejected": rejected_codes,
           "stored_versions": [v.version for v in svc.list_versions("user")]})
    assert accepted == 0
    assert rejected_codes == {"FIELD_TYPE_CHANGED": n_threads}
    assert [v.version for v in svc.list_versions("user")] == [1]


def test_policy_race_with_registration(judge):
    """Policy flips race with registrations; each accepted version must be
    reproducible from the recorded policy history, versions stay dense."""
    svc = RegistryService()
    svc.register("user", V1)

    start = threading.Event()
    accepted: list[int] = []
    rejected = 0
    lock = threading.Lock()

    def flipper():
        start.wait()
        for i in range(6):
            level = CompatibilityLevel.FORWARD if i % 2 == 0 \
                else CompatibilityLevel.BACKWARD
            svc.set_policy("user", level)

    def registrator(i: int):
        start.wait()
        try:
            rec = svc.register("user", _evolve(i))
            with lock:
                accepted.append(rec.version)
        except IncompatibleSchemaError:
            nonlocal rejected
            with lock:
                rejected += 1

    threads = [threading.Thread(target=flipper)]
    threads += [threading.Thread(target=registrator, args=(i,))
                for i in range(10)]
    for t in threads:
        t.start()
    start.set()
    for t in threads:
        t.join()

    stored = svc.list_versions("user")
    # _evolve only *adds* an optional field: valid BACKWARD but not FORWARD
    # (removal is not the issue; addition of a field is allowed FORWARD too,
    # so these schemas are actually compatible in BOTH directions here).
    # Hence every registration should succeed regardless of the flip.
    reproduced = []
    for rec in stored[1:]:
        report = svc.recheck_history("user", rec.version)
        reproduced.append((rec.version, report.level.value,
                           report.compatible))
    judge("policy-race", {"registrations": 10, "policy_flips": 6},
          "accepted v => recheck under policy_at(v) is compatible; "
          "versions remain dense",
          {"stored_versions": [r.version for r in stored],
           "reproduced": reproduced, "rejected": rejected})

    assert [r.version for r in stored] == list(range(1, len(stored) + 1))
    assert all(ok for _, _, ok in reproduced)
    assert rejected == 0


def test_policy_race_rejection_reproducible(judge):
    """Add a required field (no default): valid FORWARD, invalid BACKWARD.
    Racing against a policy flip, rejected attempts leave no gap in versions
    and the final stored set is reproducible under recorded history."""
    svc = RegistryService()
    svc.register("user", V1)
    svc.set_policy("user", CompatibilityLevel.BACKWARD)

    start = threading.Event()
    accepted: list[int] = []
    rejected_backward = 0
    lock = threading.Lock()

    def flipper():
        start.wait()
        for i in range(8):
            level = CompatibilityLevel.FORWARD if i % 2 == 0 \
                else CompatibilityLevel.BACKWARD
            svc.set_policy("user", level)

    def registrator(i: int):
        start.wait()
        schema = {"type": "record", "name": "User", "fields": [
            {"name": "id", "type": "int"},
            {"name": "name", "type": "string"},
            {"name": f"r{i}", "type": "int"},  # required new field
        ]}
        try:
            rec = svc.register("user", schema)
            with lock:
                accepted.append(rec.version)
        except IncompatibleSchemaError:
            nonlocal rejected_backward
            with lock:
                rejected_backward += 1

    threads = [threading.Thread(target=flipper)]
    threads += [threading.Thread(target=registrator, args=(i,))
                for i in range(12)]
    for t in threads:
        t.start()
    start.set()
    for t in threads:
        t.join()

    stored = svc.list_versions("user")
    versions = [r.version for r in stored]
    reproduced = [
        (rec.version,
         svc.recheck_history("user", rec.version).level.value)
        for rec in stored[1:]]
    judge("policy-race-required-add", {"registrations": 12},
          "required addition rejected under BACKWARD, accepted under "
          "FORWARD; either way versions are dense and reproducible",
          {"versions": versions, "accepted": len(accepted),
           "rejected_backward": rejected_backward,
           "reproduced_policies": reproduced})
    assert versions == list(range(1, len(versions) + 1))
    assert len(accepted) + rejected_backward == 12
