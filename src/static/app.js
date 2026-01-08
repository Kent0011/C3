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

function initDurationSelect(selectId) {
  const sel = document.getElementById(selectId);
  sel.innerHTML = "";

  // ここは MIN_RESERVE_MINUTES / MAX_RESERVE_MINUTES をフロントで知らないので、
  // ひとまず 30, 60, 90, 120 分に固定しておく。
  // 将来 env と同期させたいなら、/api/config 的なものを別途作る必要がある。
  const durations = [30, 60, 90, 120];

  for (const d of durations) {
    const opt = document.createElement("option");
    opt.value = String(d); // 後で parseInt するので文字列でOK
    opt.textContent = `${d} 分`;
    sel.appendChild(opt);
  }
}

function setTodayToDateInput(inputId) {
  const input = document.getElementById(inputId);
  const now = new Date();
  // ローカルタイムをそのまま使う（JST環境ならこれでOK）
  const yyyy = now.getFullYear();
  const mm = String(now.getMonth() + 1).padStart(2, "0");
  const dd = String(now.getDate()).padStart(2, "0");
  input.value = `${yyyy}-${mm}-${dd}`;
}

function hmToMinutes(hm) {
  const [hh, mm] = hm.split(":").map((v) => parseInt(v, 10));
  if (Number.isNaN(hh) || Number.isNaN(mm)) return null;
  return hh * 60 + mm;
}

function minutesToHHMM(totalMinutes) {
  const h = Math.floor(totalMinutes / 60);
  const m = totalMinutes % 60;
  const hhStr = String(h).padStart(2, "0");
  const mmStr = String(m).padStart(2, "0");
  return `${hhStr}:${mmStr}`;
}

/**
 * 日時文字列を見やすい形式に変換する (YYYY/MM/DD HH:MM)
 */
function formatDateTime(dateStr) {
  if (!dateStr) return "";
  const d = new Date(dateStr);
  if (isNaN(d.getTime())) return dateStr; // パース失敗時はそのまま返す

  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  const hh = String(d.getHours()).padStart(2, "0");
  const min = String(d.getMinutes()).padStart(2, "0");

  return `${yyyy}/${mm}/${dd} ${hh}:${min}`;
}

/**
 * 日付文字列を見やすい形式に変換する (YYYY年MM月DD日)
 */
function formatDateJP(dateStr) {
  if (!dateStr) return "";
  const d = new Date(dateStr);
  if (isNaN(d.getTime())) return dateStr;

  const yyyy = d.getFullYear();
  const mm = d.getMonth() + 1;
  const dd = d.getDate();

  return `${yyyy}年${mm}月${dd}日`;
}

/**
 * 開始時刻 (HH:MM) と duration 分 から、同じ日の終了時刻 (HH:MM) を計算する。
 * 日付をまたぐ場合は null を返す。
 */
function computeEndHm(startHm, durationMinutes) {
  const startTotal = hmToMinutes(startHm);
  if (startTotal == null) return null;
  const endTotal = startTotal + durationMinutes;
  const dayMinutes = 24 * 60;
  if (endTotal >= dayMinutes) {
    return null; // 日跨ぎは UI では禁止
  }
  return minutesToHHMM(endTotal);
}

function setDefaultTimes() {
  const startSel = document.getElementById("startTimeSelect");
  const durationSel = document.getElementById("durationSelect");
  if (!startSel || !durationSel) return;

  const now = new Date();

  // 現在時刻（分単位）
  const totalMinutes = now.getHours() * 60 + now.getMinutes();

  // 「今＋5分」を開始時刻の候補にする
  let startTotal = totalMinutes + 5;

  // 5分刻みに丸める
  startTotal = Math.ceil(startTotal / 5) * 5;

  // 24時間内に正規化
  const dayMinutes = 24 * 60;
  startTotal = ((startTotal % dayMinutes) + dayMinutes) % dayMinutes;

  function minutesToHHMM(m) {
    const h = Math.floor(m / 60);
    const mm = m % 60;
    const hhStr = String(h).padStart(2, "0");
    const mmStr = String(mm).padStart(2, "0");
    return `${hhStr}:${mmStr}`;
  }

  const startValue = minutesToHHMM(startTotal);

  // option が存在する場合のみ値をセット
  if ([...startSel.options].some((opt) => opt.value === startValue)) {
    startSel.value = startValue;
  }

  // duration のデフォルトは 30 分（initDurationSelect で作った値と揃える）
  if ([...durationSel.options].some((opt) => opt.value === "30")) {
    durationSel.value = "30";
  } else if (durationSel.options.length > 0) {
    // 30 が無ければ先頭の候補
    durationSel.value = durationSel.options[0].value;
  }
}

