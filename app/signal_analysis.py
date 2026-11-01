"""Deterministic signal-level screening; not a trained tampering detector."""

from __future__ import annotations

import numpy as np


def analyze_signal(audio: np.ndarray, sample_rate: int) -> dict:
    """Return visualisation data and cautious observations for mono or multichannel audio."""
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    if audio.size == 0:
        raise ValueError("audio must not be empty")

    samples = np.asarray(audio, dtype=np.float32)
    if samples.ndim == 2:
        mono = samples.mean(axis=1)
    elif samples.ndim == 1:
        mono = samples
    else:
        raise ValueError("audio must be mono or multichannel")
    mono = np.nan_to_num(mono, copy=False)

    duration = len(mono) / sample_rate
    frame_size = max(1, int(sample_rate * 0.02))
    frame_count = (len(mono) + frame_size - 1) // frame_size
    padded = np.pad(mono, (0, frame_count * frame_size - len(mono)))
    frames = padded.reshape(frame_count, frame_size)
    rms = np.sqrt(np.mean(np.square(frames, dtype=np.float64), axis=1))
    frame_times = (np.arange(frame_count) * frame_size / sample_rate).tolist()

    observations: list[dict] = []
    silence = rms < 0.003
    starts = np.flatnonzero(silence & ~np.r_[False, silence[:-1]])
    ends = np.flatnonzero(silence & ~np.r_[silence[1:], False]) + 1
    for start, end in zip(starts, ends):
        start_time = start * frame_size / sample_rate
        end_time = min(end * frame_size / sample_rate, duration)
        if end_time - start_time >= 0.5:
            observations.append({
                "kind": "low_level_interval",
                "start_seconds": round(start_time, 3),
                "end_seconds": round(end_time, 3),
                "description": "Sustained low signal level; may be silence or a quiet passage.",
            })

    clipped = np.abs(mono) >= 0.999
    clipped_count = int(np.count_nonzero(clipped))
    if clipped_count:
        clip_frames = np.flatnonzero(
            np.any(np.pad(clipped, (0, frame_count * frame_size - len(clipped))).reshape(frame_count, frame_size), axis=1)
        )
        observations.append({
            "kind": "near_full_scale_samples",
            "start_seconds": round(float(clip_frames[0] * frame_size / sample_rate), 3),
            "end_seconds": round(float(min((clip_frames[-1] + 1) * frame_size / sample_rate, duration)), 3),
            "description": f"{clipped_count} samples are at or near full scale; clipping is possible.",
        })

    if len(rms) > 1:
        ratios = (rms[1:] + 1e-6) / (rms[:-1] + 1e-6)
        jumps = np.flatnonzero((np.maximum(ratios, 1 / ratios) > 8) & (np.abs(np.diff(rms)) > 0.08))
        for index in jumps[:100]:
            observations.append({
                "kind": "amplitude_change",
                "start_seconds": round(float((index + 1) * frame_size / sample_rate), 3),
                "end_seconds": round(float(min((index + 2) * frame_size / sample_rate, duration)), 3),
                "description": "Abrupt frame-to-frame RMS change; inspect in context.",
            })

    # A short-time spectrum is included for visual inspection, not classification.
    target_frames = 160
    hop = max(frame_size, int(np.ceil(len(mono) / target_frames)))
    fft_size = max(256, 1 << (max(1, hop) - 1).bit_length())
    centers = np.linspace(0, max(0, len(mono) - 1), min(target_frames, max(1, len(mono) // hop + 1))).astype(int)
    window = np.hanning(fft_size).astype(np.float32)
    spectra = []
    for center in centers:
        left = max(0, center - fft_size // 2)
        segment = mono[left:left + fft_size]
        if len(segment) < fft_size:
            segment = np.pad(segment, (0, fft_size - len(segment)))
        spectra.append(np.abs(np.fft.rfft(segment * window)))
    spectrum = np.mean(np.stack(spectra), axis=0)
    spectrum_db = 20 * np.log10(np.maximum(spectrum, 1e-8))
    spectrum_bins = np.linspace(0, len(spectrum_db) - 1, 96).astype(int)
    spectrum_view = spectrum_db[spectrum_bins].tolist()

    # Compress STFT magnitudes into a compact log-frequency image for the UI.
    spectrogram = np.stack(spectra, axis=1)
    spectral_energy = np.linalg.norm(spectrogram, axis=0)
    normalized_spectra = spectrogram / np.maximum(spectral_energy, 1e-8)
    if normalized_spectra.shape[1] > 1:
        similarity = np.sum(normalized_spectra[:, 1:] * normalized_spectra[:, :-1], axis=0)
        changed = np.flatnonzero(
            (similarity < 0.35) & (spectral_energy[1:] > 1e-5) & (spectral_energy[:-1] > 1e-5)
        )
        for index in changed[:100]:
            observations.append({
                "kind": "spectral_change",
                "start_seconds": round(float(centers[index + 1] / sample_rate), 3),
                "end_seconds": round(float(min(centers[index + 1] / sample_rate + hop / sample_rate, duration)), 3),
                "description": "Abrupt change in normalized spectral shape; inspect in context.",
            })

    frequency_edges = np.geomspace(1, max(2, sample_rate / 2), 49)
    frequency_bins = np.fft.rfftfreq(fft_size, 1 / sample_rate)
    mel_view = []
    for lower, upper in zip(frequency_edges[:-1], frequency_edges[1:]):
        mask = (frequency_bins >= lower) & (frequency_bins < upper)
        band = np.mean(spectrogram[mask], axis=0) if np.any(mask) else np.zeros(spectrogram.shape[1])
        mel_view.append((20 * np.log10(np.maximum(band, 1e-8))).tolist())

    peak = float(np.max(np.abs(mono)))
    return {
        "duration_seconds": round(duration, 3),
        "sample_rate_hz": int(sample_rate),
        "channels": int(audio.shape[1]) if audio.ndim == 2 else 1,
        "peak_amplitude": round(peak, 6),
        "rms_frames": [round(float(value), 6) for value in rms.tolist()],
        "frame_times_seconds": [round(float(value), 3) for value in frame_times],
        "spectrum_db": [round(float(value), 2) for value in spectrum_view],
        "spectrogram_db": [[round(float(value), 1) for value in row] for row in mel_view],
        "observations": observations,
        "limitations": "Automated signal observations only. They are not proof of manipulation or authenticity.",
    }
