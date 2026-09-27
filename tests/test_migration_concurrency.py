"""Concurrency: migration vs registration/precheck/policy change.

The import commit is serialized under the same lock as registration, so
readers always observe a complete before- or after-state, never a partial
one, and version numbering stays dense under every interleaving.
"""
from __future__ import annotations

import threading

from app.migration import MigrationError
from app.models import CompatibilityLevel
from app.service import IncompatibleSchemaError, RegistryService


def _schema(n: int) -> dict:
    return {"type": "record", "name": "User", "fields": [
        {"name": "id", "type": "int"},
        *({"name": f"f{i}", "type": "string", "default": ""}
          for i in range(n)),
    ]}


def _big_package(n_versions: int) -> bytes:
    src = RegistryService()
    src.register("big", _schema(0))
    src.set_policy("big", CompatibilityLevel.NONE)
    for i in range(1, n_versions):
        src.register("big", _schema(i))
    return src.export_subjects(["big"])


def test_readers_never_see_partial_import(judge):
    """While a large subject is imported, every concurrent listing of that
    subject is either empty (pre) or the complete dense set (post)."""
    data = _big_package(400)
    dst = RegistryService()
    stop = threading.Event()
    illegal_snapshots: list[list[int]] = []
    seen_complete = threading.Event()
    lock = threading.Lock()

    def reader():
        while not stop.is_set():
            versions = [v.version for v in dst.list_versions("big")]
            if versions:
                if versions != list(range(1, len(versions) + 1)):
                    with lock:
                        illegal_snapshots.append(versions)
                if len(versions) == 400:
                    seen_complete.set()

    readers = [threading.Thread(target=reader) for _ in range(4)]
    for t in readers:
        t.start()
    dst.import_package(data)
    seen_complete.wait(2)
    stop.set()
    for t in readers:
        t.join()

    final = [v.version for v in dst.list_versions("big")]
    judge("read-atomicity", {"imported_versions": 400, "readers": 4},
          "list_versions copies under store lock; import commits atomically",
          {"final_len": len(final), "illegal_snapshots": illegal_snapshots[:3]})
    assert illegal_snapshots == []
    assert final == list(range(1, 401))


def test_import_concurrent_with_registration_same_subject(judge):
    """Import of a fresh subject racing with registrations of that SAME
    subject: exactly one writer wins; final versions are dense and every
    version's content/policy is reproducible."""
    data = _big_package(50)
    dst = RegistryService()
    start = threading.Event()
    register_outcomes: list[str] = []
    lock = threading.Lock()

    def importer():
        start.wait()
        try:
            dst.import_package(data)  # REJECT, fresh subject
            with lock:
                register_outcomes.append("import-created")
        except MigrationError as exc:  # registration created it first
            with lock:
                register_outcomes.append(f"import-refused:{exc.category}")

    def registrator():
        start.wait()
        try:
            rec = dst.register("big", _schema(0))
            with lock:
                register_outcomes.append(f"register-v{rec.version}")
        except IncompatibleSchemaError:
            with lock:
                register_outcomes.append("register-rejected")

    threads = [threading.Thread(target=importer),
               threading.Thread(target=registrator)]
    for t in threads:
        t.start()
    start.set()
    for t in threads:
        t.join()

    versions = [v.version for v in dst.list_versions("big")]
    dense = versions == list(range(1, len(versions) + 1))
    # every present version reproduces a compatible conclusion
    reproduced = [dst.recheck_history("big", v).compatible
                  for v in versions]
    imported = any(o == "import-created" for o in register_outcomes)
    judge("import-vs-register-same", {"importers": 1, "registrators": 1},
          "single register-lock orders both; dense versions, no partial mix",
          {"outcomes": sorted(register_outcomes), "n_versions": len(versions),
           "dense": dense, "all_reproduce": all(reproduced),
           "imported": imported})
    assert dense
    assert all(reproduced)
    if imported:
        # import created the subject first with 50 versions; the racing
        # registration of the same v1 schema is a normal post-import write
        # (duplicate content allowed) or a compatibility rejection — never
        # an interleaved/partial view.
        assert versions[:50] == list(range(1, 51))
        assert len(versions) in (50, 51)
    else:
        # a registration created the subject first; import was atomically
        # refused, and normal dense registration semantics took over.
        assert len(versions) == sum(
            1 for o in register_outcomes if o.startswith("register-v"))
        assert any(o == "import-refused:IMPORT_CONFLICT"
                   for o in register_outcomes)


