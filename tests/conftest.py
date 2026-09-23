"""Shared fixtures and logging.

Every test gets a logger that prints, for each judgment:
  * INPUT     - the schemas/policy fed in
  * BASIS     - what rule was consulted
  * CONCLUSION- compatible / rejected, and the categorized reasons
"""
from __future__ import annotations

import logging
import sys

import pytest

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)s [%(name)s] %(message)s"))
logging.getLogger("schema_registry").handlers[:] = [handler]
logging.getLogger("schema_registry").setLevel(logging.INFO)
logging.getLogger("schema_registry").propagate = False


def log_judgment(case: str, inp: object, basis: object, conclusion: object):
    lg = logging.getLogger("schema_registry.test")
    lg.info("CASE=%s | INPUT=%s", case, inp)
    lg.info("CASE=%s | BASIS=%s", case, basis)
    lg.info("CASE=%s | CONCLUSION=%s", case, conclusion)


@pytest.fixture
def judge():
    return log_judgment
