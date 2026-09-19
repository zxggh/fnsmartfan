/**
 * SmartFan Nexus 前端主逻辑
 *
 * 功能:
 *   - 登录认证 (JWT, 记住我 localStorage / sessionStorage)
 *   - WebSocket 实时推送 (状态 + 通信日志)
 *   - ECharts 温度趋势图 (1h/24h/3d/7d/30d)
 *   - 风扇自动/手动模式切换 (手动滑块 300ms 防抖)
 *   - 磁盘明细展开面板
 *   - 温度颜色分级 (<45 绿, 45-55 橙, >=55 红)
 *   - PC 双栏 / 移动底部导航 响应式
 */

const API = "";
let token = null;
let ws = null;
let chart = null;
let sliderDebounceTimer = null;

// ============================================================
//  认证相关
// ============================================================
function getToken() {
  return localStorage.getItem("sf_token") || sessionStorage.getItem("sf_token");
}

function setToken(t, remember) {
  token = t;
  if (remember) {
    localStorage.setItem("sf_token", t);
    sessionStorage.removeItem("sf_token");
  } else {
    sessionStorage.setItem("sf_token", t);
    localStorage.removeItem("sf_token");
  }
}

function clearToken() {
  token = null;
  localStorage.removeItem("sf_token");
  sessionStorage.removeItem("sf_token");
}

function authHeader() {
  return { "Authorization": "Bearer " + (token || ""), "Content-Type": "application/json" };
}

// 检查登录态
function checkAuth() {
  token = getToken();
  if (!token) {
    showLogin();
    return false;
  }
  // 验证 token 是否有效 (调用一个需要鉴权的接口)
  fetch(`${API}/api/config`, { headers: authHeader() })
    .then(r => {
      if (r.status === 401) {
        clearToken();
        showLogin();
      } else {
        showApp();
      }
    })
    .catch(() => showLogin());
  return true;
}

function showLogin() {
  document.getElementById("login-mask").classList.remove("hidden");
  document.getElementById("app").classList.add("hidden");
  // 绑定登录页事件 (登录页不经过 initApp, 必须在这里绑)
  document.getElementById("login-btn").onclick = doLogin;
  document.getElementById("login-password").onkeydown = e => { if (e.key === "Enter") doLogin(); };
}

function showApp() {
  document.getElementById("login-mask").classList.add("hidden");
  document.getElementById("app").classList.remove("hidden");
  initApp();
}

