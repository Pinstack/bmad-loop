"""Publication authority and byte preservation without involving receipt proof."""

import base64
import errno
import hashlib
import json
import os
import shutil
import stat
import sys
import time
import tracemalloc
from contextlib import contextmanager

import pytest
from conftest import git

from bmad_loop import artifact_publication as publication
from bmad_loop import platform_util
from bmad_loop.journal import save_state
from bmad_loop.model import RunState, StoryTask


def bind_and_prepare(task, paths, source, **limits):
    """Exercise the production sequence: arm, bind at acceptance, then freeze."""
    publication.arm_binding(task, "dev:0")
    publication.bind_armed(task, source, **limits)
    publication.prepare(task, paths, source, **limits)


@pytest.fixture
def publication_case(project, monkeypatch):
    source = project.rebased(project.project / "unit")
    source.implementation_artifacts.mkdir(parents=True)
    spec = source.implementation_artifacts / "spec.md"
    spec.write_text("---\nstatus: done\nartifact_deliverables: [report.bin]\n---\n")
    (source.implementation_artifacts / "report.bin").write_bytes(b"\xff\x00\r\nreport")
    task = StoryTask(story_key="dw-fix", epic=0, dw_ids=["DW-1"], spec_file=str(spec))
    monkeypatch.setattr(publication.verify, "path_ignored", lambda *_: True)
    monkeypatch.setattr(publication.verify, "path_tracked", lambda *_: False)
    publication.capture(task, project)
    return task, project, source


def test_exact_selection_and_frozen_binary_payload(publication_case):
    task, paths, source = publication_case
    (source.implementation_artifacts / "unrelated.md").write_text("unrelated")
    bind_and_prepare(task, paths, source)
    (source.implementation_artifacts / "report.bin").write_bytes(b"later source edit")
    publication.publish(task, paths)
    assert (paths.implementation_artifacts / "report.bin").read_bytes() == b"\xff\x00\r\nreport"
    assert (paths.implementation_artifacts / "spec.md").read_bytes() == (
        source.implementation_artifacts / "spec.md"
    ).read_bytes()
    assert not (paths.implementation_artifacts / "unrelated.md").exists()
    assert task.artifact_publication_complete


def test_binding_records_the_exact_ignored_selection_before_payload_freeze(publication_case):
    task, paths, source = publication_case

    publication.arm_binding(task, "dev:0")
    publication.bind_armed(task, source)

    assert task.artifact_acceptance_identity == "dev:0"
    assert task.artifact_source_digests == {
        "report.bin": publication._digest(b"\xff\x00\r\nreport"),
        "spec.md": publication._digest((source.implementation_artifacts / "spec.md").read_bytes()),
    }
    assert task.artifact_payload is None

    publication.prepare(task, paths, source)
    assert set(task.artifact_payload) == set(task.artifact_source_digests)


def test_changed_accepted_bytes_refuse_before_any_payload_is_encoded(publication_case, monkeypatch):
    task, paths, source = publication_case
    publication.arm_binding(task, "dev:0")
    publication.bind_armed(task, source)
    accepted = dict(task.artifact_source_digests)
    (source.implementation_artifacts / "report.bin").write_bytes(b"post-verify writer")
    monkeypatch.setattr(
        publication.base64,
        "b64encode",
        lambda _data: pytest.fail("payload encoding started before binding comparison"),
    )

    with pytest.raises(publication.PublicationError, match="report\\.bin") as exc:
        publication.prepare(task, paths, source)

    assert "post-verify writer" not in str(exc.value)
    assert accepted["report.bin"] not in str(exc.value)
    assert task.artifact_source_digests == accepted
    assert task.artifact_payload is None


def test_declaration_set_drift_is_an_exact_mapping_mismatch(publication_case, monkeypatch):
    task, paths, source = publication_case
    spec = source.implementation_artifacts / "spec.md"
    monkeypatch.setattr(
        publication.verify,
        "path_tracked",
        lambda _repo, rel: rel.endswith("spec.md"),
    )
    publication.arm_binding(task, "dev:0")
    publication.bind_armed(task, source)
    assert set(task.artifact_source_digests) == {"report.bin"}
    spec.write_text("---\nstatus: done\nartifact_deliverables: []\n---\n")

    with pytest.raises(publication.PublicationError, match="report\\.bin"):
        publication.prepare(task, paths, source)

    assert task.artifact_payload is None


def test_added_ignored_path_is_named_by_complete_map_refusal(publication_case, monkeypatch):
    task, paths, source = publication_case
    spec = source.implementation_artifacts / "spec.md"
    monkeypatch.setattr(
        publication.verify,
        "path_tracked",
        lambda _repo, rel: rel.endswith("spec.md"),
    )
    publication.arm_binding(task, "dev:0")
    publication.bind_armed(task, source)
    (source.implementation_artifacts / "added.bin").write_bytes(b"secret payload")
    spec.write_text("---\nstatus: done\nartifact_deliverables: [report.bin, added.bin]\n---\n")

    with pytest.raises(publication.PublicationError, match="added\\.bin") as exc:
        publication.prepare(task, paths, source)

    assert "report.bin" not in str(exc.value)
    assert "secret payload" not in str(exc.value)


def test_distinct_accepted_result_refreshes_binding_but_same_result_does_not(
    publication_case,
):
    task, paths, source = publication_case
    report = source.implementation_artifacts / "report.bin"
    publication.arm_binding(task, "dev:0")
    publication.bind_armed(task, source)
    first = dict(task.artifact_source_digests)
    report.write_bytes(b"accepted repair")

    assert publication.arm_binding(task, "dev:0") is False
    publication.bind_armed(task, source)
    assert task.artifact_source_digests == first
    with pytest.raises(publication.PublicationError, match="changed since accepted verification"):
        publication.prepare(task, paths, source)

    assert publication.arm_binding(task, "dev:1") is True
    publication.bind_armed(task, source)
    assert task.artifact_source_digests != first
    publication.prepare(task, paths, source)
    assert base64.b64decode(task.artifact_payload["report.bin"]) == b"accepted repair"


def test_default_per_file_limit_is_inclusive(publication_case):
    task, _paths, source = publication_case
    data = b"x" * publication.DEFAULT_FILE_MAX_BYTES
    (source.implementation_artifacts / "report.bin").write_bytes(data)

    bind_and_prepare(task, _paths, source)

    assert base64.b64decode(task.artifact_payload["report.bin"]) == data


def test_default_per_file_limit_plus_one_refuses_before_encoding(publication_case, monkeypatch):
    task, paths, source = publication_case
    report = source.implementation_artifacts / "report.bin"
    report.write_bytes(b"x" * (publication.DEFAULT_FILE_MAX_BYTES + 1))
    publication.arm_binding(task, "dev:0")
    publication.bind_armed(
        task,
        source,
        file_max_bytes=publication.DEFAULT_FILE_MAX_BYTES + 1,
    )
    encoded = []
    monkeypatch.setattr(publication.base64, "b64encode", lambda data: encoded.append(data))

    with pytest.raises(publication.PublicationSizeError) as exc:
        publication.prepare(task, paths, source)

    assert exc.value.cause == "file-limit"
    assert exc.value.measured_bytes == publication.DEFAULT_FILE_MAX_BYTES + 1
    assert exc.value.limit_bytes == publication.DEFAULT_FILE_MAX_BYTES
    assert encoded == []
    assert task.artifact_payload is None


def test_implicit_spec_preliminary_read_obeys_smaller_aggregate_limit(publication_case):
    task, paths, source = publication_case
    spec = source.implementation_artifacts / "spec.md"
    spec.write_bytes(b"---\nstatus: done\n---\n" + b"x" * 200)
    publication.arm_binding(task, "dev:0")
    publication.bind_armed(task, source)

    with pytest.raises(publication.PublicationSizeError) as exc:
        publication.prepare(task, paths, source, file_max_bytes=300, payload_max_bytes=100)

    assert exc.value.cause == "payload-limit"
    assert exc.value.measured_bytes == 101
    assert exc.value.limit_bytes == 100
    assert task.artifact_payload is None


def test_extreme_positive_limits_use_fixed_size_read_requests(publication_case):
    task, paths, source = publication_case
    extreme_legal_limit = sys.maxsize * 1_048_576

    bind_and_prepare(
        task,
        paths,
        source,
        file_max_bytes=extreme_legal_limit,
        payload_max_bytes=extreme_legal_limit,
    )

    assert base64.b64decode(task.artifact_payload["report.bin"]) == b"\xff\x00\r\nreport"


@pytest.mark.parametrize("cause", ["file-limit", "payload-limit"])
def test_metadata_preflight_refuses_before_any_payload_read(publication_case, monkeypatch, cause):
    task, paths, source = publication_case
    spec = source.implementation_artifacts / "spec.md"
    if cause == "file-limit":
        (source.implementation_artifacts / "report.bin").write_bytes(
            b"x" * (publication.DEFAULT_FILE_MAX_BYTES + 1)
        )
    else:
        spec.write_text("---\nstatus: done\nartifact_deliverables: [a.bin, z.bin]\n---\n")
        (source.implementation_artifacts / "a.bin").write_bytes(
            b"a" * publication.DEFAULT_FILE_MAX_BYTES
        )
        second = (
            publication.DEFAULT_PAYLOAD_MAX_BYTES
            - len(spec.read_bytes())
            - publication.DEFAULT_FILE_MAX_BYTES
        )
        (source.implementation_artifacts / "z.bin").write_bytes(b"z" * (second + 1))
    publication.arm_binding(task, "dev:0")
    publication.bind_armed(
        task,
        source,
        file_max_bytes=(
            publication.DEFAULT_FILE_MAX_BYTES + 1
            if cause == "file-limit"
            else publication.DEFAULT_FILE_MAX_BYTES
        ),
        payload_max_bytes=(
            publication.DEFAULT_PAYLOAD_MAX_BYTES + 1
            if cause == "payload-limit"
            else publication.DEFAULT_PAYLOAD_MAX_BYTES
        ),
    )
    read = publication._contents
    preliminary_reads = 0

    def reject_payload_read(root, path, **kwargs):
        nonlocal preliminary_reads
        if path == spec and preliminary_reads == 0:
            preliminary_reads += 1
            return read(root, path, **kwargs)
        pytest.fail(f"payload read started before {cause} metadata preflight completed: {path}")

    monkeypatch.setattr(publication, "_contents", reject_payload_read)

    with pytest.raises(publication.PublicationSizeError) as exc:
        publication.prepare(task, paths, source)

    assert exc.value.cause == cause
    assert preliminary_reads == 1
    assert task.artifact_payload is None


@pytest.mark.parametrize("over", [0, 1], ids=["exact", "plus-one"])
def test_default_aggregate_limit_counts_unique_ignored_inputs(publication_case, monkeypatch, over):
    task, paths, source = publication_case
    spec = source.implementation_artifacts / "spec.md"
    spec.write_text(
        "---\nstatus: done\n" "artifact_deliverables: [spec.md, a.bin, z.bin, spec.md]\n---\n"
    )
    first = publication.DEFAULT_FILE_MAX_BYTES
    last = publication.DEFAULT_PAYLOAD_MAX_BYTES - first - len(spec.read_bytes()) + over
    (source.implementation_artifacts / "a.bin").write_bytes(b"a" * first)
    (source.implementation_artifacts / "z.bin").write_bytes(b"z" * last)
    encoded = []
    original_encode = publication.base64.b64encode

    def record_encode(data):
        encoded.append(len(data))
        return original_encode(data)

    publication.arm_binding(task, "dev:0")
    publication.bind_armed(
        task,
        source,
        payload_max_bytes=publication.DEFAULT_PAYLOAD_MAX_BYTES + over,
    )
    monkeypatch.setattr(publication.base64, "b64encode", record_encode)
    if over:
        with pytest.raises(publication.PublicationSizeError) as exc:
            publication.prepare(task, paths, source)
        assert exc.value.cause == "payload-limit"
        assert exc.value.measured_bytes == publication.DEFAULT_PAYLOAD_MAX_BYTES + 1
        assert encoded == []
        assert task.artifact_payload is None
    else:
        publication.prepare(task, paths, source)
        assert sum(len(base64.b64decode(value)) for value in task.artifact_payload.values()) == (
            publication.DEFAULT_PAYLOAD_MAX_BYTES
        )
        assert len(encoded) == 3  # duplicate spec declarations count once


def test_tracked_oversize_declaration_does_not_consume_payload_budget(
    publication_case, monkeypatch
):
    task, paths, source = publication_case
    report = source.implementation_artifacts / "report.bin"
    report.write_bytes(b"x" * (publication.DEFAULT_FILE_MAX_BYTES + 1))
    monkeypatch.setattr(
        publication.verify,
        "path_tracked",
        lambda _repo, rel: rel.endswith("report.bin"),
    )

    bind_and_prepare(task, paths, source)

    assert set(task.artifact_payload) == {"spec.md"}


def test_tracked_oversize_implicit_spec_does_not_consume_payload_budget(
    publication_case, monkeypatch
):
    task, paths, source = publication_case
    spec = source.implementation_artifacts / "spec.md"
    spec.write_bytes(
        b"---\nstatus: done\nartifact_deliverables: [report.bin]\n---\n"
        + b"x" * publication.DEFAULT_FILE_MAX_BYTES
    )
    monkeypatch.setattr(
        publication.verify,
        "path_tracked",
        lambda _repo, rel: rel.endswith("spec.md"),
    )

    bind_and_prepare(task, paths, source)

    assert set(task.artifact_payload) == {"report.bin"}


@pytest.mark.parametrize("cause", ["file-limit", "payload-limit"])
def test_growth_after_preflight_is_bounded_and_nothing_is_encoded(
    publication_case, monkeypatch, cause
):
    task, paths, source = publication_case
    spec = source.implementation_artifacts / "spec.md"
    report = source.implementation_artifacts / "report.bin"
    if cause == "file-limit":
        report.write_bytes(b"x")
    else:
        spec.write_text("---\nstatus: done\nartifact_deliverables: [a.bin, z.bin]\n---\n")
        (source.implementation_artifacts / "a.bin").write_bytes(
            b"a" * publication.DEFAULT_FILE_MAX_BYTES
        )
        remaining = (
            publication.DEFAULT_PAYLOAD_MAX_BYTES
            - publication.DEFAULT_FILE_MAX_BYTES
            - len(spec.read_bytes())
            - 1
        )
        report = source.implementation_artifacts / "z.bin"
        report.write_bytes(b"z" * remaining)
    publication.arm_binding(task, "dev:0")
    publication.bind_armed(task, source)
    measured = publication._file_size
    grew = False

    def grow_after_measurement(root, path):
        nonlocal grew
        size = measured(root, path)
        if path == report and not grew:
            grew = True
            if cause == "file-limit":
                path.write_bytes(b"x" * (publication.DEFAULT_FILE_MAX_BYTES + 50_000))
            else:
                with path.open("ab") as stream:
                    stream.write(b"zz")
        return size

    monkeypatch.setattr(publication, "_file_size", grow_after_measurement)
    monkeypatch.setattr(
        publication.base64,
        "b64encode",
        lambda _data: pytest.fail("encoding started before every bounded read passed"),
    )

    with pytest.raises(publication.PublicationSizeError) as exc:
        publication.prepare(task, paths, source)

    assert exc.value.cause == cause
    assert exc.value.measured_bytes == exc.value.limit_bytes + 1
    assert exc.value.measurement_is_lower_bound is True
    assert task.artifact_payload is None


