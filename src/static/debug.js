async function createReservation() {
  const userId = document.getElementById("userId").value.trim();
  const roomId = document.getElementById("roomId").value.trim();
  const date = document.getElementById("resDate").value; // "2025-11-29"
  const startSel = document.getElementById("startTimeSelect").value; // "HH:MM"
  const endSel = document.getElementById("endTimeSelect").value; // "HH:MM"

  if (!userId) {
    alert("user_id は必須です");
    return;
  }
  if (!date || !startSel || !endSel) {
    alert("日付・開始時刻・終了時刻を入力してください");
    return;
  }

  const startIso = `${date}T${startSel}:00+09:00`;
  const endIso = `${date}T${endSel}:00+09:00`;

  const body = {
    user_id: userId,
    room_id: roomId || undefined,
    start_time: startIso,
    end_time: endIso,
  };

  const res = await fetch("/debug/reservations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  const text = await res.text();
  document.getElementById("createReservationResult").textContent =
    `status: ${res.status}\n` + text;
}

async function setOccupancy(occupied) {
  const res = await fetch("/debug/occupancy", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ occupied }),
  });
  const text = await res.text();
  document.getElementById("occupancyResult").textContent =
    `status: ${res.status}\n` + text;
}

async function fetchStatus() {
  const res = await fetch("/api/status");
  const text = await res.text();

  // 生JSONをそのまま表示（失敗時の確認用）
  document.getElementById("statusResult").textContent =
    `status: ${res.status}\n` + text;

  let data = null;
  try {
    data = JSON.parse(text);
  } catch (e) {
    document.getElementById("statusView").textContent =
      "JSONとして解析できませんでした";
    return;
  }

  const keys = [
    "timestamp",
    "room_id",
    "room_state",
    "people_count",
    "is_used",
    "reservation_id",
    "alert",
  ];

  const rows = keys
    .map((k) => {
      const v = data[k];
      return `<tr><th>${k}</th><td>${v == null ? "" : v}</td></tr>`;
    })
    .join("");

  document.getElementById(
    "statusView"
  ).innerHTML = `<table><tbody>${rows}</tbody></table>`;
}

async function fetchReservations() {
  const res = await fetch("/debug/reservations");
  const text = await res.text();

  document.getElementById("reservationsResult").textContent =
    `status: ${res.status}\n` + text;

  let data = null;
  try {
    data = JSON.parse(text);
  } catch (e) {
    document.querySelector("#reservationsTable tbody").innerHTML = "";
    return;
  }

  if (!Array.isArray(data)) {
    document.querySelector("#reservationsTable tbody").innerHTML = "";
    return;
  }

  const tbody = document.querySelector("#reservationsTable tbody");
  const rows = data
    .map((r) => {
      return `
        <tr>
          <td>${r.reservation_id}</td>
          <td>${r.user_id}</td>
          <td>${r.start_time}</td>
          <td>${r.end_time}</td>
          <td>${r.status}</td>
        </tr>
      `;
    })
    .join("");

  tbody.innerHTML = rows;
}

async function fetchPenalty() {
  const userId = document.getElementById("penaltyUserId").value.trim();
  const res = await fetch(`/debug/penalties/${encodeURIComponent(userId)}`);
  const text = await res.text();
  document.getElementById("penaltyResult").textContent =
    `status: ${res.status}\n` + text;
}
async function loadStateParams() {
  const res = await fetch("/debug/state_params");
  const text = await res.text();
  document.getElementById("stateParamsResult").textContent =
    `status: ${res.status}\n` + text;

  let data = null;
  try {
    data = JSON.parse(text);
  } catch (e) {
    return;
  }

  document.getElementById("arrivalBefore").value =
    data.arrival_window_before_sec ?? "";
  document.getElementById("arrivalAfter").value =
    data.arrival_window_after_sec ?? "";
  document.getElementById("gracePeriod").value = data.grace_period_sec ?? "";
  document.getElementById("cleanupMargin").value =
    data.cleanup_margin_sec ?? "";
}

async function updateStateParams() {
  const body = {
    arrival_window_before_sec: parseInt(
      document.getElementById("arrivalBefore").value,
      10
    ),
    arrival_window_after_sec: parseInt(
      document.getElementById("arrivalAfter").value,
      10
    ),
    grace_period_sec: parseInt(
      document.getElementById("gracePeriod").value,
      10
    ),
    cleanup_margin_sec: parseInt(
      document.getElementById("cleanupMargin").value,
      10
    ),
  };

  const res = await fetch("/debug/state_params", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  const text = await res.text();
  document.getElementById("stateParamsResult").textContent =
    `status: ${res.status}\n` + text;
}

function initTimeSelect(selectId) {
  const sel = document.getElementById(selectId);
  sel.innerHTML = "";

  for (let h = 0; h < 24; h++) {
    for (let m = 0; m < 60; m += 5) {
      const hh = String(h).padStart(2, "0");
      const mm = String(m).padStart(2, "0");
      const value = `${hh}:${mm}`;
      const opt = document.createElement("option");
      opt.value = value;
      opt.textContent = value;
      sel.appendChild(opt);
    }
  }
}

async function loadTimeStatus() {
  const res = await fetch("/debug/time");
  const text = await res.text();
  document.getElementById("timeResult").textContent =
    `status: ${res.status}\n` + text;

  let data = null;
  try {
    data = JSON.parse(text);
  } catch (e) {
    return;
  }
  updateTimeStatusView(data);
}

function updateTimeStatusView(data) {
  document.getElementById("timeUseSim").textContent = String(
    data.use_simulated
  );
  document.getElementById("timeScale").textContent = String(data.scale);
  document.getElementById("timeSystemNow").textContent = data.system_now || "";
  document.getElementById("timeCurrentNow").textContent =
    data.current_now || "";
  document.getElementById("timeBaseReal").textContent = data.base_real || "";
  document.getElementById("timeBaseSim").textContent = data.base_sim || "";

  // フォーム側にも反映しておくと便利
  document.getElementById("timeScaleInput").value =
    data.scale != null ? data.scale : 1.0;
}

async function applyTimeSetting() {
  const mode = document.getElementById("timeMode").value;
  const nowStr = document.getElementById("timeNowInput").value.trim();
  const scaleStr = document.getElementById("timeScaleInput").value;
  const scale = parseFloat(scaleStr || "1.0");

  let body = { mode };

  if (mode === "real") {
    // 実時間に戻すだけ
    body = { mode: "real" };
  } else if (mode === "simulated") {
    body = { mode: "simulated", scale };
    if (nowStr) {
      body.now = nowStr;
    }
  } else {
    alert("mode が不正です");
    return;
  }

  const res = await fetch("/debug/time", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  const text = await res.text();
  document.getElementById("timeResult").textContent =
    `status: ${res.status}\n` + text;

  let data = null;
  try {
    data = JSON.parse(text);
  } catch (e) {
    return;
  }
  updateTimeStatusView(data);
}

window.addEventListener("DOMContentLoaded", () => {
  initTimeSelect("startTimeSelect");
  initTimeSelect("endTimeSelect");

  // デフォルトで「今時刻＋α」をセットしたければここでやる
  // 例: 開始 = 5分後, 終了 = 35分後 など
  loadTimeStatus();
});
