from anvil.benchmark import BenchmarkRecorder


def test_disabled_benchmark_recorder_is_noop():
    recorder = BenchmarkRecorder(enabled=False)

    with recorder.phase("phase_seconds"):
        pass
    recorder.set("value", 1)
    recorder.update({"other": 2})

    assert recorder.data is None
    assert recorder.pop("phase_seconds") is None


def test_enabled_benchmark_recorder_records_into_live_data():
    data: dict[str, object] = {"existing": True}
    recorder = BenchmarkRecorder(data=data)

    with recorder.phase("phase_seconds"):
        pass
    recorder.set("value", 1)
    recorder.update({"other": 2})

    assert recorder.data is data
    assert recorder.data["phase_seconds"] >= 0
    assert recorder.data["value"] == 1
    assert recorder.data["other"] == 2
    assert recorder.data["existing"] is True
    assert recorder.pop("value") == 1
    assert "value" not in recorder.data
