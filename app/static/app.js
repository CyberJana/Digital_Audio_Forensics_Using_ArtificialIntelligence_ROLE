const $ = (selector) => document.querySelector(selector);
let selectedId = null;
let records = [];

function setStatus(message, error = false) {
  const status = $("#status");
  status.textContent = message;
  status.classList.toggle("error", error);
}

async function request(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try { message = (await response.json()).detail || message; } catch {}
    throw new Error(message);
  }
  return response;
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

async function loadInventory() {
  const response = await request("/api/evidence");
  records = await response.json();
  $("#evidence-count").textContent = String(records.length).padStart(2, "0");
  const list = $("#evidence-list");
  if (!records.length) {
    list.innerHTML = '<p class="empty">No evidence registered in this local workspace.</p>';
    return;
  }
  list.innerHTML = records.map((item) => `
    <button class="evidence-row ${selectedId === item.id ? "selected" : ""}" data-id="${esc(item.id)}">
      <div class="evidence-top"><strong>${esc(item.original_name)}</strong><span>${esc(item.case_number)}</span></div>
      <small>${esc(item.examiner)} · ${new Date(item.acquired_at_utc).toLocaleString()}</small>
    </button>`).join("");
  list.querySelectorAll(".evidence-row").forEach((row) => row.addEventListener("click", () => selectEvidence(row.dataset.id)));
}

function drawLine(canvas, values, color, baselineZero = false) {
  const rect = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(rect.width * ratio));
  canvas.height = Math.max(1, Math.floor(rect.height * ratio));
  const context = canvas.getContext("2d");
  context.scale(ratio, ratio);
  const width = rect.width;
  const height = rect.height;
  context.clearRect(0, 0, width, height);
  context.strokeStyle = "#293d45";
  context.lineWidth = 1;
  for (let i = 1; i <= 3; i++) {
    const y = (height * i) / 4;
    context.beginPath(); context.moveTo(0, y); context.lineTo(width, y); context.stroke();
  }
  if (!values?.length) return;
  const min = baselineZero ? 0 : Math.min(...values);
  const max = baselineZero ? Math.max(...values, 1e-6) : Math.max(...values);
  const spread = Math.max(max - min, 1e-6);
  context.strokeStyle = color;
  context.lineWidth = 1.6;
  context.beginPath();
  values.forEach((value, index) => {
    const x = (index / Math.max(1, values.length - 1)) * width;
    const y = height - 7 - ((value - min) / spread) * (height - 14);
    if (index === 0) context.moveTo(x, y); else context.lineTo(x, y);
  });
  context.stroke();
}

function drawSpectrogram(canvas, rows) {
  const rect = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(rect.width * ratio));
  canvas.height = Math.max(1, Math.floor(rect.height * ratio));
  const context = canvas.getContext("2d");
  context.scale(ratio, ratio);
  context.fillStyle = "#101b22";
  context.fillRect(0, 0, rect.width, rect.height);
  if (!rows?.length) return;
  const columns = rows[0].length;
  const flat = rows.flat();
  const low = Math.min(...flat);
  const high = Math.max(...flat);
  const image = context.createImageData(columns, rows.length);
  for (let y = 0; y < rows.length; y++) {
    for (let x = 0; x < columns; x++) {
      const n = Math.max(0, Math.min(1, (rows[rows.length - 1 - y][x] - low) / Math.max(1, high - low)));
      const pixel = (y * columns + x) * 4;
      image.data[pixel] = Math.round(12 + n * 45);
      image.data[pixel + 1] = Math.round(35 + n * 190);
      image.data[pixel + 2] = Math.round(50 + n * 170);
      image.data[pixel + 3] = 255;
    }
  }
  const temp = document.createElement("canvas");
  temp.width = columns;
  temp.height = rows.length;
  temp.getContext("2d").putImageData(image, 0, 0);
  context.imageSmoothingEnabled = false;
  context.drawImage(temp, 0, 0, rect.width, rect.height);
}

