/**
 * STC 智能风扇控制器 v2.1 — 前端(体验优化版)
 *
 * 优化点:
 *   1. 执行按钮: 点下后立即用当前DOM上的温度值乐观更新UI(转速条/预览),
 *      不等后端串行8秒的真实返回; 请求在后台异步执行, 真实结果回来后再修正.
 *   2. 曲线图: 4条曲线(CPU/SSD/HDD/NTC环境), 系统识别到几种显示几种
 *      (连续无数据自动隐藏图例), X轴显示时间戳.
 */
const API = "";
const POLL = 3000;
let chart = null;

// ── 全局缓存 ──
// 最近一次 NTC 温度值(供 updateTemps 写入图表第4条曲线用,
// 因为 /api/temps 和 /api/ntc 是两个独立接口, 轮询时需要合并)
let lastNtcValue = null;
// 每种数据源连续"无有效数据"计数, 用于动态隐藏曲线图例
let dataMissingCount = { cpu:0, ssd:0, hdd:0, ntc:0 };
// 数据源显示名 → 数据集索引
const DATASET_KEYS = ["cpu", "ssd", "hdd", "ntc"];
// 图表最多保留的数据点数(3s/点 × 480 点 ≈ 24分钟).
// 若需更长时间(如全天), 请在后端增加历史记录持久化API.
const CHART_MAX_POINTS = 480;

document.addEventListener("DOMContentLoaded", () => {
  initChart();
  setTimeout(loadConfig, 500);
  setTimeout(loadAutoCmd, 600);
  poll();
  setInterval(poll, POLL);
});

// ── 自动命令发送开关 ──
let autoCmdEnabled = true;

async function loadAutoCmd() {
  try {
    const r = await fetch(`${API}/api/auto-cmd`).then(r => r.json());
    if (r.ok) updateAutoCmdUI(r.enabled);
  } catch(e) { console.error("加载自动命令开关失败:", e); }
}

function updateAutoCmdUI(enabled) {
  autoCmdEnabled = !!enabled;
  const btn = document.getElementById("auto-cmd-btn");
  const hint = document.getElementById("auto-cmd-hint");
  if (!btn) return;
  if (enabled) {
    btn.textContent = "⏸ 已启用";
    btn.className = "btn btn-auto-cmd on";
    if (hint) {
      hint.textContent = "✅ 自动命令已开启：正常按时间下发心跳/温控（手动发送仍可用）";
      hint.className = "auto-cmd-hint on";
    }
  } else {
    btn.textContent = "▶ 已停用";
    btn.className = "btn btn-auto-cmd off";
    if (hint) {
      hint.textContent = "⏸ 自动命令已关闭：仅手动发送可用（方便调试，不会占用串口）";
      hint.className = "auto-cmd-hint off";
    }
  }
}

async function toggleAutoCmd() {
  const newVal = !autoCmdEnabled;
  const btn = document.getElementById("auto-cmd-btn");
  if (btn) { btn.disabled = true; btn.textContent = "⏳..."; }
  try {
    const r = await fetch(`${API}/api/auto-cmd`, {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({enabled: newVal}),
    }).then(r => r.json());
    if (r.ok) {
      updateAutoCmdUI(r.enabled);
    } else {
      throw new Error(r.error || "切换失败");
    }
  } catch(e) {
    alert("自动命令开关切换失败: " + e.message);
    updateAutoCmdUI(autoCmdEnabled);
  }
  if (btn) btn.disabled = false;
}

async function poll() {
  try {
    const [tr, sr, nr, ir] = await Promise.all([
      fetch(`${API}/api/temps`).then(r => r.json()).catch(()=>({ok:false})),
      fetch(`${API}/api/status`).then(r => r.json()).catch(()=>({ok:false})),
      fetch(`${API}/api/ntc`).then(r => r.json()).catch(()=>({ok:false})),
      fetch(`${API}/api/info`).then(r => r.json()).catch(()=>({ok:false})),
    ]);
    // 先更新 NTC 缓存, 这样 updateTemps 写图表时 NTC 曲线能拿到最新值
    if (nr.ok) updateNtc(nr);
    if (tr.ok) updateTemps(tr.data);
    if (sr.ok) updateFans(sr);
    updateConn(ir);
    if (ir.ok && ir.data && typeof ir.data.auto_cmd_enabled === "boolean") {
      if (autoCmdEnabled !== ir.data.auto_cmd_enabled) {
        updateAutoCmdUI(ir.data.auto_cmd_enabled);
      }
    }
  } catch(e) { console.error(e); }
}

