"""
SmartFan Nexus — FastAPI 主入口 (v3.0 重构版)

核心功能:
  - JWT 认证 (auth.py): 登录/改密/登出, 所有 REST API 需鉴权
  - WebSocket 实时推送 (websocket_service.py): 每 2 秒状态 + 实时日志
  - 动态硬盘温度采集 (temp_collector.py): lsblk 识别 + smartctl timeout=5
  - 温控逻辑 (config.py): 最高温线性映射, 自动/手动模式
  - 串口通信 (serial_ctrl.py): STCController, 自动重连 + USB 死锁恢复
  - 温度历史 (temp_history.py): JSONL 持久化, 30 天保留
  - 通信日志: 每天一个文件, 7 天滚动

API 端点:
  POST  /api/login              登录 (返回 JWT)
  POST  /api/change-password    修改密码 (旧 JWT 失效)
  POST  /api/logout             登出
  GET   /api/temperature/history?range=1h|24h|3d  温度历史
  POST  /api/temperature/config  提交温控配置
  GET   /api/config             查询当前配置
  POST  /api/command            发送单条串口命令
  GET   /api/device/status      设备连接状态
  GET   /api/info               服务信息
  WS    /ws?token=<jwt>         WebSocket 实时推送
"""

import os
import sys
import json
import time
import logging
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from serial_ctrl import STCController
from temp_collector import TempCollector
from config import ConfigManager
import auth as auth_module
import websocket_service as ws_module
from temp_history import TempHistory, parse_range, RANGE_MAP, fmt_timestamp
from disconnect_log import DisconnectLogger

# ── 日志配置 ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("fan_ctrl")

# ── 全局实例 ──
controller: Optional[STCController] = None
temp_collector: Optional[TempCollector] = None
config_mgr: Optional[ConfigManager] = None
temp_history: Optional[TempHistory] = None
disc_log: Optional[DisconnectLogger] = None
auth_manager = None   # 由 init_auth() 返回值赋值
ws_manager = None     # 由 init_ws_manager() 返回值赋值
start_time = time.time()

# ── 温度缓存 (12s 窗口, 避免重复扫 SMART) ──
_TEMPS_CACHE_DATA: Optional[dict] = None
_TEMPS_CACHE_TIME: float = 0.0
_TEMPS_CACHE_MAX_AGE: float = 12.0


async def collect_temps_cached(max_age_s: float = _TEMPS_CACHE_MAX_AGE) -> dict:
    """温度采集带缓存包装."""
    global _TEMPS_CACHE_DATA, _TEMPS_CACHE_TIME
    now = time.time()
    age = (now - _TEMPS_CACHE_TIME) if _TEMPS_CACHE_DATA is not None else None
    if _TEMPS_CACHE_DATA is not None and age is not None and age <= max_age_s:
        return _TEMPS_CACHE_DATA
    if temp_collector is None:
        return {}
    _TEMPS_CACHE_TIME = time.time()
    data = await temp_collector.collect_all()
    _TEMPS_CACHE_DATA = data
    return data


# ── 通信日志 (每天一个文件, 7 天滚动) ──
COMM_LOG_DIR = Path("/data/comm_logs")

