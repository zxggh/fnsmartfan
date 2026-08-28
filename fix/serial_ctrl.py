"""
STC 风扇控制器串口通信 — v5(诊断增强版)

允许命令: F1CPD?, F1CPD=XX, GETNTC?, LED=ON, LED=OFF

v5 在 v4 基础上加诊断日志 + 修复 NTC 解析:

[原 v4 修复]
  - ping() 同时解析 NTC 响应并更新 ntc_cache, 删除心跳里的二次 GETNTC? 调用
    -> 心跳节奏从 5.1s 降到 3.5s, 远离 STC 5s 复位窗口
  - ping() 单次失败不再立即置 _connected=False, 连续 2 次失败才判定断开
  - ensure_connected() 永不放弃: 前 60 秒高频(2s/次), 之后低频(30s/次)
  - _send_raw() except 改 Exception 兜底

[v5 新增]
  - NTC 正则修复: 用 (?!\d) 负向断言, 排除 "NTC1:" / "NTC2:" 这种带编号的响应
    之前的 r"NTC\s*[:：]?\s*([+-]?\d+\.?\d*)" 会把 "NTC1: 26.8" 中的 "1" 当温度
  - NTC 解析失败时记录原始响应到 logger(docker logs 可见), 便于调试"温度明显不对"
  - ensure_connected 进入前 dump 现场到 logger: 设备节点列表 / writer 状态 /
    上次成功响应时间, 让无规律断连时 docker logs 直接能看出问题
  - connect 失败时记录异常类型 + 信息(区分 OSError / RuntimeError / SerialException)
  - ping 失败时明确区分 "writer 已断" vs "ping 无响应" vs "ping 异常"
  - heartbeat 改为包 try/except Exception 兜底(在 main.py)
"""

import re
import asyncio
import logging
import os
import glob
import time

logger = logging.getLogger("fan_ctrl.serial")

# NTC 合理范围(环境温度, 收紧到 -20~80°C, 异常值直接丢弃)
NTC_MIN, NTC_MAX = -20.0, 80.0

# NTC 响应解析正则(v5 修复):
# - (?!\d) 负向断言: NTC 后面不能紧跟数字, 排除 "NTC1:" / "NTC2:" 这种带编号的字段
# - 支持中英文冒号或空格分隔
# - 匹配示例: "NTC:26.8" / "NTC: 26.8°C" / "NTC 26.8" / "当前NTC：26.8"
NTC_RE = re.compile(r"NTC(?!\d)\s*[:：]?\s*([+-]?\d+\.?\d*)")

# ── v7 新增: 命令"响应完整性"判断 + 命令分类
#   发送命令后, 控制器通常在 50~150ms 内回复确认 (单行 \r\n 结尾),
#   旧代码每次都死等 wait 秒 (如 1.0s) 才解包返回, 白白浪费 ~0.9s × 每条命令,
#   这就是进度条更新迟滞的又一层隐藏原因.
#   这里用正则 + 命令类型判断: 只要内容已包含"典型结束标记", 就立刻返回.
# 典型确认回复:
#   F1CPD=30  → 回复: F1_CPD=30%
#   PING     → 回复: OK  或  PONG  或  NTC:26.5°C
#   GETNTC?  → 回复: NTC: 26.5°C   或  NOCONNECT
#   F1CPD?   → 回复: 当前转速 : 30%  或  F1_CPD=30%
_RESP_END_OK_FAIL = re.compile(r"(^|\r?\n|\s)(OK|FAIL|NOCONNECT|PONG)(\r?\n|$)", re.I)
_RESP_FAN_CONFIRM = re.compile(r"F\d_CPD\s*=\s*\d+%?|当前转速\s*[:：]\s*\d+%", re.I)
_RESP_NTC = re.compile(r"NTC\s*[:：]?\s*[+-]?\d+\.?\d*|NOCONNECT", re.I)
# 哪些命令支持 "提前返回" (ping / set / query 这种 "一问一答短响应" 的)
_EARLY_CMD_RE = re.compile(
    r"^(PING|F\dCPD[=?]|LED|GETNTC\?|VERSION\?|REBOOT)", re.I
)


def _is_cmd_resp_complete(cmd: str) -> bool:
    """判断此命令是否属于"短响应"类型, 可以 early return."""
    if not cmd:
        return False
    return bool(_EARLY_CMD_RE.match(cmd.strip()))