async function doLogin() {
  const username = document.getElementById("login-username").value.trim();
  const password = document.getElementById("login-password").value;
  const remember = document.getElementById("login-remember").checked;
  const errEl = document.getElementById("login-error");
  errEl.textContent = "";
  try {
    const r = await fetch(`${API}/api/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password })
    });
    if (!r.ok) {
      errEl.textContent = "用户名或密码错误 (或账号已锁定, 请稍后重试)";
      return;
    }
    const data = await r.json();
    setToken(data.token, remember);
    showApp();
  } catch (e) {
    errEl.textContent = "登录失败: " + e.message;
  }
}

function doLogout() {
  clearToken();
  if (ws) { ws.close(); ws = null; }
  showLogin();
}

async function changePassword() {
  const oldPwd = document.getElementById("pwd-old").value;
  const newPwd = document.getElementById("pwd-new").value;
  const errEl = document.getElementById("pwd-error");
  errEl.textContent = "";
  try {
    const r = await fetch(`${API}/api/change-password`, {
      method: "POST",
      headers: authHeader(),
      body: JSON.stringify({ oldPassword: oldPwd, newPassword: newPwd })
    });
    if (!r.ok) {
      errEl.textContent = "原密码错误";
      return;
    }
    closePwdModal();
    alert("密码已修改, 请重新登录");
    doLogout();
  } catch (e) {
    errEl.textContent = "修改失败: " + e.message;
  }
}

function openPwdModal() {
  document.getElementById("pwd-modal").classList.remove("hidden");
  document.getElementById("pwd-old").value = "";
  document.getElementById("pwd-new").value = "";
  document.getElementById("pwd-error").textContent = "";
}

function closePwdModal() {
  document.getElementById("pwd-modal").classList.add("hidden");
}

// ============================================================
//  应用初始化
// ============================================================
function initApp() {
  // 初始化页面显示状态 (移动端默认只显示监控页)
  switchPage("monitor");
  initChart();
  connectWebSocket();
  loadConfig();
  loadHistory("24h");
  loadDisconnectLog();
  setInterval(loadDisconnectLog, 30000);
  // 绑定事件
  document.getElementById("range-select").addEventListener("change", e => loadHistory(e.target.value));
  document.getElementById("btn-change-pwd").onclick = openPwdModal;
  document.getElementById("btn-logout").onclick = doLogout;
  document.getElementById("login-btn").onclick = doLogin;
  document.getElementById("login-password").addEventListener("keydown", e => { if (e.key === "Enter") doLogin(); });
}

// ============================================================
//  WebSocket
// ============================================================
function connectWebSocket() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl = `${proto}//${location.host}/ws?token=${encodeURIComponent(token || "")}`;
  ws = new WebSocket(wsUrl);
  ws.onopen = () => console.log("WebSocket 已连接");
  ws.onmessage = onWsMessage;
  ws.onclose = () => {
    console.log("WebSocket 断开, 3秒后重连...");
    setTimeout(connectWebSocket, 3000);
  };
  ws.onerror = e => console.error("WebSocket 错误", e);
}

function onWsMessage(event) {
  try {
    const msg = JSON.parse(event.data);
    if (msg.type === "status") {
      updateStatus(msg.data);
    } else if (msg.type === "log") {
      appendLog(msg.direction, msg.content, msg.timestamp);
    }
  } catch (e) {
    console.error("WS 消息解析失败", e);
  }
}

// ============================================================
//  状态更新
// ============================================================
function updateStatus(data) {
  // 连接状态
  const badge = document.getElementById("conn-badge");
  badge.textContent = data.connected ? "🟢 已连接" : "⚪ 未连接";
  badge.className = "conn-badge " + (data.connected ? "online" : "offline");
  // 温度
  setTemp("cpu-temp", data.cpu_temp);
  setTemp("ssd-temp", data.ssd_temp);
  setTemp("hdd-temp", data.hdd_temp);
  setTemp("ntc-temp", data.ambient_temp);
  // 硬盘标签: 只有1块时不显示"最高", >=2块才显示
  const disks = data.disk_details || [];
  const ssdCount = disks.filter(d => d.type === "SSD").length;
  const hddCount = disks.filter(d => d.type === "HDD").length;
  document.getElementById("ssd-temp").previousElementSibling.textContent = ssdCount >= 2 ? "SSD 最高" : "SSD";
  document.getElementById("hdd-temp").previousElementSibling.textContent = hddCount >= 2 ? "HDD 最高" : "HDD";
  // 风扇
  document.getElementById("fan-bar").style.width = (data.fan_speed || 0) + "%";
  document.getElementById("fan-speed-text").textContent = (data.fan_speed || 0) + "%";
  // 风扇模式
  const modeToggle = document.getElementById("fan-mode-toggle");
  const modeLabel = document.getElementById("fan-mode-label");
  const isManual = data.fan_mode === "manual";
  modeToggle.checked = isManual;
  modeLabel.textContent = isManual ? "手动" : "自动";
  document.getElementById("manual-control").classList.toggle("hidden", !isManual);
  // 计算转速 (根据当前最高温 + 配置推算, 供用户对比)
  const calcSpeed = calcExpectedSpeed(data);
  const calcEl = document.getElementById("calc-speed-text");
  if (isManual) {
    calcEl.textContent = "手动模式 (不参与自动计算)";
  } else if (calcSpeed == null) {
    calcEl.textContent = "-- (无温度数据)";
  } else {
    calcEl.textContent = calcSpeed + "%";
  }
  // 磁盘明细
  renderDiskDetails(disks);
}

// 根据当前温度和配置推算目标转速 (与后端 calc_target_speed 逻辑一致)
function calcExpectedSpeed(data) {
  const temps = [data.cpu_temp, data.ssd_temp, data.hdd_temp].filter(t => t != null);
  if (!temps.length) return null;
  const hottest = Math.max(...temps);
  const start = parseFloat(document.getElementById("cfg-start").value) || 35;
  const maxT = parseFloat(document.getElementById("cfg-max").value) || 60;
  if (hottest <= start) return 0;
  if (hottest >= maxT) return 100;
  return Math.round((hottest - start) / (maxT - start) * 100);
}

function setTemp(id, val) {
  const el = document.getElementById(id);
  if (val == null) {
    el.textContent = "N/A";
    el.className = "temp-val";
  } else {
    el.textContent = val.toFixed(1) + "°C";
    el.className = "temp-val " + tempColorClass(val);
  }
}

function tempColorClass(t) {
  if (t < 45) return "temp-green";
  if (t < 55) return "temp-orange";
  return "temp-red";
}

function renderDiskDetails(disks) {
  const list = document.getElementById("disk-list");
  if (!disks.length) {
    list.innerHTML = "<div class='disk-empty'>未检测到硬盘</div>";
    return;
  }
  list.innerHTML = disks.map(d => {
    const temp = d.temp == null ? "N/A" : d.temp.toFixed(1) + "°C";
    const cls = d.temp == null ? "" : tempColorClass(d.temp);
    return `<div class="disk-row"><span class="disk-dev">${d.device}</span><span class="disk-type">${d.type}</span><span class="disk-temp ${cls}">${temp}</span></div>`;
  }).join("");
}

function toggleDiskDetails() {
  const list = document.getElementById("disk-list");
  const arrow = document.getElementById("disk-arrow");
  list.classList.toggle("hidden");
  arrow.textContent = list.classList.contains("hidden") ? "▶" : "▼";
}

// ============================================================
//  图表
// ============================================================
function initChart() {
  const dom = document.getElementById("temp-chart");
  chart = echarts.init(dom);
  window.addEventListener("resize", () => chart.resize());
}

async function loadHistory(range) {
  try {
    const r = await fetch(`${API}/api/temperature/history?range=${range}`, { headers: authHeader() });
    const data = await r.json();
    if (!data.ok || !data.points) return;
    const labels = data.points.map(p => p.label);
    const series = [
      { name: "CPU", data: data.points.map(p => p.cpu), color: "#ff6b6b" },
      { name: "SSD", data: data.points.map(p => p.ssd), color: "#4ecdc4" },
      { name: "HDD", data: data.points.map(p => p.hdd), color: "#ffd93d" },
      { name: "环境", data: data.points.map(p => p.ntc), color: "#a8e6cf" },
    ].filter(s => s.data.some(v => v != null));
    chart.setOption({
      tooltip: { trigger: "axis" },
      legend: {
        data: series.map(s => s.name), top: 0,
        textStyle: { color: "#fff" },
        inactiveColor: "#888"
      },
      grid: { top: 40, bottom: 30, left: 40, right: 20 },
      xAxis: { type: "category", data: labels },
      yAxis: { type: "value", name: "°C" },
      series: series.map(s => ({
        name: s.name, type: "line", data: s.data, smooth: true,
        lineStyle: { color: s.color }, itemStyle: { color: s.color }, showSymbol: false
      }))
    });
  } catch (e) {
    console.error("加载历史数据失败", e);
  }
}

// ============================================================
//  风扇控制
// ============================================================
async function onFanModeChange() {
  const isManual = document.getElementById("fan-mode-toggle").checked;
  const mode = isManual ? "manual" : "auto";
  document.getElementById("fan-mode-label").textContent = isManual ? "手动" : "自动";
  document.getElementById("manual-control").classList.toggle("hidden", !isManual);
  try {
    const r = await fetch(`${API}/api/temperature/config`, {
      method: "POST", headers: authHeader(),
      body: JSON.stringify({ fan_mode: mode })
    });
    const data = await r.json();
    // 切换模式后后端会立即下发, 用返回的确认转速更新进度条
    if (data.ok && data.fan_speed !== undefined) {
      updateFanBar(data.fan_speed);
    }
  } catch (e) {
    console.error("切换模式失败", e);
  }
}

function updateFanBar(speed) {
  document.getElementById("fan-bar").style.width = speed + "%";
  document.getElementById("fan-speed-text").textContent = speed + "%";
}

function onFanSliderInput() {
  const val = document.getElementById("fan-slider").value;
  document.getElementById("fan-slider-val").textContent = val + "%";
  // 300ms 防抖
  clearTimeout(sliderDebounceTimer);
  sliderDebounceTimer = setTimeout(() => sendManualDuty(parseInt(val)), 300);
}

async function sendManualDuty(duty) {
  try {
    const r = await fetch(`${API}/api/temperature/config`, {
      method: "POST", headers: authHeader(),
      body: JSON.stringify({ manual_duty: duty })
    });
    const data = await r.json();
    // 只能根据控制器返回的确认值更新进度条
    if (data.ok && data.fan_speed !== undefined) {
      updateFanBar(data.fan_speed);
    }
  } catch (e) {
    console.error("设置风扇转速失败", e);
  }
}

// ============================================================
//  配置
// ============================================================
async function loadConfig() {
  try {
    const r = await fetch(`${API}/api/config`, { headers: authHeader() });
    const data = await r.json();
    if (data.ok && data.config) {
      document.getElementById("cfg-start").value = data.config.start_temp || 35;
      document.getElementById("cfg-max").value = data.config.max_temp || 60;
    }
  } catch (e) {
    console.error("加载配置失败", e);
  }
}

async function saveConfig() {
  const start = parseInt(document.getElementById("cfg-start").value);
  const max = parseInt(document.getElementById("cfg-max").value);
  try {
    await fetch(`${API}/api/temperature/config`, {
      method: "POST", headers: authHeader(),
      body: JSON.stringify({ start_temp: start, max_temp: max })
    });
    alert("配置已保存");
  } catch (e) {
    alert("保存失败: " + e.message);
  }
}

// ============================================================
//  断连记录
// ============================================================
async function loadDisconnectLog() {
  try {
    const r = await fetch(`${API}/api/disconnect-log`, { headers: authHeader() });
    const data = await r.json();
    if (!data.ok) return;
    const list = document.getElementById("disc-list");
    if (!data.entries || !data.entries.length) {
      list.innerHTML = "暂无断连记录";
      return;
    }
    list.innerHTML = data.entries.map(e => {
      const status = e.status === "disconnected" ? '<span style="color:var(--red)">● 断连中</span>' : '<span style="color:var(--green)">● 已恢复</span>';
      const dur = e.duration_s != null ? ` (${e.duration_s}s)` : "";
      return `<div class="disc-item">${status} ${e.start} → ${e.end || "..." + dur}</div>`;
    }).join("");
  } catch (e) {
    console.error("加载断连记录失败", e);
  }
}

// ============================================================
//  命令终端
// ============================================================
async function sendRaw() {
  const cmd = document.getElementById("raw-cmd").value.trim();
  if (!cmd) return;
  try {
    const r = await fetch(`${API}/api/command`, {
      method: "POST", headers: authHeader(),
      body: JSON.stringify({ content: cmd })
    });
    const data = await r.json();
    if (data.response) {
      appendLog("rx", data.response, Date.now());
    }
  } catch (e) {
    console.error("发送命令失败", e);
  }
}

// ============================================================
//  通信日志
// ============================================================
const MAX_LOG_LINES = 100;
function appendLog(direction, content, timestamp) {
  const logBox = document.getElementById("comm-log");
  const ts = new Date(timestamp).toLocaleTimeString();
  const prefix = direction === "tx" ? "→ TX" : "← RX";
  const line = `[${ts}] ${prefix}: ${content}\n`;
  logBox.textContent += line;
  // 限制行数
  const lines = logBox.textContent.split("\n");
  if (lines.length > MAX_LOG_LINES) {
    logBox.textContent = lines.slice(-MAX_LOG_LINES).join("\n");
  }
  logBox.scrollTop = logBox.scrollHeight;
}

// ============================================================
//  折叠 / 页面切换
// ============================================================
function toggleCard(id) {
  const body = document.getElementById(id);
  body.classList.toggle("collapsed");
  const btn = document.querySelector(`[onclick="toggleCard('${id}')"] .fold-btn`);
  if (btn) btn.textContent = body.classList.contains("collapsed") ? "展开" : "收起";
}

function switchPage(page) {
  // 移动端: 切换显示区域
  document.querySelectorAll(".nav-btn").forEach(b => b.classList.toggle("active", b.dataset.page === page));
  const monitor = document.getElementById("page-monitor");
  const right = document.querySelector(".col-right");
  // 配置/日志分组卡片
  const configCards = document.querySelectorAll('.col-right [data-group="config"]');
  const logCards = document.querySelectorAll('.col-right [data-group="log"]');

  if (page === "monitor") {
    monitor.classList.remove("hidden");
    right.classList.add("hidden");
    // 切回监控页时让 ECharts 重新计算尺寸 (移动端切页后容器尺寸变化)
    if (typeof chart !== "undefined" && chart) {
      setTimeout(() => chart.resize(), 50);
    }
  } else {
    monitor.classList.add("hidden");
    right.classList.remove("hidden");
    // 配置页只显示配置组, 日志页只显示日志组
    configCards.forEach(c => c.classList.toggle("hidden", page !== "config"));
    logCards.forEach(c => c.classList.toggle("hidden", page !== "log"));
  }
}

// ============================================================
//  启动
// ============================================================
document.addEventListener("DOMContentLoaded", () => {
  checkAuth();
});