def test_legacy_frozen_oversize_payload_still_publishes(publication_case):
    task, paths, _source = publication_case
    intended = b"x" * (publication.DEFAULT_FILE_MAX_BYTES + 1)
    task.artifact_payload = {"report.bin": base64.b64encode(intended).decode("ascii")}

    publication.publish(task, paths)

    assert (paths.implementation_artifacts / "report.bin").read_bytes() == intended
    assert task.artifact_publication_complete


def _ten_mib_payload_five_save_capacity_envelope(tmp_path):
    raw_size = publication.DEFAULT_PAYLOAD_MAX_BYTES
    started_tracing = not tracemalloc.is_tracing()
    if started_tracing:
        tracemalloc.start()
    started = time.perf_counter()
    peak = None
    try:
        raw = b"x" * raw_size
        encoded = base64.b64encode(raw).decode("ascii")
        task = StoryTask(story_key="dw-capacity", epic=0, artifact_payload={"bundle.bin": encoded})
        state = RunState(
            run_id="capacity",
            project=str(tmp_path),
            started_at="now",
            tasks={task.story_key: task},
        )
        run_dir = tmp_path / "run"
        for _ in range(5):
            save_state(run_dir, state)
        elapsed = time.perf_counter() - started
        if started_tracing:
            _, peak = tracemalloc.get_traced_memory()
    finally:
        if started_tracing:
            tracemalloc.stop()

    structural_base64 = 4 * ((raw_size + 2) // 3)
    assert len(encoded) == structural_base64
    state_bytes = (run_dir / "state.json").read_bytes()
    assert len(state_bytes) <= structural_base64 + 64 * 1024
    assert json.loads(state_bytes)["tasks"]["dw-capacity"]["artifact_payload"]["bundle.bin"] == (
        encoded
    )
    if peak is not None:
        assert peak < 160 * 1_048_576
    assert elapsed < 20


def test_ten_mib_payload_five_save_capacity_envelope(tmp_path):
    _ten_mib_payload_five_save_capacity_envelope(tmp_path)


def test_capacity_envelope_preserves_an_existing_tracemalloc_session(tmp_path):
    was_tracing = tracemalloc.is_tracing()
    if not was_tracing:
        tracemalloc.start()
    try:
        _ten_mib_payload_five_save_capacity_envelope(tmp_path)
        assert tracemalloc.is_tracing()
    finally:
        if not was_tracing:
            tracemalloc.stop()


def test_late_declaration_does_not_capture_late_destination(publication_case):
    task, paths, source = publication_case
    destination = paths.implementation_artifacts / "report.bin"
    destination.write_bytes(b"operator")
    bind_and_prepare(task, paths, source)
    with pytest.raises(publication.PublicationError, match="conflict.*report.bin"):
        publication.publish(task, paths)
    assert destination.read_bytes() == b"operator"
    assert not task.artifact_publication_complete
    assert task.artifact_payload is not None


def test_large_destination_baseline_is_streamed_without_contents_materialization(
    publication_case, monkeypatch
):
    task, paths, _source = publication_case
    destination = paths.implementation_artifacts / "unrelated-large.bin"
    block = b"baseline" * 8192
    digest = hashlib.sha256()
    with destination.open("wb") as stream:
        for _ in range(192):
            stream.write(block)
            digest.update(block)
    directory = paths.implementation_artifacts / "baseline-directory"
    directory.mkdir()
    link = paths.implementation_artifacts / "baseline-link"
    link.symlink_to(destination)

    monkeypatch.setattr(
        publication,
        "_contents",
        lambda *_args, **_kwargs: pytest.fail("capture materialized destination contents"),
    )
    started_tracing = not tracemalloc.is_tracing()
    if started_tracing:
        tracemalloc.start()
    traced_before = tracemalloc.get_traced_memory()[0]
    try:
        tracemalloc.reset_peak()
        publication.capture(task, paths)
        peak_growth = tracemalloc.get_traced_memory()[1] - traced_before
    finally:
        if started_tracing:
            tracemalloc.stop()

    assert task.artifact_baseline["unrelated-large.bin"] == digest.hexdigest()
    assert task.artifact_baseline["baseline-directory"] == "directory"
    assert task.artifact_baseline["baseline-link"] == "nonregular"
    assert peak_growth < 2 * 1_048_576


def test_destination_helpers_use_size_first_fixed_chunk_reads(publication_case, monkeypatch):
    _task, paths, _source = publication_case
    destination = paths.implementation_artifacts / "bounded.bin"
    expected = b"a" * 96
    destination.write_bytes(expected)
    chunk_size = 32
    requests = []
    open_regular = publication._open_regular

    class GuardedStream:
        def __init__(self, stream):
            self.stream = stream

        def fileno(self):
            return self.stream.fileno()

        def read(self, size=-1):
            requests.append(size)
            assert 0 < size <= chunk_size
            return self.stream.read(size)

    @contextmanager
    def guarded_open(root, path):
        with open_regular(root, path) as stream:
            yield None if stream is None else GuardedStream(stream)

    monkeypatch.setattr(publication, "_BOUNDED_READ_CHUNK_BYTES", chunk_size)
    monkeypatch.setattr(publication, "_open_regular", guarded_open)
    monkeypatch.setattr(
        publication,
        "_contents",
        lambda *_args, **_kwargs: pytest.fail("destination helper called _contents"),
    )

    observed = publication._destination_observation(paths.implementation_artifacts, destination)
    assert observed == publication._DestinationObservation(
        size=len(expected), digest=hashlib.sha256(expected).hexdigest()
    )
    assert requests == [chunk_size, chunk_size, chunk_size, chunk_size]

    requests.clear()
    assert publication._destination_equals(paths.implementation_artifacts, destination, expected)
    assert requests == [chunk_size, chunk_size, chunk_size, chunk_size]

    empty = paths.implementation_artifacts / "empty.bin"
    empty.write_bytes(b"")
    requests.clear()
    assert publication._destination_equals(paths.implementation_artifacts, empty, b"")
    assert requests == [chunk_size]

    requests.clear()
    assert not publication._destination_equals(
        paths.implementation_artifacts, destination, expected + b"x"
    )
    assert requests == []

    requests.clear()
    assert not publication._destination_equals(
        paths.implementation_artifacts, destination, b"z" + expected[1:]
    )
    assert requests == [chunk_size]


def test_destination_observation_stops_after_one_growth_chunk(publication_case, monkeypatch):
    _task, paths, _source = publication_case
    destination = paths.implementation_artifacts / "continuous-growth.bin"
    destination.write_bytes(b"x")
    open_regular = publication._open_regular
    requests = []

    class GrowingStream:
        def __init__(self, stream):
            self.stream = stream

        def fileno(self):
            return self.stream.fileno()

        def read(self, size=-1):
            requests.append(size)
            return b"g" * size

    @contextmanager
    def growing_open(root, path):
        with open_regular(root, path) as stream:
            yield None if stream is None else GrowingStream(stream)

    monkeypatch.setattr(publication, "_open_regular", growing_open)

    observed = publication._destination_observation(paths.implementation_artifacts, destination)

    assert observed is not None
    assert not observed.complete
    assert requests == [1, publication._BOUNDED_READ_CHUNK_BYTES]


def test_capture_refuses_incomplete_destination_observation(publication_case, monkeypatch):
    task, paths, _source = publication_case
    destination = paths.implementation_artifacts / "growing-capture.bin"
    destination.write_bytes(b"x")
    task.artifact_baseline = None
    open_regular = publication._open_regular

    class GrowingStream:
        def __init__(self, stream):
            self.stream = stream

        def fileno(self):
            return self.stream.fileno()

        def read(self, size=-1):
            return b"g" * size

    @contextmanager
    def growing_open(root, path):
        with open_regular(root, path) as stream:
            if stream is not None and path == destination:
                yield GrowingStream(stream)
            else:
                yield stream

    monkeypatch.setattr(publication, "_open_regular", growing_open)

    with pytest.raises(publication.PublicationError, match="changed during inventory"):
        publication.capture(task, paths)

    assert task.artifact_baseline is None


def test_capture_refuses_destination_truncated_before_first_read(publication_case, monkeypatch):
    task, paths, _source = publication_case
    destination = paths.implementation_artifacts / "truncated-capture.bin"
    destination.write_bytes(b"operator baseline")
    task.artifact_baseline = None
    open_regular = publication._open_regular
    mutated = False

    class TruncatingStream:
        def __init__(self, stream):
            self.stream = stream

        def fileno(self):
            return self.stream.fileno()

        def read(self, size=-1):
            nonlocal mutated
            if not mutated:
                mutated = True
                destination.write_bytes(b"")
            return self.stream.read(size)

    @contextmanager
    def truncating_open(root, path):
        with open_regular(root, path) as stream:
            if stream is not None and path == destination:
                yield TruncatingStream(stream)
            else:
                yield stream

    monkeypatch.setattr(publication, "_open_regular", truncating_open)

    with pytest.raises(publication.PublicationError, match="changed during inventory"):
        publication.capture(task, paths)

    assert mutated
    assert task.artifact_baseline is None


def test_large_exact_destination_publication_has_bounded_extra_allocation(
    publication_case, monkeypatch
):
    task, paths, _source = publication_case
    intended = b"visible" * (2 * 1_048_576)
    destination = paths.implementation_artifacts / "report.bin"
    destination.write_bytes(b"before")
    publication.capture(task, paths)
    task.artifact_payload = {"report.bin": "frozen-large-payload"}
    monkeypatch.setattr(publication.base64, "b64decode", lambda *_args, **_kwargs: intended)
    started_tracing = not tracemalloc.is_tracing()
    if started_tracing:
        tracemalloc.start()
    traced_before = tracemalloc.get_traced_memory()[0]
    try:
        tracemalloc.reset_peak()
        publication.publish(task, paths)
        peak_growth = tracemalloc.get_traced_memory()[1] - traced_before
    finally:
        if started_tracing:
            tracemalloc.stop()

    assert task.artifact_publication_complete
    assert peak_growth < 2 * 1_048_576


@pytest.mark.parametrize("case", ["authorized", "conflict"])
def test_large_baseline_publication_decisions_have_bounded_extra_allocation(publication_case, case):
    task, paths, source = publication_case
    destination = paths.implementation_artifacts / "report.bin"
    large = b"operator" * (2 * 1_048_576)
    if case == "authorized":
        destination.write_bytes(large)
        publication.capture(task, paths)
    bind_and_prepare(task, paths, source)
    if case == "conflict":
        destination.write_bytes(large)
    started_tracing = not tracemalloc.is_tracing()
    if started_tracing:
        tracemalloc.start()
    traced_before = tracemalloc.get_traced_memory()[0]
    try:
        tracemalloc.reset_peak()
        if case == "conflict":
            with pytest.raises(publication.PublicationError, match="destination conflict"):
                publication.publish(task, paths)
        else:
            publication.publish(task, paths)
        peak_growth = tracemalloc.get_traced_memory()[1] - traced_before
    finally:
        if started_tracing:
            tracemalloc.stop()

    if case == "authorized":
        assert destination.read_bytes() == b"\xff\x00\r\nreport"
        assert task.artifact_publication_complete
    else:
        assert destination.read_bytes() == large
        assert not task.artifact_publication_complete
    assert peak_growth < 2 * 1_048_576


@pytest.mark.parametrize("fallback", [False, True], ids=["descriptor", "fallback"])
@pytest.mark.parametrize("kind", ["missing", "directory", "symlink", "parent-symlink"])
def test_destination_streaming_preserves_shape_checks(
    publication_case, monkeypatch, fallback, kind
):
    _task, paths, source = publication_case
    if not fallback and not publication.DIR_FD_ANCHORED_WRITES:
        pytest.skip("descriptor-relative reads are unavailable")
    if fallback:
        monkeypatch.setattr(publication, "DIR_FD_ANCHORED_WRITES", False)
    root = paths.implementation_artifacts
    destination = root / "shape.bin"
    if kind == "directory":
        destination.mkdir()
    elif kind == "symlink":
        destination.symlink_to(source.implementation_artifacts / "report.bin")
    elif kind == "parent-symlink":
        outside = paths.project / "outside-shape"
        outside.mkdir()
        (outside / "shape.bin").write_bytes(b"outside")
        linked = root / "linked"
        linked.symlink_to(outside, target_is_directory=True)
        destination = linked / "shape.bin"

    if kind == "missing":
        assert publication._destination_observation(root, destination) is None
        assert not publication._destination_equals(root, destination, b"")
    else:
        with pytest.raises(publication.PublicationError, match="symlink|regular file"):
            publication._destination_observation(root, destination)
        with pytest.raises(publication.PublicationError, match="symlink|regular file"):
            publication._destination_equals(root, destination, b"outside")


def test_destination_streaming_propagates_read_fault(publication_case, monkeypatch):
    _task, paths, _source = publication_case
    destination = paths.implementation_artifacts / "fault.bin"
    destination.write_bytes(b"expected")
    open_regular = publication._open_regular

    class FaultingStream:
        def __init__(self, stream):
            self.stream = stream

        def fileno(self):
            return self.stream.fileno()

        def read(self, _size=-1):
            raise OSError("destination read fault")

    @contextmanager
    def faulting_open(root, path):
        with open_regular(root, path) as stream:
            yield None if stream is None else FaultingStream(stream)

    monkeypatch.setattr(publication, "_open_regular", faulting_open)

    with pytest.raises(OSError, match="destination read fault"):
        publication._destination_observation(paths.implementation_artifacts, destination)
    with pytest.raises(OSError, match="destination read fault"):
        publication._destination_equals(paths.implementation_artifacts, destination, b"expected")


@pytest.mark.parametrize("fallback", [False, True], ids=["descriptor", "fallback"])
@pytest.mark.parametrize("helper", ["observation", "equals"])
@pytest.mark.parametrize("replacement", ["missing", "directory", "symlink"])
def test_destination_streaming_rejects_detached_descriptor_shape(
    publication_case, monkeypatch, fallback, helper, replacement
):
    _task, paths, _source = publication_case
    if sys.platform == "win32":
        pytest.skip("Windows refuses rename of an open destination")
    if not fallback and not publication.DIR_FD_ANCHORED_WRITES:
        pytest.skip("descriptor-relative reads are unavailable")
    if fallback:
        monkeypatch.setattr(publication, "DIR_FD_ANCHORED_WRITES", False)
    root = paths.implementation_artifacts
    destination = root / "detached.bin"
    detached = root / "detached-old.bin"
    expected = b"expected"
    destination.write_bytes(expected)
    open_regular = publication._open_regular
    replaced = False

    class ReplacingStream:
        def __init__(self, stream):
            self.stream = stream

        def fileno(self):
            return self.stream.fileno()

        def read(self, size=-1):
            nonlocal replaced
            data = self.stream.read(size)
            if not data and not replaced:
                replaced = True
                destination.rename(detached)
                if replacement == "directory":
                    destination.mkdir()
                elif replacement == "symlink":
                    destination.symlink_to(detached)
            return data

    @contextmanager
    def replacing_open(root, path):
        with open_regular(root, path) as stream:
            yield None if stream is None else ReplacingStream(stream)

    monkeypatch.setattr(publication, "_open_regular", replacing_open)

    if helper == "observation":
        observed = publication._destination_observation(root, destination)
        assert observed is not None
        assert not observed.complete
    else:
        assert not publication._destination_equals(root, destination, expected)
    assert replaced
    assert detached.read_bytes() == expected


@pytest.mark.parametrize("helper", ["observation", "equals"])
def test_destination_streaming_propagates_identity_fault(publication_case, monkeypatch, helper):
    _task, paths, _source = publication_case
    root = paths.implementation_artifacts
    destination = root / "identity-fault.bin"
    expected = b"expected"
    destination.write_bytes(expected)

    def faulting_identity(*_args):
        raise OSError("destination identity fault")

    monkeypatch.setattr(publication, "_destination_path_identity", faulting_identity)

    with pytest.raises(OSError, match="destination identity fault"):
        if helper == "observation":
            publication._destination_observation(root, destination)
        else:
            publication._destination_equals(root, destination, expected)


@pytest.mark.parametrize("inode_available", [True, False], ids=["zero", "unavailable"])
def test_destination_streaming_rejects_indeterminate_inode(
    publication_case, monkeypatch, inode_available
):
    _task, paths, _source = publication_case
    root = paths.implementation_artifacts
    destination = root / "indeterminate-inode.bin"
    expected = b"expected"
    destination.write_bytes(expected)
    real_fstat = os.fstat

    class IndeterminateInode:
        def __init__(self, metadata):
            self.st_dev = metadata.st_dev
            self.st_mode = metadata.st_mode
            self.st_size = metadata.st_size
            if inode_available:
                self.st_ino = 0

    def indeterminate_leaf(fd):
        # Only the destination leaf is indeterminate: a directory fstat is the
        # confined walk's root pin (DW-338), which refuses an identity-less root
        # outright rather than reporting an incomplete observation.
        metadata = real_fstat(fd)
        return IndeterminateInode(metadata) if stat.S_ISREG(metadata.st_mode) else metadata

    monkeypatch.setattr(os, "fstat", indeterminate_leaf)
    monkeypatch.setattr(
        publication,
        "_destination_path_identity",
        lambda *_args: publication._file_identity(IndeterminateInode(destination.stat())),
    )

    observed = publication._destination_observation(root, destination)
    assert observed is not None
    assert not observed.complete
    assert not publication._destination_equals(root, destination, expected)


@pytest.mark.parametrize("case", ["idempotent", "authorized", "conflict"])
def test_publication_destination_decisions_never_materialize_contents(
    publication_case, monkeypatch, case
):
    task, paths, source = publication_case
    destination = paths.implementation_artifacts / "report.bin"
    if case == "authorized":
        destination.write_bytes(b"before")
        publication.capture(task, paths)
    bind_and_prepare(task, paths, source)
    if case == "idempotent":
        destination.write_bytes(b"\xff\x00\r\nreport")
    elif case == "conflict":
        destination.write_bytes(b"operator")
    monkeypatch.setattr(
        publication,
        "_contents",
        lambda *_args, **_kwargs: pytest.fail("publish materialized destination contents"),
    )

    if case == "conflict":
        with pytest.raises(publication.PublicationError, match="conflict.*report.bin"):
            publication.publish(task, paths)
        assert destination.read_bytes() == b"operator"
        assert not task.artifact_publication_complete
    else:
        publication.publish(task, paths)
        assert destination.read_bytes() == b"\xff\x00\r\nreport"
        assert task.artifact_publication_complete


@pytest.mark.parametrize("mutation", ["grow", "shrink"])
def test_initial_idempotence_probe_refuses_file_mutation(publication_case, monkeypatch, mutation):
    task, paths, source = publication_case
    report = source.implementation_artifacts / "report.bin"
    intended = b"i" * (publication._BOUNDED_READ_CHUNK_BYTES * 2)
    report.write_bytes(intended)
    bind_and_prepare(task, paths, source)
    destination = paths.implementation_artifacts / "report.bin"
    destination.write_bytes(intended)
    open_regular = publication._open_regular
    mutated = False

    class MutatingStream:
        def __init__(self, stream):
            self.stream = stream

        def fileno(self):
            return self.stream.fileno()

        def read(self, size=-1):
            nonlocal mutated
            data = self.stream.read(size)
            if data and not mutated:
                mutated = True
                if mutation == "grow":
                    with destination.open("ab") as writer:
                        writer.write(b"operator growth")
                else:
                    with destination.open("r+b") as writer:
                        writer.truncate(0)
            return data

    @contextmanager
    def mutating_open(root, path):
        with open_regular(root, path) as stream:
            if stream is not None and path == destination:
                yield MutatingStream(stream)
            else:
                yield stream

    monkeypatch.setattr(publication, "_open_regular", mutating_open)
    monkeypatch.setattr(
        publication,
        "atomic_write_bytes_confined",
        lambda *_args, **_kwargs: pytest.fail("unstable destination reached replacement"),
    )

    with pytest.raises(publication.PublicationError, match="destination conflict"):
        publication.publish(task, paths)

    assert mutated
    assert destination.read_bytes() != intended
    assert not task.artifact_publication_complete


@pytest.mark.parametrize("fallback", [False, True], ids=["descriptor", "fallback"])
def test_initial_idempotence_probe_refuses_leaf_replacement(
    publication_case, monkeypatch, fallback
):
    task, paths, source = publication_case
    if sys.platform == "win32":
        pytest.skip("Windows refuses rename of an open destination")
    if not fallback and not publication.DIR_FD_ANCHORED_WRITES:
        pytest.skip("descriptor-relative reads are unavailable")
    if fallback:
        monkeypatch.setattr(publication, "DIR_FD_ANCHORED_WRITES", False)
    intended = b"intended"
    report = source.implementation_artifacts / "report.bin"
    report.write_bytes(intended)
    bind_and_prepare(task, paths, source)
    destination = paths.implementation_artifacts / "report.bin"
    detached = paths.implementation_artifacts / "detached-report.bin"
    replacement = b"operator replacement"
    destination.write_bytes(intended)
    open_regular = publication._open_regular
    writer = publication.atomic_write_bytes_confined
    replaced = False

    class ReplacingStream:
        def __init__(self, stream):
            self.stream = stream

        def fileno(self):
            return self.stream.fileno()

        def read(self, size=-1):
            nonlocal replaced
            data = self.stream.read(size)
            if not data and not replaced:
                replaced = True
                destination.rename(detached)
                destination.write_bytes(replacement)
            return data

    @contextmanager
    def replacing_open(root, path):
        with open_regular(root, path) as stream:
            if stream is not None and path == destination:
                yield ReplacingStream(stream)
            else:
                yield stream

    monkeypatch.setattr(publication, "_open_regular", replacing_open)

    def refuse_destination_write(path, data, **kwargs):
        if path == destination:
            pytest.fail("detached destination reached replacement")
        writer(path, data, **kwargs)

    monkeypatch.setattr(
        publication,
        "atomic_write_bytes_confined",
        refuse_destination_write,
    )

    with pytest.raises(publication.PublicationError, match="destination conflict"):
        publication.publish(task, paths)

    assert replaced
    assert destination.read_bytes() == replacement
    assert detached.read_bytes() == intended
    assert not task.artifact_publication_complete


def test_growing_destination_between_observations_refuses_replace(publication_case, monkeypatch):
    task, paths, source = publication_case
    destination = paths.implementation_artifacts / "report.bin"
    destination.write_bytes(b"before")
    publication.capture(task, paths)
    bind_and_prepare(task, paths, source)

    def operator_growth(*_args):
        with destination.open("ab") as stream:
            stream.write(b"g" * (publication._BOUNDED_READ_CHUNK_BYTES * 3))
        return False

    monkeypatch.setattr(publication.verify, "path_tracked", operator_growth)
    monkeypatch.setattr(
        publication,
        "atomic_write_bytes_confined",
        lambda *_args, **_kwargs: pytest.fail("replacement staged before destination recheck"),
    )
    monkeypatch.setattr(
        publication,
        "_contents",
        lambda *_args, **_kwargs: pytest.fail("publish materialized growing destination"),
    )

    with pytest.raises(publication.PublicationError, match="changed during publication"):
        publication.publish(task, paths)

    assert destination.read_bytes().startswith(b"before")
    assert destination.stat().st_size > publication._BOUNDED_READ_CHUNK_BYTES
    assert not task.artifact_publication_complete


def test_initial_equality_and_baseline_identity_share_one_probe(publication_case, monkeypatch):
    task, paths, source = publication_case
    destination = paths.implementation_artifacts / "report.bin"
    destination.write_bytes(b"before")
    publication.capture(task, paths)
    bind_and_prepare(task, paths, source)
    destination.write_bytes(b"conflicting operator bytes")
    probe_destination = publication._probe_destination
    probes = 0

    def probe_then_restore_baseline(root, path, expected=None):
        nonlocal probes
        result = probe_destination(root, path, expected)
        if path == destination:
            probes += 1
            if probes == 1:
                destination.write_bytes(b"before")
        return result

    monkeypatch.setattr(publication, "_probe_destination", probe_then_restore_baseline)

    with pytest.raises(publication.PublicationError, match="destination conflict"):
        publication.publish(task, paths)

    assert probes == 1
    assert destination.read_bytes() == b"before"
    assert not task.artifact_publication_complete


@pytest.mark.parametrize("mutation", ["grow", "shrink"])
def test_post_write_visibility_refuses_file_mutation_during_read(
    publication_case, monkeypatch, mutation
):
    task, paths, source = publication_case
    bind_and_prepare(task, paths, source)
    destination = paths.implementation_artifacts / "report.bin"
    open_regular = publication._open_regular
    mutated = False

    class MutatingStream:
        def __init__(self, stream):
            self.stream = stream

        def fileno(self):
            return self.stream.fileno()

        def read(self, size=-1):
            nonlocal mutated
            data = self.stream.read(size)
            if data and not mutated:
                mutated = True
                if mutation == "grow":
                    with destination.open("ab") as writer:
                        writer.write(b"operator growth")
                else:
                    with destination.open("r+b") as writer:
                        writer.truncate(0)
            return data

    @contextmanager
    def mutating_open(root, path):
        with open_regular(root, path) as stream:
            if stream is not None and path == destination:
                yield MutatingStream(stream)
            else:
                yield stream

    def write_then_mutate(path, data, **kwargs):
        kwargs["_before_replace"]()
        path.write_bytes(data)

    monkeypatch.setattr(publication, "_open_regular", mutating_open)
    monkeypatch.setattr(publication, "atomic_write_bytes_confined", write_then_mutate)

    with pytest.raises(publication.PublicationError, match="not visible at destination"):
        publication.publish(task, paths)

    assert mutated
    assert not task.artifact_publication_complete


@pytest.mark.parametrize("fallback", [False, True], ids=["descriptor", "fallback"])
def test_post_write_visibility_refuses_leaf_replacement(publication_case, monkeypatch, fallback):
    task, paths, source = publication_case
    if sys.platform == "win32":
        pytest.skip("Windows refuses rename of an open destination")
    if not fallback and not publication.DIR_FD_ANCHORED_WRITES:
        pytest.skip("descriptor-relative reads are unavailable")
    if fallback:
        monkeypatch.setattr(publication, "DIR_FD_ANCHORED_WRITES", False)
    intended = b"intended"
    report = source.implementation_artifacts / "report.bin"
    report.write_bytes(intended)
    bind_and_prepare(task, paths, source)
    destination = paths.implementation_artifacts / "report.bin"
    detached = paths.implementation_artifacts / "detached-report.bin"
    replacement = b"operator replacement"
    open_regular = publication._open_regular
    replaced = False

    class ReplacingStream:
        def __init__(self, stream):
            self.stream = stream

        def fileno(self):
            return self.stream.fileno()

        def read(self, size=-1):
            nonlocal replaced
            data = self.stream.read(size)
            if not data and not replaced:
                replaced = True
                destination.rename(detached)
                destination.write_bytes(replacement)
            return data

    @contextmanager
    def replacing_open(root, path):
        with open_regular(root, path) as stream:
            if stream is not None and path == destination:
                yield ReplacingStream(stream)
            else:
                yield stream

    def write_then_check(path, data, **kwargs):
        kwargs["_before_replace"]()
        path.write_bytes(data)

    monkeypatch.setattr(publication, "_open_regular", replacing_open)
    monkeypatch.setattr(publication, "atomic_write_bytes_confined", write_then_check)

    with pytest.raises(publication.PublicationError, match="not visible at destination"):
        publication.publish(task, paths)

    assert replaced
    assert destination.read_bytes() == replacement
    assert detached.read_bytes() == intended
    assert not task.artifact_publication_complete


@pytest.mark.parametrize(
    "visible",
    [None, b"short", b"\xff\x00\r\nreport-more", b"\x00\x00\r\nreport"],
    ids=["missing", "truncated", "extended", "different"],
)
def test_visibility_check_streams_and_refuses_inexact_destination(
    publication_case, monkeypatch, visible
):
    task, paths, source = publication_case
    bind_and_prepare(task, paths, source)

    def inexact_write(path, _data, **kwargs):
        kwargs["_before_replace"]()
        if visible is not None:
            path.write_bytes(visible)

    monkeypatch.setattr(publication, "atomic_write_bytes_confined", inexact_write)
    monkeypatch.setattr(
        publication,
        "_contents",
        lambda *_args, **_kwargs: pytest.fail("visibility check materialized destination"),
    )

    with pytest.raises(publication.PublicationError, match="not visible at destination"):
        publication.publish(task, paths)

    assert not task.artifact_publication_complete


@pytest.mark.parametrize("relative", ["report.bin", "errata/correction.md"])
def test_existing_baseline_allows_replace(publication_case, relative):
    task, paths, source = publication_case
    destination = paths.implementation_artifacts / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"before")
    output = source.implementation_artifacts / relative
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"\xff\x00\r\nreport")
    (source.implementation_artifacts / "spec.md").write_text(
        f"---\nstatus: done\nartifact_deliverables: [{relative}]\n---\n"
    )
    publication.capture(task, paths)
    bind_and_prepare(task, paths, source)
    publication.publish(task, paths)
    assert destination.read_bytes() == b"\xff\x00\r\nreport"


