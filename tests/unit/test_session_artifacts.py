from __future__ import annotations

import json
import struct
import zlib
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest

from sagasu.artifacts import atomic
from sagasu.artifacts.html import validate_html
from sagasu.artifacts.png import validate_png
from sagasu.protocol import SagasuError
from sagasu.sessions.artifacts import (
    save_action_sequence_screenshot,
    save_dom,
    save_screenshot,
)
from sagasu.sessions.executor import SessionExecutor
from sagasu.sessions.models import ResolvedSession


def png(width=2, height=3):
    def chunk(kind, data):
        crc = zlib.crc32(kind)
        crc = zlib.crc32(data, crc) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    scanlines = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(scanlines))
        + chunk(b"IEND", b"")
    )


class FakeDocker:
    def __init__(self, image=None, error=None):
        self.image = image if image is not None else png()
        self.error = error
        self.stream_names = []
        self.stream_arguments = []
        self.stream_inputs = []
        self.stream_json_kwargs = []

    def exec_stream(self, container_id, arguments, destination):
        self.stream_names.append(destination.name)
        self.stream_arguments.append((container_id, list(arguments)))
        destination.write(self.image)
        if self.error:
            raise self.error

    def exec_json(self, container_id, arguments):
        return {
            "ok": True,
            "operation": "cursor.move",
            "backend": "humancursor",
            "display": {"width": 100, "height": 80},
            "pointer": {"x": 10, "y": 20},
        }

    def exec_stream_json(
        self,
        container_id,
        arguments,
        destination,
        input_data=None,
        **kwargs,
    ):
        self.stream_names.append(destination.name)
        self.stream_arguments.append((container_id, list(arguments)))
        self.stream_inputs.append(input_data)
        self.stream_json_kwargs.append(kwargs)
        if arguments[0] == "sequence":
            destination.write(self.image)
            action_count = len(json.loads(input_data))
            return {
                "ok": True,
                "operation": "actions.sequence",
                "backend": "mixed",
                "display": {"width": 2, "height": 3},
                "pointer": {"x": 10, "y": 20},
                "completed": True,
                "action_count": action_count,
                "actions_completed": action_count,
                "settle_ms": 1_000,
                "pointer_included": "--no-pointer" not in arguments,
                "results": [
                    {
                        "ok": True,
                        "index": index,
                        "operation": "cursor.move",
                        "backend": "humancursor",
                        "display": {"width": 2, "height": 3},
                        "pointer": {"x": 10, "y": 20},
                    }
                    for index in range(action_count)
                ],
            }
        document = "<!doctype html><html><body>live</body></html>".encode()
        destination.write(document)
        return {
            "ok": True,
            "operation": "dom.fetch",
            "backend": "cdp",
            "display": {"width": 100, "height": 80},
            "pointer": {"x": 10, "y": 20},
            "target_id": "target-1",
            "title": "Live",
            "url": "https://example.test/",
            "bytes": len(document),
        }


SESSION = ResolvedSession(
    session_id="6f1c908d-2acc-4a1e-85f6-0f1b96857672",
    container_id="abc123",
    container_name="session",
)


def observation_failure_state(*, partial_failure=False):
    results = [
        {
            "ok": True,
            "index": index,
            "operation": "cursor.move",
            "backend": "humancursor",
            "display": {"width": 2, "height": 3},
            "pointer": {"x": 10, "y": 20},
        }
        for index in range(1 if partial_failure else 2)
    ]
    state = {
        "completed": not partial_failure,
        "action_count": 2,
        "actions_completed": len(results),
        "display": {"width": 2, "height": 3},
        "results": results,
        "settle_ms": 1_000,
        "settle_completed": True,
        "pointer_included": True,
        "screenshot_captured": False,
        "pointer_observed": False,
        "observation_stage": "screenshot",
    }
    if partial_failure:
        state.update(
            {
                "failed_index": 1,
                "failure": {
                    "code": "input_failed",
                    "message": "the second movement failed",
                    "details": {"backend": "humancursor"},
                },
            }
        )
    return state


def test_invoke_adds_host_authoritative_session_metadata():
    payload = SessionExecutor(FakeDocker(), SESSION).invoke(
        ["cursor", "move"]
    )
    assert payload["session_id"] == SESSION.session_id
    assert payload["container_id"] == SESSION.container_id
    assert payload["backend"] == "humancursor"


