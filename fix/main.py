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
from datetime import datetime
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional, Tuple

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

# ── v7 新增: 温度采集缓存 (解决滑块change后 PUT /api/control 立即下发要等 5s 的问题)
#    temp_collector.collect_all() 要读 SMART/HDD 等, 单次 3~5s.
#    auto_control (3s 周期) / history_collector / run_control 一直在刷新温度,
#    所以 PUT /api/control 没必要重采, 用 <=3s 内的缓存即可, 立即下发快 10 倍.
_TEMPS_CACHE_DATA: Optional[dict] = None
_TEMPS_CACHE_TIME: float = 0.0       # 缓存"开始采集"的时间戳 (真采开始时就写, 不是完成时!)
# v7 修复: max_age 从 2.5s → 12s
#   ★ 原BUG（时间窗口悖论）: NAS 扫 HDD SMART 真采一次要 3~8s，旧 max_age=2.5s
#     导致每次真采完成后，缓存里写的 age 其实已经 = 采集耗时(8s) > 2.5s
#     → 下一次 collect_temps_cached 永远判定 "缓存已过期" → 100% 真采 → 永远 5s 延迟
#   修复: 温度是慢变化物理量，12s 的缓存对人无感，但 12s > 最坏采集耗时 8s → 必定命中
_TEMPS_CACHE_MAX_AGE: float = 12.0

async def collect_temps_cached(max_age_s: float = _TEMPS_CACHE_MAX_AGE) -> dict:
    """温度采集(带缓存包装): max_age_s 秒内有缓存直接返回, 否则真采一次并写缓存.

    所有需要温度的地方(立即下发/auto_control/run_control/历史采样)都应走这个包装,
    避免重复扫 SMART → 5 秒延迟.

    ★ 时间戳策略: 真采"开始前"就写 _TEMPS_CACHE_TIME, 不是完成后.
      这样即使采集花了 8 秒, 完成后后续请求的 age=8s < 12s → 缓存命中.
    """
    global _TEMPS_CACHE_DATA, _TEMPS_CACHE_TIME
    now = time.time()
    age = (now - _TEMPS_CACHE_TIME) if _TEMPS_CACHE_DATA is not None else None
    if (_TEMPS_CACHE_DATA is not None and age is not None and age <= max_age_s):
        logger.info(f"collect_temps_cached: 缓存命中 (age={age:.1f}s <= max_age={max_age_s:.0f}s)")
        return _TEMPS_CACHE_DATA
    # 缓存失效/为空: 真采
    if temp_collector is None:
        return {}
    # ★ 关键: 开始真采"前"就写时间戳(而不是完成后)
    #   这样就算这次采了 8s, 完成后后续请求的 age=8s, 仍然在 12s 窗口内
    _TEMPS_CACHE_TIME = time.time()
    t0 = time.time()
    miss_reason = "空缓存" if _TEMPS_CACHE_DATA is None else f"过期 (age={age:.1f}s > max_age={max_age_s:.0f}s)"
    logger.info(f"collect_temps_cached: 缓存未命中({miss_reason}), 开始真采...")
    data = await temp_collector.collect_all()
    cost = round(time.time() - t0, 2)
    _TEMPS_CACHE_DATA = data
    # ★ 注意: _TEMPS_CACHE_TIME 不再重新赋值! 保持"开始真采"那一刻的值
    #   (这样完成后后续请求的 age = cost, 仍 < 12s, 命中)
    logger.info(f"collect_temps_cached: 真采完成, 耗时 {cost}s (>=1s属正常: 扫HDD SMART). 缓存生效窗口剩余 ~{max(0, max_age_s - cost):.0f}s")
    return data


