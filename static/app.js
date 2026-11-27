/* video-subtitler web UI - vanilla JS, no build step. */
const $ = (sel) => document.querySelector(sel);

const state = {
  file: null,
  jobId: null,
  timer: null,
};

function fmtTime(sec) {
  const total = Math.max(0, Math.round(sec || 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return [h, m, s].map((n) => String(n).padStart(2, "0")).join(":");
}

function setStatus(text) {
  $("#status").textContent = text;
}

function setProgress(frac) {
  $("#barFill").style.width = `${Math.round(Math.min(1, Math.max(0, frac)) * 100)}%`;
}

function pickFile(file) {
  if (!file) return;
  state.file = file;
  $("#fileName").textContent = `${file.name} · ${(file.size / 1048576).toFixed(1)} MB`;
  $("#go").disabled = false;
  setStatus("就绪，点「开始」");
}

$("#drop").addEventListener("click", () => $("#file").click());
$("#file").addEventListener("change", (e) => pickFile(e.target.files[0]));

["dragenter", "dragover"].forEach((ev) =>
  $("#drop").addEventListener(ev, (e) => {
    e.preventDefault();
    $("#drop").classList.add("over");
  })
);
["dragleave", "drop"].forEach((ev) =>
  $("#drop").addEventListener(ev, (e) => {
    e.preventDefault();
    $("#drop").classList.remove("over");
    if (ev === "drop" && e.dataTransfer.files.length) pickFile(e.dataTransfer.files[0]);
  })
);

$("#reset").addEventListener("click", () => {
  if (state.timer) clearInterval(state.timer);
  state.file = null;
  state.jobId = null;
  $("#file").value = "";
  $("#fileName").textContent = "mp4 · mkv · mov · mp3 · wav · m4a（≤ 4 GB）";
  $("#go").disabled = true;
  $("#resultCard").hidden = true;
  setProgress(0);
  setStatus("等待文件");
});

$("#go").addEventListener("click", async () => {
  if (!state.file) return;
  const btn = $("#go");
  btn.disabled = true;
  setStatus("上传中…");
  setProgress(0.02);

  const headers = {
    "Content-Type": "application/octet-stream",
    "X-Filename": encodeURIComponent(state.file.name),
    "X-Model": $("#model").value,
    "X-Task": $("#task").value,
    "X-Language": $("#language").value,
    "X-Max-Chars": $("#maxChars").value || "0",
    "X-Burn": $("#burn").checked ? "1" : "0",
  };

  try {
    const resp = await fetch("/api/jobs", { method: "POST", headers, body: state.file });
    if (!resp.ok) throw new Error(`upload failed: ${resp.status}`);
    const { id } = await resp.json();
    state.jobId = id;
    setStatus("已排队…");
    poll(id);
  } catch (err) {
    setStatus(`出错：${err.message}`);
    btn.disabled = false;
  }
});

function poll(id) {
  if (state.timer) clearInterval(state.timer);
  state.timer = setInterval(async () => {
    try {
      const res = await fetch(`/api/jobs/${id}`);
      if (!res.ok) return;
      const job = await res.json();
      setProgress(job.progress || 0);
      if (job.state === "running") {
        setStatus(job.message ? `${job.message}` : "处理中…");
      }
      if (job.state === "done") {
        clearInterval(state.timer);
        setProgress(1);
        setStatus(`完成 · ${job.segments.length} 条字幕`);
        render(job);
        $("#go").disabled = false;
      }
      if (job.state === "error") {
        clearInterval(state.timer);
        setStatus(`失败：${job.error}`);
        $("#go").disabled = false;
      }
    } catch (e) {
      /* keep polling */
    }
  }, 700);
}

function render(job) {
  $("#resultCard").hidden = false;
  $("#resultTitle").textContent = `结果 · ${job.name}`;

  const dl = $("#downloads");
  dl.innerHTML = "";
  for (const [name, info] of Object.entries(job.files || {})) {
    const a = document.createElement("a");
    a.href = `/api/jobs/${job.id}/file/${encodeURIComponent(name)}`;
    a.textContent = `${info.kind} · ${name}`;
    a.setAttribute("download", name);
    dl.appendChild(a);
  }

  const tbody = $("#segTable tbody");
  tbody.innerHTML = "";
  for (const seg of job.segments) {
    const tr = document.createElement("tr");
    const td1 = document.createElement("td");
    td1.className = "c-time";
    td1.textContent = `${fmtTime(seg.start)} → ${fmtTime(seg.end)}`;
    const td2 = document.createElement("td");
    td2.textContent = seg.text;
    tr.appendChild(td1);
    tr.appendChild(td2);
    tbody.appendChild(tr);
  }
}

fetch("/api/health")
  .then((r) => r.json())
  .then((j) => {
    if (j.version) $("#ver").textContent = `v${j.version}`;
  })
  .catch(() => {});