function updateConn(info) {
  const el = document.getElementById("conn-badge");
  if (info.ok && info.data?.controller_connected) {
    el.textContent = "🟢 已连接"; el.className = "badge online";
    const s = info.data.uptime;
    document.getElementById("uptime").textContent = `运行: ${Math.floor(s/3600)}h${Math.floor((s%3600)/60)}m`;
  } else {
    el.textContent = "🔴 未连接"; el.className = "badge";
  }
}

function setTemp(id, val) {
  const el = document.getElementById(id);
  if (!el) return;
  if (val == null) { el.textContent = "N/A"; el.className = "temp-val na"; return; }
  el.textContent = `${val.toFixed(1)}°C`;
  el.className = "temp-val" + (val >= 60 ? " hot" : val <= 30 ? " cold" : "");
}

function updateTemps(data) {
  // ── 温度卡片显示 ──
  setTemp("cpu-temp", data.cpu);
  setTemp("ssd-temp", data.ssd);
  setTemp("hdd-temp", data.hdd);

  // ── 最高温 → 目标转速预览 ──
  const candidates = [data.cpu, data.ssd, data.hdd, lastNtcValue]
        .filter(v => typeof v === "number" && v != null && !isNaN(v));
  if (candidates.length) {
    const peak = Math.max(...candidates);
    const start = parseInt(document.getElementById("ctrl-start").value) || 35;
    const max = parseInt(document.getElementById("ctrl-max").value) || 60;
    const spd = peak <= start ? 0 : peak >= max ? 100 : Math.round((peak-start)/(max-start)*100);
    document.getElementById("ctrl-preview").textContent = `${Math.round(peak)}°C → ${spd}%`;
  }

  // ── 写入图表 (4条曲线) ──
  if (!chart) return;
  const now = new Date();
  const ts = now.toLocaleTimeString();  // HH:MM:SS
  chart.data.labels.push(ts);

  // 4个数据源的值(无效值填 null, Chart.js 会断开该点而不是画到0)
  const vals = [
    (typeof data.cpu === "number" && !isNaN(data.cpu)) ? data.cpu : null,  // 0:CPU
    (typeof data.ssd === "number" && !isNaN(data.ssd)) ? data.ssd : null,  // 1:SSD
    (typeof data.hdd === "number" && !isNaN(data.hdd)) ? data.hdd : null,  // 2:HDD
    (typeof lastNtcValue === "number" && !isNaN(lastNtcValue)) ? lastNtcValue : null, // 3:NTC
  ];
  for (let i = 0; i < 4; i++) {
    chart.data.datasets[i].data.push(vals[i]);
    // 维护"连续无数据"计数, 用于动态隐藏图例
    const key = DATASET_KEYS[i];
    if (vals[i] == null) dataMissingCount[key]++;
    else dataMissingCount[key] = 0;
    // 连续 30 次(≈90秒)没数据 → 认为该数据源不可用, 隐藏图例和线;
    // 有任何一个点恢复数据 → 立即显示
    if (dataMissingCount[key] >= 30) {
      chart.data.datasets[i].hidden = true;
    } else if (vals[i] != null) {
      chart.data.datasets[i].hidden = false;
    }
  }

  // 限制总点数, 超出就移除最早的
  while (chart.data.labels.length > CHART_MAX_POINTS) {
    chart.data.labels.shift();
    chart.data.datasets.forEach(ds => ds.data.shift());
  }
  chart.update("none");
}

function updateNtc(data) {
  // 更新卡片 + 缓存值(供 updateTemps 下次写图表 NTC 曲线用)
  const el = document.getElementById("ntc-temp");
  if (data.ok && typeof data.value === "number" && !isNaN(data.value)) {
    lastNtcValue = data.value;
    if (el) { el.textContent = `${data.value.toFixed(1)}°C`; el.className = "temp-val"; }
  } else {
    lastNtcValue = null;
    if (el) { el.textContent = "N/A"; el.className = "temp-val na"; }
  }
}

function updateFans(data) {
  const info = data["fan1"] || {};
  const speed = info["转速"] || "--";
  const val = parseInt(speed) || 0;
  const sp = document.getElementById("fan1-speed");
  const bar = document.getElementById("fan1-bar");
  if (sp) sp.textContent = speed;
  if (bar) bar.style.width = val + "%";
}