def _resp_text_is_complete(raw_bytes: bytes, cmd: str) -> bool:
    """检查已经收到的字节是否构成 "完整回复".

    判断规则:
      1. 结尾包含 \r\n (控制器完整行结尾)
         且内容匹配 OK/FAIL/NOCONNECT/PONG / F*_CPD=xx% / 当前转速 / NTC:xx° 任一种;
      2. 或者 字节长度 > 8 且包含 % 和 \n (F*CPD=xx% 确认最常见的情况)
    """
    if not raw_bytes:
        return False
    try:
        text = raw_bytes.decode("gbk", errors="ignore")
    except Exception:
        text = raw_bytes.decode("utf-8", errors="ignore")
    if not text:
        return False
    # 结尾完整行
    ends_with_crlf = text.endswith("\n") or text.endswith("\r")
    # 模式匹配
    has_terminal = bool(_RESP_END_OK_FAIL.search(text)
                        or _RESP_FAN_CONFIRM.search(text)
                        or _RESP_NTC.search(text))
    if ends_with_crlf and has_terminal:
        return True
    # 兜底: 典型的 set_fan_speed 确认格式 (F1_CPD=30%)
    if _RESP_FAN_CONFIRM.search(text):
        return True
    return False


class STCController:
    def __init__(self, port="/dev/ttyACM0", baudrate=115200, timeout=2.0):
        self._port = port
        self._baudrate = baudrate
        self._timeout = timeout
        self._reader = None
        self._writer = None
        self._lock = asyncio.Lock()
        self._connected = False
        # 连续失败计数(达到阈值才判定断开, 避免抖动误判)
        self._ping_fail_streak = 0
        # 转速缓存(仅控制器确认后的实际值)
        self.speed_cache = {1: 0, 2: 0}
        # 环境温度缓存(由心跳 ping 顺带更新, API 直接读缓存, 避免串口竞争)
        self.ntc_cache = None
        self.ntc_cache_time = 0.0
        # 上次成功 ping 的时间戳(诊断用)
        self._last_ping_ok_time = 0.0
        # 日志
        self.log = []

    def _log(self, d, data):
        self.log.append({"t": time.time(), "dir": d, "data": data[:200]})
        if len(self.log) > 50:
            self.log.pop(0)

    # ── 诊断辅助: dump 当前现场状态到 logger(docker logs 可见) ──
    def _dump_state(self, tag):
        """断连/重连时把现场状态输出到 logger, 便于无规律问题诊断."""
        try:
            acm_ports = sorted(glob.glob("/dev/ttyACM*"))
            usb_ports = sorted(glob.glob("/dev/ttyUSB*"))
            host_acm = sorted(glob.glob("/dev-host-dev/ttyACM*")) if os.path.isdir("/dev-host-dev") else []
            now = time.time()
            last_ok_age = "从未成功" if self._last_ping_ok_time == 0 else f"{now - self._last_ping_ok_time:.0f}s前"
            ntc_age = "无缓存" if self.ntc_cache_time == 0 else f"{now - self.ntc_cache_time:.0f}s前"
            logger.warning(
                f"[诊断-{tag}] port={self._port} | writer={'有' if self._writer else '无'} | "
                f"reader={'有' if self._reader else '无'} | connected={self._connected} | "
                f"fail_streak={self._ping_fail_streak} | 上次ping成功={last_ok_age} | "
                f"NTC缓存={self.ntc_cache}({ntc_age}) | "
                f"ttyACM*={acm_ports or '空'} | ttyUSB*={usb_ports or '空'} | "
                f"/dev-host-dev/ttyACM*={host_acm or '空'}"
            )
        except Exception as e:
            logger.error(f"[诊断-{tag}] dump_state 自身异常: {e}")

    # ── 连接管理 ──
    async def connect(self):
        import serial_asyncio
        self._reader, self._writer = await serial_asyncio.open_serial_connection(
            url=self._port, baudrate=self._baudrate, timeout=self._timeout)
        # 激活设备: 置 DTR/RTS (PC串口工具默认会置位, serial_asyncio 不会)
        try:
            ser = self._writer.transport._serial
            ser.dtr = True
            ser.rts = True
            self._log("SYS", f"connect({self._port}) DTR=ON")
        except Exception as e:
            self._log("SYS", f"connect({self._port}) DTR设置失败: {e}")
        self._connected = True
        self._ping_fail_streak = 0

    async def disconnect(self):
        self._connected = False
        r, w = self._reader, self._writer
        self._reader = None
        self._writer = None
        if w:
            try:
                w.close()
                await w.wait_closed()
            except Exception:
                pass
        self._log("SYS", "disconnect()")

    @property
    def connected(self):
        return self._connected

    async def ensure_connected(self):
        """断线重连 — 永不放弃 + 全程诊断日志.

        关键改进:
        - 进入前 _dump_state('重连开始'): 把设备节点/writer状态/上次ping时间打到 logger
        - 候选端口逐个测试, connect 失败时记录异常类型
        - ping 持续失败时明确区分 "writer已断" vs "ping无响应"
        - 永不返回 False: 前 60 秒高频(2s/次), 之后低频(30s/次)持续重试
        - 每轮结束 _dump_state, 让 docker logs 看到现场
        """
        if self._connected:
            try:
                if await self.ping():
                    return True
            except Exception as e:
                logger.warning(f"ensure_connected: 已连接态 ping 异常 {type(e).__name__}: {e}")
            self._connected = False

        self._dump_state("重连开始")
        await self.disconnect()
        await asyncio.sleep(1)

        # 高频阶段时长(秒): 前 60 秒每 2 秒一轮; 之后每 30 秒一轮
        high_freq_phase = 60
        start_time = time.time()
        attempt = 0

        while True:
            attempt += 1
            elapsed = time.time() - start_time

            # 候选端口: 配置端口优先, 再扫描所有 ttyACM* + ttyUSB*
            ports = [self._port]
            for c in sorted(glob.glob("/dev/ttyACM*")):
                if c not in ports:
                    ports.append(c)
            for c in sorted(glob.glob("/dev/ttyUSB*")):
                if c not in ports:
                    ports.append(c)

            tested = False
            for port in ports:
                if not os.path.exists(port):
                    continue
                tested = True
                self._port = port
                try:
                    await self.connect()  # DTR=ON 激活
                except Exception as e:
                    # v5: 记录 connect 失败的异常类型, 区分 OSError/RuntimeError/SerialException
                    logger.warning(f"ensure_connected: {port} 打开失败 {type(e).__name__}: {e}")
                    self._log("SYS", f"{port} 打开失败: {e}")
                    self._connected = False
                    continue
                # 保持连接, 多次 ping(间隔 1s, 满足固件 5s 窗口)
                for p in range(3):
                    await asyncio.sleep(1.0)
                    if await self.ping():
                        self._connected = True
                        self._last_ping_ok_time = time.time()
                        self._log("SYS", f"重连成功({port}) 尝试{attempt}次 耗时{elapsed:.0f}s")
                        logger.info(f"重连成功({port}) 尝试{attempt}次 耗时{elapsed:.0f}s")
                        return True
                # 该端口 ping 持续失败, 断开试下一个
                logger.warning(f"ensure_connected: {port} ping持续失败(尝试{attempt}, 已重试{elapsed:.0f}s)")
                self._log("SYS", f"{port} ping持续失败, 试下一个端口")
                await self.disconnect()

            if not tested:
                # 无可用端口时不刷屏, 每 10 次记一条
                if attempt % 10 == 1:
                    self._dump_state(f"无可用端口#{attempt}")
                    logger.info(f"重连中: 无可用端口, 尝试{attempt}次 耗时{elapsed:.0f}s")

            # 高频阶段 sleep 2s, 低频阶段 sleep 30s
            if elapsed < high_freq_phase:
                await asyncio.sleep(2)
            else:
                # 低频阶段每 30 秒一轮, 每 5 分钟打一条完整诊断避免静默
                if attempt % 10 == 1:
                    self._dump_state(f"持续重连#{attempt}")
                    logger.info(f"重连持续中: 尝试{attempt}次 耗时{elapsed:.0f}s")
                await asyncio.sleep(30)

    # ── 心跳(ping + 顺带取 NTC, 不再二次发 GETNTC?) ──
    async def ping(self):
        """心跳: 发 GETNTC? 一次, 既验证连接又顺带更新 NTC 缓存.

        - 成功(收到响应且能解析出 NTC 或响应长度合理): _ping_fail_streak=0, _connected=True
        - 失败(无响应/异常): _ping_fail_streak+=1, 连续 2 次才置 _connected=False
        """
        if not self._writer:
            self._ping_fail_streak += 1
            if self._ping_fail_streak >= 2:
                self._connected = False
            return False
        try:
            # wait=2.0 适度, retries=0(心跳本身不重发, 避免拖慢节奏)
            resp = await self._send_raw("GETNTC?", wait=2.0, retries=0)
            if resp and len(resp) > 3:
                # 顺带解析 NTC(失败也不影响 ping 成功判定)
                self._parse_ntc_from_resp(resp)
                self._ping_fail_streak = 0
                self._connected = True
                self._last_ping_ok_time = time.time()
                return True
            # 响应过短视为失败(可能是残留字节)
            self._ping_fail_streak += 1
            if self._ping_fail_streak >= 2:
                self._connected = False
            return False
        except Exception as e:
            # 兜底: 任何异常都视为 ping 失败, 不冒泡
            logger.debug(f"ping exception: {type(e).__name__}: {e}")
            self._ping_fail_streak += 1
            if self._ping_fail_streak >= 2:
                self._connected = False
            return False

    def _parse_ntc_from_resp(self, resp):
        """从 GETNTC? 响应解析 NTC 温度并更新缓存.

        v5 改进:
        - 用 NTC_RE(已带 (?!\d) 负向断言), 排除 NTC1/NTC2 字段
        - 解析失败/超出合理范围时记录原始响应到 logger, 便于排查"温度明显不对"
        """
        m = NTC_RE.search(resp)
        if not m:
            # 响应里压根没匹配到 NTC 字段, 记录原始响应(限长 100 字符)
            logger.warning(f"NTC解析失败(无NTC字段): 原始='{resp[:100]}'")
            return
        try:
            val = float(m.group(1))
        except ValueError:
            logger.warning(f"NTC解析失败(数值转换): 截获='{m.group(1)}' 原始='{resp[:100]}'")
            return
        if not (NTC_MIN <= val <= NTC_MAX):
            # 超出合理范围, 视为解析错误, 记录原始响应
            logger.warning(f"NTC异常值 {val}(超出{NTC_MIN}~{NTC_MAX}): 原始='{resp[:100]}'")
            self._log("SYS", f"NTC异常值 {val}, 原始: {resp[:80]}")
            return
        # 合理值, 更新缓存
        self.ntc_cache = val
        self.ntc_cache_time = time.time()

    # ── 发送 ──
    async def send(self, cmd, wait=2.0):
        """发送命令, 返回响应. 空响应重发一次.

        注意: 不在这里触发长重连(避免阻塞/死锁), 重连由 heartbeat 负责.

        v7 优化: 识别 set_fan_speed 类命令 (F<N>CPD=<value>), 控制器通常 100ms 内就回确认,
                此时 wait 会设 1.0s, 但实际上死等 full wait 秒才解包返回 → 白白浪费 0.9s.
                改为: 读到以 \r\n 结尾的单行"完整确认响应"就提前返回, 不再死等 wait 秒.
                (判断完整: 含 F*_CPD=xx% 或 OK 或 FAIL 或 NOCONNECT 或 NTC: xx° 这些典型结束标记)
        """
        if not self._connected:
            return ""
        raw = await self._send_raw(cmd, wait=wait, early_return=_is_cmd_resp_complete(cmd))
        if raw:
            return raw
        # 空响应 -> 重发一次(避免偶发丢失)
        await asyncio.sleep(0.2)
        raw = await self._send_raw(cmd, wait=wait, early_return=_is_cmd_resp_complete(cmd))
        return raw

    async def _send_raw(self, cmd, wait=2.0, retries=0, early_return=False):
        """底层发送(带锁). 发命令后最多等 wait 秒收集响应.

        v4 改进: except 改成 Exception 兜底, 任何异常都视为发送失败返回空串,
                 不让 RuntimeError / SerialException 等未预期异常冒泡到调用方.
        v7 改进: early_return=True 时, 收到一行完整响应就提前返回 (原每次死等 wait 秒).
        """
        async with self._lock:
            for attempt in range(retries + 1):
                if not self._writer:
                    return ""
                try:
                    # 清空输入缓冲(短超时)
                    try:
                        while True:
                            junk = await asyncio.wait_for(
                                self._reader.read(4096), timeout=0.05)
                            if not junk:
                                break
                    except asyncio.TimeoutError:
                        pass
                    except Exception:
                        # 清缓冲异常直接返回空, 不冒泡
                        return ""

                    frame = f"{cmd}\r\n".encode()
                    self._writer.write(frame)
                    await self._writer.drain()
                    self._log("TX", cmd)

                    # 读取响应, 最多等 wait 秒
                    resp = b""
                    deadline = time.time() + wait
                    while time.time() < deadline:
                        remaining = deadline - time.time()
                        if remaining <= 0:
                            break
                        try:
                            chunk = await asyncio.wait_for(
                                self._reader.read(2048), timeout=min(remaining, 0.05))
                        except asyncio.TimeoutError:
                            # v7: early_return 时, 每次短超时(50ms)都检查一下目前收到的内容是否够了
                            if early_return and resp and _resp_text_is_complete(resp, cmd):
                                break
                            continue
                        except Exception:
                            # 读取异常(如 fd 失效): 立即返回已读到的内容
                            break
                        if not chunk:
                            break
                        resp += chunk
                        if len(resp) > 4096:
                            break
                        # v7: early_return 下, 有新数据就判断是否 "完整", 完整就立刻跳出
                        if early_return and resp and _resp_text_is_complete(resp, cmd):
                            # 稍微再等 10ms 防止确认行被截断
                            try:
                                tail = await asyncio.wait_for(
                                    self._reader.read(512), timeout=0.01)
                                if tail:
                                    resp += tail
                            except Exception:
                                pass
                            break

                    if resp:
                        text = resp.decode("gbk", errors="replace").strip()
                        self._log("RX", text[:150])
                        return text
                except Exception as e:
                    # v4: Exception 兜底, 任何异常都视为发送失败
                    logger.debug(f"send fail(attempt {attempt+1}): {type(e).__name__}: {e}")
                    if attempt < retries:
                        await asyncio.sleep(0.3)
            return ""

    # ── 业务API ──

    async def get_ntc_temp(self):
        """环境温度(GETNTC?). 校验合理范围. 成功后更新缓存.

        注意: 心跳已通过 ping 顺带更新 ntc_cache, 这个方法主要给 API 直接调用.
        """
        resp = await self.send("GETNTC?", wait=2.0)
        m = NTC_RE.search(resp)
        if m:
            try:
                val = float(m.group(1))
            except ValueError:
                val = None
            if val is not None and NTC_MIN <= val <= NTC_MAX:
                self.ntc_cache = val
                self.ntc_cache_time = time.time()
                return {"ok": True, "value": val}
            # 超出合理范围, 视为解析错误
            logger.warning(f"get_ntc_temp 异常值 {val}: 原始='{resp[:100]}'")
            self._log("SYS", f"NTC异常值 {val}, 原始: {resp[:80]}")
            return {"ok": False, "error": f"NTC异常值({val})"}
        # 没匹配到 NTC, 记录原始响应
        logger.warning(f"get_ntc_temp 无NTC字段: 原始='{resp[:100]}'")
        return {"ok": False, "error": (resp or "无响应")[:60]}

    def get_ntc_cached(self):
        """返回缓存的最近环境温度(不访问串口)."""
        if self.ntc_cache is not None:
            return {"ok": True, "value": self.ntc_cache,
                    "age_s": round(time.time() - self.ntc_cache_time, 1)}
        return {"ok": False, "error": "暂无缓存"}

    async def get_fan_speed(self, fan=1):
        """查询实际转速(更新缓存)."""
        resp = await self.send(f"F{fan}CPD?", wait=2.0)
        val = None
        m = re.search(rf"F{fan}_CPD=(\d+)%?", resp)
        if m:
            val = int(m.group(1))
        else:
            m2 = re.search(r"当前转速\s*:\s*(\d+)%", resp)
            if m2:
                val = int(m2.group(1))
        if val is not None:
            self.speed_cache[fan] = val
            return {"ok": True, "value": val}
        return {"ok": False, "error": (resp or "无响应")[:60]}

    async def set_fan_speed(self, fan=1, speed=50):
        """设置转速 — 等控制器回复确认后才更新缓存.

        v7优化: 1秒无回复重发, 最多2次, 全部无反应则返回失败(不更新缓存).
        原参数(2秒/3次)累计耗时过长, 温控响应慢.
        """
        speed = max(0, min(100, speed))
        for attempt in range(2):
            resp = await self.send(f"F{fan}CPD={speed}", wait=1.0)
            val = None
            m = re.search(rf"F{fan}_CPD=(\d+)%?", resp)
            if m:
                val = int(m.group(1))
            else:
                m2 = re.search(r"当前转速\s*:\s*(\d+)%", resp)
                if m2:
                    val = int(m2.group(1))
            if val is not None:
                self.speed_cache[fan] = val
                self._log("SYS", f"F{fan}确认 {val}%")
                return {"ok": True, "value": val}
            if attempt < 1:
                self._log("SYS", f"F{fan}设置无回复, 重试{attempt+2}/2")
                await asyncio.sleep(0.3)
        self._log("SYS", f"F{fan}设置2次无响应, 忽略")
        return {"ok": False, "error": "2次无响应"}

    async def set_led(self, state: bool):
        val = "ON" if state else "OFF"
        resp = await self.send(f"LED={val}", wait=2.0)
        if "OK" in resp:
            return {"ok": True, "state": val}
        return {"ok": False, "error": (resp or "无响应")[:60]}

    async def send_raw(self, cmd):
        return await self.send(cmd, wait=2.0)
