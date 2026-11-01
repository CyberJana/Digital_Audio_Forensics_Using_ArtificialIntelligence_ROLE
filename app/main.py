from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import soundfile as sf
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.signal_analysis import analyze_signal

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("AUDIO_FORENSICS_DATA_DIR", ROOT / "data")).resolve()
DB_PATH = DATA_DIR / "forensics.sqlite3"
ALLOWED_EXTENSIONS = {".wav", ".mp3", ".flac", ".aac", ".m4a", ".ogg"}
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_DECODED_SAMPLES = 20_000_000
CHUNK_SIZE = 1024 * 1024

app = FastAPI(title="Audio Evidence Lab", version="0.1.0")
app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with db_connect() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS evidence (
                id TEXT PRIMARY KEY,
                case_number TEXT NOT NULL,
                examiner TEXT NOT NULL,
                source TEXT NOT NULL,
                original_name TEXT NOT NULL,
                original_path TEXT NOT NULL,
                working_path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                md5 TEXT NOT NULL,
                working_sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                metadata_json TEXT NOT NULL,
                analysis_json TEXT,
                acquired_at_utc TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                evidence_id TEXT NOT NULL REFERENCES evidence(id),
                event_type TEXT NOT NULL,
                actor TEXT NOT NULL,
                details_json TEXT NOT NULL,
                timestamp_utc TEXT NOT NULL
            );
        """)


def log_event(evidence_id: str, event_type: str, actor: str, details: dict) -> None:
    with db_connect() as db:
        db.execute(
            "INSERT INTO events (evidence_id,event_type,actor,details_json,timestamp_utc) VALUES (?,?,?,?,?)",
            (evidence_id, event_type, actor, json.dumps(details), utc_now()),
        )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_audio(path: Path) -> tuple[np.ndarray, int, dict]:
    """Decode with SoundFile first, then FFmpeg where present; never modify the source."""
    try:
        info = sf.info(str(path))
        if info.frames * info.channels > MAX_DECODED_SAMPLES:
            raise ValueError("Decoded audio exceeds the 20 million sample analysis limit")
        samples, sample_rate = sf.read(str(path), dtype="float32", always_2d=True)
        if samples.size == 0:
            raise ValueError("The audio contains no decodable samples")
        metadata = {
            "format": info.format,
            "subtype": info.subtype,
            "channels": info.channels,
            "sample_rate_hz": info.samplerate,
            "frames": info.frames,
            "duration_seconds": round(info.duration, 3),
        }
        return samples, int(sample_rate), metadata
    except ValueError as error:
        if str(error).startswith("Decoded audio exceeds"):
            raise
        soundfile_error = error
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise ValueError(
                "This media format could not be decoded by SoundFile. Install FFmpeg and add it to PATH."
            ) from soundfile_error
        return decode_with_ffmpeg(path, ffmpeg)
    except (RuntimeError, OSError) as soundfile_error:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise ValueError(
                "This media format could not be decoded by SoundFile. Install FFmpeg and add it to PATH."
            ) from soundfile_error
        return decode_with_ffmpeg(path, ffmpeg)


def decode_with_ffmpeg(path: Path, ffmpeg: str) -> tuple[np.ndarray, int, dict]:
    with tempfile.TemporaryDirectory(prefix="audio-forensics-") as directory:
        decoded = Path(directory) / "decoded.wav"
        try:
            subprocess.run(
                [ffmpeg, "-nostdin", "-v", "error", "-i", str(path), "-map", "0:a:0",
                 "-acodec", "pcm_s16le", "-fs", "40000000", str(decoded)],
                check=True, timeout=60, capture_output=True,
            )
            info = sf.info(str(decoded))
            if info.frames * info.channels > MAX_DECODED_SAMPLES:
                raise ValueError("Decoded audio exceeds the 20 million sample analysis limit")
            samples, sample_rate = sf.read(str(decoded), dtype="float32", always_2d=True)
            if samples.size == 0:
                raise ValueError("The audio contains no decodable samples")
        except ValueError:
            raise
        except (subprocess.SubprocessError, RuntimeError) as error:
            raise ValueError("Audio decoding failed; verify that the upload is a supported audio file.") from error
        return samples, int(sample_rate), {
            "format": path.suffix.lstrip(".").upper(),
            "subtype": info.subtype,
            "channels": info.channels,
            "sample_rate_hz": info.samplerate,
            "frames": info.frames,
            "duration_seconds": round(info.duration, 3),
            "decoded_with": "FFmpeg",
        }


def get_evidence(evidence_id: str) -> dict:
    try:
        parsed_id = str(uuid.UUID(evidence_id))
    except ValueError as error:
        raise HTTPException(status_code=404, detail="Evidence not found") from error
    with db_connect() as db:
        row = db.execute("SELECT * FROM evidence WHERE id=?", (parsed_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Evidence not found")
    item = dict(row)
    item["metadata"] = json.loads(item.pop("metadata_json"))
    item["analysis"] = json.loads(item.pop("analysis_json")) if item["analysis_json"] else None
    item.pop("original_path")
    item.pop("working_path")
    return item


def safe_filename(filename: str) -> str:
    return Path(filename.replace("\\", "/")).name[:200] or "evidence-audio"


def remove_evidence_directory(directory: Path) -> None:
    def make_writable_and_retry(function, target, _error) -> None:
        os.chmod(target, 0o600)
        function(target)

    shutil.rmtree(directory, onerror=make_writable_and_retry)


def verify_copy_hash(evidence_id: str, kind: str, path: Path, expected: str, actor: str) -> None:
    actual = sha256_file(path) if path.is_file() else None
    valid = actual == expected
    log_event(evidence_id, "evidence_integrity_verified" if valid else "evidence_integrity_failure",
              actor, {"copy": kind, "expected_sha256": expected, "actual_sha256": actual})
    if not valid:
        raise HTTPException(status_code=409, detail=f"{kind.capitalize()} evidence hash mismatch; operation stopped")


def pdf_text(value: object) -> str:
    """Keep user-provided Unicode traceable in reports using the built-in PDF font."""
    return str(value).encode("ascii", "backslashreplace").decode("ascii")


@app.on_event("startup")
def startup() -> None:
    initialize_db()


@app.get("/")
def home() -> FileResponse:
    return FileResponse(ROOT / "app" / "static" / "index.html")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "service": "Audio Evidence Lab", "time_utc": utc_now()}


@app.post("/api/evidence")
async def upload_evidence(
    file: UploadFile = File(...),
    case_number: str = Form(..., min_length=1, max_length=100),
    examiner: str = Form(..., min_length=1, max_length=120),
    source: str = Form("", max_length=500),
) -> dict:
    original_name = safe_filename(file.filename or "")
    extension = Path(original_name).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=415, detail=f"Unsupported extension. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}")
    if not case_number.strip() or not examiner.strip():
        raise HTTPException(status_code=422, detail="Case number and examiner must not be blank")

    initialize_db()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    evidence_id = str(uuid.uuid4())
    evidence_dir = DATA_DIR / "evidence" / evidence_id
    try:
        with tempfile.NamedTemporaryFile(dir=DATA_DIR, prefix=".upload-", delete=False) as target:
            temp_path = Path(target.name)
            sha_digest = hashlib.sha256()
            md5_digest = hashlib.md5(usedforsecurity=False)
            total = 0
            while chunk := await file.read(CHUNK_SIZE):
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="Upload exceeds the 100 MiB limit")
                sha_digest.update(chunk)
                md5_digest.update(chunk)
                target.write(chunk)
        if total == 0:
            raise HTTPException(status_code=400, detail="The uploaded file is empty")
        try:
            _, _, metadata = read_audio(temp_path)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

        evidence_dir.mkdir(parents=True, exist_ok=False)
        original_path = evidence_dir / f"original{extension}"
        working_path = evidence_dir / f"working-copy{extension}"
        os.replace(temp_path, original_path)
        temp_path = None
        shutil.copyfile(original_path, working_path)
        working_hash = sha256_file(working_path)
        if working_hash != sha_digest.hexdigest():
            raise RuntimeError("Working-copy integrity verification failed")
        try:
            original_path.chmod(0o444)
        except OSError:
            pass

        acquired = utc_now()
        with db_connect() as db:
            db.execute(
                """INSERT INTO evidence
                   (id,case_number,examiner,source,original_name,original_path,working_path,
                    sha256,md5,working_sha256,size_bytes,metadata_json,acquired_at_utc)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (evidence_id, case_number.strip(), examiner.strip(), source.strip(), original_name,
                 str(original_path), str(working_path), sha_digest.hexdigest(), md5_digest.hexdigest(),
                 working_hash, total, json.dumps(metadata), acquired),
            )
            db.execute(
                "INSERT INTO events (evidence_id,event_type,actor,details_json,timestamp_utc) VALUES (?,?,?,?,?)",
                (evidence_id, "evidence_acquired", examiner.strip(), json.dumps({
                    "original_name": original_name, "size_bytes": total,
                    "sha256": sha_digest.hexdigest(), "working_copy_sha256": working_hash,
                }), acquired),
            )
        return get_evidence(evidence_id)
    except HTTPException:
        if evidence_dir.exists():
            remove_evidence_directory(evidence_dir)
        raise
    except Exception as error:
        if evidence_dir.exists():
            remove_evidence_directory(evidence_dir)
        raise HTTPException(status_code=500, detail="Evidence acquisition failed; no record was retained") from error
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()
        await file.close()