def test_screenshot_validates_and_atomically_publishes(tmp_path):
    docker = FakeDocker(png(7, 9))
    output = tmp_path / "screen.png"
    payload = save_screenshot(
        SessionExecutor(docker, SESSION),
        output,
        include_pointer=True,
        overwrite=False,
    )
    assert output.read_bytes() == png(7, 9)
    assert payload["display"] == {"width": 7, "height": 9}
    assert payload["pointer_included"] is True
    assert docker.stream_arguments == [
        ("abc123", ["screenshot"])
    ]
    assert not list(tmp_path.glob("*.tmp"))


def test_no_pointer_is_forwarded(tmp_path):
    docker = FakeDocker()
    save_screenshot(
        SessionExecutor(docker, SESSION),
        tmp_path / "screen.png",
        include_pointer=False,
        overwrite=False,
    )
    assert docker.stream_arguments[0][1] == ["screenshot", "--no-pointer"]


def test_sequence_screenshot_is_validated_and_published(tmp_path):
    docker = FakeDocker()
    output = tmp_path / "sequence.png"
    arguments = [
        "sequence",
    ]
    input_data = b'[{"operation":"cursor.move","x":1,"y":2}]'

    payload = save_action_sequence_screenshot(
        SessionExecutor(docker, SESSION),
        output,
        executor_arguments=arguments,
        executor_input=input_data,
        overwrite=False,
    )

    assert output.read_bytes() == png()
    assert payload["operation"] == "actions.sequence"
    assert payload["completed"] is True
    assert payload["output"] == str(output)
    assert payload["session_id"] == SESSION.session_id
    assert docker.stream_arguments == [("abc123", arguments)]
    assert docker.stream_inputs == [input_data]
    assert docker.stream_json_kwargs == [
        {
            "failure_code": "sequence_failed",
            "failure_message": "The in-container action sequence failed",
        }
    ]


def test_sequence_preexisting_destination_prevents_mutation(tmp_path):
    docker = FakeDocker()
    output = tmp_path / "sequence.png"
    output.write_bytes(b"foreign")

    with pytest.raises(SagasuError) as error:
        save_action_sequence_screenshot(
            SessionExecutor(docker, SESSION),
            output,
            executor_arguments=["sequence"],
            executor_input=b'[{"operation":"cursor.move","x":1,"y":2}]',
            overwrite=False,
        )

    assert error.value.code == "output_exists"
    assert docker.stream_arguments == []
    assert output.read_bytes() == b"foreign"
    assert not list(tmp_path.glob(f".{output.name}.*.tmp"))


