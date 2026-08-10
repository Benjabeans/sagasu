from __future__ import annotations

import pytest

from sagasu.protocol import SagasuError


def test_error_payload_preserves_non_default_exit_status():
    payload = SagasuError(
        "invalid_arguments",
        "The arguments are invalid",
        exit_status=2,
    ).as_dict()

    assert payload["error"]["exit_status"] == 2
    assert SagasuError.from_payload(payload).exit_status == 2


def test_error_payload_keeps_default_wire_shape_backward_compatible():
    payload = SagasuError("input_failed", "The input failed").as_dict()

    assert payload == {
        "ok": False,
        "error": {
            "code": "input_failed",
            "message": "The input failed",
        },
    }
    assert SagasuError.from_payload(payload).exit_status == 1


@pytest.mark.parametrize(
    "exit_status",
    [True, False, 0, -1, 256, 2.0, "2", None],
)
def test_error_payload_rejects_invalid_exit_status(exit_status):
    error = SagasuError.from_payload(
        {
            "ok": False,
            "error": {
                "code": "invalid_arguments",
                "message": "The arguments are invalid",
                "exit_status": exit_status,
            },
        }
    )

    assert error.exit_status == 1