def test_partial_write_replays_saved_intent(publication_case, monkeypatch):
    task, paths, source = publication_case
    bind_and_prepare(task, paths, source)
    writer = publication.atomic_write_bytes_confined

    def interrupted(path, data, **kw):
        writer(path, data, **kw)
        raise OSError("host lost after replacement")

    monkeypatch.setattr(publication, "atomic_write_bytes_confined", interrupted)
    with pytest.raises(OSError, match="host lost"):
        publication.publish(task, paths)
    back = StoryTask.from_dict(task.to_dict())
    assert not back.artifact_publication_complete
    (source.implementation_artifacts / "report.bin").write_bytes(b"unverified")
    monkeypatch.setattr(publication, "atomic_write_bytes_confined", writer)
    publication.publish(back, paths)
    assert (paths.implementation_artifacts / "report.bin").read_bytes() == b"\xff\x00\r\nreport"
    assert back.artifact_publication_complete


@pytest.mark.parametrize(
    "declaration",
    [
        "../escape",
        "/absolute",
        "C:/absolute",
        "*.md",
        "dir/../x",
        "deferred-work.md",
        "sprint-status.yaml",
        "report.bin/",
        "SPRINT-STATUS.YAML",
        "Deferred-Work.md",
        "sprint-status.yaml. ",
        "deferred-work.md ",
        "dir./report.bin",
        "NUL.txt",
        "report.bin:stream",
        "...",
    ],
)
def test_invalid_paths_refused(publication_case, declaration):
    task, paths, source = publication_case
    (source.implementation_artifacts / "spec.md").write_text(
        f"---\nartifact_deliverables: ['{declaration}']\n---\n"
    )
    with pytest.raises(publication.PublicationError, match="invalid artifact|reserved"):
        bind_and_prepare(task, paths, source)
    assert task.artifact_payload is None