def test_concurrent_sequences_reserve_before_mutation(tmp_path):
    first_mutation_started = Event()
    allow_first_to_finish = Event()

    class BlockingDocker(FakeDocker):
        def __init__(self):
            super().__init__()
            self.sequence_starts = 0

        def exec_stream_json(
            self, container_id, arguments, destination, **kwargs
        ):
            self.sequence_starts += 1
            first_mutation_started.set()
            if not allow_first_to_finish.wait(timeout=2):
                raise AssertionError("the first sequence was not released")
            return super().exec_stream_json(
                container_id, arguments, destination, **kwargs
            )

    docker = BlockingDocker()
    executor = SessionExecutor(docker, SESSION)
    output = tmp_path / "shared-sequence.png"

    def run_sequence():
        return save_action_sequence_screenshot(
            executor,
            output,
            executor_arguments=["sequence"],
            executor_input=b'[{"operation":"cursor.move","x":1,"y":2}]',
            overwrite=False,
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(run_sequence)
        assert first_mutation_started.wait(timeout=2)
        try:
            with pytest.raises(SagasuError) as error:
                run_sequence()
            assert error.value.code == "output_exists"
            # Only the reservation winner was allowed to enter Docker.
            assert docker.sequence_starts == 1
            assert output.read_bytes() == b""
        finally:
            allow_first_to_finish.set()
        assert first.result(timeout=2)["completed"] is True

    assert len(docker.stream_arguments) == 1
    assert output.read_bytes() == png()
    assert not list(tmp_path.glob(f".{output.name}.*.tmp"))


def test_sequence_reservation_does_not_require_hardlinks(
    monkeypatch, tmp_path
):
    def unsupported_hardlink(source, destination):
        del source, destination
        raise OSError("hard links are not supported")

    monkeypatch.setattr(atomic.os, "link", unsupported_hardlink)
    docker = FakeDocker()
    output = tmp_path / "sequence.png"

    payload = save_action_sequence_screenshot(
        SessionExecutor(docker, SESSION),
        output,
        executor_arguments=["sequence"],
        executor_input=b'[{"operation":"cursor.move","x":1,"y":2}]',
        overwrite=False,
    )

    assert payload["completed"] is True
    assert len(docker.stream_arguments) == 1
    assert output.read_bytes() == png()


def test_sequence_does_not_clobber_foreign_reservation_replacement(tmp_path):
    output = tmp_path / "sequence.png"

    class ReplacingDocker(FakeDocker):
        def exec_stream_json(
            self, container_id, arguments, destination, **kwargs
        ):
            # Simulate a non-cooperating publisher replacing our placeholder
            # while the browser mutation is in flight.
            output.unlink()
            output.write_bytes(b"foreign")
            return super().exec_stream_json(
                container_id, arguments, destination, **kwargs
            )

    docker = ReplacingDocker()
    with pytest.raises(SagasuError) as error:
        save_action_sequence_screenshot(
            SessionExecutor(docker, SESSION),
            output,
            executor_arguments=["sequence"],
            executor_input=b'[{"operation":"cursor.move","x":1,"y":2}]',
            overwrite=False,
        )

    assert error.value.code == "output_exists"
    assert len(docker.stream_arguments) == 1
    assert output.read_bytes() == b"foreign"
    assert not list(tmp_path.glob(f".{output.name}.*.tmp"))


def test_sequence_temporary_creation_failure_cleans_reservation_before_mutation(
    monkeypatch, tmp_path
):
    def fail_temporary_creation(**kwargs):
        del kwargs
        raise OSError("temporary creation failed")

    monkeypatch.setattr(
        atomic.tempfile, "NamedTemporaryFile", fail_temporary_creation
    )
    docker = FakeDocker()
    output = tmp_path / "sequence.png"

    with pytest.raises(SagasuError) as error:
        save_action_sequence_screenshot(
            SessionExecutor(docker, SESSION),
            output,
            executor_arguments=["sequence"],
            executor_input=b'[{"operation":"cursor.move","x":1,"y":2}]',
            overwrite=False,
        )

    assert error.value.code == "output_failed"
    assert docker.stream_arguments == []
    assert not output.exists()
    assert not list(tmp_path.glob(f".{output.name}.*.tmp"))


def test_failed_sequence_cleans_owned_reservation_and_temporary(tmp_path):
    class FailingDocker(FakeDocker):
        def exec_stream_json(
            self, container_id, arguments, destination, **kwargs
        ):
            self.stream_arguments.append((container_id, list(arguments)))
            destination.write(b"partial")
            raise SagasuError("sequence_failed", "mutation failed")

    docker = FailingDocker()
    output = tmp_path / "failed-sequence.png"

    with pytest.raises(SagasuError) as error:
        save_action_sequence_screenshot(
            SessionExecutor(docker, SESSION),
            output,
            executor_arguments=["sequence"],
            executor_input=b'[{"operation":"cursor.move","x":1,"y":2}]',
            overwrite=False,
        )

    assert error.value.code == "sequence_failed"
    assert len(docker.stream_arguments) == 1
    assert not output.exists()
    assert not list(tmp_path.glob(f".{output.name}.*.tmp"))


def test_sequence_accepts_explicit_pointer_observation_failure(tmp_path):
    class UnobservedPointerDocker(FakeDocker):
        def exec_stream_json(self, container_id, arguments, destination, **kwargs):
            payload = super().exec_stream_json(
                container_id, arguments, destination, **kwargs
            )
            payload["results"][0]["pointer"] = None
            payload["results"][0]["pointer_observation"] = {
                "ok": False,
                "error": {
                    "code": "input_failed",
                    "message": "pointer query failed",
                    "details": {"reason": "transient"},
                },
            }
            return payload

    output = tmp_path / "unobserved-pointer.png"
    payload = save_action_sequence_screenshot(
        SessionExecutor(UnobservedPointerDocker(), SESSION),
        output,
        executor_arguments=[
            "sequence",
        ],
        executor_input=b'[{"operation":"cursor.move","x":1,"y":2}]',
        overwrite=False,
    )

    assert payload["completed"] is True
    assert payload["actions_completed"] == 1
    assert payload["results"][0]["pointer"] is None
    assert output.read_bytes() == png()


def test_sequence_rejects_unstructured_pointer_observation_failure(tmp_path):
    class InvalidPointerDocker(FakeDocker):
        def exec_stream_json(self, container_id, arguments, destination, **kwargs):
            payload = super().exec_stream_json(
                container_id, arguments, destination, **kwargs
            )
            payload["results"][0]["pointer"] = None
            payload["results"][0]["pointer_observation"] = {"ok": True}
            return payload

    output = tmp_path / "invalid-pointer.png"
    with pytest.raises(SagasuError) as error:
        save_action_sequence_screenshot(
            SessionExecutor(InvalidPointerDocker(), SESSION),
            output,
            executor_arguments=[
                "sequence",
            ],
            executor_input=b'[{"operation":"cursor.move","x":1,"y":2}]',
            overwrite=False,
        )

    assert error.value.code == "invalid_response"
    assert not output.exists()


@pytest.mark.parametrize("partial_failure", [False, True])
def test_sequence_observation_failure_state_is_validated_and_preserved(
    tmp_path, partial_failure
):
    state = observation_failure_state(partial_failure=partial_failure)

    class ObservationFailureDocker(FakeDocker):
        def exec_stream_json(
            self, container_id, arguments, destination, **kwargs
        ):
            del container_id, arguments, kwargs
            destination.write(b"incomplete screenshot")
            raise SagasuError(
                "capture_failed",
                "the final screenshot failed",
                {"reason": "scrot exited", "sequence_state": state},
                exit_status=7,
            )

    output = tmp_path / "failed-observation.png"
    with pytest.raises(SagasuError) as error:
        save_action_sequence_screenshot(
            SessionExecutor(ObservationFailureDocker(), SESSION),
            output,
            executor_arguments=["sequence"],
            executor_input=(
                b'[{"operation":"cursor.move","x":1,"y":2},'
                b'{"operation":"cursor.move","x":3,"y":4}]'
            ),
            overwrite=False,
        )

    assert error.value.code == "capture_failed"
    assert error.value.exit_status == 7
    assert error.value.details["reason"] == "scrot exited"
    assert error.value.details["sequence_state"] == state
    assert error.value.details["session_id"] == SESSION.session_id
    assert error.value.details["container_id"] == SESSION.container_id
    assert not output.exists()


def test_sequence_observation_failure_rejects_invalid_action_state(tmp_path):
    state = observation_failure_state()
    state["actions_completed"] = 1

    class InvalidObservationFailureDocker(FakeDocker):
        def exec_stream_json(
            self, container_id, arguments, destination, **kwargs
        ):
            del container_id, arguments, destination, kwargs
            raise SagasuError(
                "capture_failed",
                "the final screenshot failed",
                {"sequence_state": state},
            )

    output = tmp_path / "invalid-observation.png"
    with pytest.raises(SagasuError) as error:
        save_action_sequence_screenshot(
            SessionExecutor(InvalidObservationFailureDocker(), SESSION),
            output,
            executor_arguments=["sequence"],
            executor_input=b"[]",
            overwrite=False,
        )

    assert error.value.code == "invalid_response"
    assert not output.exists()


def test_failed_sequence_keeps_diagnostic_screenshot(tmp_path):
    class FailedSequenceDocker(FakeDocker):
        def exec_stream_json(self, container_id, arguments, destination, **kwargs):
            payload = super().exec_stream_json(
                container_id, arguments, destination, **kwargs
            )
            payload.update(
                {
                    "completed": False,
                    "actions_completed": 0,
                    "results": [],
                    "failed_index": 0,
                    "failure": {
                        "code": "input_failed",
                        "message": "the movement failed",
                        "details": {"backend": "humancursor"},
                        "exit_status": 2,
                    },
                }
            )
            return payload

    output = tmp_path / "failed-sequence.png"
    arguments = [
        "sequence",
    ]
    input_data = b'[{"operation":"cursor.move","x":1,"y":2}]'
    with pytest.raises(SagasuError) as error:
        save_action_sequence_screenshot(
            SessionExecutor(FailedSequenceDocker(), SESSION),
            output,
            executor_arguments=arguments,
            executor_input=input_data,
            overwrite=False,
        )

    assert error.value.code == "input_failed"
    assert error.value.exit_status == 2
    assert error.value.details == {
        "backend": "humancursor",
        "output": str(output),
        "failed_index": 0,
        "actions_completed": 0,
        "action_count": 1,
    }
    assert output.read_bytes() == png()


def test_invalid_sequence_metadata_leaves_no_destination(tmp_path):
    class InvalidSequenceDocker(FakeDocker):
        def exec_stream_json(self, container_id, arguments, destination, **kwargs):
            payload = super().exec_stream_json(
                container_id, arguments, destination, **kwargs
            )
            payload["results"][0]["text"] = "must not cross the protocol"
            return payload

    output = tmp_path / "invalid-sequence.png"
    arguments = [
        "sequence",
    ]
    input_data = b'[{"operation":"cursor.move","x":1,"y":2}]'
    with pytest.raises(SagasuError) as error:
        save_action_sequence_screenshot(
            SessionExecutor(InvalidSequenceDocker(), SESSION),
            output,
            executor_arguments=arguments,
            executor_input=input_data,
            overwrite=False,
        )

    assert error.value.code == "invalid_response"
    assert not output.exists()


def test_existing_output_requires_overwrite(tmp_path):
    output = tmp_path / "screen.png"
    output.write_bytes(b"old")
    with pytest.raises(SagasuError) as error:
        save_screenshot(
            SessionExecutor(FakeDocker(), SESSION),
            output,
            include_pointer=True,
            overwrite=False,
        )
    assert error.value.code == "output_exists"
    assert output.read_bytes() == b"old"

    save_screenshot(
        SessionExecutor(FakeDocker(), SESSION),
        output,
        include_pointer=True,
        overwrite=True,
    )
    assert output.read_bytes().startswith(b"\x89PNG")


def test_failed_or_invalid_capture_leaves_no_partial_destination(tmp_path):
    for docker in (
        FakeDocker(image=b"not png"),
        FakeDocker(error=SagasuError("capture_failed", "boom")),
    ):
        output = tmp_path / f"{id(docker)}.png"
        with pytest.raises(SagasuError):
            save_screenshot(
                SessionExecutor(docker, SESSION),
                output,
                include_pointer=True,
                overwrite=False,
            )
        assert not output.exists()
        assert not list(tmp_path.glob(f".{output.name}.*.tmp"))


def test_concurrent_captures_use_unique_temporary_names(tmp_path):
    docker = FakeDocker()
    session_executor = SessionExecutor(docker, SESSION)

    def capture(number):
        return save_screenshot(
            session_executor,
            tmp_path / f"{number}.png",
            include_pointer=True,
            overwrite=False,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(capture, (1, 2)))
    assert len(set(docker.stream_names)) == 2


def test_concurrent_no_overwrite_publication_has_one_winner(tmp_path):
    barrier = Barrier(2)

    class BarrierDocker(FakeDocker):
        def exec_stream(self, container_id, arguments, destination):
            barrier.wait(timeout=2)
            super().exec_stream(container_id, arguments, destination)

    docker = BarrierDocker()
    session_executor = SessionExecutor(docker, SESSION)
    output = tmp_path / "shared.png"

    def capture():
        try:
            save_screenshot(
                session_executor,
                output,
                include_pointer=True,
                overwrite=False,
            )
            return "ok"
        except SagasuError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: capture(), (1, 2)))
    assert sorted(results) == ["ok", "output_exists"]
    assert output.read_bytes().startswith(b"\x89PNG")


