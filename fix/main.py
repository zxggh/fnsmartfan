"""
STC 智能风扇控制器 — FastAPI REST API

端点:
  GET   /api/temps          — NAS温度(CPU/SSD/HDD)
  GET   /api/ntc            — 控制器环境温度(GETNTC?)
  GET   /api/status         — 风扇实时状态
  GET   /api/speed/{fan}    — 查询风扇转速(F1CPD?)
  PUT   /api/speed/{fan}    — 设置风扇转速(F1CPD=80)
  GET   /api/control        — 温控配置
  PUT   /api/control        — 更新温控配置
  POST  /api/control/reset  — 恢复默认温控参数
  POST  /api/control/run    — 执行一次温控(读温度→算转速→设转速)
  POST  /api/raw            — 发送原始命令
  GET   /api/info           — 服务/连接信息
  GET   /api/temps/history?range=1h|6h|24h|3d|7d  — [v6 新增] 温度历史曲线数据
                                              (1分钟采样, 7天保留, JSONL存/data)

v4 修复:
  - heartbeat 心跳节奏从 5.1s 降到 3.5s(sleep 1.5s + ping 2s), 远离 STC 5s 复位窗口
  - 删除心跳里冗余的 get_ntc_temp 调用(ping 已顺带更新 NTC 缓存)
  - heartbeat 循环体外层包 try/except Exception 兜底, 永不崩溃
  - task_hb 增加 add_done_callback, 协程意外退出时自动重建(双保险)
"""

import os, sys, json, yaml, time, logging, asyncio
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from serial_ctrl import STCController
from temp_collector import TempCollector
from control import FanController, DEFAULT_CONFIG
from disconnect_log import DisconnectLogger
# v6 新增: 温度历史持久化 (JSONL 写入 /data 卷, 7 天保留, 降采样查询)
from temp_history import TempHistory, parse_range, RANGE_MAP, fmt_timestamp

# ── 配置 ──
CONFIG_PATH = Path(__file__).parent / "config.yaml"
def load_config() -> dict:
    default = {
        "serial": {"port": "/dev/ttyACM0", "baudrate": 115200, "timeout": 2.0},
        "server": {"host": "0.0.0.0", "port": 8780},
        "logging": {"level": "INFO"},
    }
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                user = yaml.safe_load(f) or {}
                for k, v in user.items():
                    if isinstance(v, dict) and k in default:
                        default[k].update(v)
                    else:
                        default[k] = v
        except Exception as e:
            logging.warning(f"config load: {e}")
    return default