@pytest.mark.parametrize("declaration", ["null", "report.bin", "{}", "[null]"])
def test_malformed_list_refused(publication_case, declaration):
    task, paths, source = publication_case
    (source.implementation_artifacts / "spec.md").write_text(
        f"---\nartifact_deliverables: {declaration}\n---\n"
    )
    with pytest.raises(publication.PublicationError):
        bind_and_prepare(task, paths, source)


@pytest.mark.parametrize("kind", ["directory", "symlink", "parent-symlink", "missing"])
def test_nonregular_sources_refused(publication_case, kind):
    task, paths, source = publication_case
    report = source.implementation_artifacts / "report.bin"
    report.unlink()
    if kind == "directory":
        report.mkdir()
    elif kind == "symlink":
        report.symlink_to(source.implementation_artifacts / "spec.md")
    elif kind == "parent-symlink":
        linked = source.implementation_artifacts / "linked"
        linked.symlink_to(paths.implementation_artifacts, target_is_directory=True)
        (paths.implementation_artifacts / "report.bin").write_bytes(b"outside")
        (source.implementation_artifacts / "spec.md").write_text(
            "---\nartifact_deliverables: [linked/report.bin]\n---\n"
        )
    with pytest.raises(publication.PublicationError):
        bind_and_prepare(task, paths, source)


@pytest.mark.parametrize("kind", ["missing", "directory", "symlink"])
def test_future_tracked_declaration_still_validates_source_shape(
    publication_case, monkeypatch, kind
):
    task, paths, source = publication_case
    report = source.implementation_artifacts / "report.bin"
    report.unlink()
    if kind == "directory":
        report.mkdir()
    elif kind == "symlink":
        report.symlink_to(source.implementation_artifacts / "spec.md")
    monkeypatch.setattr(publication.verify, "path_tracked", lambda *_: False)
    monkeypatch.setattr(publication.verify, "path_ignored", lambda *_: False)

    with pytest.raises(publication.PublicationError, match="missing|regular file|symlink"):
        bind_and_prepare(task, paths, source)

    assert task.artifact_source_digests is None
    assert task.artifact_payload is None


def test_old_state_cannot_create_overwrite_authority(publication_case):
    task, paths, source = publication_case
    task.artifact_baseline = None
    bind_and_prepare(task, paths, source)
    with pytest.raises(publication.PublicationError, match="no pre-execution"):
        publication.publish(task, paths)
    assert task.artifact_payload is not None
    assert base64.b64decode(task.artifact_payload["report.bin"]) == b"\xff\x00\r\nreport"


def test_destination_symlink_refused_even_when_equal(publication_case):
    task, paths, source = publication_case
    bind_and_prepare(task, paths, source)
    destination = paths.implementation_artifacts / "report.bin"
    destination.symlink_to(source.implementation_artifacts / "report.bin")
    with pytest.raises(publication.PublicationError, match="symlink"):
        publication.publish(task, paths)
    assert destination.is_symlink()


def test_destination_changed_during_git_probe_is_preserved(publication_case, monkeypatch):
    task, paths, source = publication_case
    bind_and_prepare(task, paths, source)
    destination = paths.implementation_artifacts / "report.bin"

    def operator_edit(*_):
        destination.write_bytes(b"operator while git ran")
        return False

    monkeypatch.setattr(publication.verify, "path_tracked", operator_edit)
    with pytest.raises(publication.PublicationError, match="changed during publication"):
        publication.publish(task, paths)
    assert destination.read_bytes() == b"operator while git ran"


def test_tracked_deliverables_ride_git(publication_case, monkeypatch):
    task, paths, source = publication_case
    monkeypatch.setattr(publication.verify, "path_tracked", lambda *_: True)
    bind_and_prepare(task, paths, source)
    assert task.artifact_payload == {}
    publication.publish(task, paths)
    assert task.artifact_publication_complete


def test_destination_that_becomes_tracked_is_refused(publication_case, monkeypatch):
    task, paths, source = publication_case
    bind_and_prepare(task, paths, source)
    destination = paths.implementation_artifacts / "report.bin"
    monkeypatch.setattr(publication.verify, "path_tracked", lambda *_: True)
    with pytest.raises(publication.PublicationError, match="became tracked"):
        publication.publish(task, paths)
    assert not destination.exists()
    assert not task.artifact_publication_complete


def test_destination_that_becomes_unignored_is_refused(publication_case, monkeypatch):
    task, paths, source = publication_case
    bind_and_prepare(task, paths, source)
    destination = paths.implementation_artifacts / "report.bin"
    monkeypatch.setattr(publication.verify, "path_ignored", lambda *_: False)
    with pytest.raises(publication.PublicationError, match="no longer ignored"):
        publication.publish(task, paths)
    assert not destination.exists()
    assert not task.artifact_publication_complete


def test_unignored_declaration_is_left_to_the_pending_git_commit(publication_case, monkeypatch):
    task, paths, source = publication_case
    monkeypatch.setattr(publication.verify, "path_ignored", lambda *_: False)
    publication.arm_binding(task, "dev:0")
    publication.bind_armed(task, source)
    assert task.artifact_source_digests == {}
    assert set(task.artifact_tracked_source_oids) == {"report.bin", "spec.md"}
    monkeypatch.setattr(publication.verify, "path_tracked", lambda *_: True)
    publication.prepare(task, paths, source)
    assert task.artifact_payload == {}


def test_binding_records_git_normalized_tracked_and_pending_identities(project):
    root = project.implementation_artifacts
    root.mkdir(parents=True, exist_ok=True)
    spec = root / "tracked-spec.md"
    tracked = root / "tracked.txt"
    pending = root / "pending.txt"
    spec.write_text("---\nstatus: done\nartifact_deliverables: [tracked.txt, pending.txt]\n---\n")
    tracked.write_bytes(b"accepted\r\n")
    (project.project / ".gitattributes").write_text("*.txt text eol=lf\n")
    git(project.project, "add", ".gitattributes", spec, tracked)
    git(project.project, "commit", "-q", "-m", "tracked publication inputs")
    pending.write_bytes(b"pending\r\n")
    task = StoryTask(story_key="dw-fix", epic=0, dw_ids=["DW-1"], spec_file=str(spec))
    publication.capture(task, project)

    publication.arm_binding(task, "dev:0")
    publication.bind_armed(task, project)

    assert task.artifact_source_digests == {}
    assert set(task.artifact_tracked_source_oids) == {
        "pending.txt",
        "tracked-spec.md",
        "tracked.txt",
    }
    tracked_rel = tracked.relative_to(project.repo_root).as_posix()
    assert task.artifact_tracked_source_oids["tracked.txt"] == git(
        project.project, "hash-object", f"--path={tracked_rel}", tracked
    )

    first = dict(task.artifact_tracked_source_oids)
    tracked.write_bytes(b"accepted repair\r\n")
    pending.write_bytes(b"pending repair\r\n")
    assert publication.arm_binding(task, "review:1")
    publication.bind_armed(task, project)
    assert task.artifact_tracked_source_oids["tracked.txt"] != first["tracked.txt"]
    assert task.artifact_tracked_source_oids["pending.txt"] != first["pending.txt"]


def test_non_spec_git_deliverable_binding_hashes_a_streamed_confined_snapshot(project, monkeypatch):
    root = project.implementation_artifacts
    root.mkdir(parents=True, exist_ok=True)
    spec = root / "spec.md"
    report = root / "large-report.bin"
    spec.write_text("---\nstatus: done\nartifact_deliverables: [large-report.bin]\n---\n")
    report.write_bytes(b"tracked bytes")
    git(project.repo_root, "add", "-A")
    git(project.repo_root, "commit", "-q", "-m", "tracked publication inputs")
    task = StoryTask(story_key="dw-fix", epic=0, dw_ids=["DW-1"], spec_file=str(spec))
    publication.capture(task, project)
    publication.arm_binding(task, "dev:0")
    path_hash = publication.verify.git_normalized_blob_oid
    bytes_hash = publication.verify.git_normalized_blob_oid_for_bytes
    path_calls = []
    bytes_calls = []

    def record_path_hash(repo, rel, path):
        assert path != report
        assert path.is_file() and path.read_bytes() == b"tracked bytes"
        path_calls.append(path)
        return path_hash(repo, rel, path)

    def record_bytes_hash(repo, rel, data):
        bytes_calls.append(data)
        return bytes_hash(repo, rel, data)

    monkeypatch.setattr(publication.verify, "git_normalized_blob_oid", record_path_hash)
    monkeypatch.setattr(publication.verify, "git_normalized_blob_oid_for_bytes", record_bytes_hash)

    publication.bind_armed(task, project)

    assert len(path_calls) == 1
    assert path_calls[0] != report and not path_calls[0].exists()
    assert bytes_calls == [spec.read_bytes()]

    publication.arm_binding(task, "dev:1")
    open_regular = publication._open_regular

    class GrowingStream:
        def __init__(self, stream):
            self.stream = stream
            self.grew = False

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            chunk = self.stream.read(size)
            if not self.grew:
                self.grew = True
                with report.open("ab") as writer:
                    writer.write(b"growth")
            return chunk

    @contextmanager
    def grow_report_during_copy(open_root, path):
        with open_regular(open_root, path) as stream:
            if path == report and stream is not None:
                yield GrowingStream(stream)
            else:
                yield stream

    monkeypatch.setattr(publication, "_open_regular", grow_report_during_copy)

    with pytest.raises(publication.PublicationError, match="changed during binding"):
        publication.bind_armed(task, project)

    assert len(path_calls) == 1


def test_staged_validation_refuses_sorted_paths_without_exposing_object_ids(project):
    root = project.implementation_artifacts
    root.mkdir(parents=True, exist_ok=True)
    spec = root / "spec.md"
    first = root / "z-last.txt"
    second = root / "a-first.txt"
    spec.write_text("---\nstatus: done\nartifact_deliverables: [z-last.txt, a-first.txt]\n---\n")
    first.write_text("accepted z\n")
    second.write_text("accepted a\n")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "accepted publication inputs")
    task = StoryTask(story_key="dw-fix", epic=0, dw_ids=["DW-1"], spec_file=str(spec))
    publication.capture(task, project)
    publication.arm_binding(task, "dev:0")
    publication.bind_armed(task, project)
    accepted_oids = set(task.artifact_tracked_source_oids.values())
    first.write_text("later z\n")
    second.write_text("later a\n")
    git(project.project, "add", "-A")

    with pytest.raises(publication.PublicationError) as raised:
        publication.validate_staged(task, project)

    assert str(raised.value).endswith("a-first.txt, z-last.txt")
    assert not any(oid in str(raised.value) for oid in accepted_oids)


def test_staged_validation_refuses_an_ignored_deliverable_that_becomes_tracked(
    publication_case,
):
    task, _paths, source = publication_case
    publication.arm_binding(task, "dev:0")
    publication.bind_armed(task, source)
    report = source.implementation_artifacts / "report.bin"
    report_rel = report.relative_to(source.repo_root).as_posix()
    git(source.repo_root, "add", "-f", "--", report_rel)

    with pytest.raises(publication.PublicationError, match=r"report\.bin"):
        publication.validate_staged(task, source)


def test_staged_validation_refuses_missing_or_malformed_persisted_maps(project):
    incomplete = StoryTask(story_key="dw-fix", epic=0)
    with pytest.raises(publication.PublicationError, match="binding is missing"):
        publication.validate_staged(incomplete, project)

    malformed = StoryTask(story_key="dw-fix", epic=0)
    malformed.artifact_source_digests = {}  # type: ignore[reportAssignmentType]
    malformed.artifact_tracked_source_oids = []  # type: ignore[reportAssignmentType]
    with pytest.raises(publication.PublicationError, match="malformed"):
        publication.validate_staged(malformed, project)


def test_integrated_validation_checks_commit_and_post_hook_index_with_path_only_errors(project):
    root = project.implementation_artifacts
    root.mkdir(parents=True, exist_ok=True)
    spec = root / "spec.md"
    report = root / "report.bin"
    spec.write_text("---\nstatus: done\nartifact_deliverables: [report.bin]\n---\n")
    report.write_bytes(b"accepted\r\n")
    (project.project / ".gitattributes").write_text("*.bin text eol=lf\n")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "accepted target")
    task = StoryTask(story_key="dw-fix", epic=0, dw_ids=["DW-1"], spec_file=str(spec))
    publication.capture(task, project)
    publication.arm_binding(task, "dev:0")
    publication.bind_armed(task, project)
    accepted_oids = set(task.artifact_tracked_source_oids.values())

    assert publication.validate_integrated(task, project, "HEAD") is True

    report.write_bytes(b"hook drift\n")
    git(project.project, "add", "--", report)
    with pytest.raises(publication.PublicationError) as raised:
        publication.validate_integrated(task, project, "HEAD")
    assert str(raised.value).endswith("report.bin")
    assert not any(oid in str(raised.value) for oid in accepted_oids)


def test_integrated_validation_preserves_legacy_frozen_payload_compatibility(project):
    task = StoryTask(story_key="dw-fix", epic=0, dw_ids=["DW-1"])
    task.artifact_source_digests = {"report.bin": "legacy-digest"}
    task.artifact_tracked_source_oids = None
    task.artifact_acceptance_identity = "legacy-owner"
    task.artifact_payload = {"report.bin": "bGVnYWN5"}

    assert publication.validate_integrated(task, project, "HEAD") is False