async function selectEvidence(id) {
  try {
    selectedId = id;
    const response = await request(`/api/evidence/${encodeURIComponent(id)}`);
    const item = await response.json();
    $("#detail-panel").classList.remove("hidden");
    $("#detail-title").textContent = item.original_name;
    const metadata = [
      ["Case", item.case_number], ["Evidence ID", item.id], ["Examiner", item.examiner],
      ["Acquired · UTC", item.acquired_at_utc], ["SHA-256 · original", item.sha256],
      ["SHA-256 · working", item.working_sha256], ["Size", `${(item.size_bytes / 1048576).toFixed(2)} MiB`],
      ["Audio", `${item.metadata.duration_seconds}s · ${item.metadata.sample_rate_hz} Hz · ${item.metadata.channels} ch`],
    ];
    $("#metadata").innerHTML = metadata.map(([label, value]) =>
      `<div class="meta-cell"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join("");
    if (item.analysis) renderAnalysis(item.analysis); else clearAnalysis();
    await loadEvents(id);
    await loadInventory();
    setStatus("");
    $("#detail-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) { setStatus(error.message, true); }
}

function clearAnalysis() {
  drawLine($("#waveform"), [], "#33d6bc");
  drawLine($("#spectrum"), [], "#70aaff");
  drawSpectrogram($("#spectrogram"), []);
  $("#observations").innerHTML = '<p class="empty">Run screening to display measurable observations.</p>';
}

function renderAnalysis(data) {
  drawLine($("#waveform"), data.rms_frames, "#33d6bc", true);
  drawLine($("#spectrum"), data.spectrum_db, "#70aaff");
  drawSpectrogram($("#spectrogram"), data.spectrogram_db);
  const observations = data.observations || [];
  $("#observations").innerHTML = observations.length
    ? observations.map((item) => `<div class="observation"><time>${item.start_seconds.toFixed(3)}–${item.end_seconds.toFixed(3)} s</time><span>${esc(item.description)}</span></div>`).join("")
    : '<p class="empty">No observations met these screening thresholds. This is not evidence that the audio is authentic.</p>';
}

async function loadEvents(id) {
  const response = await request(`/api/evidence/${encodeURIComponent(id)}/events`);
  const events = await response.json();
  $("#events").innerHTML = events.map((event) =>
    `<div class="event-row"><strong>${esc(event.event_type)}</strong> · ${esc(event.timestamp_utc)} · ${esc(event.actor)}</div>`).join("");
}

$("#upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = event.currentTarget.querySelector("button[type=submit]");
  submit.disabled = true;
  setStatus("Acquiring evidence and verifying the working copy…");
  try {
    const response = await request("/api/evidence", { method: "POST", body: new FormData(event.currentTarget) });
    const item = await response.json();
    await loadInventory();
    await selectEvidence(item.id);
    event.currentTarget.reset();
    setStatus("Evidence registered; the original and working-copy hashes match.");
  } catch (error) { setStatus(error.message, true); }
  finally { submit.disabled = false; }
});

$("#analyze").addEventListener("click", async () => {
  if (!selectedId) return;
  const button = $("#analyze");
  button.disabled = true;
  setStatus("Verifying the working copy and screening the signal…");
  try {
    const response = await request(`/api/evidence/${encodeURIComponent(selectedId)}/analyze`, { method: "POST" });
    renderAnalysis(await response.json());
    await loadEvents(selectedId);
    setStatus("Signal screening completed. Interpret observations with examiner review.");
  } catch (error) { setStatus(error.message, true); }
  finally { button.disabled = false; }
});

$("#report").addEventListener("click", async () => {
  if (!selectedId) return;
  try {
    const response = await request(`/api/evidence/${encodeURIComponent(selectedId)}/report.pdf`);
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url; link.download = `audio-examination-${selectedId}.pdf`; link.click();
    URL.revokeObjectURL(url);
    setStatus("Draft examination report downloaded.");
  } catch (error) { setStatus(error.message, true); }
});

$("#refresh").addEventListener("click", () => loadInventory().catch((error) => setStatus(error.message, true)));
window.addEventListener("resize", () => {
  if (!selectedId) return;
  const item = records.find((record) => record.id === selectedId);
  if (item?.analysis) renderAnalysis(item.analysis);
});
loadInventory().catch((error) => setStatus(error.message, true));