def _extract_temp_sensors(td: dict) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """从 temp_collector.collect_all() 返回的大 dict 中统一提取 (CPU, SSD, HDD).

    原代码在 auto_control / update_control / reset_control / run_control
    / history_collector / GET /api/temps 里重复了 6 遍相同的核心提取逻辑,
    极易出现 copy-paste 不一致 BUG. 现在统一走这个函数.
    """
    # CPU: 优先取 i915(SoC温度, 与飞牛NAS系统显示一致), 无则取 coretemp Package
    cpu: Optional[float] = None
    # 飞牛NAS系统显示的是 i915(Intel集显/SoC)温度, 比coretemp低~2°C
    i915 = td.get("hwmon_i915", {}).get("temp1_input")
    if isinstance(i915, (int, float)) and not isinstance(i915, bool):
        cpu = round(float(i915), 1)
    if cpu is None:
        for k, v in td.items():
            if "coretemp" in k and isinstance(v, dict):
                vals = [x for x in v.values() if isinstance(x, (int, float))]
                if vals:
                    cpu = round(float(max(vals)), 1)
                break
    # SSD: 仅取 hwmon_nvme (NVMe SSD), 无NVMe则为None
    # 不兜底其他hwmon设备(避免把网卡/GPU温度误当SSD温度)
    ssd = td.get("hwmon_nvme", {}).get("temp1_input")
    if not isinstance(ssd, (int, float)) or isinstance(ssd, bool):
        ssd = None
    # HDD: 优先 hdd.temperature (多盘已取max), 兜底 disk_sda.temperature
    hdd = None
    if isinstance(td.get("hdd"), dict):
        hdd = td["hdd"].get("temperature")
    if hdd is None and isinstance(td.get("disk_sda"), dict):
        hdd = td["disk_sda"].get("temperature")
    if not isinstance(hdd, (int, float)) or isinstance(hdd, bool):
        # 最后兜底: 任何 disk_* 键里的 temperature
        for k, v in td.items():
            if isinstance(k, str) and k.startswith("disk_") and isinstance(v, dict):
                candidate = v.get("temperature")
                if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
                    hdd = candidate
                    break
    return cpu, float(ssd) if isinstance(ssd, (int, float)) and not isinstance(ssd, bool) else None, \
           float(hdd) if isinstance(hdd, (int, float)) and not isinstance(hdd, bool) else None

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
    # ★ v6 加固: 任何初始化异常(最常见 PermissionError 卷权限)都不抛到 lifespan,
    #   设为 None 降级 — 核心温控/实时温度完全正常, 仅历史曲线功能暂时不可用,
    #   避免 FastAPI startup fail → 容器直接退出 → 无限重启.
    try:
        temp_history = TempHistory()
    except Exception as e:
        temp_history = None
        logger.error(
            f"⚠️  温度历史模块初始化失败(已停用历史功能, 服务继续启动): {e}. "
            f"最常见原因: /data 卷无写入权限 — 请执行 chmod -R 777 /你的卷目录 "
            f"或确保容器以 root 用户启动."
        , exc_info=False)
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

    # ── v7 温度缓存预热 (解决容器刚启动3秒内首次操作真采5秒问题) ──
    #   容器刚启动: auto_control要等3秒, history_collector要等10秒才第一次采温度
    #   此时若用户立刻点「▶ 执行」或改配置: _TEMPS_CACHE_DATA 为空 → 真采 3~5s
    #   这里在 lifespan 最后异步启动一个预热任务, 后台立刻采一次温度填缓存, 不阻塞启动
    async def _warmup_temp_cache():
        try:
            # 稍微等 0.5s, 让 controller/heartbeat 初始化完
            await asyncio.sleep(0.5)
            t0 = time.time()
            result = await collect_temps_cached(max_age_s=99999)  # 强制真采
            cost = round(time.time() - t0, 2)
            if result:
                logger.info(f"[缓存预热] 完成, 耗时{cost}s, 温度CPU={_TEMPS_CACHE_DATA.get('coretemp')} SSD={_TEMPS_CACHE_DATA.get('hwmon_nvme')} HDD={_TEMPS_CACHE_DATA.get('hdd') or _TEMPS_CACHE_DATA.get('disk_sda')}")
            else:
                logger.warning(f"[缓存预热] 无数据, 耗时{cost}s")
        except Exception as e:
            logger.warning(f"[缓存预热] 失败(不影响服务, 后续调用会自动真采): {e}")
    _ = asyncio.create_task(_warmup_temp_cache())
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

    # 后台自动控温任务(每3秒计算温控, 转速变化才发送, 降低命令量)
    # ★ 自动命令开关关闭(auto_cmd_enabled=False)时: 跳过自动控温, 但手动/API仍可用
    # ★ v7优化: 轮询周期从10s缩短到3s, 提升温控响应速度
    # ★ v7修复: 只控制 F1 (系统只有一个风扇, F2 不存在, 不要再下发 F2CPD 命令)
    async def auto_control():
        last_target = None
        while True:
            try:
                await asyncio.sleep(3)
                # ── 自动命令开关: 关闭时跳过自动控温 ──
                if not auto_cmd_enabled:
                    continue
                if not controller or not fan_control:
                    continue
                if not controller.connected:
                    continue
                td = await collect_temps_cached()
                # 统一走公共函数提取 CPU/SSD/HDD (原重复6处copy-paste容易出错)
                cpu, ssd, hdd = _extract_temp_sensors(td)
                target = fan_control.calc_target_speed(cpu, ssd, hdd)
                # 只有目标转速变化时才下发, 只控制 F1 (无 F2)
                # set_fan_speed 会等控制器确认, 2次无响应返回失败(不更新缓存)
                if target != last_target:
                    r1 = await controller.set_fan_speed(1, target)
                    if r1.get("ok"):
                        last_target = target
                        logger.info(f"[auto_control] 目标{target}% → F1={r1.get('value')}%")
                    else:
                        logger.warning(f"[auto_control] 目标{target}% → F1设置失败")
            except Exception as e:
                # v4: 兜底, auto_control 也加保护
                logger.error(f"auto_control 异常(已吞掉, 3秒后继续): {e}", exc_info=True)
                await asyncio.sleep(3)
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
                td = await collect_temps_cached()
                cpu, ssd, hdd = _extract_temp_sensors(td)
                # NTC 从缓存读 (不占串口, ping 每 3.5s 就在刷新缓存)
                # ★ v8 修复: get_ntc_cached() 返回字段是 "value", 之前写 "temperature" → NTC 永远 None
                #     导致 JSONL 历史文件 ntc 列全 null, 前端环境温度曲线始终画不出来
                ntc = None
                if controller:
                    res = controller.get_ntc_cached()
                    if res.get("ok"):
                        ntc_val = res.get("value")     # ← 正确字段名: "value" (不是 "temperature")
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
        data = await collect_temps_cached()
        cpu, ssd, hdd = _extract_temp_sensors(data)
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
    """更新温控参数(启动温度/最高温度) - v7: 更新后立即下发一次风扇转速."""
    if not fan_control:
        return {"ok": False, "error": "未初始化"}
    fan_control.update_config(req.start, req.max)
    # ── v7优化: 配置变更后立即计算并下发, 不等3秒轮询周期 ──
    immediate_result = None
    try:
        if controller and controller.connected and temp_collector:
            # ★ 必须走缓存包装! auto_control 3s周期本来就在刷新, 直接复用 <=12s 内缓存
            #   不走 temp_collector.collect_all() (真采要 3~8s, 就是之前用户说的5秒延迟根源)
            td = await collect_temps_cached()
            cpu, ssd, hdd = _extract_temp_sensors(td)
            target = fan_control.calc_target_speed(cpu, ssd, hdd)
            r = await controller.set_fan_speed(1, target)
            immediate_result = {"target_speed": target, "send_result": r}
            logger.info(f"[温控配置更新] 立即下发: CPU={cpu} SSD={ssd} HDD={hdd} → F1={target}% 结果={r.get('ok')}")
    except Exception as e:
        logger.warning(f"[温控配置更新] 立即下发失败(不影响配置保存): {e}")
    resp = {"ok": True, "config": fan_control.config}
    if immediate_result:
        resp["immediate"] = immediate_result
    return resp