@pytest.mark.parametrize("payload", [["not-a-map"], {"report.bin": "not base64!"}])
def test_integrated_validation_refuses_malformed_legacy_payload(project, payload):
    task = StoryTask(story_key="dw-fix", epic=0, dw_ids=["DW-1"])
    task.artifact_payload = payload  # type: ignore[reportAssignmentType]

    with pytest.raises(publication.PublicationError, match="payload"):
        publication.validate_integrated(task, project, "HEAD")


def test_integrated_validation_refuses_partial_modern_authority(project):
    task = StoryTask(story_key="dw-fix", epic=0, dw_ids=["DW-1"])
    task.artifact_payload = {}
    task.artifact_tracked_source_oids = {}
    task.artifact_source_digests = None
    task.artifact_acceptance_identity = "review:dev:0"

    with pytest.raises(publication.PublicationError, match="binding"):
        publication.validate_integrated(task, project, "HEAD")


def test_integrated_validation_binds_modern_payload_to_accepted_ignored_digests(project):
    task = StoryTask(story_key="dw-fix", epic=0, dw_ids=["DW-1"])
    task.artifact_payload = {"report.bin": base64.b64encode(b"changed").decode("ascii")}
    task.artifact_source_digests = {"report.bin": hashlib.sha256(b"accepted").hexdigest()}
    task.artifact_tracked_source_oids = {}
    task.artifact_acceptance_identity = "review:dev:0"

    with pytest.raises(publication.PublicationError, match="differs from accepted"):
        publication.validate_integrated(task, project, "HEAD")


def test_integrated_validation_refuses_commit_drift_even_when_index_is_accepted(project):
    root = project.implementation_artifacts
    root.mkdir(parents=True, exist_ok=True)
    spec = root / "spec.md"
    report = root / "report.bin"
    spec.write_text("---\nstatus: done\nartifact_deliverables: [report.bin]\n---\n")
    report.write_bytes(b"accepted\n")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "accepted target")
    task = StoryTask(story_key="dw-fix", epic=0, dw_ids=["DW-1"], spec_file=str(spec))
    publication.capture(task, project)
    publication.arm_binding(task, "dev:0")
    publication.bind_armed(task, project)
    accepted_oids = set(task.artifact_tracked_source_oids.values())
    report.write_bytes(b"commit drift\n")
    git(project.project, "add", "--", report)
    git(project.project, "commit", "-q", "-m", "drifted target")
    report.write_bytes(b"accepted\n")
    git(project.project, "add", "--", report)

    with pytest.raises(publication.PublicationError) as raised:
        publication.validate_integrated(task, project, "HEAD")

    assert str(raised.value).endswith("report.bin")
    assert not any(oid in str(raised.value) for oid in accepted_oids)


def test_integrated_validation_keeps_accepted_ignored_paths_absent(publication_case):
    task, _paths, source = publication_case
    publication.arm_binding(task, "dev:0")
    publication.bind_armed(task, source)
    report = source.implementation_artifacts / "report.bin"
    git(source.repo_root, "add", "-f", "--", report)

    with pytest.raises(publication.PublicationError, match=r"report\.bin"):
        publication.validate_integrated(task, source, "HEAD")


def test_unignored_untracked_declaration_must_be_tracked_by_preparation(
    publication_case, monkeypatch
):
    task, paths, source = publication_case
    nested = source.implementation_artifacts / "embedded"
    (nested / ".git").mkdir(parents=True)
    output = nested / "output.bin"
    output.write_bytes(b"nested repository output")
    (source.implementation_artifacts / "spec.md").write_text(
        "---\nstatus: done\nartifact_deliverables: [embedded/output.bin]\n---\n"
    )
    monkeypatch.setattr(publication.verify, "path_tracked", lambda *_: False)
    monkeypatch.setattr(
        publication.verify,
        "path_ignored",
        lambda _repo, path: path.name == "spec.md",
    )

    publication.arm_binding(task, "dev:0")
    publication.bind_armed(task, source)
    assert set(task.artifact_source_digests) == {"spec.md"}

    with pytest.raises(publication.PublicationError, match="was not tracked.*embedded"):
        publication.prepare(task, paths, source)

    assert task.artifact_payload is None


def test_external_spec_tracked_declaration_swap_is_refused_by_preparation(project):
    # A spec inside the project but outside implementation_artifacts is never
    # itself a selected deliverable, so the ignored map alone ({} == {}) cannot
    # see its declarations move; the tracked rel set has to be re-derived and
    # compared whole (Codex P1 on #795).
    root = project.implementation_artifacts
    root.mkdir(parents=True, exist_ok=True)
    accepted = root / "a.bin"
    swapped = root / "b.bin"
    accepted.write_bytes(b"accepted deliverable")
    swapped.write_bytes(b"unaccepted deliverable")
    spec = project.project / "docs" / "external-spec.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("---\nstatus: done\nartifact_deliverables: [a.bin]\n---\n")
    git(project.repo_root, "add", "-A")
    git(project.repo_root, "commit", "-q", "-m", "tracked publication inputs")
    task = StoryTask(story_key="dw-fix", epic=0, dw_ids=["DW-1"], spec_file=str(spec))
    publication.capture(task, project)
    publication.arm_binding(task, "dev:0")
    publication.bind_armed(task, project)
    assert task.artifact_source_digests == {}
    assert set(task.artifact_tracked_source_oids) == {"a.bin"}
    bound = dict(task.artifact_tracked_source_oids)

    spec.write_text("---\nstatus: done\nartifact_deliverables: [b.bin]\n---\n")

    with pytest.raises(
        publication.PublicationError,
        match="tracked artifact deliverables changed since accepted verification: a\\.bin, b\\.bin",
    ) as exc:
        publication.prepare(task, project, project)

    assert bound["a.bin"] not in str(exc.value)
    assert task.artifact_payload is None
    assert task.artifact_tracked_source_oids == bound

    spec.write_text("---\nstatus: done\nartifact_deliverables: [a.bin]\n---\n")
    publication.prepare(task, project, project)
    assert task.artifact_payload == {}


def test_preparation_never_reopens_a_tracked_deliverable_behind_the_sealed_commit(
    publication_case, monkeypatch
):
    # The rel-set check must be read off the classification alone: a tracked
    # deliverable removed from the working tree after `finalize_commit` sealed
    # it is tolerated (the commit carries it), not a refusal (CodeRabbit on
    # #795 round 4).
    task, paths, source = publication_case
    monkeypatch.setattr(publication.verify, "path_tracked", lambda *_: True)
    publication.arm_binding(task, "dev:0")
    publication.bind_armed(task, source)
    assert set(task.artifact_tracked_source_oids) == {"report.bin", "spec.md"}
    (source.implementation_artifacts / "report.bin").unlink()

    publication.prepare(task, paths, source)

    assert task.artifact_payload == {}


def test_read_fault_retains_baseline_and_refuses_payload(publication_case, monkeypatch):
    task, paths, source = publication_case
    report = source.implementation_artifacts / "report.bin"
    read = publication._contents

    def unreadable(root, path, **kwargs):
        if path == report:
            raise OSError("unreadable report.bin")
        return read(root, path, **kwargs)

    monkeypatch.setattr(publication, "_contents", unreadable)
    with pytest.raises(OSError, match="unreadable"):
        bind_and_prepare(task, paths, source)
    assert task.artifact_baseline is not None
    assert task.artifact_payload is None


def test_accepted_spec_parent_traversal_refused_before_read(publication_case, monkeypatch):
    task, paths, source = publication_case
    task.spec_file = str(source.project / ".." / "escaped.md")
    escaped = source.project.parent / "escaped.md"
    escaped.write_text("---\nstatus: done\n---\n")
    with pytest.raises(publication.PublicationError, match="parent traversal"):
        bind_and_prepare(task, paths, source)
    assert task.artifact_payload is None


@pytest.mark.parametrize(
    "text",
    [
        "---\nartifact_deliverables: [report.bin\n---\n",
        "---\n- report.bin\n---\n",
        "---\nstatus: done\n",
        "not frontmatter",
        "---\n{}\n---\n",
    ],
)
def test_malformed_frontmatter_refuses_publication_intent(publication_case, text):
    task, paths, source = publication_case
    (source.implementation_artifacts / "spec.md").write_text(text)
    with pytest.raises(publication.PublicationError, match="invalid accepted spec frontmatter"):
        bind_and_prepare(task, paths, source)
    assert task.artifact_payload is None


def test_external_declaration_is_refused(publication_case, tmp_path):
    from dataclasses import replace

    task, paths, source = publication_case
    external = replace(source, implementation_artifacts=tmp_path / "external")
    external.implementation_artifacts.mkdir()
    with pytest.raises(publication.PublicationError, match="strictly inside"):
        bind_and_prepare(task, paths, external)
    assert task.artifact_payload is None


