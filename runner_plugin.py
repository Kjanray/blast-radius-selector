"""pytest plugin used by selector.py to run a chosen set of tests and record failures."""
import json
import os

import pytest

_failed = set()
_collect_failed = set()
_seen = set()
_wanted = None
_timeouts = {}


def pytest_configure(config):
    global _wanted, _timeouts
    w = os.environ.get("BR_WANTED")
    if w:
        with open(w) as fh:
            _wanted = set(x for x in fh.read().split("\n") if x)
    t = os.environ.get("BR_TIMEOUTS")
    if t:
        with open(t) as fh:
            _timeouts = json.load(fh)


def pytest_collection_modifyitems(session, config, items):
    if _wanted is None:
        return
    keep, drop = [], []
    for it in items:
        (keep if it.nodeid in _wanted else drop).append(it)
    if drop:
        config.hook.pytest_deselected(items=drop)
    items[:] = keep
    for it in keep:
        to = _timeouts.get(it.nodeid)
        if to:
            it.add_marker(pytest.mark.timeout(to))


def pytest_collectreport(report):
    if report.failed:
        _collect_failed.add(report.nodeid)


def pytest_runtest_logreport(report):
    _seen.add(report.nodeid)
    if report.failed:
        _failed.add(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    out = os.environ.get("BR_RES")
    if out:
        with open(out, "w") as fh:
            json.dump({"failed": sorted(_failed), "collect_failed": sorted(_collect_failed),
                       "seen": sorted(_seen), "exit": int(exitstatus)}, fh)


# ---- run process-pool work in-process (saves spawning workers that re-import everything)
import pickle
import sys
from concurrent.futures import Future


class InlineExecutor:
    def __init__(self, *a, initializer=None, initargs=(), **k):
        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, *args, **kwargs):
        f = Future()
        try:
            args, kwargs = pickle.loads(pickle.dumps((args, kwargs)))
            r = fn(*args, **kwargs)
            f.set_result(pickle.loads(pickle.dumps(r)))
        except BaseException as e:  # noqa: BLE001
            f.set_exception(e)
        return f

    def shutdown(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.shutdown()


def pytest_collection_finish(session):
    if os.environ.get("BR_INLINE", "1") != "1":
        return
    for name in ("tests.test_optimizer", "tests.test_executor"):
        m = sys.modules.get(name)
        if m is not None and hasattr(m, "ProcessPoolExecutor"):
            m.ProcessPoolExecutor = InlineExecutor


# ---- a test with a failing subtest fails as a whole: stop at the first failure instead of
# collecting (and formatting) every failing subtest.
import contextlib
import unittest


@contextlib.contextmanager
def _subtest(self, msg=None, **params):
    yield


if os.environ.get("BR_FASTSUB", "1") == "1":
    unittest.TestCase.subTest = _subtest