@app.get("/api/evidence")
def list_evidence() -> list[dict]:
    initialize_db()
    with db_connect() as db:
        ids = db.execute("SELECT id FROM evidence ORDER BY acquired_at_utc DESC").fetchall()
    return [get_evidence(row["id"]) for row in ids]


@app.get("/api/evidence/{evidence_id}")
def evidence_detail(evidence_id: str) -> dict:
    return get_evidence(evidence_id)


@app.post("/api/evidence/{evidence_id}/analyze")
def analyze_evidence(evidence_id: str) -> dict:
    item = get_evidence(evidence_id)
    with db_connect() as db:
        row = db.execute("SELECT original_path,working_path FROM evidence WHERE id=?", (evidence_id,)).fetchone()
    verify_copy_hash(evidence_id, "original", Path(row["original_path"]), item["sha256"], item["examiner"])
    working_path = Path(row["working_path"])
    verify_copy_hash(evidence_id, "working copy", working_path, item["working_sha256"], item["examiner"])
    try:
        samples, sample_rate, metadata = read_audio(working_path)
        result = analyze_signal(samples, sample_rate)
    except (ValueError, RuntimeError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    result["metadata"] = metadata
    result["analyzed_at_utc"] = utc_now()
    with db_connect() as db:
        db.execute("UPDATE evidence SET analysis_json=? WHERE id=?", (json.dumps(result), evidence_id))
    log_event(evidence_id, "signal_screening_completed", item["examiner"], {
        "observation_count": len(result["observations"]), "method": "deterministic signal screening",
    })
    return result


@app.get("/api/evidence/{evidence_id}/events")
def evidence_events(evidence_id: str) -> list[dict]:
    get_evidence(evidence_id)
    with db_connect() as db:
        rows = db.execute(
            "SELECT event_type,actor,details_json,timestamp_utc FROM events WHERE evidence_id=? ORDER BY id",
            (evidence_id,),
        ).fetchall()
    return [{
        "event_type": row["event_type"], "actor": row["actor"],
        "details": json.loads(row["details_json"]), "timestamp_utc": row["timestamp_utc"],
    } for row in rows]


@app.get("/api/evidence/{evidence_id}/report.pdf")
def evidence_report(evidence_id: str) -> Response:
    item = get_evidence(evidence_id)
    if item["analysis"] is None:
        raise HTTPException(status_code=409, detail="Run signal screening before generating the report")
    with db_connect() as db:
        paths = db.execute("SELECT original_path,working_path FROM evidence WHERE id=?", (evidence_id,)).fetchone()
    verify_copy_hash(evidence_id, "original", Path(paths["original_path"]), item["sha256"], item["examiner"])
    verify_copy_hash(evidence_id, "working copy", Path(paths["working_path"]), item["working_sha256"], item["examiner"])
    analysis = item["analysis"]
    events = evidence_events(evidence_id)
    output = tempfile.SpooledTemporaryFile()
    doc = SimpleDocTemplate(output, pagesize=letter, rightMargin=0.65 * inch, leftMargin=0.65 * inch)
    styles = getSampleStyleSheet()
    heading = ParagraphStyle("Section", parent=styles["Heading2"], textColor=colors.HexColor("#20c7b5"))
    story = [
        Paragraph("Digital Audio Examination - Draft", styles["Title"]),
        Paragraph("Automated observations for examiner review; not a finding of authenticity or manipulation.", styles["Italic"]),
        Spacer(1, 14),
        Paragraph("Case and acquisition", heading),
    ]
    rows = [
        ["Case number", pdf_text(item["case_number"])], ["Evidence ID", item["id"]],
        ["Examiner", pdf_text(item["examiner"])], ["Source", pdf_text(item["source"] or "Not supplied")],
        ["Original filename", pdf_text(item["original_name"])], ["Acquired (UTC)", item["acquired_at_utc"]],
        ["Original SHA-256", item["sha256"]], ["Working copy SHA-256", item["working_sha256"]],
        ["Original MD5", item["md5"]], ["File size", f'{item["size_bytes"]} bytes'],
        ["Metadata", json.dumps(item["metadata"], ensure_ascii=True)],
    ]
    table = Table(rows, colWidths=[1.55 * inch, 5.55 * inch], repeatRows=0)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eff4f5")),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#cbd5da")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("WORDWRAP", (0, 0), (-1, -1), "CJK"),
    ]))
    story.extend([table, Spacer(1, 14), Paragraph("Method and limitations", heading)])
    story.append(Paragraph(
        "A deterministic signal-level screening was run on the separately stored working copy. "
        "The original SHA-256 and working-copy SHA-256 are recorded above. The checks are not a "
        "validated forensic classifier; no calibrated confidence or conclusion about authenticity "
        "is produced. Compression, recording conditions, content, and benign edits can create similar signals. "
        "An examiner must verify the evidence, parameters, and observations and document any conclusion.",
        styles["BodyText"],
    ))
    story.extend([Spacer(1, 12), Paragraph("Signal observations", heading)])
    story.append(Paragraph(f'Duration: {analysis["duration_seconds"]} s; sample rate: {analysis["sample_rate_hz"]} Hz; '
                           f'channels: {analysis["channels"]}; peak amplitude: {analysis["peak_amplitude"]}.',
                           styles["BodyText"]))
    observations = analysis["observations"]
    if not observations:
        story.append(Paragraph("No observations met the screening thresholds. This does not establish that the audio is authentic.", styles["BodyText"]))
    for observation in observations:
        story.append(Paragraph(
            f'{observation["start_seconds"]:.3f} to {observation["end_seconds"]:.3f} s - '
            f'{observation["description"]} [{observation["kind"]}]',
            styles["BodyText"],
        ))
    story.extend([Spacer(1, 12), Paragraph("Chain-of-custody application events", heading)])
    for event in events:
        story.append(Paragraph(
            f'{event["timestamp_utc"]} - {event["event_type"]} by {pdf_text(event["actor"])}: '
            f'{json.dumps(event["details"], ensure_ascii=True)}',
            styles["BodyText"],
        ))
    story.extend([Spacer(1, 12), Paragraph("Examiner comments and conclusion", heading),
                  Paragraph("To be completed and verified by the examiner.", styles["BodyText"])])
    doc.build(story)
    output.seek(0)
    pdf = output.read()
    output.close()
    return Response(
        content=pdf, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="audio-examination-{evidence_id}.pdf"'},
    )