@pytest.mark.parametrize("fallback", [False, True])
def test_destination_edit_during_fsync_refuses_replace(publication_case, monkeypatch, fallback):
    from bmad_loop import platform_util

    task, paths, source = publication_case
    bind_and_prepare(task, paths, source)
    destination = paths.implementation_artifacts / "report.bin"
    fsync = os.fsync

    def edit_during_fsync(fd):
        fsync(fd)
        destination.write_bytes(b"operator during fsync")

    if fallback:
        monkeypatch.setattr(platform_util, "HANDLE_ANCHORED_WRITES", False)
        monkeypatch.setattr(platform_util, "DIR_FD_ANCHORED_WRITES", False)
        monkeypatch.setattr(publication, "DIR_FD_ANCHORED_WRITES", False)
        monkeypatch.setattr(publication, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(os, "fsync", edit_during_fsync)
    with pytest.raises(publication.PublicationError, match="changed during publication"):
        publication.publish(task, paths)
    assert destination.read_bytes() == b"operator during fsync"
    assert not task.artifact_publication_complete
    assert list(destination.parent.glob("*.tmp")) == []


def test_publication_refuses_when_confined_write_is_not_visible(publication_case, monkeypatch):
    task, paths, source = publication_case
    bind_and_prepare(task, paths, source)
    destination = paths.implementation_artifacts / "report.bin"

    def detached_write(path, data, **kwargs):
        kwargs["_before_replace"]()
        (path.parent / "detached-report.bin").write_bytes(data)

    monkeypatch.setattr(publication, "atomic_write_bytes_confined", detached_write)
    with pytest.raises(publication.PublicationError, match="not visible at destination"):
        publication.publish(task, paths)
    assert not destination.exists()
    assert not task.artifact_publication_complete


@pytest.mark.skipif(not publication.DIR_FD_ANCHORED_WRITES, reason="POSIX descriptor reads")
@pytest.mark.parametrize("swap", ["file", "parent"])
def test_source_swap_between_check_and_read_is_refused(publication_case, monkeypatch, swap):
    task, paths, source = publication_case
    report = source.implementation_artifacts / "report.bin"
    outside = paths.implementation_artifacts / "outside"
    outside.mkdir()
    (outside / report.name).write_bytes(b"outside secrets")
    if swap == "file":
        opener = os.open

        def swap_file(name, flags, *args, **kwargs):
            if name == report.name and "dir_fd" in kwargs:
                report.unlink()
                report.symlink_to(outside / report.name)
            return opener(name, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", swap_file)
    else:
        opener = publication.open_dir_confined

        def swap_parent(root, parent, **kwargs):
            report.parent.rename(report.parent.with_name("original"))
            report.parent.symlink_to(outside, target_is_directory=True)
            return opener(root, parent, **kwargs)

        monkeypatch.setattr(publication, "open_dir_confined", swap_parent)
    with pytest.raises((OSError, publication.PublicationError)):
        publication._contents(source.project, report)
    assert (outside / report.name).read_bytes() == b"outside secrets"


def _plant_directory_redirect(link, target):
    """A directory redirect at ``link``: a symlink on POSIX; on win32 a JUNCTION,
    the redirect an unprivileged session can plant (see test_recovery_flow)."""
    if sys.platform == "win32":
        import _winapi  # Windows-only stdlib module

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def _swap_publication_root(root, outside):
    """Replace the in-checkout artifact root with a redirect to ``outside`` (DW-338)."""
    root.rename(root.with_name(root.name + "-aside"))
    _plant_directory_redirect(root, outside)


@pytest.mark.skipif(not publication.DIR_FD_ANCHORED_WRITES, reason="POSIX descriptor reads")
def test_root_swapped_between_confined_and_open_is_not_read(publication_case, monkeypatch):
    """DW-338, read side: `implementation_artifacts` replaced by a link after
    `_confined` accepted it but before the root open. The pinned walk refuses, so
    the outside tree's bytes are never read.

    Ablation: stop passing `_confined`'s identity as `root_identity` in
    `_open_regular` (or remove the compare in `open_dir_confined`) and this fails
    `DID NOT RAISE` — `_contents` returns `outside secrets`."""
    _task, paths, _source = publication_case
    root = paths.implementation_artifacts
    report = root / "report.bin"
    report.write_bytes(b"inside")
    outside = paths.project.parent / "outside-artifacts"
    outside.mkdir()
    (outside / report.name).write_bytes(b"outside secrets")
    opener = publication.open_dir_confined
    swapped = []

    def swap_then_open(walk_root, target, **kwargs):
        if not swapped:
            _swap_publication_root(root, outside)
            swapped.append(True)
        return opener(walk_root, target, **kwargs)

    monkeypatch.setattr(publication, "open_dir_confined", swap_then_open)
    with pytest.raises(publication.PublicationError):
        publication._contents(root, report)
    assert swapped


@pytest.mark.skipif(not publication.DIR_FD_ANCHORED_WRITES, reason="POSIX descriptor reads")
def test_root_swapped_between_confined_and_open_is_not_measured(publication_case, monkeypatch):
    """DW-338, `_file_size`: a root replaced by a link after `_confined` accepted
    it is refused rather than measured.

    Ablation: stop passing `root_identity=` in `_file_size` and this fails
    `DID NOT RAISE` — it returns the outside file's size."""
    _task, paths, _source = publication_case
    root = paths.implementation_artifacts
    report = root / "report.bin"
    report.write_bytes(b"inside")
    outside = paths.project.parent / "outside-artifacts"
    outside.mkdir()
    (outside / report.name).write_bytes(b"outside secrets")
    opener = publication.open_dir_confined
    swapped = []

    def swap_then_open(walk_root, target, **kwargs):
        if not swapped:
            _swap_publication_root(root, outside)
            swapped.append(True)
        return opener(walk_root, target, **kwargs)

    monkeypatch.setattr(publication, "open_dir_confined", swap_then_open)
    with pytest.raises(publication.PublicationError):
        publication._file_size(root, report)
    assert swapped


@pytest.mark.skipif(not publication.DIR_FD_ANCHORED_WRITES, reason="POSIX descriptor inventory")
def test_capture_refuses_a_root_swapped_between_confined_and_open(publication_case, monkeypatch):
    """DW-338, `capture`: `implementation_artifacts` replaced by a link to an
    empty outside directory between `walk`'s `_confined` and its root open is
    refused, and no baseline is recorded.

    Ablation: stop passing `root_identity=` in `walk` and this fails
    `DID NOT RAISE` — the outside directory is inventoried as an empty baseline."""
    task, paths, _source = publication_case
    root = paths.implementation_artifacts
    outside = paths.project.parent / "outside-artifacts"
    outside.mkdir()
    task.artifact_baseline = None
    opener = publication.open_dir_confined
    swapped = []

    def swap_then_open(walk_root, target, **kwargs):
        if not swapped:
            _swap_publication_root(root, outside)
            swapped.append(True)
        return opener(walk_root, target, **kwargs)

    monkeypatch.setattr(publication, "open_dir_confined", swap_then_open)
    with pytest.raises(publication.PublicationError):
        publication.capture(task, paths)
    assert swapped
    assert task.artifact_baseline is None


@pytest.mark.skipif(not publication.DIR_FD_ANCHORED_WRITES, reason="POSIX descriptor writes")
def test_root_swapped_before_the_publication_write_lands_nothing_outside(
    publication_case, monkeypatch
):
    """DW-338, write side: the root is replaced by a link after `publish`'s last
    `_confined` predicate, inside the confined writer's own root open. The writer
    is pinned to the identity that predicate accepted, so it refuses before
    staging anything — not a byte is ever created outside the repository.

    `publish`'s `_before_replace` validation would ALSO refuse this swap (it
    re-runs `_confined`), but only after a temp was staged in the outside tree;
    so the assertion that pins the fix is that nothing was staged at all.

    Ablation: drop `root_identity=` from `publish`'s `atomic_write_bytes_confined`
    call and this fails — a staging temp is created inside `outside`."""
    task, paths, source = publication_case
    bind_and_prepare(task, paths, source)
    root = paths.implementation_artifacts
    outside = paths.project.parent / "outside-artifacts"
    outside.mkdir()
    writer_open = platform_util.open_dir_confined
    stage = platform_util._open_exclusive_at
    swapped = []
    staged = []

    def swap_then_open(walk_root, target, **kwargs):
        if not swapped and walk_root == root:
            _swap_publication_root(root, outside)
            swapped.append(True)
        return writer_open(walk_root, target, **kwargs)

    def record_stage(dir_fd, prefix, name):
        staged.append(os.fstat(dir_fd).st_ino)
        return stage(dir_fd, prefix, name)

    monkeypatch.setattr(platform_util, "open_dir_confined", swap_then_open)
    monkeypatch.setattr(platform_util, "_open_exclusive_at", record_stage)
    with pytest.raises(platform_util.UnconfinedWriteError):
        publication.publish(task, paths)
    assert swapped
    assert staged == []
    assert list(outside.iterdir()) == []
    assert not task.artifact_publication_complete


@pytest.mark.skipif(not publication.DIR_FD_ANCHORED_WRITES, reason="POSIX descriptor reads")
def test_root_swapped_before_the_destination_relookup_is_refused(publication_case, monkeypatch):
    """DW-338, `_destination_path_identity`: the fresh leaf re-lookup behind
    `_destination_still_names` is pinned too, so a root replaced by a link after
    its `_confined` never reports an outside leaf's identity.

    Ablation: drop `root_identity=` from `_destination_path_identity`'s
    `open_dir_confined` call and this fails — the outside file's identity is
    returned instead of None."""
    _task, paths, _source = publication_case
    root = paths.implementation_artifacts
    report = root / "report.bin"
    report.write_bytes(b"inside")
    outside = paths.project.parent / "outside-artifacts"
    outside.mkdir()
    (outside / report.name).write_bytes(b"outside secrets")
    opener = publication.open_dir_confined
    swapped = []

    def swap_then_open(walk_root, target, **kwargs):
        if not swapped:
            _swap_publication_root(root, outside)
            swapped.append(True)
        return opener(walk_root, target, **kwargs)

    monkeypatch.setattr(publication, "open_dir_confined", swap_then_open)
    assert publication._destination_path_identity(root, report) is None
    assert swapped


def test_publish_refuses_when_the_root_identity_cannot_be_retaken(publication_case, monkeypatch):
    """DW-338/DW-421: `_make_parents` creates a root it found missing and then
    re-takes the root identity; a root that still has none by then is refused
    rather than handing the parent creation and the writer `root_identity=None`
    (an unpinned walk and write).

    Ablation: delete the `if root_identity is None: raise` guard in
    `_make_parents` and this fails `DID NOT RAISE` — the payload is written
    unpinned."""
    task, paths, source = publication_case
    bind_and_prepare(task, paths, source)
    confined = publication._confined
    calls_from_make_parents = []

    def lose_root_identity(root, path):
        if sys._getframe(1).f_code.co_name == "_make_parents":
            calls_from_make_parents.append(path)
            return None  # the first look and the re-take after creation
        return confined(root, path)

    monkeypatch.setattr(publication, "_confined", lose_root_identity)
    with pytest.raises(publication.PublicationError, match="artifact directory is missing"):
        publication.publish(task, paths)
    assert len(calls_from_make_parents) == 2
    assert not (paths.implementation_artifacts / "report.bin").exists()
    assert not task.artifact_publication_complete


@pytest.mark.skipif(not publication.DIR_FD_ANCHORED_WRITES, reason="POSIX descriptor inventory")
def test_baseline_directory_swap_never_reads_redirected_contents(publication_case, monkeypatch):
    task, paths, _ = publication_case
    root = paths.implementation_artifacts
    directory = root / "nested"
    directory.mkdir()
    (directory / "report.bin").write_bytes(b"before")
    outside = paths.project / "outside"
    outside.mkdir()
    (outside / "report.bin").write_bytes(b"outside secrets")
    inode = directory.stat().st_ino
    scandir = os.scandir

    def swap_directory(fd):
        if isinstance(fd, int) and os.fstat(fd).st_ino == inode:
            directory.rename(root / "original")
            directory.symlink_to(outside, target_is_directory=True)
        return scandir(fd)

    monkeypatch.setattr(os, "scandir", swap_directory)
    with pytest.raises(publication.PublicationError, match="symlink"):
        publication.capture(task, paths)


def test_undecodable_accepted_spec_refuses_intent(publication_case):
    task, paths, source = publication_case
    (source.implementation_artifacts / "spec.md").write_bytes(b"---\nstatus: done\n\xff\n---\n")
    with pytest.raises(UnicodeDecodeError):
        bind_and_prepare(task, paths, source)
    assert task.artifact_payload is None


_JUNCTION_TAG = 0xA0000003  # IO_REPARSE_TAG_MOUNT_POINT


class _JunctionStat:
    """`os.lstat` stand-in for a win32 directory junction: a DIRECTORY mode (so
    `S_ISLNK` misses it) carrying a reparse tag, with the real directory's
    identity so an accepted root identity still pins (see test_platform_util's
    `_ReparseStat`)."""

    st_reparse_tag = _JUNCTION_TAG

    def __init__(self, real):
        self.st_mode = stat.S_IFDIR | 0o755
        self.st_dev = real.st_dev
        self.st_ino = real.st_ino
        self.st_size = real.st_size


def _simulate_junctions(monkeypatch, *junctions):
    """Make `os.lstat` report each of ``junctions`` as a win32 junction."""
    wanted = {str(path) for path in junctions}
    real_lstat = os.lstat

    def lstat(path, *args, **kwargs):
        real = real_lstat(path, *args, **kwargs)
        return _JunctionStat(real) if str(path) in wanted else real

    monkeypatch.setattr(platform_util, "_LINK_REPARSE_TAGS", (_JUNCTION_TAG,))
    monkeypatch.setattr(os, "lstat", lstat)


def _force_path_fallback(monkeypatch):
    """Neither handle arm: path-based reads, parent creation and writes."""
    monkeypatch.setattr(publication, "DIR_FD_ANCHORED_WRITES", False)
    monkeypatch.setattr(publication, "HANDLE_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "DIR_FD_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "HANDLE_ANCHORED_WRITES", False)


def _declare_nested_deliverable(task, paths, source, relative):
    output = source.implementation_artifacts / relative
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"nested correction")
    (source.implementation_artifacts / "spec.md").write_text(
        f"---\nstatus: done\nartifact_deliverables: [{relative}]\n---\n"
    )
    publication.capture(task, paths)
    bind_and_prepare(task, paths, source)


@pytest.mark.parametrize("where", ["root", "below"])
def test_confined_refuses_a_junction_at_or_below_the_artifacts_root(
    publication_case, monkeypatch, where
):
    """DW-422: a win32 junction's `lstat` is `S_IFDIR`, so the `S_ISLNK` refusal
    alone accepted it and the path-based fallback reads followed it. `_confined`
    refuses a link-like reparse point at the artifacts root and below it.

    Ablation: delete the `link_like_stat` refusal in `_confined` and this fails
    `DID NOT RAISE`."""
    _task, paths, _source = publication_case
    root = paths.implementation_artifacts
    nested = root / "nested"
    nested.mkdir()
    report = nested / "report.bin"
    report.write_bytes(b"inside")
    assert publication._confined(root, report) is not None  # positive control
    _simulate_junctions(monkeypatch, root if where == "root" else nested)
    with pytest.raises(publication.PublicationError, match="reparse point"):
        publication._confined(root, report)


@pytest.mark.parametrize("where", ["repo_root", "intermediate"])
def test_a_junction_above_the_artifacts_root_is_accepted(publication_case, monkeypatch, where):
    """DW-422, recorded decision "refuse below root only": the repository root
    and the directories between it and the artifacts root are the operator's
    layout, so a junction there keeps working — `_confined`, `_root`, capture
    and publish all accept it.

    Ablation: drop the `part.is_relative_to(root)` scope of the reparse refusal
    in `_confined`, or revert `_root` to `_confined(paths.repo_root, root)`, and
    this fails with `link-like reparse point`."""
    task, paths, source = publication_case
    root = paths.implementation_artifacts
    junction = paths.repo_root if where == "repo_root" else root.parent
    assert root.parent != paths.repo_root  # the layout has an intermediate
    _simulate_junctions(monkeypatch, junction)
    assert platform_util.link_like_stat(os.lstat(junction))  # the simulation is live
    assert publication._confined(root, root / "report.bin") is not None
    assert publication._root(paths) == root
    publication.capture(task, paths)
    bind_and_prepare(task, paths, source)
    publication.publish(task, paths)
    assert (root / "report.bin").read_bytes() == b"\xff\x00\r\nreport"
    assert task.artifact_publication_complete


def _swap_root_after_confined(monkeypatch, root, outside, *, caller=None, nested_only=False):
    """Swap ``root`` for a link to ``outside`` right after one `_confined` call
    (optionally only one made from ``caller``) accepted it."""
    confined = publication._confined
    swapped = []

    def confined_then_swap(walk_root, path):
        identity = confined(walk_root, path)
        if (
            not swapped
            and (caller is None or sys._getframe(1).f_code.co_name == caller)
            and (not nested_only or path.parent != walk_root)
        ):
            _swap_publication_root(root, outside)
            swapped.append(True)
        return identity

    monkeypatch.setattr(publication, "_confined", confined_then_swap)
    return swapped


@pytest.mark.parametrize("reader", ["contents", "size", "identity"])
def test_fallback_reads_refuse_a_root_swapped_after_confined(publication_case, monkeypatch, reader):
    """DW-422: without descriptor-relative reads, `_open_regular`, `_file_size`
    and `_destination_path_identity` read by path; a root replaced by a link
    after `_confined` accepted it is caught by the `_still_pinned` lstat compare,
    so the outside file is never read, measured or identified.

    Ablation: delete the `_still_pinned` check in the matching fallback arm and
    this fails — `outside secrets`, its size, or its identity comes back. The
    swap is a symlink on POSIX and a junction on win32, this fallback's host."""
    _task, paths, _source = publication_case
    monkeypatch.setattr(publication, "DIR_FD_ANCHORED_WRITES", False)
    root = paths.implementation_artifacts
    report = root / "report.bin"
    report.write_bytes(b"inside")
    outside = paths.project.parent / "outside-artifacts"
    outside.mkdir()
    (outside / report.name).write_bytes(b"outside secrets")
    swapped = _swap_root_after_confined(monkeypatch, root, outside)
    if reader == "identity":
        assert publication._destination_path_identity(root, report) is None
    else:
        read = publication._contents if reader == "contents" else publication._file_size
        with pytest.raises(publication.PublicationError, match="replaced"):
            read(root, report)
    assert swapped


def test_fallback_capture_refuses_a_root_swapped_after_confined(publication_case, monkeypatch):
    """DW-422, `capture` without descriptor-relative inventory: a root replaced
    by a link to an empty outside directory after `walk`'s `_confined` is
    refused, and no baseline is recorded.

    Ablation: delete the `_still_pinned` check in `walk`'s fallback and this
    fails `DID NOT RAISE` — the outside directory becomes an empty baseline."""
    task, paths, _source = publication_case
    monkeypatch.setattr(publication, "DIR_FD_ANCHORED_WRITES", False)
    root = paths.implementation_artifacts
    outside = paths.project.parent / "outside-artifacts"
    outside.mkdir()
    task.artifact_baseline = None
    swapped = _swap_root_after_confined(monkeypatch, root, outside, caller="walk")
    with pytest.raises(publication.PublicationError, match="replaced"):
        publication.capture(task, paths)
    assert swapped
    assert task.artifact_baseline is None


@pytest.mark.parametrize("fallback", [False, True], ids=["descriptor", "fallback"])
def test_capture_inventories_a_link_like_directory_as_nonregular(
    publication_case, monkeypatch, fallback
):
    """DW-422: a junction entry under the artifacts root reports `S_IFDIR`, so
    `walk` recursed into it. It is inventoried `"nonregular"`, as a POSIX
    symlink entry is, and never walked.

    Ablation: drop the `link_like_stat` arm in `walk` and this fails — the entry
    is a `"directory"` and `linked/secret.md` is inventoried."""
    task, paths, _source = publication_case
    root = paths.implementation_artifacts
    linked = root / "linked"
    linked.mkdir()
    (linked / "secret.md").write_text("behind the junction")
    (root / "plain.md").write_text("inventoried")
    if fallback:
        monkeypatch.setattr(publication, "DIR_FD_ANCHORED_WRITES", False)
    monkeypatch.setattr(platform_util, "_LINK_REPARSE_TAGS", (_JUNCTION_TAG,))
    real_scandir = os.scandir

    class _Entry:
        def __init__(self, entry):
            self._entry = entry
            self.name = entry.name

        def stat(self, *, follow_symlinks=True):
            real = self._entry.stat(follow_symlinks=follow_symlinks)
            return _JunctionStat(real) if self.name == linked.name else real

    @contextmanager
    def scandir(target):
        with real_scandir(target) as entries:
            yield [_Entry(entry) for entry in entries]

    monkeypatch.setattr(os, "scandir", scandir)
    publication.capture(task, paths)
    assert task.artifact_baseline["linked"] == "nonregular"
    assert "linked/secret.md" not in task.artifact_baseline
    assert "plain.md" in task.artifact_baseline  # the walk itself still ran


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink swap")
@pytest.mark.parametrize("fallback", [False, True], ids=["descriptor", "fallback"])
def test_root_swapped_before_parent_creation_creates_nothing_outside(
    publication_case, monkeypatch, fallback
):
    """DW-421: `publish` created missing parents with `mkdir(parents=True)`,
    which follows a root swapped for a link after `_confined`, so empty
    directories landed outside the repository before the pinned writer refused.
    `_make_parents` creates them anchored below the root pinned to the accepted
    identity, so the swap is refused and nothing is created outside.

    Ablation: drop `root_identity=` from `_create_directories`'
    `open_dir_confined` call (descriptor), or its `_root_still_pinned` check
    (fallback), and this fails — `errata/` is created inside `outside`."""
    task, paths, source = publication_case
    if not fallback and not publication.DIR_FD_ANCHORED_WRITES:
        pytest.skip("descriptor-relative writes are unavailable")
    _declare_nested_deliverable(task, paths, source, "errata/nested/correction.md")
    root = paths.implementation_artifacts
    outside = paths.project.parent / "outside-artifacts"
    outside.mkdir()
    if fallback:
        _force_path_fallback(monkeypatch)
    swapped = _swap_root_after_confined(
        monkeypatch, root, outside, caller="_make_parents", nested_only=True
    )
    refusal = "was replaced" if fallback else "could not be opened"
    with pytest.raises(publication.PublicationError, match=refusal):
        publication.publish(task, paths)
    assert swapped
    assert list(outside.iterdir()) == []
    assert not task.artifact_publication_complete


@pytest.mark.parametrize("fallback", [False, True], ids=["descriptor", "fallback"])
def test_publish_creates_a_missing_artifacts_root(publication_case, monkeypatch, fallback):
    """DW-421: with `implementation_artifacts` absent and an empty baseline,
    `_make_parents` creates the root by an anchored walk from the repository
    root, re-takes its identity, and creates the nested parents pinned to it.
    The handle variant runs the POSIX `dir_fd` arm, and on win32 the
    `open_at(O_CREAT | AT_DIRECTORY | AT_NOFOLLOW)` arm.

    Ablation: delete the `_create_directories(repo_root, root, ...)` call in
    `_make_parents` and this fails with `artifact directory is missing`."""
    task, paths, source = publication_case
    if not fallback and not publication.HANDLE_ANCHORED_WRITES:
        pytest.skip("handle-anchored writes are unavailable")
    root = paths.implementation_artifacts
    shutil.rmtree(root)
    _declare_nested_deliverable(task, paths, source, "errata/correction.md")
    assert task.artifact_baseline == {}
    if fallback:
        _force_path_fallback(monkeypatch)
    publication.publish(task, paths)
    assert (root / "errata" / "correction.md").read_bytes() == b"nested correction"
    assert task.artifact_publication_complete


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink swap")
@pytest.mark.parametrize("fallback", [False, True], ids=["descriptor", "fallback"])
def test_missing_root_with_a_swapped_ancestor_creates_nothing_outside(
    publication_case, monkeypatch, fallback
):
    """DW-421: creating a missing root walks from the repository root without
    following a link, so an intermediate directory swapped for a link after
    `_make_parents` found the root missing gets nothing created through it.

    Ablation: replace the `_create_directories(repo_root, root, ...)` call with
    `root.mkdir(parents=True, exist_ok=True)` and this fails —
    `implementation-artifacts/` is created inside `outside`."""
    task, paths, source = publication_case
    if not fallback and not publication.DIR_FD_ANCHORED_WRITES:
        pytest.skip("descriptor-relative writes are unavailable")
    root = paths.implementation_artifacts
    intermediate = root.parent
    assert intermediate != paths.repo_root
    shutil.rmtree(root)
    _declare_nested_deliverable(task, paths, source, "errata/correction.md")
    outside = paths.project.parent / "outside-output"
    outside.mkdir()
    if fallback:
        _force_path_fallback(monkeypatch)
    swapped = _swap_root_after_confined(monkeypatch, intermediate, outside, caller="_make_parents")
    if fallback:
        with pytest.raises(publication.PublicationError, match="is redirected"):
            publication.publish(task, paths)
    else:
        with pytest.raises(OSError) as refused:
            publication.publish(task, paths)
        assert refused.value.errno in (errno.ELOOP, errno.ENOTDIR)  # the no-follow open
    assert swapped
    assert list(outside.iterdir()) == []
    assert not task.artifact_publication_complete


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink swap")
@pytest.mark.parametrize("fallback", [False, True], ids=["descriptor", "fallback"])
def test_existing_root_parent_swapped_for_a_link_holding_a_real_root_is_refused(
    publication_case, monkeypatch, fallback
):
    """DW-446: the artifacts root gets no persisted mint-time record — it is
    operator-configured and shared across runs — because `_confined` `lstat`s EVERY
    ancestor and refuses a symlink anywhere in the chain. An EXISTING root's parent
    swapped for a link to a tree holding a REAL artifacts directory (so the root
    path reaches a real, non-link directory a fresh `lstat` of the root would
    accept) refuses both capture and publish with `PublicationError`, and nothing
    is written outside.

    Ablation: have `_confined` skip the components ABOVE `root` (both its `S_ISLNK`
    and its parent-is-a-directory refusal — either alone refuses this swap) and
    publish writes into the outside artifacts directory."""
    task, paths, source = publication_case
    if not fallback and not publication.DIR_FD_ANCHORED_WRITES:
        pytest.skip("descriptor-relative writes are unavailable")
    bind_and_prepare(task, paths, source)
    root = paths.implementation_artifacts
    assert root.is_dir()  # an EXISTING root
    parent = root.parent
    assert parent != paths.repo_root
    outside = paths.project.parent / "outside-output"
    (outside / root.name).mkdir(parents=True)
    parent.rename(parent.with_name(parent.name + "-aside"))
    parent.symlink_to(outside, target_is_directory=True)
    assert root.is_dir() and not root.is_symlink()  # the premise
    if fallback:
        _force_path_fallback(monkeypatch)

    with pytest.raises(publication.PublicationError):
        publication.publish(task, paths)
    fresh = StoryTask(story_key="dw-fix-2", epic=0, dw_ids=["DW-2"])
    with pytest.raises(publication.PublicationError):
        publication.capture(fresh, paths)

    assert list((outside / root.name).iterdir()) == []
    assert not task.artifact_publication_complete


@pytest.mark.skipif(sys.platform != "win32", reason="real win32 directory junction")
def test_a_real_junction_below_the_artifacts_root_is_refused(publication_case, tmp_path):
    """DW-422 on a real win32 junction under the artifacts root: `_confined`
    refuses it, capture inventories it `"nonregular"` without walking it, and a
    deliverable declared beneath it is refused with nothing written into the
    junction's target.

    Ablation: delete the `link_like_stat` refusal in `_confined` (and its arm in
    `walk`) and this fails — the junction is walked and published through."""
    import _winapi  # Windows-only stdlib module, as test_win32_at uses it

    task, paths, source = publication_case
    root = paths.implementation_artifacts
    target = tmp_path / "junction-target"
    target.mkdir()
    (target / "secret.md").write_text("behind the junction")
    junction = root / "linked"
    _winapi.CreateJunction(str(target), str(junction))
    with pytest.raises(publication.PublicationError, match="reparse point"):
        publication._confined(root, junction / "secret.md")
    publication.capture(task, paths)
    assert task.artifact_baseline["linked"] == "nonregular"
    assert "linked/secret.md" not in task.artifact_baseline
    output = source.implementation_artifacts / "linked" / "report.md"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"report")
    (source.implementation_artifacts / "spec.md").write_text(
        "---\nstatus: done\nartifact_deliverables: [linked/report.md]\n---\n"
    )
    bind_and_prepare(task, paths, source)
    with pytest.raises(publication.PublicationError, match="reparse point"):
        publication.publish(task, paths)
    assert sorted(entry.name for entry in target.iterdir()) == ["secret.md"]
    assert not task.artifact_publication_complete


