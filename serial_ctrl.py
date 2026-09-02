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
        # ── v8 新增: 死锁检测 + 心跳超时复位计数器 ──
        #   STC USB CDC 库存在 bUsbInBusy=1 发送死锁 BUG:
        #   一旦 USB IN 端点中断丢失, 固件认为缓冲区"正在发送中", 永远卡住
        #   → Python 端 disconnect/connect 重开串口完全无效 (死锁在固件内部)
        #   → 唯一解法: 检测到疑似死锁后主动发 REBOOT 命令, 让控制器整体硬复位
        #
        #   判定规则(满足任一即触发 REBOOT):
        #   A. ensure_connected() 重连累计超过 _REBOOT_AFTER_RECONNECT_ATTEMPTS 次仍失败
        #   B. "已连接态" 下 ping 连续失败超过 _REBOOT_AFTER_PING_FAILS 次
        #      (writer 存在, 串口未报错, 但就是收不到响应 → 典型 bUsbInBusy 死锁)
        self._DEADLOCK_REBOOT_AFTER_RECONNECT = 12   # 重连尝试累计 12 次 ≈ 低频阶段 6 分钟
        self._DEADLOCK_REBOOT_AFTER_PING_FAILS = 10  # 已连接态连续 10 次 ping 失败 ≈ 35 秒
        self._reconnect_total_attempts = 0           # ensure_connected 内累计重连次数
        self._consecutive_ping_fails_since_ok = 0    # 上次成功后累计 ping 失败次数 (含已断连期间)
        self._reboot_issued_count = 0                # 历史 REBOOT 总次数 (诊断用)
        self._reboot_in_progress = False             # v9: 防重入(REBOOT 期间 ping 直接返回 False)
        self._usb_reset_ok_count = 0                 # v9: 主机侧USB复位成功次数(诊断)
        self._autosuspend_disabled = False           # v9: 是否已禁用 Linux USB autosuspend
        # 日志
        self.log = []

    def _log(self, d, data):
        self.log.append({"t": time.time(), "dir": d, "data": data[:200]})
        if len(self.log) > 50:
            self.log.pop(0)

    # ── v9 新增: 主机侧 USB 物理复位 — 不依赖固件合作 ──
    async def _host_usb_reset(self):
        """★v9: 主机侧 USB 设备复位 — 强制物理重枚举, 不依赖固件合作.

        bUsbInBusy 死锁的本质: 固件内部 USB IN 端点状态机卡死,
        无论主机端怎么 close/open 串口都无效 (死锁在固件里, 不在主机端).
        REBOOT 命令也可能发不进去 (TX 通路也可能被死锁影响).

        唯一可靠解法: 主机侧强制 USB 设备物理断开+重连,
        让 USB 控制器重新枚举设备, 固件 USB 状态机从头初始化.

        三层尝试(任一成功即返回 True):
        1. /sys authorized toggle (需 privileged 模式, 最可靠)
           — 写 '0' 到 authorized 让 USB 设备物理断开, 等 2s, 写 '1' 重新枚举
        2. fcntl USBDEVFS_RESET ioctl (需 /dev/bus/usb 访问权限)
           — 对 USB 设备文件发 USBDEVFS_RESET ioctl, 软复位
        3. subprocess usbreset 命令(如果系统已安装)
        """
        port_name = self._port.split('/')[-1]  # ttyACM0
        logger.info(f"[USB复位] 尝试主机侧USB物理复位 {self._port} (不依赖固件)")

        # ── 方法1: /sys authorized toggle (最可靠, 需 privileged) ──
        # 原理: 写 '0' 让内核断开 USB 设备(等同于物理拔出), 写 '1' 重新枚举(等同于插入)
        # 效果: 固件 USB 状态机被强制重新初始化, bUsbInBusy 死锁必定清除
        try:
            tty_sys = f'/sys/class/tty/{port_name}'
            if os.path.isdir(tty_sys):
                # 从 /sys/class/tty/ttyACM0/device 逐层向上找 authorized 文件
                current = os.path.realpath(f'{tty_sys}/device')
                for _ in range(10):  # 最多向上 10 层
                    auth = os.path.join(current, 'authorized')
                    if os.path.exists(auth):
                        logger.info(f"[USB复位] 找到 authorized: {auth}")
                        # deauthorize: USB 设备物理断开
                        with open(auth, 'w') as f:
                            f.write('0')
                        logger.info("[USB复位] 已 deauthorize (USB 设备物理断开)")
                        await asyncio.sleep(2)
                        # reauthorize: USB 设备重新枚举, 驱动重新加载
                        with open(auth, 'w') as f:
                            f.write('1')
                        logger.info("[USB复位] 已 reauthorize (USB 设备重新枚举)")
                        await asyncio.sleep(3)  # 等待重枚举 + cdc_acm 驱动加载
                        self._usb_reset_ok_count += 1
                        logger.info("[USB复位] ✅ authorized toggle 成功")
                        return True
                    parent = os.path.dirname(current)
                    if parent == current:
                        break
                    current = parent
                logger.warning("[USB复位] /sys 路径下未找到 authorized 文件 (容器未用 privileged?)")
        except Exception as e:
            logger.warning(f"[USB复位] authorized toggle 异常: {type(e).__name__}: {e}")

        # ── 方法2: fcntl USBDEVFS_RESET ioctl (软复位, 不需要断开设备) ──
        # 对 /dev/bus/usb/<bus>/<dev> 发 USBDEVFS_RESET ioctl
        try:
            import fcntl
            USBDEVFS_RESET = 21780  # _IO('U', 20)
            tty_sys = f'/sys/class/tty/{port_name}'
            if os.path.isdir(tty_sys):
                current = os.path.realpath(f'{tty_sys}/device')
                for _ in range(10):
                    devnum_file = os.path.join(current, 'devnum')
                    busnum_file = os.path.join(current, 'busnum')
                    if os.path.exists(devnum_file) and os.path.exists(busnum_file):
                        busnum = int(open(busnum_file).read().strip())
                        devnum = int(open(devnum_file).read().strip())
                        usb_dev_file = f'/dev/bus/usb/{busnum:03d}/{devnum:03d}'
                        if os.path.exists(usb_dev_file):
                            logger.info(f"[USB复位] 尝试 ioctl on {usb_dev_file}")
                            fd = os.open(usb_dev_file, os.O_WRONLY)
                            fcntl.ioctl(fd, USBDEVFS_RESET, 0)
                            os.close(fd)
                            logger.info(f"[USB复位] ✅ ioctl USBDEVFS_RESET 成功")
                            await asyncio.sleep(3)
                            self._usb_reset_ok_count += 1
                            return True
                    parent = os.path.dirname(current)
                    if parent == current:
                        break
                    current = parent
                logger.warning("[USB复位] 未找到 USB bus/dev 号")
        except Exception as e:
            logger.warning(f"[USB复位] ioctl 异常: {type(e).__name__}: {e}")

        # ── 方法3: usbreset 命令(如果已安装) ──
        try:
            import subprocess
            result = subprocess.run(
                ['usbreset', self._port],
                capture_output=True, timeout=10
            )
            if result.returncode == 0:
                logger.info("[USB复位] ✅ usbreset 命令成功")
                await asyncio.sleep(3)
                self._usb_reset_ok_count += 1
                return True
            else:
                logger.warning(f"[USB复位] usbreset 返回非0: {result.stderr.decode()[:100]}")
        except FileNotFoundError:
            logger.info("[USB复位] usbreset 命令未安装(跳过)")
        except Exception as e:
            logger.warning(f"[USB复位] usbreset 异常: {type(e).__name__}: {e}")

        logger.warning("[USB复位] ❌ 所有主机侧复位方法均失败 (容器可能未启用 privileged 模式?)")
        return False

    # ── v9: 禁用 Linux USB autosuspend (STC官方确认的根因之一) ──
    async def _disable_autosuspend(self):
        """禁用 Linux USB 自动挂起 — STC官方AI确认是 NAS 环境断连根因.

        Linux 内核默认开启 USB autosuspend, 总线空闲时对 USB 设备发 SUSPEND 信号.
        如果固件没正确处理 SUSIF/RSUIF 中断, 挂起后无法恢复 → 永久断连.
        禁用后 USB 总线永不挂起, 消除诱因.
        """
        # 方法1: 全局禁用
        try:
            with open('/sys/module/usbcore/parameters/autosuspend', 'w') as f:
                f.write('-1')
            logger.info("[USB] 已禁用全局 USB autosuspend (autosuspend=-1)")
        except Exception as e:
            logger.warning(f"[USB] 禁用全局 autosuspend 失败(需 privileged): {type(e).__name__}")

        # 方法2: 对所有已连接 USB 设备禁用 per-device autosuspend
        try:
            import glob
            count = 0
            for ctl in glob.glob('/sys/bus/usb/devices/*/power/control'):
                try:
                    with open(ctl, 'w') as f:
                        f.write('on')
                    count += 1
                except Exception:
                    pass
            if count > 0:
                logger.info(f"[USB] 已对 {count} 个 USB 设备禁用 per-device autosuspend")
        except Exception as e:
            logger.warning(f"[USB] per-device autosuspend 禁用失败: {type(e).__name__}")

    # ── v8/v9: 死锁恢复 — REBOOT 命令 + 主机侧 USB 物理复位 ──
    async def _issue_reboot(self, reason: str):
        """检测到死锁/心跳超时后, 强制控制器复位.

        ★v9 关键改进: 不再只依赖 REBOOT 命令(固件死锁时 TX 也断, 命令发不进去),
           新增主机侧 USB 物理复位(不依赖固件合作):
           Step 1: 尝试发 REBOOT 命令(固件 TX 通路可能还活着)
           Step 2: 断开串口(释放文件句柄)
           Step 3: ★主机侧 USB 设备复位(authorized toggle / ioctl / usbreset)
                   — 即使 REBOOT 命令没发到固件, 物理重枚举也能清除 bUsbInBusy 死锁
           Step 4: 等待 USB 重枚举完成, ensure_connected 后续循环自动连上新设备
        """
        self._reboot_issued_count += 1
        self._reboot_in_progress = True   # v9: 防重入
        # ★ 先暂存计数再打印 (否则先清零再 log 永远显示 0/0, 诊断等于白打)
        _save_recon_attempts = self._reconnect_total_attempts
        _save_ping_fails = self._consecutive_ping_fails_since_ok
        self._reconnect_total_attempts = 0      # 计数清零, 避免每次都立刻再触发 reboot
        self._consecutive_ping_fails_since_ok = 0
        logger.critical(
            f"[心跳超时复位#{self._reboot_issued_count}] 触发原因={reason} | "
            f"重连累计={_save_recon_attempts} | ping连续失败={_save_ping_fails} | "
            f"准备: REBOOT命令 + 断开 + ★主机侧USB物理复位..."
        )
        self._dump_state(f"REBOOT#{self._reboot_issued_count}_{reason}")
        self._log("SYS", f"[死锁恢复] 触发 REBOOT: {reason}")

        # ── Step 1: 尝试发 REBOOT 命令 (固件 TX 通路可能还活着, 只有 RX 死锁) ──
        reboot_sent_ok = False
        for i in range(2):
            try:
                if self._writer:
                    frame = f"REBOOT\r\n".encode()
                    # 注意: 不用 _lock, 死锁时锁可能被占用; 直接底层暴力写 (不怕脏, 反正要复位)
                    self._writer.write(frame)
                    await asyncio.wait_for(self._writer.drain(), timeout=0.5)
                    reboot_sent_ok = True
                    self._log("TX", f"REBOOT(尝试{i+1}/2)")
                    await asyncio.sleep(0.15)
            except Exception as e:
                logger.warning(f"[REBOOT] 第{i+1}次发送异常(死锁状态属正常): {type(e).__name__}: {e}")
        if not reboot_sent_ok:
            logger.warning("[REBOOT] 命令未发出(典型bUsbInBusy死锁,TX也断了) → 转主机侧USB复位")

        # ── Step 2: 断开串口 (释放文件句柄, 为 USB 重枚举做准备) ──
        await self.disconnect()

        # ── Step 3: ★v9 核心 — 主机侧 USB 物理复位 (不依赖固件!) ──
        #   即使 REBOOT 命令因 TX 死锁没发到固件,
        #   主机侧 authorized toggle / ioctl 能强制 USB 设备物理断开+重枚举,
        #   固件 USB 状态机从头初始化, bUsbInBusy 死锁必定清除
        usb_reset_ok = await self._host_usb_reset()

        # ── Step 4: 等待设备重新枚举 ──
        if not usb_reset_ok:
            # USB 复位失败/不可用, 兜底等 5 秒(可能固件自己收到 REBOOT 也会复位)
            logger.warning("[REBOOT] 主机侧USB复位失败/不可用, 等待5秒后继续重试(靠REBOOT命令或固件自身超时)")
            await asyncio.sleep(5)
        else:
            # USB 复位成功, _host_usb_reset 内部已 sleep(3~5s), 再额外等 2s 确保驱动加载
            await asyncio.sleep(2)

        self._reboot_in_progress = False
        logger.info(
            f"[REBOOT#{self._reboot_issued_count}] 完成 | "
            f"REBOOT命令={'已发' if reboot_sent_ok else '未发'} | "
            f"主机USB复位={'成功' if usb_reset_ok else '失败'} | "
            f"进入后续重连流程..."
        )

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
                # v8: 死锁/复位诊断计数 (最关键的排障字段)
                f"REBOOT累计触发={self._reboot_issued_count}次 | "
                f"USB复位成功={getattr(self, '_usb_reset_ok_count', 0)}次 | "
                f"重连尝试累计={self._reconnect_total_attempts} | "
                f"ping连续失败={self._consecutive_ping_fails_since_ok} | "
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
        # v8: 连接建立时重置失败计数 (但不重置 _reboot_issued_count 累计诊断数)
        self._consecutive_ping_fails_since_ok = 0
        # v9: 连接后发 LED=ON (给固件一个"主机已连接"视觉信号)
        #   即使 USB 刚连上可能还没完全就绪, 发了不坏事; 固件收不到就靠自己的超时逻辑
        if self._writer:
            try:
                self._writer.write(b"LED=ON\r\n")
                await asyncio.wait_for(self._writer.drain(), timeout=0.3)
                self._log("TX", "LED=ON(连接指示)")
            except Exception:
                pass

    async def disconnect(self):
        self._connected = False
        r, w = self._reader, self._writer
        # v9: 断开前尝试发 LED=OFF (给固件一个"主机侧断开"信号)
        #   如果 USB 已死锁这步会失败, 但固件自己的命令超时会处理
        if w:
            try:
                w.write(b"LED=OFF\r\n")
                await asyncio.wait_for(w.drain(), timeout=0.3)
                self._log("TX", "LED=OFF(断开指示)")
            except Exception:
                pass
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
        """断线重连 — 永不放弃 + 全程诊断日志 + v8 心跳超时复位(死锁 REBOOT).

        关键改进:
        - 进入前 _dump_state('重连开始'): 把设备节点/writer状态/上次ping时间打到 logger
        - 候选端口逐个测试, connect 失败时记录异常类型
        - ping 持续失败时明确区分 "writer已断" vs "ping无响应"
        - 永不返回 False: 前 60 秒高频(2s/次), 之后低频(30s/次)持续重试
        - 每轮结束 _dump_state, 让 docker logs 看到现场
        - ★ v8 修复 A: connect() 成功后「立刻 ping」不再先 sleep 1s
            原流程: disconnect+sleep(1) → connect() → sleep(1.0) → ping
                    从断开到第1条命令发出 ≈ 2s + connect耗时, 高频重连时偶发 >5s 固件窗口 → 固件复位
            新流程: connect() → 立刻 ping → 间隔 0.8s → ping → ...
                    connect 成功到第 1 条命令 < 50ms, 稳在 5s 窗口内
        - ★ v8 修复 B: 重连累计超阈值触发 _issue_reboot()
            死锁 (bUsbInBusy=1) 下, 串口能正常 open/close, 但永远收不到 ping 响应
            → 12 次 (≈低频 6 分钟) 仍失败 → 发 REBOOT 命令强制控制器复位
        """
        # v9: 首次进入重连流程时, 禁用 Linux USB autosuspend (只需一次)
        if not getattr(self, '_autosuspend_disabled', False):
            await self._disable_autosuspend()
            self._autosuspend_disabled = True

        if self._connected:
            try:
                if await self.ping():
                    # v8: 确保重连计数归零 (正常连接态走这个快捷分支时也要清零)
                    self._reconnect_total_attempts = 0
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
            # v8: 累计重连轮次 (用于死锁判定)
            self._reconnect_total_attempts = max(self._reconnect_total_attempts + 1, attempt)

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
                # ★ v8 修复 A: 保持连接, 多次 ping — 第 1 次立刻发, 后续间隔 0.8s
                #   之前 for p in range(3): sleep(1.0) → ping 导致 "connect到首条命令" 超1秒
                #   加上 disconnect + sleep(1) 前置开销, 最坏接近 5s 临界, 偶发固件自复位
                #   现在: ping_immediate → sleep(0.8s) → ping → sleep(0.8s) → ping
                #         connect 成功到第 1 次 TX < 50ms, 3 次总计 ≈ 1.6s, 远 < 5s 窗口
                for p in range(3):
                    if p > 0:
                        await asyncio.sleep(0.8)   # 仅第 2/3 次前等 0.8s
                    # ★ v9 修复: _issue_reboot 内部 disconnect 后 writer 变 None,
                    #   不再浪费 2 次空转 ping, 直接跳出 for 循环进入下一轮重连
                    if not self._writer:
                        break
                    if await self.ping():
                        self._connected = True
                        self._last_ping_ok_time = time.time()
                        # v8: 重连成功, 清零死锁计数
                        self._reconnect_total_attempts = 0
                        self._consecutive_ping_fails_since_ok = 0
                        self._log("SYS", f"重连成功({port}) 尝试{attempt}次 耗时{elapsed:.0f}s")
                        logger.info(f"重连成功({port}) 尝试{attempt}次 耗时{elapsed:.0f}s")
                        return True
                # 该端口 ping 持续失败, 断开试下一个
                logger.warning(f"ensure_connected: {port} ping持续失败(尝试{attempt}, 已重试{elapsed:.0f}s)")
                self._log("SYS", f"{port} ping持续失败, 试下一个端口")
                await self.disconnect()

            if not tested:
                # ★v10修复: 无物理设备(USB断开)时, 不触发REBOOT(不是死锁, 是物理断开)
                #   重置死锁计数器, 只等设备重新出现, 避免空转1430次REBOOT无用功
                self._reconnect_total_attempts = 0
                if attempt % 10 == 1:
                    self._dump_state(f"无可用端口#{attempt}")
                    logger.info(f"等待USB设备重新出现(物理断开), 尝试{attempt}次 耗时{elapsed:.0f}s")
                # 无设备时快速轮询(3s), 尽快捕捉重连窗口
                await asyncio.sleep(3)
                continue

            # ★ v8 修复 B: 死锁检测 A 路径 — 重连累计超阈值 → 强制 REBOOT
            #   仅在端口存在但ping无响应时才计数(真正的bUsbInBusy死锁场景)
            if self._reconnect_total_attempts >= self._DEADLOCK_REBOOT_AFTER_RECONNECT:
                await self._issue_reboot(
                    f"重连累计{self._reconnect_total_attempts}次无响应(疑似bUsbInBusy死锁)"
                )
                # REBOOT 后 attempt 重置为 0 (相当于重新开始一轮), 避免下次循环立刻又触发
                attempt = 0
                start_time = time.time()
                # _issue_reboot 内部已经清零了 reconnect_total_attempts, 所以 continue 是安全的
                continue

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
        - v8 新增: 连续失败累计 + 阈值触发 REBOOT (防 bUsbInBusy=1 死锁)
        - v9 新增: _reboot_in_progress 时直接返回 False (不空转, 不累计计数)
        """
        # v9: REBOOT 期间不空转 ping (writer 已断, 等重枚举)
        if self._reboot_in_progress:
            return False
        if not self._writer:
            self._ping_fail_streak += 1
            self._consecutive_ping_fails_since_ok += 1
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
                self._consecutive_ping_fails_since_ok = 0   # v8: 成功清零
                self._connected = True
                self._last_ping_ok_time = time.time()
                return True
            # 响应过短视为失败(可能是残留字节)
            self._ping_fail_streak += 1
            self._consecutive_ping_fails_since_ok += 1   # v8: 累计
            if self._ping_fail_streak >= 2:
                self._connected = False
            # ── v8: 死锁防护 B 路径 ──
            #   writer 存在(串口没报错)但连续 N 次 ping 无响应 → 疑似 bUsbInBusy=1 死锁 → REBOOT
            if (self._writer is not None
                    and self._consecutive_ping_fails_since_ok >= self._DEADLOCK_REBOOT_AFTER_PING_FAILS):
                await self._issue_reboot(
                    f"已连接态连续{self._consecutive_ping_fails_since_ok}次ping失败(疑似USB死锁)"
                )
                return False
            return False
        except Exception as e:
            # 兜底: 任何异常都视为 ping 失败, 不冒泡
            logger.debug(f"ping exception: {type(e).__name__}: {e}")
            self._ping_fail_streak += 1
            self._consecutive_ping_fails_since_ok += 1   # v8: 累计
            if self._ping_fail_streak >= 2:
                self._connected = False
            # ── v8: 死锁防护 B 路径(异常也累计) ──
            if (self._writer is not None
                    and self._consecutive_ping_fails_since_ok >= self._DEADLOCK_REBOOT_AFTER_PING_FAILS):
                await self._issue_reboot(
                    f"ping异常累计{self._consecutive_ping_fails_since_ok}次(疑似USB死锁)"
                )
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