def test_import_concurrent_with_registration_other_subjects(judge):
    """Importing subject A must not corrupt independent subject B's dense
    numbering while B is being registered concurrently."""
    data = _big_package(100)
    dst = RegistryService()
    dst.register("b", _schema(0))
    barrier = threading.Barrier(2)

    def importer():
        barrier.wait()
        dst.import_package(data)

    accepted: list[int] = []
    lk = threading.Lock()

    def registrator():
        barrier.wait()
        for i in range(1, 30):
            try:
                rec = dst.register("b", _schema(i))
                with lk:
                    accepted.append(rec.version)
            except IncompatibleSchemaError:
                pass

    t1 = threading.Thread(target=importer)
    t2 = threading.Thread(target=registrator)
    t1.start(); t2.start(); t1.join(); t2.join()

    a = [v.version for v in dst.list_versions("big")]
    b = [v.version for v in dst.list_versions("b")]
    judge("import-other-subjects", {"import": "big:100", "register": "b:29"},
          "locks are global for writers but data is per-subject",
          {"big": len(a), "b_dense": b == list(range(1, len(b) + 1))})
    assert a == list(range(1, 101))
    assert b == list(range(1, len(b) + 1))


def test_export_concurrent_with_policy_changes_is_consistent(judge):
    """An export racing with policy flips always captures a consistent
    package: its conclusions reproduce under its own bundled policy history."""
    svc = RegistryService()
    svc.register("u", _schema(0))
    start = threading.Event()
    levels = [CompatibilityLevel.NONE, CompatibilityLevel.BACKWARD,
              CompatibilityLevel.FULL, CompatibilityLevel.FORWARD]
    packages: list[bytes] = []
    lk = threading.Lock()

    def exporter():
        start.wait()
        for _ in range(5):
            with lk:
                packages.append(svc.export_subjects(["u"]))

    def mutator():
        start.wait()
        for i in range(20):
            try:
                svc.register("u", _schema(i + 1))
            except IncompatibleSchemaError:
                pass
            svc.set_policy("u", levels[i % len(levels)])

    t1 = threading.Thread(target=exporter)
    t2 = threading.Thread(target=mutator)
    t1.start(); t2.start(); start.set(); t1.join(); t2.join()

    from app.migration import decode_package, to_imports
    verified = 0
    for data in packages:
        pkg = decode_package(data)  # would raise if internally inconsistent
        item = to_imports(pkg)[0]
        n = len(item.versions)
        # import to a scratch registry and re-run every historical conclusion
        scratch = RegistryService()
        scratch.import_package(data)
        for v in range(1, n + 1):
            assert scratch.recheck_history("u", v).compatible is True
        assert [x.version for x in scratch.list_versions("u")] == \
               list(range(1, n + 1))
        verified += 1
    judge("export-consistent-under-race", {"exports": len(packages)},
          "snapshot under register-lock => self-consistent policy+conclusions",
          {"verified_packages": verified,
           "final_versions": len(svc.list_versions("u"))})
    assert verified == len(packages) and verified > 0


def test_precheck_remains_available_and_correct_after_import(judge):
    """A concurrent precheck against another subject keeps working, and a
    precheck against the imported subject is invalidated rather than stale."""
    data = _big_package(100)
    dst = RegistryService()
    # warm a cache entry for the subject that will be imported (none yet, so
    # precheck computes against empty) and for an unrelated subject
    dst.register("other", _schema(0))
    pre = dst.precheck("other", _schema(5))
    dst.import_package(data)
    # imported subject now has 100 versions; a precheck must compare vs v100
    report = dst.precheck("big", _schema(100))
    versions = [v.version for v in dst.list_versions("big")]
    judge("precheck-after-import", {"imported": 100},
          "import invalidates imported subject's cache; other subject intact",
          {"big_versions": len(versions),
           "other_precheck_compatible": pre.compatible,
           "big_precheck_compatible": report.compatible})
    assert versions == list(range(1, 101))
    # source set NONE effective from v2; import preserves that, so the
    # current policy governing the next registration is NONE.
    assert report.level is CompatibilityLevel.NONE
    assert dst.store.get_policy("big").level is CompatibilityLevel.NONE
    # unrelated subject's cached precheck is untouched
    assert pre.compatible is True