// 開始時刻に応じて「終了候補」を制約する
function updateEndTimeOptions() {
  const startSel = document.getElementById("startTimeSelect");
  const endSel = document.getElementById("endTimeSelect");
  if (!startSel || !endSel) return;

  const startValue = startSel.value; // "HH:MM"
  if (!startValue) return;

  let firstValidValue = null;

  // "HH:MM" はゼロ詰めなので、文字列比較で時刻順になる
  for (const opt of endSel.options) {
    if (opt.value <= startValue) {
      opt.disabled = true;
    } else {
      opt.disabled = false;
      if (!firstValidValue) {
        firstValidValue = opt.value;
      }
    }
  }

  // 現在の選択が無効なら、最初の有効候補にずらす
  if (
    endSel.value <= startValue ||
    endSel.options[endSel.selectedIndex]?.disabled
  ) {
    if (firstValidValue) {
      endSel.value = firstValidValue;
    }
  }
}

async function loadUserStatus() {
  const userId = user_id;
  const headerPenalty = document.getElementById("headerPenalty");
  const banAlert = document.getElementById("globalBanAlert");
  const reserveButton = document.querySelector(
    'button[onclick="createReservation()"]'
  );

  if (!userId) {
    if (headerPenalty) headerPenalty.textContent = "累積ペナルティ: -";
    if (banAlert) banAlert.style.display = "none";
    if (reserveButton) reserveButton.disabled = false;
    return;
  }

  try {
    // キャッシュ回避のためにタイムスタンプを付与
    const url = `/api/penalties/${encodeURIComponent(userId)}?t=${Date.now()}`;
    const res = await fetch(url);
    if (!res.ok) {
      // エラー時は非表示
      if (banAlert) banAlert.style.display = "none";
      return;
    }

    const data = await res.json();
    const isBanned = !!data.is_banned;
    const totalPenalty = data.total_penalty_count ?? 0;
    const banUntil = data.ban_until || null;

    // ヘッダー更新
    if (headerPenalty) {
      headerPenalty.textContent = `累積ペナルティ: ${totalPenalty}回`;
    }

    // BAN アラート更新
    if (banAlert) {
      if (isBanned) {
        let daysRemainingText = "";
        if (banUntil) {
          const today = new Date();
          // 時間情報を削除して日付のみで比較
          today.setHours(0, 0, 0, 0);

          const untilDate = new Date(banUntil);
          if (!isNaN(untilDate.getTime())) {
            const diffTime = untilDate - today;
            const diffDays = Math.ceil(diffTime / (1000 * 60 * 60 * 24));
            if (diffDays > 0) {
              daysRemainingText = `（解除まで残り ${diffDays} 日）`;
            } else {
              daysRemainingText = `（本日解除予定）`;
            }
          }
        }

        banAlert.innerHTML = `<strong>利用停止中:</strong> アカウントは現在停止されています。${daysRemainingText} 解除予定日: ${
          formatDateJP(banUntil) || "不明"
        }`;
        banAlert.style.display = "block";
        if (reserveButton) reserveButton.disabled = true;
      } else {
        banAlert.style.display = "none";
        if (reserveButton) reserveButton.disabled = false;
      }
    }
  } catch (e) {
    console.error("loadUserStatus error", e);
  }
}

