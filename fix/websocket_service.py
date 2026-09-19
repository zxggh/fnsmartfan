"""
WebSocket 服务 — 实时状态推送 + 通信日志 + 命令下发

功能:
  - 管理所有 WebSocket 连接 (带 JWT 鉴权)
  - 每 2 秒推送一次 status 消息 (温度/风扇/连接状态/磁盘明细)
  - 实时推送串口通信日志 (rx/tx)
  - 接收客户端命令并转发到串口控制器

消息格式:
  服务端 → 客户端:
    status: { type:"status", data:{ cpu_temp, ssd_temp, hdd_temp, ambient_temp,
                                      fan_speed, fan_mode, connected, disk_details } }
    log:    { type:"log", direction:"rx"|"tx", content:"...", timestamp:1234567890 }
  客户端 → 服务端:
    command:{ type:"command", content:"F1_CUR_DUTY=30" }
"""

import json
import time
import asyncio
import logging
from typing import Set, Callable, Optional, Dict, Any

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger("fan_ctrl.ws")


class WebSocketManager:
    """WebSocket 连接管理器."""

    def __init__(self):
        self._connections: Set[WebSocket] = set()
        # 状态数据获取回调 (由 main.py 注入)
        self._status_getter: Optional[Callable[[], Dict[str, Any]]] = None
        # 命令处理回调 (由 main.py 注入)
        self._command_handler: Optional[Callable[[str], str]] = None
        # 最近一次状态推送时间
        self._last_push = 0.0

    def set_status_getter(self, getter: Callable[[], Dict[str, Any]]):
        """注入状态数据获取函数."""
        self._status_getter = getter

    def set_command_handler(self, handler: Callable[[str], str]):
        """注入命令处理函数 (返回响应字符串)."""
        self._command_handler = handler

    async def connect(self, websocket: WebSocket):
        """接受新连接."""
        await websocket.accept()
        self._connections.add(websocket)
        logger.info(f"WebSocket 已连接, 当前连接数: {len(self._connections)}")

    def disconnect(self, websocket: WebSocket):
        """移除连接."""
        self._connections.discard(websocket)
        logger.info(f"WebSocket 已断开, 当前连接数: {len(self._connections)}")

    async def broadcast(self, message: dict):
        """向所有连接广播消息."""
        if not self._connections:
            return
        text = json.dumps(message, ensure_ascii=False, default=str)
        dead = []
        for ws in list(self._connections):
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def push_log(self, direction: str, content: str):
        """推送一条通信日志."""
        msg = {
            "type": "log",
            "direction": direction,
            "content": content,
            "timestamp": int(time.time() * 1000),
        }
        await self.broadcast(msg)

    async def push_status(self):
        """推送当前状态 (由定时任务调用, 每 2 秒一次)."""
        if not self._connections:
            return
        if not self._status_getter:
            return
        try:
            data = self._status_getter()
            await self.broadcast({"type": "status", "data": data})
        except Exception as e:
            logger.warning(f"推送状态失败: {e}")

    async def handle_client_message(self, websocket: WebSocket, message: str):
        """处理客户端发来的消息."""
        try:
            data = json.loads(message)
            msg_type = data.get("type")
            if msg_type == "command":
                content = data.get("content", "")
                if not content:
                    return
                # 转发命令到串口控制器
                if self._command_handler:
                    response = self._command_handler(content)
                    # 命令响应也作为日志推送给所有客户端
                    await self.push_log("tx", content)
                    if response:
                        await self.push_log("rx", response)
        except json.JSONDecodeError:
            logger.warning(f"客户端发送非法 JSON: {message[:100]}")
        except Exception as e:
            logger.warning(f"处理客户端消息失败: {e}")

    async def status_push_loop(self):
        """定时推送状态的协程 (每 2 秒)."""
        while True:
            try:
                await self.push_status()
            except Exception as e:
                logger.error(f"status push loop 异常: {e}")
            await asyncio.sleep(2)


# 全局单例
ws_manager: Optional[WebSocketManager] = None


def init_ws_manager() -> WebSocketManager:
    """初始化全局 WebSocket 管理器."""
    global ws_manager
    ws_manager = WebSocketManager()
    return ws_manager
