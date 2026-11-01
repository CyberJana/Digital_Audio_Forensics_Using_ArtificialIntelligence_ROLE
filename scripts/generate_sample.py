"""Generate a synthetic tone recording for a dashboard smoke test, not model validation."""

from pathlib import Path

import numpy as np
import soundfile as sf


def main() -> None:
    sample_rate = 16_000
    time = np.arange(sample_rate * 6, dtype=np.float32) / sample_rate
    audio = 0.24 * np.sin(2 * np.pi * 440 * time)
    audio[sample_rate * 2:sample_rate * 3] = 0
    audio[sample_rate * 4:] *= 0.15
    output = Path("synthetic-demo.wav")
    sf.write(output, audio, sample_rate)
    print(f"Wrote {output.resolve()} (synthetic demonstration audio; not forensic evidence).")


if __name__ == "__main__":
    main()