function initChart() {
  const ctx = document.getElementById("temp-chart");
  if (!ctx) return;
  chart = new Chart(ctx, {
    type: "line",
    data: {
      labels: [],
      datasets: [
        // 0: CPU — 红色
        { label:"CPU 温度",
          borderColor:"#ef4444", backgroundColor:"rgba(239,68,68,0.08)",
          fill:true, tension:0.3, pointRadius:0, pointHoverRadius:3, borderWidth:2 },
        // 1: SSD — 绿色
        { label:"SSD 温度",
          borderColor:"#34d399", backgroundColor:"rgba(52,211,153,0.05)",
          fill:false, tension:0.3, pointRadius:0, pointHoverRadius:2, borderWidth:1.8 },
        // 2: HDD — 橙色
        { label:"HDD 温度",
          borderColor:"#f59e0b", backgroundColor:"rgba(245,158,11,0.05)",
          fill:false, tension:0.3, pointRadius:0, pointHoverRadius:2, borderWidth:1.8 },
        // 3: 环境温度 NTC — 蓝色
        { label:"环境温度",
          borderColor:"#3b82f6", backgroundColor:"rgba(59,130,246,0.05)",
          fill:false, tension:0.3, pointRadius:0, pointHoverRadius:2, borderWidth:1.8 },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 200 },
      spanGaps: false,   // 数据空缺的位置要断开(不连虚线), 真实反映采集失败
      scales: {
        x: {
          // ★ v2 修复: 显示时间轴, 不再隐藏
          display: true,
          grid: { display: false },
          ticks: {
            color: "#888a9e",
            font: { size: 9 },
            maxRotation: 0,
            autoSkip: true,
            // 最多显示 8 个时间刻度, 避免文字堆叠
            maxTicksLimit: 8,
          },
        },
        y: {
          min: 10, max: 80,
          grid: { color: "rgba(255,255,255,0.05)" },
          ticks: {
            color: "#888a9e",
            font: { size: 10 },
            callback: (v) => v + "°C",
          },
        },
      },
      plugins: {
        legend: {
          position: "top",
          labels: {
            color: "#888a9e",
            font: { size: 11 },
            boxWidth: 14,
            padding: 10,
            // 点击图例不做切换(避免误操作), 只用来显示/隐藏基于数据自动判定
            filter: (item) => !item.hidden,
          },
        },
        tooltip: {
          mode: "index",
          intersect: false,
          callbacks: {
            label: (ctx) => {
              const v = ctx.parsed.y;
              if (v == null || isNaN(v)) return null;
              return `${ctx.dataset.label}: ${v.toFixed(1)}°C`;
            },
          },
        },
      },
    },
  });
}

// ── 温控配置 ──
async function loadConfig() {
  try {
    const r = await fetch(`${API}/api/control`).then(r => r.json());
    if (!r.ok) return;
    document.getElementById("ctrl-start").value = r.data.start;
    document.getElementById("ctrl-max").value = r.data.max;
  } catch(e) { console.error(e); }
}
document.addEventListener("change", e => {
  if (e.target.id === "ctrl-start" || e.target.id === "ctrl-max") {
    const start = parseInt(document.getElementById("ctrl-start").value) || 0;
    const max = parseInt(document.getElementById("ctrl-max").value) || 0;
    fetch(`${API}/api/control`, {
      method:"PUT", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({start, max}),
    }).catch(()=>{});
    // 配置改了, 立即重算预览
    syncPreviewFromDom();
  }
});

/** 从当前DOM上的温度显示值计算最高温和目标转速(纯前端, 不访问后端).
 *  用于"点执行后立即更新UI", 让用户感觉响应是即时的,
 *  不用等后端串行4个串口命令(~8秒)才看到进度条变化.
 */
function calcTargetFromDom() {
  const parseVal = (id) => {
    const el = document.getElementById(id);
    if (!el) return null;
    const txt = el.textContent.replace(/°C/g, "").trim();
    if (txt === "N/A" || txt === "--" || txt === "") return null;
    const v = parseFloat(txt);
    return isNaN(v) ? null : v;
  };
  const vals = ["cpu-temp", "ssd-temp", "hdd-temp", "ntc-temp"]
        .map(parseVal).filter(v => typeof v === "number");
  if (!vals.length) return { peak: null, target: null };
  const peak = Math.max(...vals);
  const start = parseInt(document.getElementById("ctrl-start").value) || 35;
  const max = parseInt(document.getElementById("ctrl-max").value) || 60;
  const target = peak <= start ? 0 : peak >= max ? 100 : Math.round((peak-start)/(max-start)*100);
  return { peak, target };
}

