import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import pytest
from e2e_harness import BusMaster, SlaveNode


@pytest.fixture
def make_slave():
    """
    Factory fixture that creates SlaveNodes and stops them all after the test.

    Usage:
        slave = make_slave()              # unassigned (0xFF), auto-started
        slave = make_slave(0x01)          # assigned addr, auto-started
        slave = make_slave(auto_start=False)  # caller calls slave.start()
    """
    nodes = []

    def _factory(assigned_addr=None, *, auto_start=True):
        slave = SlaveNode(assigned_addr)
        nodes.append(slave)
        if auto_start:
            slave.start()
        return slave

    yield _factory
    for node in nodes:
        node.stop()


@pytest.fixture
def make_bus(make_slave):
    """
    Factory fixture that creates a (BusMaster, SlaveNode) pair.
    Teardown is handled by make_slave.

    Usage:
        master, slave = make_bus()       # default addr 0x01
        master, slave = make_bus(0x02)   # custom addr
    """

    def _factory(assigned_addr=0x01):
        slave = make_slave(assigned_addr)
        return BusMaster(slave), slave

    return _factory


@pytest.fixture
def bus(make_bus):
    """Ready-to-use (BusMaster, SlaveNode) pair at addr 0x01."""
    return make_bus()