def test_png_crc_is_validated(tmp_path):
    path = tmp_path / "bad.png"
    data = bytearray(png())
    data[-5] ^= 1
    path.write_bytes(data)
    with pytest.raises(SagasuError) as error:
        validate_png(path)
    assert error.value.code == "capture_failed"


def test_dom_validates_and_atomically_publishes(tmp_path):
    output = tmp_path / "page.html"
    payload = save_dom(
        SessionExecutor(FakeDocker(), SESSION),
        output,
        overwrite=False,
    )
    assert output.read_text() == (
        "<!doctype html><html><body>live</body></html>"
    )
    assert payload["operation"] == "dom.fetch"
    assert payload["backend"] == "cdp"
    assert payload["session_id"] == SESSION.session_id
    assert payload["container_id"] == SESSION.container_id
    assert payload["output"] == str(output)


def test_invalid_dom_metadata_leaves_no_destination(tmp_path):
    class InvalidMetadataDocker(FakeDocker):
        def exec_stream_json(
            self,
            container_id,
            arguments,
            destination,
            **kwargs,
        ):
            payload = super().exec_stream_json(
                container_id, arguments, destination, **kwargs
            )
            payload["bytes"] += 1
            return payload

    output = tmp_path / "page.html"
    with pytest.raises(SagasuError) as error:
        save_dom(
            SessionExecutor(InvalidMetadataDocker(), SESSION),
            output,
            overwrite=False,
        )
    assert error.value.code == "invalid_response"
    assert not output.exists()
    assert not list(tmp_path.glob(f".{output.name}.*.tmp"))


def test_invalid_dom_is_rejected(tmp_path):
    path = tmp_path / "bad.html"
    path.write_text("not a document")
    with pytest.raises(SagasuError) as error:
        validate_html(path)
    assert error.value.code == "dom_failed"