/** 同步预览文字(最高温→目标转速) */
function syncPreviewFromDom() {
  const { peak, target } = calcTargetFromDom();
  if (peak != null && target != null) {
    document.getElementById("ctrl-preview").textContent = `${Math.round(peak)}°C → ${target}%`;
  }
}

/** 乐观更新风扇进度条: 点执行后立即显示预期转速, 不等后端确认 */
function applyOptimisticFanSpeed(targetPercent) {
  const bar = document.getElementById("fan1-bar");
  const sp = document.getElementById("fan1-speed");
  if (bar) bar.style.width = targetPercent + "%";
  if (sp) sp.textContent = targetPercent + "%";
}

// 立即刷新一次风扇转速和温度(不等3秒轮询), 用于手动操作后尽快更新
async function refreshFansNow() {
  try {
    const [sr, nr, tr] = await Promise.all([
      fetch(`${API}/api/status`).then(r => r.json()).catch(()=>({ok:false})),
      fetch(`${API}/api/ntc`).then(r => r.json()).catch(()=>({ok:false})),
      fetch(`${API}/api/temps`).then(r => r.json()).catch(()=>({ok:false})),
    ]);
    if (nr.ok) updateNtc(nr);
    if (tr.ok) updateTemps(tr.data);
    if (sr.ok) updateFans(sr);
  } catch(e) { console.error(e); }
}

/**
 * 执行按钮(核心优化):
 *   旧流程: 点按钮 → 等后端8秒(温度采集+4个串口) → 按钮恢复+UI刷新
 *   新流程: 点按钮 → 100ms 内: 前端计算预期转速 → 进度条立刻动 → 按钮变"已执行"
 *           → 0.5秒后按钮恢复可点击(用户无需等8秒)
 *           → 后端请求后台完成后, 再把真实返回的结果修正回UI(如果和预期一致就没变)
 */
async function runControl() {
  const btn = event?.target;

  // ── ① 立即乐观更新 (耗时 < 50ms, 用户感觉"秒响应") ──
  const { peak, target } = calcTargetFromDom();
  if (target != null) {
    applyOptimisticFanSpeed(target);
  }
  // 按钮立即变"执行中..."样式, 但只 disabled 很短时间
  if (btn) {
    btn.textContent = "⚡ 已执行";
    btn.classList.add("exec-flash");
    btn.disabled = true;
  }
  // 0.5 秒后按钮恢复可点击(而不是等后端8秒).
  // 这样即使后端慢, 用户也可以连续点两次(比如改了配置后马上再执行)
  setTimeout(() => {
    if (btn) {
      btn.textContent = "▶ 执行";
      btn.classList.remove("exec-flash");
      btn.disabled = false;
    }
  }, 500);

  // ── ② 后台异步发请求, 不阻塞 UI ──
  (async () => {
    try {
      const r = await fetch(`${API}/api/control/run`, {method:"POST"}).then(r => r.json());
      if (r.ok) {
        // 后端真实返回: 用真实值再覆盖一次, 修正乐观计算可能的偏差
        // (例如刚执行完温度有变化, 或控制器实际和理论有误差)
        if (typeof r.target_speed === "number") {
          applyOptimisticFanSpeed(r.target_speed);
        }
        // 触发一次立即刷新, 让卡片/图表和控制器真实状态对齐
        // (不再等3秒轮询自动来, 但这里也不 await, 让它在后台跑)
        refreshFansNow();
      }
    } catch(e) { console.error("runControl 后台执行失败:", e); }
  })();
}

async function resetControl() {
  try {
    const r = await fetch(`${API}/api/control/reset`, {method:"POST"}).then(r => r.json());
    if (r.ok) await loadConfig();
  } catch(e) { console.error(e); }
}

