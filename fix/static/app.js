/**
 * STC 智能风扇控制器 v6 — 前端(历史曲线大改版)
 *
 * 改进点:
 *   1. 执行按钮: 乐观更新立即UI响应 (v5 已有)
 *   2. 曲线图: 4条曲线 (CPU/SSD/HDD/环境NTC), 系统识别几种显示几种 (v5 已有)
 *   3. ★ v6 核心: 后端持久化 + 前端范围选择器
 *        - 后端 1 分钟采样写入 JSONL 到 /data (容器卷, 7 天保留)
 *        - 前端下拉选 1h / 6h / 24h / 3d / 7d, 调 /api/temps/history 一次性拉整段历史
 *        - 返回点数 > 360 时后端自动均值降采样, 画图不卡
 *        - 只有短范围(≤6小时)时才在 poll 时追加实时点, 保持实时感
 *   4. 静态文件版本号: ?v=4, 强制浏览器刷新缓存
 */
const API = "";
const POLL = 3000;
let chart = null;

// ── 全局缓存 ──
// 最近一次 NTC 温度值(供 updateTemps 写入图表第4条曲线用)
let lastNtcValue = null;
// 每种数据源连续"无有效数据"计数, 用于短范围 append 模式下的动态隐藏
let dataMissingCount = { cpu:0, ssd:0, hdd:0, ntc:0 };
const DATASET_KEYS = ["cpu", "ssd", "hdd", "ntc"];
// 兜底最多点数 (appendEnabled=true 但还没 loadHistory 完成时用)
const CHART_MAX_POINTS = 480;

// ── v6 新增: 历史曲线状态 ──
const state = {
  currentRange: "24h",           // 当前选择的范围
  ranges: [],                    // 后端返回的 ranges 选项(备用)
  appendEnabled: false,          // ≤6小时: poll 时继续追加实时点  >6小时: 只看历史快照
  historyLabelsCount: 60,        // loadHistory 返回的 points 数, append 时按这个裁剪
  historyLoaded: false,          // 首次 loadHistory 是否完成
};

