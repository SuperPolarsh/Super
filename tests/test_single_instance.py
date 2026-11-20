import sys
import uuid

import app


def test_a_second_launch_cannot_claim_the_same_slot():
    if sys.platform != "win32":
        return  # the mutex guard is Windows-only
    name = f"Super.Test.{uuid.uuid4().hex}"
    assert app._acquire_single_instance(name) is True
    assert app._acquire_single_instance(name) is False


def test_different_slots_do_not_block_each_other():
    if sys.platform != "win32":
        return
    assert app._acquire_single_instance(f"Super.Test.{uuid.uuid4().hex}") is True
    assert app._acquire_single_instance(f"Super.Test.{uuid.uuid4().hex}") is True