def log_comm(direction: str, content: str):
    """记录通信日志到文件 + 推送给 WebSocket 客户端."""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        log_file = COMM_LOG_DIR / f"{today}.log"
        COMM_LOG_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] [{direction}] {content}\n"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line)
        # 清理 7 天前的日志
        cutoff = time.time() - 7 * 86400
        for old in COMM_LOG_DIR.glob("*.log"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except Exception:
                pass
    except Exception as e:
        logger.debug(f"通信日志写入失败: {e}")
    # 推送给 WebSocket 客户端
    if ws_manager:
        asyncio.create_task(ws_manager.push_log(direction, content))


# ── Pydantic 模型 ──
class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    oldPassword: str
    newPassword: str


class CommandRequest(BaseModel):
    content: str


class TempConfigRequest(BaseModel):
    start_temp: Optional[int] = None
    max_temp: Optional[int] = None
    fan_mode: Optional[str] = None
    manual_duty: Optional[int] = None


# ── JWT 鉴权依赖 ──
def get_current_user(request: Request) -> str:
    """从请求头提取并校验 JWT. 失败抛 401."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未提供认证令牌")
    token = auth_header[7:]
    if auth_manager is None:
        raise HTTPException(status_code=500, detail="认证模块未初始化")
    username = auth_manager.verify_token(token)
    if not username:
        raise HTTPException(status_code=401, detail="令牌无效或已过期")
    return username


# ── FastAPI 应用 ──
from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    global controller, temp_collector, config_mgr, temp_history, disc_log, auth_manager, ws_manager

    # 初始化认证 (返回值赋给全局, 避免 from import 陷阱)
    auth_manager = auth_module.init_auth()
    # 初始化 WebSocket 管理器
    ws_manager = ws_module.init_ws_manager()

    # 初始化温度历史 (30 天保留)
    try:
        temp_history = TempHistory()
    except Exception as e:
        temp_history = None
        logger.error(f"温度历史初始化失败: {e}")

    # 初始化串口控制器 + 温度采集 + 配置
    controller = STCController(port="/dev/ttyACM0", baudrate=115200, timeout=2.0)
    temp_collector = TempCollector()
    config_mgr = ConfigManager()
    disc_log = DisconnectLogger()

    # 注入 WebSocket 回调
    if ws_manager:
        ws_manager.set_status_getter(get_status_data)
        ws_manager.set_command_handler(handle_command_sync)

    # 尝试连接控制器
    try:
        await controller.connect()
        ping_ok = await controller.ping()
        logger.info(f"控制器已连接, 心跳={'正常' if ping_ok else '无响应'}")
        if not ping_ok:
            disc_log.start()
    except Exception as e:
        logger.warning(f"控制器连接失败: {e}")
        disc_log.start()

    # 后台任务: 心跳保活
    async def heartbeat():
        fail_count = 0
        while True:
            try:
                await asyncio.sleep(1.5)
                if not controller:
                    continue
                if await controller.ping():
                    fail_count = 0
                    if disc_log and disc_log.is_disconnected():
                        disc_log.end()
                else:
                    fail_count += 1
                    if fail_count >= 2:
                        logger.warning("连续2次心跳失败, 尝试重连")
                        if disc_log:
                            disc_log.start()
                        ok = await controller.ensure_connected()
                        if ok and disc_log:
                            disc_log.end()
                        fail_count = 0
            except Exception as e:
                logger.error(f"heartbeat 异常: {e}")
                await asyncio.sleep(5)

    task_hb = asyncio.create_task(heartbeat())

    # 后台任务: 温度采集 (每 10 秒, 不受风扇模式影响, 保证 UI 有数据)
    async def temp_collector_loop():
        while True:
            try:
                await collect_temps_cached()
            except Exception as e:
                logger.error(f"temp_collector_loop 异常: {e}")
            await asyncio.sleep(10)

    task_tc = asyncio.create_task(temp_collector_loop())

    # 后台任务: 串口日志推送 (每 1 秒, 把 NTC 轮询等日志推到前端)
    async def log_drain_loop():
        while True:
            try:
                if controller and ws_manager:
                    for entry in controller.drain_logs():
                        await ws_manager.push_log(entry["dir"], entry["data"])
            except Exception as e:
                logger.error(f"log_drain_loop 异常: {e}")
            await asyncio.sleep(1)

    task_ld = asyncio.create_task(log_drain_loop())

    # 后台任务: 自动控温 (每 3 秒)
    async def auto_control():
        last_target = None
        while True:
            try:
                await asyncio.sleep(3)
                if not controller or not config_mgr:
                    continue
                if not controller.connected:
                    continue
                cfg = config_mgr.config
                if cfg.get("fan_mode") != "auto":
                    continue
                td = await collect_temps_cached()
                # 取所有温度中的最高温 (CPU + SSD最高 + HDD最高)
                temps = [t for t in (td.get("cpu"), td.get("max_ssd_temp"), td.get("max_hdd_temp")) if t is not None]
                if not temps:
                    continue
                hottest = max(temps)
                target = config_mgr.calc_target_speed(hottest)
                if target != last_target:
                    r = await controller.set_fan_speed(1, target)
                    if r.get("ok"):
                        last_target = target
                        log_comm("tx", f"F1_CUR_DUTY={target}")
                        log_comm("rx", f"F1_CUR_DUTY={r.get('value')}%")
            except Exception as e:
                logger.error(f"auto_control 异常: {e}")
                await asyncio.sleep(3)

    task_ac = asyncio.create_task(auto_control())

    # 后台任务: 温度历史采样 (每 60 秒)
    async def history_collector():
        while True:
            try:
                await asyncio.sleep(60)
                if not temp_history:
                    continue
                td = await collect_temps_cached()
                cpu = td.get("cpu")
                ssd = td.get("max_ssd_temp")
                hdd = td.get("max_hdd_temp")
                ntc = None
                if controller:
                    res = controller.get_ntc_cached()
                    if res.get("ok"):
                        ntc = res.get("value")
                temp_history.add_record(cpu=cpu, ssd=ssd, hdd=hdd, ntc=ntc)
            except Exception as e:
                logger.error(f"history_collector 异常: {e}")

    task_hist = asyncio.create_task(history_collector())

    # 后台任务: WebSocket 状态推送 (每 2 秒)
    task_ws = asyncio.create_task(ws_manager.status_push_loop()) if ws_manager else None

    yield

    # 清理
    for t in (task_hb, task_tc, task_ld, task_ac, task_hist):
        t.cancel()
    if task_ws:
        task_ws.cancel()
    if controller:
        await controller.disconnect()


app = FastAPI(title="SmartFan Nexus", version="3.1.0", lifespan=lifespan)

# ── 静态文件 ──
STATIC_DIR = Path(__file__).parent / "static"


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


if STATIC_DIR.exists():
    app.mount("/static", NoCacheStaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def root():
    idx = STATIC_DIR / "index.html"
    if idx.exists():
        html = idx.read_text(encoding="utf-8")
        return HTMLResponse(content=html, headers={"Cache-Control": "no-cache"})
    return {"service": "SmartFan Nexus", "version": "3.1.0"}


# ============================================================
#  认证相关 API
# ============================================================
@app.post("/api/login")
async def login(req: LoginRequest):
    """登录. 成功返回 JWT (7 天有效期)."""
    if auth_manager is None:
        raise HTTPException(500, "认证模块未初始化")
    token = auth_manager.login(req.username, req.password)
    if not token:
        raise HTTPException(401, "用户名或密码错误 (或账号已锁定)")
    return {"token": token, "expiresIn": 7 * 24 * 3600}


@app.post("/api/change-password")
async def change_password(req: ChangePasswordRequest, request: Request):
    """修改密码. 成功后旧 JWT 立即失效."""
    username = get_current_user(request)
    if auth_manager is None:
        raise HTTPException(500, "认证模块未初始化")
    ok = auth_manager.change_password(username, req.oldPassword, req.newPassword)
    if not ok:
        raise HTTPException(400, "原密码错误")
    return {"ok": True, "message": "密码已修改, 请重新登录"}


@app.post("/api/logout")
async def logout(request: Request):
    """登出 (前端清 token 即可)."""
    get_current_user(request)
    return {"ok": True}


# ============================================================
#  温度与配置 API
# ============================================================
@app.get("/api/temperature/history")
async def get_temperature_history(request: Request, range: str = Query("24h")):
    """查询温度历史曲线数据."""
    get_current_user(request)
    if not temp_history:
        return {"ok": False, "error": "未初始化"}
    seconds = parse_range(range)
    records = temp_history.query_range_seconds(seconds)
    points = []
    for r in records:
        points.append({
            "t": r["t"],
            "label": fmt_timestamp(r["t"], seconds),
            "cpu": r.get("cpu"),
            "ssd": r.get("ssd"),
            "hdd": r.get("hdd"),
            "ntc": r.get("ntc"),
        })
    return {
        "ok": True,
        "range": range,
        "points": points,
    }


@app.post("/api/temperature/config")
async def update_temp_config(req: TempConfigRequest, request: Request):
    """提交温控配置."""
    get_current_user(request)
    if not config_mgr:
        raise HTTPException(503, "未初始化")
    kwargs = {k: v for k, v in req.model_dump().items() if v is not None}
    new_config = config_mgr.update(**kwargs)
    confirmed_speed = None

    # 情况1: 切到手动模式 → 立即下发手动占空比
    # 情况2: 已是手动模式且更新了 manual_duty → 立即下发
    current_mode = new_config.get("fan_mode", "auto")
    if current_mode == "manual" and controller and controller.connected:
        duty = new_config.get("manual_duty", 50)
        r = await controller.set_fan_speed(1, duty)
        log_comm("tx", f"F1_CUR_DUTY={duty}")
        if r.get("ok"):
            confirmed_speed = r.get("value")
            log_comm("rx", f"F1_CUR_DUTY={confirmed_speed}%")

    # 情况3: 切到自动模式 → 立即根据当前温度计算并下发 (不用等3秒轮询)
    elif kwargs.get("fan_mode") == "auto" and controller and controller.connected:
        td = await collect_temps_cached()
        temps = [t for t in (td.get("cpu"), td.get("max_ssd_temp"), td.get("max_hdd_temp")) if t is not None]
        if temps:
            target = config_mgr.calc_target_speed(max(temps))
            r = await controller.set_fan_speed(1, target)
            if r.get("ok"):
                confirmed_speed = r.get("value")
                log_comm("tx", f"F1_CUR_DUTY={target}")
                log_comm("rx", f"F1_CUR_DUTY={confirmed_speed}%")

    result = {"ok": True, "config": new_config}
    if confirmed_speed is not None:
        result["fan_speed"] = confirmed_speed
    return result


@app.get("/api/config")
async def get_config(request: Request):
    """查询当前配置."""
    get_current_user(request)
    if not config_mgr:
        raise HTTPException(503, "未初始化")
    return {"ok": True, "config": config_mgr.config}


# ============================================================
#  设备与命令 API
# ============================================================
@app.post("/api/command")
async def send_command(req: CommandRequest, request: Request):
    """发送单条串口命令."""
    get_current_user(request)
    if not controller or not controller.connected:
        raise HTTPException(503, "控制器未连接")
    resp = await controller.send_raw(req.content)
    log_comm("tx", req.content)
    if resp:
        log_comm("rx", resp)
    return {"ok": True, "command": req.content, "response": resp}


@app.get("/api/device/status")
async def device_status(request: Request):
    """设备连接状态."""
    get_current_user(request)
    if not controller:
        return {"ok": True, "connected": False}
    return {"ok": True, "connected": controller.connected}


@app.get("/api/disconnect-log")
async def get_disconnect_log(request: Request, limit: int = 50):
    """查询控制器断连记录."""
    get_current_user(request)
    if not disc_log:
        return {"ok": False, "error": "未初始化"}
    entries = disc_log.get_entries(limit=limit)
    cur_disc = disc_log.is_disconnected() or (controller is not None and not controller.connected)
    return {"ok": True, "count": len(entries), "entries": entries, "currently_disconnected": cur_disc}


@app.get("/api/info")
async def get_info():
    """服务信息 (健康检查用, 不需要鉴权)."""
    return {
        "ok": True,
        "data": {
            "service": "SmartFan Nexus",
            "version": "3.1.0",
            "uptime": round(time.time() - start_time, 1),
            "controller_connected": controller.connected if controller else False,
        }
    }


# ============================================================
#  WebSocket
# ============================================================
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = Query(...)):
    """WebSocket 实时推送. 连接时携带 ?token=<jwt> 鉴权."""
    # 鉴权
    if auth_manager is None or not auth_manager.verify_token(token):
        await websocket.close(code=4401, reason="未授权")
        return
    await ws_manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            await ws_manager.handle_client_message(websocket, data)
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception as e:
        logger.warning(f"WebSocket 异常: {e}")
        ws_manager.disconnect(websocket)


# ============================================================
#  状态数据获取 (供 WebSocket 推送)
# ============================================================
def get_status_data() -> dict:
    """获取当前状态数据 (WebSocket 每 2 秒调用)."""
    td = _TEMPS_CACHE_DATA or {}
    cpu = td.get("cpu")
    ssd = td.get("max_ssd_temp")
    hdd = td.get("max_hdd_temp")
    # 环境温度 (NTC 缓存)
    ambient = None
    if controller:
        res = controller.get_ntc_cached()
        if res.get("ok"):
            ambient = res.get("value")
    # 风扇转速 (缓存)
    fan_speed = 0
    if controller:
        fan_speed = controller.speed_cache.get(1, 0)
    # 风扇模式
    fan_mode = "auto"
    if config_mgr:
        fan_mode = config_mgr.config.get("fan_mode", "auto")
    return {
        "cpu_temp": cpu,
        "ssd_temp": ssd,
        "hdd_temp": hdd,
        "ambient_temp": ambient,
        "fan_speed": fan_speed,
        "fan_mode": fan_mode,
        "connected": controller.connected if controller else False,
        "disk_details": td.get("disk_details", []),
    }


def handle_command_sync(content: str) -> str:
    """同步处理命令 (供 WebSocket 命令回调).

    注意: 串口操作是 async, 这里用 run_coroutine_threadsafe 不太合适,
    实际命令通过 /api/command REST 接口下发更可靠.
    WebSocket 命令通道作为辅助, 返回空串由前端走 REST.
    """
    # 命令通过 REST /api/command 下发 (带 JWT), WebSocket 命令通道暂作日志回显
    return ""


# ── 入口 ──
def main():
    logger.info("启动 SmartFan Nexus v3.0.0")
    uvicorn.run("main:app", host="0.0.0.0", port=8780, reload=False, log_level="info")


if __name__ == "__main__":
    main()