@pytest.mark.skipif(sys.platform != "win32", reason="real win32 directory junction")
def test_a_real_junction_above_the_artifacts_root_is_accepted(publication_case, tmp_path):
    """DW-422 decision "refuse below root only" on a real win32 junction: the
    directory between the repository root and the artifacts root is moved out
    and junctioned back, and `_confined`, `_root` and publish still accept it.

    Ablation: drop the `part.is_relative_to(root)` scope of the reparse refusal
    in `_confined` and this fails with `link-like reparse point`."""
    import _winapi  # Windows-only stdlib module, as test_win32_at uses it

    task, paths, source = publication_case
    root = paths.implementation_artifacts
    intermediate = root.parent
    assert intermediate != paths.repo_root
    relocated = tmp_path / "relocated-output"
    intermediate.rename(relocated)
    _winapi.CreateJunction(str(relocated), str(intermediate))
    assert publication._confined(root, root / "report.bin") is not None
    assert publication._root(paths) == root
    bind_and_prepare(task, paths, source)
    publication.publish(task, paths)
    assert (relocated / root.name / "report.bin").read_bytes() == b"\xff\x00\r\nreport"
    assert task.artifact_publication_complete


@pytest.mark.skipif(not platform_util.HANDLE_ANCHORED_WRITES, reason="handle-anchored creation")
def test_create_directories_refuses_a_redirected_component(publication_case, tmp_path):
    """DW-421: `_create_directories` opens every component no-follow relative to
    the one above — `O_NOFOLLOW` on POSIX, `open_at(O_CREAT | AT_DIRECTORY |
    AT_NOFOLLOW)` on win32 — so a component planted as a redirect (a symlink, or
    a junction on win32) is refused and nothing is created through it.

    Ablation: drop `O_NOFOLLOW` (POSIX arm) or `AT_NOFOLLOW` (win32 arm) from
    `_create_directories` and this fails `DID NOT RAISE` — `inner` is created
    inside `outside`."""
    _task, paths, _source = publication_case
    root = paths.implementation_artifacts
    identity = platform_util.pinned_root_identity(root)
    assert identity is not None
    publication._create_directories(root, root / "plain" / "inner", root_identity=identity)
    assert (root / "plain" / "inner").is_dir()  # positive control
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    _plant_directory_redirect(root / "outer", outside)
    with pytest.raises(OSError) as refused:
        publication._create_directories(root, root / "outer" / "inner", root_identity=identity)
    assert refused.value.errno in (errno.ELOOP, errno.ENOTDIR)
    assert list(outside.iterdir()) == []


# ------------------------------------------------ DW-444: zero-inode artifacts root


class _ZeroInodeStat:
    """A stat stand-in from a filesystem that reports no inode identity: the
    real result with `st_ino` 0 plus any ``overrides``."""

    def __init__(self, real, **overrides):
        self._real = real
        self.st_ino = 0
        for name, value in overrides.items():
            setattr(self, name, value)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _simulate_zero_inode(monkeypatch, *roots, leaves=False, leaf_after_open=None, zero_roots=True):
    """Make every `os.lstat` of each of ``roots`` report `st_ino` 0 (unless
    ``zero_roots`` is False). With ``leaves``, also every regular file's
    `os.lstat` and `os.fstat` below them; ``leaf_after_open`` (overrides) then
    applies to a leaf's `os.lstat` once a regular file has been opened — a leaf
    that changed after its open."""
    wanted = {str(path) for path in roots}
    real_lstat = os.lstat
    real_fstat = os.fstat
    opened = []

    def below(path):
        text = str(path)
        return any(text.startswith(root + os.sep) for root in wanted)

    def lstat(path, *args, **kwargs):
        real = real_lstat(path, *args, **kwargs)
        if str(path) in wanted:
            return _ZeroInodeStat(real) if zero_roots else real
        if leaves and stat.S_ISREG(real.st_mode) and below(path):
            if opened and leaf_after_open is not None:
                return _ZeroInodeStat(real, **leaf_after_open)
            return _ZeroInodeStat(real)
        return real

    def fstat(fd):
        real = real_fstat(fd)
        if stat.S_ISREG(real.st_mode):
            opened.append(fd)
            return _ZeroInodeStat(real)
        return real

    monkeypatch.setattr(os, "lstat", lstat)
    if leaves:
        monkeypatch.setattr(os, "fstat", fstat)


@pytest.fixture
def unpinned_counter():
    """Start and end each zero-inode test with an empty degrade counter, so a
    count never leaks into another test's drain."""
    publication.drain_unpinned_observations()
    yield
    publication.drain_unpinned_observations()


def _drained_count(root):
    drained = publication.drain_unpinned_observations()
    assert [entry[0] for entry in drained] == [str(root)]
    _root, filesystem, count = drained[0]
    assert filesystem == platform_util.filesystem_name(root)
    return count


def test_fallback_capture_on_a_zero_inode_root_records_and_counts(
    publication_case, monkeypatch, unpinned_counter
):
    """DW-444: on a no-dir-fd host, a root whose `lstat` carries no inode used to
    refuse every fallback read, so `capture` refused every DW bundle. The pin now
    degrades to "still a non-link directory on the same device, still zero", the
    baseline is recorded, and the degrade is counted per root with its
    filesystem.

    Ablation: delete the zero-inode branch in `_still_pinned` and this fails
    with `artifact inventory directory was replaced`."""
    task, paths, _source = publication_case
    monkeypatch.setattr(publication, "DIR_FD_ANCHORED_WRITES", False)
    root = paths.implementation_artifacts
    root.mkdir(parents=True, exist_ok=True)
    (root / "existing.md").write_bytes(b"baseline bytes")
    _simulate_zero_inode(monkeypatch, root)
    assert os.lstat(root).st_ino == 0  # the simulation is live
    task.artifact_baseline = None

    publication.capture(task, paths)

    assert task.artifact_baseline == {"existing.md": publication._digest(b"baseline bytes")}
    assert _drained_count(root) >= 2  # the walk's pin plus the file's read pin


def test_fallback_capture_accepts_zero_inode_leaves_under_a_zero_inode_root(
    publication_case, monkeypatch, unpinned_counter
):
    """DW-444 (resolved 2026-09-27): a filesystem that reports no inode for its
    root reports none for its files either, so the strict leaf check could never
    hold and every non-empty artifacts dir paused. `capture` alone accepts an
    identity-less leaf under a degraded root — regular, not a link, same device,
    still no inode, size stable — records its digest, and counts it.

    Ablation: delete the `degraded_leaf_ok` arm in `_probe_destination` (or pass
    False from `capture`) and this fails with `artifact changed during
    inventory`."""
    task, paths, _source = publication_case
    monkeypatch.setattr(publication, "DIR_FD_ANCHORED_WRITES", False)
    root = paths.implementation_artifacts
    (root / "nested").mkdir(parents=True, exist_ok=True)
    (root / "existing.md").write_bytes(b"baseline bytes")
    (root / "nested" / "deep.bin").write_bytes(b"\x00deep")
    _simulate_zero_inode(monkeypatch, root, leaves=True)
    assert os.lstat(root / "existing.md").st_ino == 0  # the leaf simulation is live
    task.artifact_baseline = None

    publication.capture(task, paths)

    assert task.artifact_baseline == {
        "existing.md": publication._digest(b"baseline bytes"),
        "nested": "directory",
        "nested/deep.bin": publication._digest(b"\x00deep"),
    }
    # two walks + per file (read pin + leaf-probe root pin + the leaf itself)
    assert _drained_count(root) >= 2 + 2 * 3