@app.post("/api/control/reset")
async def reset_control():
    """恢复默认温控参数 - v7: 更新后立即下发一次风扇转速."""
    if not fan_control:
        return {"ok": False, "error": "未初始化"}
    fan_control.reset_config()
    # ── v7优化: 配置变更后立即计算并下发, 不等3秒轮询周期 ──
    immediate_result = None
    try:
        if controller and controller.connected and temp_collector:
            # ★ 同上: 走缓存包装, 避免真采 3~8s 延迟
            td = await collect_temps_cached()
            cpu, ssd, hdd = _extract_temp_sensors(td)
            target = fan_control.calc_target_speed(cpu, ssd, hdd)
            r = await controller.set_fan_speed(1, target)
            immediate_result = {"target_speed": target, "send_result": r}
            logger.info(f"[温控配置重置] 立即下发: CPU={cpu} SSD={ssd} HDD={hdd} → F1={target}% 结果={r.get('ok')}")
    except Exception as e:
        logger.warning(f"[温控配置重置] 立即下发失败(不影响配置保存): {e}")
    resp = {"ok": True, "data": DEFAULT_CONFIG}
    if immediate_result:
        resp["immediate"] = immediate_result
    return resp

# ═══════════════ 控制回路 ═══════════════

@app.post("/api/control/run")
async def run_control():
    """
    执行一次完整温控:
      1. 采集 NAS 温度 (CPU/SSD/HDD)
      2. 按配置计算目标转速
      3. 设置 F1 和 F2 转速 (用 set_fan_speed, 从确认响应解析并更新缓存, 不再二次查询)
      4. 读取 NTC 环境温度 (从缓存读, ping 每 3.5s 就在刷新)
      5. 返回所有结果

    ★ v7修复: 不再 send_raw 组合命令后再 get_fan_speed 查询两次.
       set_fan_speed 发送 F1CPD=xx 后, 控制器回复 F1CPD=xx%, 直接解析这个确认值
       并写入 speed_cache. 省掉 2 条 F1CPD? / F2CPD? 查询命令, 减少 2~4 秒延迟.
    """
    if not fan_control:
        return {"ok": False, "error": "未初始化"}
    if not controller or not controller.connected:
        return {"ok": False, "error": "控制器未连接"}

    # v7 诊断: 入口就打毫秒级时间戳日志 (精确测量 "点按钮 → TX发出 → RX确认" 全链路耗时)
    _t_run_start = time.time()
    _stamp = lambda: datetime.fromtimestamp(time.time()).strftime("%H:%M:%S.") + f"{int((time.time()%1)*1000):03d}"
    logger.info(f"[run_control] 入口 @ {_stamp()} (按钮点击/HTTP请求到达)")

    # 1. 采集 NAS 温度 → 统一公共函数提取 CPU/SSD/HDD
    temps_data = await collect_temps_cached()
    cpu, ssd, hdd = _extract_temp_sensors(temps_data)
    td = {"cpu": cpu, "ssd": ssd, "hdd": hdd}
    logger.info(f"[run_control] 温度采集完成 @ {_stamp()} → CPU={cpu} SSD={ssd} HDD={hdd} (距入口{round((time.time()-_t_run_start)*1000)}ms)")

    # 2. 计算目标转速
    target = fan_control.calc_target_speed(td["cpu"], td["ssd"], td["hdd"])

    # 3. 设置风扇 - 只控制 F1 (系统只有一个风扇, F2 不存在, 不要下发避免浪费时间)
    #    set_fan_speed 内部会从 F1CPD=xx 的确认回复解析, 直接更新 speed_cache
    #    不需要额外发送 F1CPD? 查询命令 (这是之前版本的 BUG, 已修复)
    t0 = time.time()
    r1 = await controller.set_fan_speed(1, target)
    t_cost = round(time.time() - t0, 2)
    logger.info(f"[run_control] 温度CPU={cpu} SSD={ssd} HDD={hdd} → 目标{target}% "
                f"→ F1结果={r1.get('ok')} 值={r1.get('value')}% 串口耗时{t_cost}s")
    logger.info(f"[run_control] 全部完成 @ {_stamp()} → 距入口{round((time.time()-_t_run_start)*1000)}ms (包含温度采集+串口下发+确认解析+NTC读取)")

    # 4. 读取 NTC (从缓存取最快, 如果缓存为空再尝试发一次 get_ntc_temp)
    ntc_cached = controller.get_ntc_cached()
    if ntc_cached.get("ok"):
        ntc_val = ntc_cached.get("value")
    else:
        r_ntc = await controller.get_ntc_temp()
        ntc_val = r_ntc.get("value") if r_ntc.get("ok") else None

    return {
        "ok": True,
        "temps": {"cpu": td["cpu"], "ssd": td["ssd"], "hdd": td["hdd"]},
        "target_speed": target,
        "fan1": r1.get("value") if r1.get("ok") else None,
        "fan2": None,  # 系统只有 F1, F2 不存在
        "ntc": ntc_val,
        "cost_s": t_cost,
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
    # v8: 把死锁/REBOOT 诊断计数也暴露给前端, 方便排障 (断连记录卡片旁可扩展显示)
    extra = {}
    if c is not None:
        try:
            extra = {
                "reboot_count": getattr(c, "_reboot_issued_count", 0),
                "usb_reset_count": getattr(c, "_usb_reset_ok_count", 0),
                "reconnect_attempts": getattr(c, "_reconnect_total_attempts", 0),
                "ping_fail_streak": getattr(c, "_consecutive_ping_fails_since_ok", 0),
            }
        except Exception:
            pass
    return {"ok": True, "data": {
        "service": "STC 智能风扇控制器",
        "version": "2.1.0-v10",     # v10: 无物理设备时不触发REBOOT(避免1430次空转) + autosuspend禁用
        "uptime": round(time.time() - start_time, 1),
        "controller_connected": c.connected if c else False,
        "controller_port": config["serial"]["port"],
        "auto_cmd_enabled": auto_cmd_enabled,
        **extra,
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
