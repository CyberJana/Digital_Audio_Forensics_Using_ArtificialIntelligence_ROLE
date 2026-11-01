import numpy as np
import pytest

from app.signal_analysis import analyze_signal


def test_identifies_long_low_level_interval_and_returns_visualization_data():
    sample_rate = 1000
    audio = np.full(sample_rate * 2, 0.1, dtype=np.float32)
    audio[500:1500] = 0

    result = analyze_signal(audio, sample_rate)

    assert result["duration_seconds"] == 2
    assert result["sample_rate_hz"] == sample_rate
    assert len(result["rms_frames"]) == 100
    assert len(result["spectrum_db"]) == 96
    assert len(result["spectrogram_db"]) == 48
    assert any(item["kind"] == "low_level_interval" for item in result["observations"])
    assert "not proof" in result["limitations"]


def test_reports_near_full_scale_samples():
    audio = np.zeros(1000, dtype=np.float32)
    audio[400:410] = 1.0
    result = analyze_signal(audio, 1000)
    assert any(item["kind"] == "near_full_scale_samples" for item in result["observations"])


@pytest.mark.parametrize("audio,sample_rate", [(np.array([]), 1000), (np.ones(5), 0)])
def test_rejects_empty_audio_or_invalid_sample_rate(audio, sample_rate):
    with pytest.raises(ValueError):
        analyze_signal(audio, sample_rate)