config = load_config()
logging.basicConfig(level=getattr(logging, config["logging"]["level"].upper(), logging.INFO),
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("fan_ctrl")

controller: Optional[STCController] = None
temp_collector: Optional[TempCollector] = None
fan_control: Optional[FanController] = None
disc_log: Optional[DisconnectLogger] = None
# v6 新增: 温度历史持久化实例 (后台 60s 采样, 7 天 JSONL 存 /data)
temp_history: Optional[TempHistory] = None
start_time = time.time()

# ── 自动命令发送使能开关(调试用) ──
# True: 正常按时间下发心跳/温控命令   False: 停止自动发送, 但允许手动发送
auto_cmd_enabled: bool = True
# 持久化文件: 放在 /data 目录(已挂卷), 容器重启后保留开关状态
AUTO_CMD_STATE_FILE = Path("/data/auto_cmd_state.json")

def _load_auto_cmd_state() -> bool:
    """从持久化文件加载自动命令开关状态."""
    try:
        if AUTO_CMD_STATE_FILE.exists():
            with open(AUTO_CMD_STATE_FILE, "r") as f:
                data = json.load(f)
                return bool(data.get("enabled", True))
    except Exception as e:
        logger.warning(f"加载自动命令开关状态失败: {e}")
    return True

def _save_auto_cmd_state(enabled: bool):
    """保存自动命令开关状态到持久化文件."""
    try:
        AUTO_CMD_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(AUTO_CMD_STATE_FILE, "w") as f:
            json.dump({"enabled": bool(enabled)}, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"保存自动命令开关状态失败: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global controller, temp_collector, fan_control, disc_log, auto_cmd_enabled, temp_history
    # 启动时加载持久化的开关状态
    auto_cmd_enabled = _load_auto_cmd_state()
    # v6 初始化温度历史持久化 (放在 /data 卷, 容器重启不丢, 启动时清理过期数据)
    temp_history = TempHistory()
    logger.info(f"自动命令发送开关: {'开启' if auto_cmd_enabled else '关闭'}")
    controller = STCController(**config["serial"])
    temp_collector = TempCollector()
    fan_control = FanController()
    disc_log = DisconnectLogger()
    try:
        await controller.connect()
        # 连接后立即做一次心跳验证
        ping_ok = await controller.ping()
        logger.info(f"控制器已连接, 心跳={'正常' if ping_ok else '无响应'}")
        if not ping_ok:
            disc_log.start()
    except Exception as e:
        logger.warning(f"控制器连接失败: {e}")
        disc_log.start()
    # 后台心跳保活任务(v4: 节奏降到 3.5s, 远离 STC 5s 复位窗口)
    # 连续2次ping失败才判定断连(v4: ping 单次失败不再立即置 _connected=False, 避免抖动误判)
    # ★ 自动命令开关关闭(auto_cmd_enabled=False)时: 跳过自动心跳, 不占用串口
    async def heartbeat():
        """心跳协程 — v4 重写.

        关键改进:
        - sleep(1.5) 替代 sleep(3): 心跳节奏从 5.1s 降到 3.5s, 远离 STC 5s 复位窗口
        - 删除冗余的 await controller.get_ntc_temp() 调用:
          ping() 已顺带解析 NTC 响应并更新 ntc_cache, 不再二次发 GETNTC?
          (这本身就是导致心跳节奏 5.1s 超过 5s 临界点的元凶之一)
        - 循环体外层包 try/except Exception 兜底:
          即使出现非预期异常(如 RuntimeError/SerialException), 也不会让协程崩溃
        - 连续2次ping失败才判定断连(配合 serial_ctrl.py 的 ping 改进)
        """
        fail_count = 0
        last_cmd = time.time()
        while True:
            try:
                await asyncio.sleep(1.5)
                # ── 自动命令开关: 关闭时跳过自动心跳 ──
                if not auto_cmd_enabled:
                    continue
                if not (controller and disc_log):
                    continue
                gap = time.time() - last_cmd
                if gap > 5.0:
                    logger.warning(f"命令间隔{gap:.1f}s 超过5s固件窗口!")
                if await controller.ping():
                    last_cmd = time.time()
                    fail_count = 0
                    # ping 成功但之前有未关闭的断连 → 恢复
                    # (NTC 缓存已由 ping 顺带更新, 不再二次调用 get_ntc_temp)
                    if disc_log.is_disconnected():
                        disc_log.end()
                else:
                    fail_count += 1
                    if fail_count >= 2:
                        # v4: 阈值从 3 降到 2, 配合 ping 单次失败不再立即置 _connected=False
                        # -> 真断连时更快触发重连; 抖动时 ping 单次失败不会立即累计
                        logger.warning("连续2次心跳失败, 判定断连, 尝试重连")
                        disc_log.start()
                        ok = await controller.ensure_connected()
                        if ok:
                            disc_log.end()
                        fail_count = 0
            except Exception as e:
                # v4: 兜底, 任何意外异常都不会让心跳协程崩溃
                logger.error(f"heartbeat 异常(已吞掉, 5秒后继续): {e}", exc_info=True)
                await asyncio.sleep(5)

    task_hb = asyncio.create_task(heartbeat())

    # v4: task 意外结束时自动重建(双保险, 配合上面的 try/except)
    def _hb_done_callback(t):
        if t.cancelled():
            return
        exc = t.exception() if not t.cancelled() else None
        if exc:
            logger.error(f"heartbeat task 意外结束: {exc}, 5秒后重建")
        else:
            logger.warning("heartbeat task 意外退出(无异常), 5秒后重建")
        # 重建
        new_task = asyncio.create_task(heartbeat())
        new_task.add_done_callback(_hb_done_callback)
    task_hb.add_done_callback(_hb_done_callback)

    # 后台自动控温任务(每10秒计算温控, 转速变化才发送, 降低命令量)
    # ★ 自动命令开关关闭(auto_cmd_enabled=False)时: 跳过自动控温, 但手动/API仍可用
    async def auto_control():
        last_target = None
        while True:
            try:
                await asyncio.sleep(10)
                # ── 自动命令开关: 关闭时跳过自动控温 ──
                if not auto_cmd_enabled:
                    continue
                if not controller or not fan_control:
                    continue
                if not controller.connected:
                    continue
                td = await temp_collector.collect_all()
                cpu = None
                for k, v in td.items():
                    if "coretemp" in k and isinstance(v, dict):
                        vals = [x for x in v.values() if isinstance(x, (int, float))]
                        if vals:
                            cpu = round(max(vals), 1)
                ssd = td.get("hwmon_nvme", {}).get("temp1_input")
                # HDD 温度: 优先用 temp_collector 汇总的多盘最高温(hdd键), 兜底 disk_sda
                hdd = td.get("hdd", {}).get("temperature")
                if hdd is None:
                    hdd = td.get("disk_sda", {}).get("temperature")
                target = fan_control.calc_target_speed(cpu, ssd, hdd)
                # 只有目标转速变化时才下发, 只控制风扇1
                # set_fan_speed 会等控制器确认, 3次无响应返回失败(不更新缓存)
                if target != last_target:
                    r = await controller.set_fan_speed(1, target)
                    if r.get("ok"):
                        last_target = target
            except Exception as e:
                # v4: 兜底, auto_control 也加保护
                logger.error(f"auto_control 异常(已吞掉, 10秒后继续): {e}", exc_info=True)
                await asyncio.sleep(10)
    task_ac = asyncio.create_task(auto_control())

    # v6 新增: 温度历史采集任务 (每 60 秒采样 1 次, 写 JSONL 到 /data, 7 天保留)
    # 注意: 这个任务不依赖 auto_cmd_enabled 开关, 即使关闭自动命令也继续记录温度
    #       (历史数据是用来分析的, 和控温逻辑解耦)
    async def history_collector():
        last_run = 0  # 避免启动时多线程抢跑
        while True:
            try:
                await asyncio.sleep(10)  # 启动先等 10s, 让其它任务先跑起来 + 连接建立
                now = time.time()
                # 每 SAMPLE_INTERVAL_S (默认60s) 采样一次
                if now - last_run < 60:
                    continue
                last_run = now
                if not (temp_history and temp_collector):
                    continue
                # 采集 NAS 温度 (CPU/SSD/HDD)
                td = await temp_collector.collect_all()
                cpu = None
                for k, v in td.items():
                    if "coretemp" in k and isinstance(v, dict):
                        vals = [x for x in v.values() if isinstance(x, (int, float))]
                        if vals:
                            cpu = round(max(vals), 1)
                ssd = td.get("hwmon_nvme", {}).get("temp1_input")
                hdd = td.get("hdd", {}).get("temperature")
                if hdd is None:
                    hdd = td.get("disk_sda", {}).get("temperature")
                # NTC 从缓存读 (不占串口, ping 每 3.5s 就在刷新缓存)
                ntc = None
                if controller:
                    res = controller.get_ntc_cached()
                    if res.get("ok"):
                        ntc_val = res.get("temperature")
                        if isinstance(ntc_val, (int, float)):
                            ntc = round(float(ntc_val), 1)
                # 写入历史 (4 个温度都可能为 None, 代表当时没采集到)
                temp_history.add_record(cpu=cpu, ssd=ssd, hdd=hdd, ntc=ntc)
            except Exception as e:
                # 兜底: 任何异常都不会让历史采集崩溃
                logger.error(f"history_collector 异常(已吞掉, 30秒后继续): {e}", exc_info=True)
                await asyncio.sleep(30)

    task_hist = asyncio.create_task(history_collector())

    # v4: task 意外结束时自动重建 (同样模式套给 history_collector)
    def _hist_done_callback(t):
        if t.cancelled():
            return
        exc = t.exception() if not t.cancelled() else None
        if exc:
            logger.error(f"history task 意外结束: {exc}, 5秒后重建")
        else:
            logger.warning("history task 意外退出(无异常), 5秒后重建")
        new_task = asyncio.create_task(history_collector())
        new_task.add_done_callback(_hist_done_callback)
    task_hist.add_done_callback(_hist_done_callback)

    yield
    task_hb.cancel()
    task_ac.cancel()
    task_hist.cancel()
    if controller and controller._writer:
        await controller.disconnect()

app = FastAPI(title="STC 智能风扇控制器", version="2.0.0", lifespan=lifespan)

# ── 模型 ──
class SpeedSet(BaseModel):
    value: int

class ControlUpdate(BaseModel):
    start: int
    max: int

class RawCmd(BaseModel):
    cmd: str

class AutoCmdSet(BaseModel):
    enabled: bool

# ═══════════════ 静态文件 + 根路径 ═══════════════

# 挂载静态文件(加 no-cache 头)
from fastapi.staticfiles import StaticFiles
class NoCacheStaticFiles(StaticFiles):
    async def __call__(self, scope, receive, send):
        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = message.get("headers", [])
                headers = [h for h in headers if h[0].lower() != b"cache-control"]
                headers.append((b"cache-control", b"no-cache, no-store, must-revalidate"))
                message["headers"] = headers
            await send(message)
        await super().__call__(scope, receive, send_wrapper)

STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", NoCacheStaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/")
async def root():
    idx = STATIC_DIR / "index.html"
    if idx.exists():
        html = idx.read_text(encoding="utf-8")
        from fastapi.responses import HTMLResponse
        return HTMLResponse(content=html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    return {"service": "STC 智能风扇控制器", "docs": "/docs"}

@app.get("/api/temps")
async def get_temps():
    """CPU/SSD/HDD/NAS温度采集."""
    if not temp_collector:
        return {"ok": False, "error": "未初始化"}
    try:
        data = await temp_collector.collect_all()
        # 提取关键值
        cpu = None
        for k, v in data.items():
            if "coretemp" in k and isinstance(v, dict):
                vals = [x for x in v.values() if isinstance(x, (int, float))]
                if vals:
                    cpu = round(max(vals), 1)
        ssd = data.get("hwmon_nvme", {}).get("temp1_input")
        # HDD 温度: 优先用 temp_collector 汇总的多盘最高温(hdd键), 兜底 disk_sda
        hdd = data.get("hdd", {}).get("temperature")
        if hdd is None:
            hdd = data.get("disk_sda", {}).get("temperature")
        return {"ok": True, "data": {
            "cpu": cpu, "ssd": ssd, "hdd": hdd,
            "raw": {k: v for k, v in data.items()
                    if not k.startswith("disk_") or v.get("temperature") is not None}
        }}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/temps/history")
async def get_temps_history(range: str = "24h"):
    """v6 新增: 查询温度历史曲线数据.

    Query 参数 range 可选: 1h / 6h / 12h / 24h / 3d / 7d
    设计说明:
      - 后端以 1 分钟/条 的频率写入 JSONL 到 /data (容器卷, 重启不丢)
      - 7 天自动保留 ≈ 1 万条, 单条 70B, 对 NAS 完全零压力
      - 查询时返回点数 > MAX_POINTS(默认360) 时自动按时间桶做均值降采样,
        保证前端 ECharts 画图不卡顿
      - 本接口和 /api/temps(实时采集, 每 3s 轮询 poll 用) 解耦:
        实时卡片用 poll, 历史曲线用本接口整图重绘
    返回:
      - ok / range / seconds        : 回显请求参数, 方便前端校验
      - total_history_records        : 磁盘上总共累积了多少条(1条=1分钟)
      - returned_points / max_points_cap : 本次返回条数 / 降采样上限
      - ranges: 可用选项列表 (给前端下拉菜单直接用, 带中文标签)
      - points: 数据点数组, 每点含 label/X轴文字 + 4 个温度(None=未采集到)
    """
    if not temp_history:
        return {"ok": False, "error": "未初始化"}
    seconds = parse_range(range)
    records = temp_history.query_range_seconds(seconds)
    # 组装返回, label 字段前端直接当 X 轴文字 (已格式化)
    points = []
    for r in records:
        ts = r["t"]
        points.append({
            "t": ts,
            "label": fmt_timestamp(ts, seconds),
            "cpu": r.get("cpu"),
            "ssd": r.get("ssd"),
            "hdd": r.get("hdd"),
            "ntc": r.get("ntc"),
        })
    # 统计总记录数 (给用户看历史总积累, 增加信任感)
    total_raw = len(records)
    try:
        from pathlib import Path as _P
        fp = _P("/data/temp_history.jsonl")
        if fp.exists():
            with open(fp, "r", encoding="utf-8") as f:
                total_raw = sum(1 for _ in f)
    except Exception:
        pass
    # ranges 选项: 中文标签 + 对应采样条数 (前端下拉直接渲染)
    ranges_ui = [
        {"value": "1h",  "label": "最近 1 小时", "samples": 60},
        {"value": "6h",  "label": "最近 6 小时", "samples": 360},
        {"value": "12h", "label": "最近 12 小时", "samples": 720},
        {"value": "24h", "label": "最近 24 小时", "samples": 1440},
        {"value": "3d",  "label": "最近 3 天",   "samples": 4320},
        {"value": "7d",  "label": "最近 7 天",   "samples": 10080},
    ]
    return {
        "ok": True,
        "range": range,
        "seconds": seconds,
        "sample_interval_s": 60,
        "retain_days": 7,
        "total_history_records": total_raw,
        "returned_points": len(points),
        "max_points_cap": 360,
        "ranges": ranges_ui,
        "points": points,
    }

@app.get("/api/ntc")
async def get_ntc():
    """环境温度(从缓存读, 不占用串口)."""
    if not controller:
        raise HTTPException(503, "未初始化")
    result = controller.get_ntc_cached()
    if result.get("ok"):
        return {"ok": True, **result}
    # 缓存无值(刚启动): 直接查询一次
    r = await controller.get_ntc_temp()
    return {"ok": r["ok"], **r}

# ═══════════════ 风扇转速 ═══════════════

@app.get("/api/speed/{fan}")
async def get_speed(fan: int):
    """查询风扇转速(F1CPD?). fan=1 或 2."""
    if fan not in (1, 2):
        raise HTTPException(400, "fan must be 1 or 2")
    if not controller or not controller.connected:
        raise HTTPException(503, "控制器未连接")
    result = await controller.get_fan_speed(fan)
    return {"ok": result["ok"], "fan": fan, **result}

@app.put("/api/speed/{fan}")
async def set_speed(fan: int, req: SpeedSet):
    """设置风扇转速(F1CPD=80). fan=1 或 2, value=0-100."""
    if fan not in (1, 2):
        raise HTTPException(400, "fan must be 1 or 2")
    if not controller or not controller.connected:
        raise HTTPException(503, "控制器未连接")
    result = await controller.set_fan_speed(fan, req.value)
    return {"ok": result["ok"], "fan": fan, "set_to": req.value, **result}

# ═══════════════ 温控配置 ═══════════════

@app.get("/api/control")
async def get_control_config():
    """获取当前温控参数(统一配置)."""
    if not fan_control:
        return {"ok": False, "error": "未初始化"}
    return {"ok": True, "data": fan_control.config,
            "defaults": DEFAULT_CONFIG}

@app.put("/api/control")
async def update_control(req: ControlUpdate):
    """更新温控参数(启动温度/最高温度)."""
    if not fan_control:
        return {"ok": False, "error": "未初始化"}
    fan_control.update_config(req.start, req.max)
    return {"ok": True, "config": fan_control.config}

@app.post("/api/control/reset")
async def reset_control():
    """恢复默认温控参数."""
    if not fan_control:
        return {"ok": False, "error": "未初始化"}
    fan_control.reset_config()
    return {"ok": True, "data": DEFAULT_CONFIG}

# ═══════════════ 控制回路 ═══════════════

@app.post("/api/control/run")
async def run_control():
    """
    执行一次完整温控:
      1. 采集 NAS 温度 (CPU/SSD/HDD)
      2. 按配置计算目标转速
      3. 设置 F1 和 F2 转速
      4. 读取 NTC 环境温度
      5. 返回所有结果
    """
    if not fan_control:
        return {"ok": False, "error": "未初始化"}
    if not controller or not controller.connected:
        return {"ok": False, "error": "控制器未连接"}

    # 1. 采集 NAS 温度
    temps_data = await temp_collector.collect_all()
    cpu = None
    for k, v in temps_data.items():
        if "coretemp" in k and isinstance(v, dict):
            vals = [x for x in v.values() if isinstance(x, (int, float))]
            if vals:
                cpu = round(max(vals), 1)
    ssd = temps_data.get("hwmon_nvme", {}).get("temp1_input")
    # HDD 温度: 优先用 temp_collector 汇总的多盘最高温(hdd键), 兜底 disk_sda
    hdd = temps_data.get("hdd", {}).get("temperature")
    if hdd is None:
        hdd = temps_data.get("disk_sda", {}).get("temperature")
    td = {"cpu": cpu, "ssd": ssd, "hdd": hdd}

    # 2. 计算目标转速
    target = fan_control.calc_target_speed(td["cpu"], td["ssd"], td["hdd"])

    # 3. 设置风扇(两条命令用;组合,一次性发送)
    combined = f"F1CPD={target};F2CPD={target}"
    await controller.send_raw(combined)
    # 读取设置后的转速验证
    f1 = await controller.get_fan_speed(1)
    f2 = await controller.get_fan_speed(2)

    # 4. 读取 NTC
    ntc = await controller.get_ntc_temp()

    return {
        "ok": True,
        "temps": {"cpu": td["cpu"], "ssd": td["ssd"], "hdd": td["hdd"]},
        "target_speed": target,
        "fan1": f1.get("value"),
        "fan2": f2.get("value"),
        "ntc": ntc.get("value"),
    }

# ═══════════════ 辅助 ═══════════════

@app.post("/api/raw")
async def send_raw(req: RawCmd):
    """发送原始命令到控制器."""
    if not controller or not controller.connected:
        raise HTTPException(503, "控制器未连接")
    try:
        resp = await controller.send_raw(req.cmd)
        return {"ok": True, "cmd": req.cmd, "response": resp}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ═══════════════ 自动命令发送开关 ═══════════════

@app.get("/api/auto-cmd")
async def get_auto_cmd():
    """获取自动命令发送开关状态."""
    return {"ok": True, "enabled": auto_cmd_enabled}

@app.post("/api/auto-cmd")
async def set_auto_cmd(req: AutoCmdSet):
    """设置自动命令发送开关状态(持久化)."""
    global auto_cmd_enabled
    auto_cmd_enabled = bool(req.enabled)
    _save_auto_cmd_state(auto_cmd_enabled)
    logger.info(f"自动命令发送开关已{'开启' if auto_cmd_enabled else '关闭'}")
    return {"ok": True, "enabled": auto_cmd_enabled}

@app.get("/api/info")
async def get_info():
    c = controller
    return {"ok": True, "data": {
        "service": "STC 智能风扇控制器",
        "version": "2.0.0",
        "uptime": round(time.time() - start_time, 1),
        "controller_connected": c.connected if c else False,
        "controller_port": config["serial"]["port"],
        "auto_cmd_enabled": auto_cmd_enabled,
    }}

@app.get("/api/status")
async def get_status():
    """风扇转速(从缓存读,即时返回)."""
    if not controller:
        raise HTTPException(503, "未初始化")
    return {"ok": True,
            "auto_cmd_enabled": auto_cmd_enabled,
            "fan1": {"转速": f"{controller.speed_cache.get(1,0)}%"},
            "fan2": {"转速": f"{controller.speed_cache.get(2,0)}%"}}

@app.post("/api/led")
async def toggle_led(req: RawCmd):
    """LED 开关. body: {"cmd": "ON"} 或 {"cmd": "OFF"}"""
    if not controller or not controller.connected:
        raise HTTPException(503, "控制器未连接")
    if req.cmd.upper() not in ("ON", "OFF"):
        return {"ok": False, "error": "cmd 需为 ON 或 OFF"}
    result = await controller.set_led(req.cmd.upper() == "ON")
    return {"ok": result["ok"], "state": result.get("state")}

@app.get("/api/log")
async def get_log():
    """返回最近的串口交互日志(调试用)."""
    if not controller:
        return {"ok": False, "error": "未初始化"}
    return {"ok": True, "log": controller.log[-30:]}

@app.get("/api/disconnect-log")
async def get_disconnect_log(limit: int = 50):
    """查询控制器断连记录(持久化, 掉电不丢失)."""
    if not disc_log:
        return {"ok": False, "error": "未初始化"}
    entries = disc_log.get_entries(limit=limit)
    # 当前断连状态 = 记录中未恢复 或 控制器实际未连接
    cur_disc = disc_log.is_disconnected() or (controller is not None and not controller.connected)
    return {"ok": True, "count": len(entries), "entries": entries,
            "currently_disconnected": cur_disc}


# ── 入口 ──
def main():
    host = config["server"]["host"]
    port = config["server"]["port"]
    logger.info(f"启动: http://{host}:{port}")
    uvicorn.run("main:app", host=host, port=port, reload=False,
                log_level=config["logging"]["level"].lower())

if __name__ == "__main__":
    main()
