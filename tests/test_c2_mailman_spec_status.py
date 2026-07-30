from pathlib import Path


SPEC = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "C2_MAILMAN_CLEARINGHOUSE_SPEC.md"
)


def test_experiment_status_does_not_promote_durable_readback_to_acceptance():
    text = SPEC.read_text(encoding="utf-8")

    expected_status = (
        "**Protocol status.** Test 1 is not passed; "
        "Test 2 and Test 3 remain blocked."
    )
    assert expected_status in text
    assert "deployed Test 1 durable-readback subcase" in text
    assert "It is not full Test 1 acceptance" in text
    assert "deployed Test 1 acceptance" not in text