def test_fallback_capture_accepts_a_zero_device_leaf_lstat(
    publication_case, monkeypatch, unpinned_counter
):
    """DW-444: a leaf `lstat` taking CPython's win32 `FindFirstFile` fallback
    reports `st_dev` 0 while the handle `fstat` reports the real volume serial;
    the weak leaf check accepts that 0 rather than refusing on it.

    Ablation: compare `leaf.st_dev == opened.st_dev` strictly in
    `_degraded_leaf_still_names` and this fails with `artifact changed during
    inventory`."""
    task, paths, _source = publication_case
    monkeypatch.setattr(publication, "DIR_FD_ANCHORED_WRITES", False)
    root = paths.implementation_artifacts
    root.mkdir(parents=True, exist_ok=True)
    (root / "existing.md").write_bytes(b"baseline bytes")
    _simulate_zero_inode(monkeypatch, root, leaves=True, leaf_after_open={"st_dev": 0})
    task.artifact_baseline = None

    publication.capture(task, paths)

    assert task.artifact_baseline == {"existing.md": publication._digest(b"baseline bytes")}


def test_fallback_capture_keeps_zero_inode_leaves_strict_under_a_real_root(
    publication_case, monkeypatch, unpinned_counter
):
    """DW-444: the leaf degrade needs a DEGRADED root. Under a root with a real
    inode, an identity-less leaf is still "changed during inventory" and nothing
    is counted.

    Ablation: drop the `root_identity.st_ino != 0` guard in
    `_degraded_leaf_still_names` and this fails `DID NOT RAISE`."""
    task, paths, _source = publication_case
    monkeypatch.setattr(publication, "DIR_FD_ANCHORED_WRITES", False)
    root = paths.implementation_artifacts
    root.mkdir(parents=True, exist_ok=True)
    (root / "existing.md").write_bytes(b"baseline bytes")
    _simulate_zero_inode(monkeypatch, root, leaves=True, zero_roots=False)
    assert os.lstat(root).st_ino != 0 and os.lstat(root / "existing.md").st_ino == 0

    with pytest.raises(publication.PublicationError, match="changed during inventory"):
        publication.capture(task, paths)
    assert publication.drain_unpinned_observations() == []


@pytest.mark.parametrize("change", ["size", "not-regular", "link", "other-device"])
def test_fallback_capture_refuses_a_zero_inode_leaf_that_changed(
    publication_case, monkeypatch, unpinned_counter, change
):
    """DW-444: the weak leaf check still catches a leaf whose fresh `lstat`, taken
    after the open, no longer matches what was streamed — a different size, no
    longer a regular file, a link, or another device — and `capture` raises
    "artifact changed during inventory" as it always has.

    Ablation: drop the matching condition in `_degraded_leaf_still_names` (the
    `st_size`, `S_ISREG` or `st_dev` compare) and that case fails `DID NOT
    RAISE`; the link case is also refused by `_confined`'s re-walk."""
    task, paths, _source = publication_case
    monkeypatch.setattr(publication, "DIR_FD_ANCHORED_WRITES", False)
    root = paths.implementation_artifacts
    root.mkdir(parents=True, exist_ok=True)
    leaf = root / "existing.md"
    leaf.write_bytes(b"baseline bytes")
    real = os.lstat(leaf)
    after = {
        "size": {"st_size": real.st_size + 1},
        "not-regular": {"st_mode": stat.S_IFDIR | 0o755},
        "link": {"st_mode": stat.S_IFLNK | 0o777},
        "other-device": {"st_dev": real.st_dev + 1},
    }[change]
    _simulate_zero_inode(monkeypatch, root, leaves=True, leaf_after_open=after)

    with pytest.raises(publication.PublicationError, match="changed during inventory"):
        publication.capture(task, paths)


def test_zero_inode_leaf_stays_strict_outside_capture(
    publication_case, monkeypatch, unpinned_counter
):
    """DW-444: the leaf degrade is capture's alone. On the same zero-inode root
    and leaf, `_destination_equals` stays False and a plain
    `_destination_observation` (the publish probe) stays incomplete.

    Ablation: default `degraded_leaf_ok` to True in `_probe_destination`, or
    call `_degraded_leaf_still_names` from `_destination_still_names`, and these
    assertions fail."""
    _task, paths, _source = publication_case
    monkeypatch.setattr(publication, "DIR_FD_ANCHORED_WRITES", False)
    root = paths.implementation_artifacts
    root.mkdir(parents=True, exist_ok=True)
    leaf = root / "existing.md"
    leaf.write_bytes(b"baseline bytes")
    _simulate_zero_inode(monkeypatch, root, leaves=True)

    assert not publication._destination_equals(root, leaf, b"baseline bytes")
    observed = publication._destination_observation(root, leaf)
    assert observed is not None and not observed.complete
    assert publication._probe_destination(root, leaf, degraded_leaf_ok=True).observation.complete


@pytest.mark.parametrize("reader", ["contents", "size", "identity"])
def test_fallback_reads_on_a_zero_inode_root_succeed_and_count(
    publication_case, monkeypatch, unpinned_counter, reader
):
    """DW-444: `_open_regular` (under `_contents`), `_file_size` and
    `_destination_path_identity` each read through the degraded pin on a
    zero-inode root, and each read is counted.

    Ablation: delete the zero-inode branch in `_still_pinned` and the contents
    and size readers raise `replaced` while the identity reader returns None."""
    _task, paths, _source = publication_case
    monkeypatch.setattr(publication, "DIR_FD_ANCHORED_WRITES", False)
    root = paths.implementation_artifacts
    root.mkdir(parents=True, exist_ok=True)
    report = root / "report.bin"
    report.write_bytes(b"inside")
    _simulate_zero_inode(monkeypatch, root)

    if reader == "contents":
        assert publication._contents(root, report) == b"inside"
    elif reader == "size":
        assert publication._file_size(root, report) == len(b"inside")
    else:
        leaf = report.lstat()
        assert publication._destination_path_identity(root, report) == (
            publication._FileIdentity(leaf.st_dev, leaf.st_ino, True)
        )
    assert _drained_count(root) == 1


def _set_publish_arm(monkeypatch, arm):
    if arm == "fallback":
        _force_path_fallback(monkeypatch)
    elif arm == "handle":
        monkeypatch.setattr(publication, "DIR_FD_ANCHORED_WRITES", False)
        monkeypatch.setattr(publication, "HANDLE_ANCHORED_WRITES", True)
    else:
        monkeypatch.setattr(publication, "DIR_FD_ANCHORED_WRITES", True)
        monkeypatch.setattr(publication, "HANDLE_ANCHORED_WRITES", True)


@pytest.mark.parametrize("arm", ["fallback", "handle", "dir-fd"])
def test_publish_to_a_zero_inode_root_refuses_before_the_arm_split(
    publication_case, monkeypatch, unpinned_counter, arm
):
    """DW-444: observation degrades on a zero-inode root, writes never do. After
    capture, binding and preparation read through the degraded pin (target and
    source roots both counted), `publish` refuses on EVERY arm — the win32 handle
    arm included — right after `_root`, with a message naming the missing inode
    identity and the filesystem, before any probe, directory or file exists.
    (A root that already read zero at preparation is refused there, pre-merge.)

    Ablation: move `_refuse_zero_inode_root` behind the arm split (into the
    neither-arm branch of `_create_directories`) and every case fails — the
    probe and parent-creation tripwires fire first, and on the handle and dir-fd
    arms that branch is never reached at all."""
    task, paths, source = publication_case
    monkeypatch.setattr(publication, "DIR_FD_ANCHORED_WRITES", False)
    root = paths.implementation_artifacts
    root.mkdir(parents=True, exist_ok=True)
    publication.capture(task, paths)
    # prepared before the root loses its inode: `prepare` itself refuses a
    # zero-inode target (covered by the prepare test below)
    bind_and_prepare(task, paths, source)
    _simulate_zero_inode(monkeypatch, root)
    _set_publish_arm(monkeypatch, arm)
    monkeypatch.setattr(
        publication, "_probe_destination", lambda *_a, **_k: pytest.fail("probed before refusal")
    )
    monkeypatch.setattr(
        publication, "_make_parents", lambda *_a, **_k: pytest.fail("created before refusal")
    )

    with pytest.raises(publication.PublicationError, match="no inode identity") as refused:
        publication.publish(task, paths)

    message = str(refused.value)
    assert platform_util.filesystem_name(root) in message
    assert "publication is refused" in message
    assert not (root / "report.bin").exists()
    assert not task.artifact_publication_complete


def test_prepare_refuses_a_zero_inode_target_root_before_merge(
    publication_case, monkeypatch, unpinned_counter
):
    """DW-444: `publish` runs after the merge, so a zero-inode target root must
    already refuse at `prepare` — before the payload is frozen and before the
    unit is integrated — once a non-empty ignored selection is known. Binding
    still read the source through the degraded pin and counted it.

    Ablation: delete the `_refuse_zero_inode_root` call in `prepare` and this
    fails `DID NOT RAISE` (the refusal would wait for `publish`, post-merge)."""
    task, paths, source = publication_case
    monkeypatch.setattr(publication, "DIR_FD_ANCHORED_WRITES", False)
    root = paths.implementation_artifacts
    root.mkdir(parents=True, exist_ok=True)
    _simulate_zero_inode(monkeypatch, root, source.implementation_artifacts)

    with pytest.raises(publication.PublicationError, match="no inode identity") as refused:
        bind_and_prepare(task, paths, source)

    assert platform_util.filesystem_name(root) in str(refused.value)
    assert task.artifact_payload is None
    counted = {entry[0] for entry in publication.drain_unpinned_observations()}
    assert str(source.implementation_artifacts) in counted


def test_prepare_of_an_empty_selection_never_refuses_a_zero_inode_root(
    publication_case, monkeypatch, unpinned_counter
):
    """DW-444: with no ignored deliverable nothing will be written, so a
    zero-inode target root still prepares (an empty frozen payload)."""
    task, paths, source = publication_case
    monkeypatch.setattr(publication, "DIR_FD_ANCHORED_WRITES", False)
    monkeypatch.setattr(
        publication.verify, "path_tracked", lambda _repo, rel: rel.endswith("spec.md")
    )
    (source.implementation_artifacts / "spec.md").write_text(
        "---\nstatus: done\nartifact_deliverables: []\n---\n"
    )
    root = paths.implementation_artifacts
    root.mkdir(parents=True, exist_ok=True)
    _simulate_zero_inode(monkeypatch, root, source.implementation_artifacts)

    bind_and_prepare(task, paths, source)

    assert task.artifact_payload == {}


def test_publish_of_an_empty_payload_never_refuses_a_zero_inode_root(
    publication_case, monkeypatch, unpinned_counter
):
    """DW-444: the refusal guards WRITES; an empty payload writes nothing, so a
    zero-inode root still latches it complete."""
    task, paths, _source = publication_case
    root = paths.implementation_artifacts
    root.mkdir(parents=True, exist_ok=True)
    task.artifact_payload = {}
    _simulate_zero_inode(monkeypatch, root)
    publication.publish(task, paths)
    assert task.artifact_publication_complete


def _accept_root_as_zero_inode(monkeypatch, root, **after):
    """`_confined` accepts ``root`` as a zero-inode directory; every `os.lstat`
    of ``root`` after that acceptance reports ``after`` — zero inode plus these
    overrides — or, with no overrides, the real (nonzero) inode."""
    confined = publication._confined
    real_lstat = os.lstat
    accepted = []

    def zero_identity(walk_root, path):
        identity = confined(walk_root, path)
        accepted.append(True)
        return None if identity is None else _ZeroInodeStat(identity)

    def lstat(path, *args, **kwargs):
        real = real_lstat(path, *args, **kwargs)
        if accepted and after and str(path) == str(root):
            return _ZeroInodeStat(real, **after)
        return real

    monkeypatch.setattr(publication, "_confined", zero_identity)
    monkeypatch.setattr(os, "lstat", lstat)
    return accepted


@pytest.mark.parametrize("change", ["nonzero-inode", "junction", "other-device", "not-dir"])
def test_zero_inode_pin_refuses_a_root_that_no_longer_matches(
    publication_case, monkeypatch, unpinned_counter, change
):
    """DW-444: the weakened pin still refuses when the fresh `lstat` of a root
    accepted with a zero inode no longer reads as a zero-inode, non-link
    directory on the same device — a root that now reports an inode (a
    different directory), a junction swapped in, another device, or no longer a
    directory. The existing `replaced` refusal fires and nothing is counted.

    Ablation: drop the `st_ino == 0`, `link_like_stat`, `st_dev` or `S_ISDIR`
    condition in `_weak_root_pin` and the matching case fails `DID NOT
    RAISE`."""
    _task, paths, _source = publication_case
    monkeypatch.setattr(publication, "DIR_FD_ANCHORED_WRITES", False)
    root = paths.implementation_artifacts
    root.mkdir(parents=True, exist_ok=True)
    report = root / "report.bin"
    report.write_bytes(b"inside")
    monkeypatch.setattr(platform_util, "_LINK_REPARSE_TAGS", (_JUNCTION_TAG,))
    after = {
        "nonzero-inode": {},
        "junction": {"st_reparse_tag": _JUNCTION_TAG},
        "other-device": {"st_dev": os.lstat(root).st_dev + 1},
        "not-dir": {"st_mode": stat.S_IFREG | 0o644},
    }[change]
    accepted = _accept_root_as_zero_inode(monkeypatch, root, **after)

    with pytest.raises(publication.PublicationError, match="replaced"):
        publication._contents(root, report)
    assert accepted
    assert publication.drain_unpinned_observations() == []


def test_a_nonzero_inode_root_counts_nothing(publication_case, monkeypatch, unpinned_counter):
    """DW-444 control: an ordinary root pins strictly, exactly as before — the
    fallback reads and the publish succeed and no degrade is counted."""
    task, paths, source = publication_case
    _force_path_fallback(monkeypatch)
    publication.capture(task, paths)
    bind_and_prepare(task, paths, source)
    publication.publish(task, paths)
    assert task.artifact_publication_complete
    assert publication.drain_unpinned_observations() == []


def test_still_pinned_never_degrades_a_missing_identity(publication_case, unpinned_counter):
    """DW-444 leaves the None-identity refusal alone: no accepted root, no pin."""
    _task, paths, _source = publication_case
    assert not publication._still_pinned(paths.implementation_artifacts, None)
    assert publication.drain_unpinned_observations() == []