// ── LED 开关(等待响应后变色) ──
let ledState = false;
async function toggleLed() {
  const btn = document.getElementById("led-btn");
  btn.disabled = true;
  const cmd = ledState ? "OFF" : "ON";
  try {
    const r = await fetch(`${API}/api/led`, {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({cmd}),
    }).then(r => r.json());
    if (r.ok) {
      ledState = cmd === "ON";
    } else {
      throw new Error(r.error || "无响应");
    }
  } catch(e) {
    console.error(e);
  }
  updateLedBtn();
  btn.disabled = false;
}

// ── 原始命令 ──
async function sendRaw() {
  const input = document.getElementById("raw-cmd");
  const pre = document.getElementById("raw-response");
  const cmd = input.value.trim();
  if (!cmd) return;
  pre.textContent = `> ${cmd}\n发送中...`;
  try {
    const r = await fetch(`${API}/api/raw`, {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({cmd}),
    }).then(r => r.json());
    pre.textContent = `> ${cmd}\n\n${r.response || "(空)"}`;
    if (r.ok) {
      const upper = cmd.toUpperCase();
      if (upper.includes("CPD=") || upper.includes("LED=") || upper.includes("ALL")
          || upper.startsWith("F1") || upper.startsWith("F2")) {
        await refreshFansNow();
        if (upper.includes("LED=ON")) { ledState = true; updateLedBtn(); }
        if (upper.includes("LED=OFF")) { ledState = false; updateLedBtn(); }
      }
    }
  } catch(e) { pre.textContent = `> ${cmd}\n\n❌ ${e.message}`; }
}

// 同步 LED 按钮显示
function updateLedBtn() {
  const btn = document.getElementById("led-btn");
  if (!btn) return;
  btn.textContent = ledState ? "💡 LED 开" : "💡 LED 关";
  btn.className = "btn btn-led" + (ledState ? " on" : "");
}

// ── 调试日志 ──
async function refreshLog() {
  const el = document.getElementById("debug-log");
  if (!el) return;
  try {
    const r = await fetch(`${API}/api/log`).then(r => r.json());
    if (!r.ok) { el.textContent = "获取日志失败"; return; }
    el.textContent = r.log.map(e => {
      const t = new Date(e.t * 1000).toLocaleTimeString();
      const icon = e.dir === "TX" ? "→" : e.dir === "RX" ? "←" : "●";
      return `${t} ${icon} ${e.data}`;
    }).join("\n") || "(暂无日志)";
  } catch(e) { el.textContent = `❌ ${e.message}`; }
}
setInterval(refreshLog, 5000);

// ── 断连记录卡片 ──
function toggleDiscCard() {
  const card = document.getElementById("disc-card");
  if (!card) return;
  card.classList.toggle("collapsed");
  const btn = card.querySelector(".card-header .btn");
  if (btn) btn.textContent = card.classList.contains("collapsed") ? "展开" : "折叠";
}

async function refreshDiscLog() {
  const body = document.getElementById("disc-body");
  const status = document.getElementById("disc-status");
  if (!body) return;
  try {
    const r = await fetch(`${API}/api/disconnect-log?limit=50`).then(r => r.json());
    if (!r.ok) { body.innerHTML = '<div class="disc-empty">获取失败</div>'; return; }
    if (status) {
      if (r.currently_disconnected) {
        status.textContent = "断连中";
        status.className = "disc-status off";
      } else {
        status.textContent = "正常";
        status.className = "disc-status on";
      }
    }
    if (!r.entries || r.entries.length === 0) {
      body.innerHTML = '<div class="disc-empty">暂无断连记录</div>';
      return;
    }
    body.innerHTML = r.entries.map(e => {
      const cls = e.status === "recovered" ? "recovered" : "disconnected";
      let dur = "";
      if (e.duration_s != null) {
        const s = e.duration_s;
        dur = s >= 60 ? ` · 持续 ${Math.floor(s/60)}分${s%60}秒` : ` · 持续 ${s}秒`;
      } else {
        dur = " · 进行中";
      }
      const end = e.end ? ` → ${e.end}` : " → ...";
      return `<div class="disc-entry ${cls}">
        <span class="disc-time">${e.start}${end}</span>
        <span class="disc-dur">${e.status === "recovered" ? "已恢复" : "未恢复"}${dur}</span>
      </div>`;
    }).join("");
  } catch(e) {
    body.innerHTML = `<div class="disc-empty">❌ ${e.message}</div>`;
  }
}
setTimeout(refreshDiscLog, 800);
setInterval(refreshDiscLog, 10000);