async function createReservation() {
  const userId = user_id;
  const date = document.getElementById("resDate").value;
  const startHm = document.getElementById("startTimeSelect").value;
  const durationStr = document.getElementById("durationSelect").value;
  const msg = document.getElementById("createMessage");

  if (!userId) {
    msg.textContent = "user_id を入力してください";
    return;
  }
  if (!date || !startHm || !durationStr) {
    msg.textContent = "日付・開始・終了を指定してください";
    return;
  }

  const durationMinutes = parseInt(durationStr, 10);
  if (!Number.isFinite(durationMinutes) || durationMinutes <= 0) {
    msg.textContent = "利用時間が不正です";
    return;
  }

  // 利用時間から end_time を計算
  const endHm = computeEndHm(startHm, durationMinutes);
  if (endHm === null) {
    msg.textContent =
      "日付をまたぐ予約はできません（開始時刻か利用時間を見直してください）";
    return;
  }

  const body = {
    user_id: userId,
    date: date,
    start_time: startHm,
    end_time: endHm,
  };

  const res = await fetch("/api/reservations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  const text = await res.text();
  try {
    const data = JSON.parse(text);
    if (!res.ok) {
      if (res.status === 403 && data.error === "user is banned") {
        const pts = data.points;
        const th = data.threshold;
        const until = data.ban_until ? formatDateJP(data.ban_until) : "未設定";
        msg.textContent =
          `ペナルティがしきい値を超えているため予約できません。` +
          `現在ポイント: ${pts}/${th}, 解除予定: ${until}`;
        // 表示も更新
        loadUserStatus();
      } else {
        msg.textContent = `エラー: ${data.error || text}`;
      }
    } else {
      msg.textContent = "予約を作成しました";
      loadReservations();
      loadUserStatus(); // 予約成功時もポイントが変わっていないか一応更新しておくのはあり
    }
  } catch (e) {
    msg.textContent = `予期せぬ応答: ${text}`;
  }
}

async function loadStatus() {
  const res = await fetch("/api/room_status");
  const text = await res.text();
  let data = null;
  try {
    data = JSON.parse(text);
  } catch (e) {
    document.querySelector("#statusTable tbody").innerHTML =
      "<tr><td>JSON parse error</td></tr>";
    return;
  }

  const labelMap = {
    timestamp: "更新日時",
    room_id: "会議室ID",
    room_state: "状態",
    people_count: "検知人数",
    is_used: "使用中判定",
    reservation_id: "現在の予約ID",
    alert: "アラート",
  };

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
      const label = labelMap[k] || k;
      let displayValue = v == null ? "" : v;

      // 更新日時のフォーマット
      if (k === "timestamp" && v) {
        displayValue = formatDateTime(v);
      }

      return `<tr><th>${label}</th><td>${displayValue}</td></tr>`;
    })
    .join("");

  document.querySelector("#statusTable tbody").innerHTML = rows;
}

async function loadReservations() {
  const userId = user_id;
  const date = document.getElementById("resDate").value;
  const err = document.getElementById("reservationError");

  const params = new URLSearchParams();
  if (userId) params.set("user_id", userId);
  if (date) params.set("date", date);

  const res = await fetch(`/api/reservations?${params.toString()}`);
  const text = await res.text();

  let data = null;
  try {
    data = JSON.parse(text);
  } catch (e) {
    err.textContent = `JSON parse error: ${text}`;
    return;
  }
  err.textContent = "";

  if (!Array.isArray(data)) {
    err.textContent = "unexpected response";
    return;
  }

  const tbody = document.querySelector("#reservationsTable tbody");
  const rows = data
    .map((r) => {
      const canCancel = r.status === "ACTIVE";
      const btn = canCancel
        ? `<button onclick="cancelReservation('${r.reservation_id}')" class="btn btn-sm btn-danger-outline">キャンセル</button>`
        : "";
      return `
        <tr>
          <td>${r.reservation_id}</td>
          <td>${formatDateTime(r.start_time)}</td>
          <td>${formatDateTime(r.end_time)}</td>
          <td>${r.status}</td>
          <td>${btn}</td>
        </tr>
      `;
    })
    .join("");
  tbody.innerHTML = rows;
}

async function cancelReservation(reservationId) {
  if (!confirm("この予約をキャンセルしますか？")) return;
  const res = await fetch(
    `/api/reservations/${encodeURIComponent(reservationId)}`,
    {
      method: "DELETE",
    }
  );
  const text = await res.text();
  if (!res.ok) {
    alert(`キャンセル失敗: ${text}`);
  } else {
    loadReservations();
  }
}

// loadPenalty was removed and merged into loadUserStatus

window.addEventListener("DOMContentLoaded", () => {
  initTimeSelect("startTimeSelect");
  initDurationSelect("durationSelect"); // ★ 追加
  setTodayToDateInput("resDate");

  // デフォルトの開始時刻と利用時間をセット
  setDefaultTimes();

  // updateEndTimeOptions() はもう不要なので呼ばない

  loadStatus();
  loadReservations();
  loadUserStatus();
});