document.addEventListener("DOMContentLoaded", () => {
  initChart();
  setTimeout(loadConfig, 500);
  setTimeout(loadAutoCmd, 600);
  // v6: 图表初始化完成后再加载历史曲线 (给后端1.5s启动时间, 避免首帧接口报错)
  setTimeout(() => loadHistory(state.currentRange), 1500);
  poll();
  setInterval(poll, POLL);
  // v6: 绑定时间范围下拉框 change 事件
  const sel = document.getElementById("range-select");
  if (sel) {
    sel.addEventListener("change", (e) => {
      loadHistory(e.target.value);
    });
  }
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
  // ★ v6 修复: 环境温度(NTC) 只做参考显示, 不参与温控阈值计算
  //   (后端 fan_control.calc_target_speed() 也同样只传 cpu/ssd/hdd)
  const candidates = [data.cpu, data.ssd, data.hdd]
        .filter(v => typeof v === "number" && v != null && !isNaN(v));
  if (candidates.length) {
    const peak = Math.max(...candidates);
    const start = parseInt(document.getElementById("ctrl-start").value) || 35;
    const max = parseInt(document.getElementById("ctrl-max").value) || 60;
    const spd = peak <= start ? 0 : peak >= max ? 100 : Math.round((peak-start)/(max-start)*100);
    document.getElementById("ctrl-preview").textContent = `${Math.round(peak)}°C → ${spd}%`;
  }

  // ── 写入图表 (4条曲线) ──
  // v6 关键改造:
  //   - 看历史长范围(>6小时, appendEnabled=false): 不追加, 整图由 loadHistory 一次性重绘
  //     目的: 不把 3s/点 的实时采集和后端 1分钟/点 的历史数据混在一起造成刻度混乱
  //   - 短范围实时查看(≤6小时, appendEnabled=true): 继续每 3s append 一个点, 保持实时感
  //     裁剪长度按 loadHistory 时记录的 historyLabelsCount (与后端范围点数对齐)
  if (!chart) return;
  if (!state.appendEnabled) return;  // 长范围: 只更新卡片, 不改图表

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
    // ★ v6 修复: 不再使用 dataset.hidden (会让图例文字出现「删除线」, 手机端刚进页面对用户不友好)
    //   只累计无数据计数用于其它逻辑, 不再去 hidden 控制图例。
    //   (图例会一直显示 4 种颜色, 只是当数据源从未有有效值时曲线为空, 视觉上就没线)
    const key = DATASET_KEYS[i];
    if (vals[i] == null) dataMissingCount[key]++;
    else dataMissingCount[key] = 0;
  }

  // 限制总点数: 优先按 loadHistory 返回的范围点数, 兜底 480
  const maxLen = Math.max(1, state.historyLabelsCount || CHART_MAX_POINTS);
  while (chart.data.labels.length > maxLen) {
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

// ── v6 新增: 加载后端持久化的温度历史曲线, 整图重绘 ──
async function loadHistory(range) {
  range = range || state.currentRange;
  state.currentRange = range;
  // 如果下拉框当前值和 range 不一致, 同步一下 (双向绑定)
  const sel = document.getElementById("range-select");
  if (sel && sel.value !== range) sel.value = range;

  try {
    const r = await fetch(`${API}/api/temps/history?range=${encodeURIComponent(range)}`).then(r => r.json());
    if (!r || !r.ok) throw new Error((r && r.error) || "接口返回失败");

    // 顶部提示: 累计记录数 / 范围 / 显示点数 (让用户知道数据在慢慢累积)
    const hint = document.getElementById("history-hint");
    if (hint) {
      hint.textContent =
        `累计 ${r.total_history_records} 条记录（每分钟1条，保留${r.retain_days}天，` +
        `本次显示 ${r.returned_points}/${r.max_points_cap} 点）`;
    }

    state.ranges = r.ranges || [];
    state.historyLabelsCount = r.returned_points || 60;
    // ≤6 小时=短范围实时查看: poll 时继续 append 最新点(追加到末尾, 保持实时感)
    // >6 小时=长范围看历史: 不做 append, 避免 3s/点 实时采集和 1min/点 历史混合造成刻度混乱
    state.appendEnabled = (r.seconds <= 6 * 3600);
    // 整图重绘 (后端已做降采样 + X 轴 label 格式化)
    rebuildChartFromHistory(r.points || []);
    state.historyLoaded = true;
  } catch (e) {
    console.error("加载温度历史失败:", e);
    const hint = document.getElementById("history-hint");
    if (hint) {
      hint.textContent =
        `⚠️ 历史暂无数据（刚启动请等 1-2 分钟，或手动刷新页面重试）: ${e.message}`;
    }
    // 失败时允许退回到 append 模式 (防止首帧没加载上导致图表一直空)
    state.appendEnabled = true;
    state.historyLabelsCount = CHART_MAX_POINTS;
  }
}

// ── v6 新增: 用后端返回的历史点, 一次性重建整张图表 (替代原 chart.push 追加模式) ──
function rebuildChartFromHistory(points) {
  if (!chart) return;
  const labels = [];
  const dData = [[], [], [], []];   // cpu/ssd/hdd/ntc
  const hasData = { cpu:false, ssd:false, hdd:false, ntc:false };
  for (let i = 0; i < points.length; i++) {
    const p = points[i];
    // 后端已经按范围格式化好了: 短范围 HH:MM, 长范围 MM/DD HH:MM
    labels.push(p.label);
    const vals = [
      (typeof p.cpu === "number" && !isNaN(p.cpu)) ? p.cpu : null,
      (typeof p.ssd === "number" && !isNaN(p.ssd)) ? p.ssd : null,
      (typeof p.hdd === "number" && !isNaN(p.hdd)) ? p.hdd : null,
      (typeof p.ntc === "number" && !isNaN(p.ntc)) ? p.ntc : null,
    ];
    for (let k = 0; k < 4; k++) {
      dData[k].push(vals[k]);
      if (vals[k] != null) hasData[DATASET_KEYS[k]] = true;
    }
  }
  chart.data.labels = labels;
  for (let i = 0; i < 4; i++) {
    chart.data.datasets[i].data = dData[i];
    // ★ v6 修复: 不再设置 dataset.hidden (会在图例文字上加删除线, 刚进页面对用户不友好)
    //   替代方案: 如果整条曲线完全没数据, 把颜色设为透明 + 边框设为虚线
    //   这样图例显示正常 (4 个颜色框), 且没有数据的曲线在视觉上本来就不显示
    if (!hasData[DATASET_KEYS[i]]) {
      chart.data.datasets[i].borderColor = chart.data.datasets[i].borderColor; // 保持原颜色
      // 不做任何隐藏, 空数据在 Chart.js 里会自然没有画线
    }
  }
  // 不同范围的视觉调整:
  //   - >1天: maxTicksLimit 调到 6, 避免跨日刻度文字堆叠
  //   - spanGaps: 短范围 false (缺数据断开更真实), 长范围 true (合并桶后点更均匀, 连线更顺)
  const SEC_24H = 24 * 3600;
  const RANGE_SEC = {
    "1h": 3600, "6h": 6*3600, "12h":12*3600, "24h": SEC_24H,
    "3d": 3*86400, "7d":7*86400,
  }[state.currentRange] || SEC_24H;
  chart.options.scales.x.ticks.maxTicksLimit = RANGE_SEC > SEC_24H ? 6 : 8;
  chart.options.spanGaps = RANGE_SEC > SEC_24H;
  chart.update("none");
  // 重置 dataMissingCount: 历史数据里自动判定了 hidden, 后面 append 从零重新开始累计
  for (const k of DATASET_KEYS) dataMissingCount[k] = 0;
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

  // ★ v6 图例布局自适应:
  //   手机端 (<=680px) 缩小方框+字体+间距, 4 个图例一行放下, 避免环境温度自动折第二行
  const isMobile = window.innerWidth <= 680;
  const LEG_BOX = isMobile ? 11 : 14;          // 方框宽度(手机更小)
  const LEG_PAD = isMobile ? 6  : 10;          // 图例间距
  const LEG_FNT = isMobile ? 10 : 12;          // 字号

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
          align: "start",
          // 防手机误触 (点一下颜色框曲线就消失)
          onClick: null,
          labels: {
            // ★ v6 自适应方框+字号+间距 (LEG_* 上面按屏幕宽度算的):
            //   PC 端: box14 / pad10 / font12 (默认舒适大小)
            //   手机端: box11 / pad6  / font10 (紧凑, 4 个一行放下, 环境温度不会折行)
            boxWidth: LEG_BOX,
            boxHeight: Math.round(LEG_BOX * 0.75),
            padding: LEG_PAD,
            font: { size: LEG_FNT },
            // ★ v6 generateLabels (这次写齐所有字段, 再也不出现黑字/黑边框!)
            generateLabels: (chart) => {
              return chart.data.datasets.map((ds, i) => ({
                text: ds.label,
                // ★ 方框内部填充白色 (用户明确要求)
                fillStyle: "#ffffff",
                // ★ 方框边框颜色 = 曲线本身的实色 (红/绿/橙/蓝)
                strokeStyle: ds.borderColor,
                lineWidth: 2,               // 边框粗 2px, 醒目
                // ★ 文字颜色必须显式设置 (自定义 generateLabels 后外层 color 会失效, 不写=黑色!)
                fontColor: "#c9ccd8",
                // ★ 防删除线 + 禁用图例点击隐藏曲线
                hidden: false,
                index: i,
              }));
            },
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
  // ★ v6 修复: 环境温度(ntc-temp) 不参与温控计算, 只从 cpu/ssd/hdd 三个里取最高
  const vals = ["cpu-temp", "ssd-temp", "hdd-temp"]
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
