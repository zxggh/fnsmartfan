"""
NAS 温度采集模块 (v3 动态硬盘识别版)

核心改进 (对应需求书第三章):
  1. 动态识别设备: 用 lsblk -d -n -o NAME,ROTA 列出所有块设备,
     ROTA=0 → SSD/NVMe, ROTA=1 → HDD. 严禁硬编码 /dev/sda.
  2. 鲁棒解析温度: HDD 的 smartctl 温度行带 (Min/Max XX/XX) 后缀,
     SSD 只有纯数字. 提取第 10 列后用 tr -cd '0-9' 过滤;
     NVMe 的 Temperature: 行通常在第 2 列, 单独处理.
  3. 多盘聚合: 同类硬盘取【最高温】(严禁平均值) 供 PWM 控制.
  4. UI 明细: 返回 disk_details 列表 (设备名/类型/温度), 供前端展开.
  5. 超时容错: 所有 smartctl 调用包 timeout=5, 失败返回 "N/A", 绝不崩溃.

返回结构 (collect_all):
  {
    "cpu": 38.5,                    # CPU 温度 (float|None)
    "max_ssd_temp": 42.0,           # 所有 SSD 最高温 (float|None)
    "max_hdd_temp": 36.0,           # 所有 HDD 最高温 (float|None)
    "ambient_temp": None,           # 预留 (环境温度由串口 NTC 提供)
    "disk_details": [               # 所有硬盘明细 (前端展开面板用)
        {"device": "/dev/sdb", "type": "SSD", "temp": 46},
        {"device": "/dev/sdc", "type": "SSD", "temp": 42},
        {"device": "/dev/sda", "type": "HDD", "temp": 39},
    ],
    # 以下为调试用原始数据
    "hwmon": {...},
    "sensors": {...},
  }
"""

import re
import os
import glob
import asyncio
import logging
import subprocess
from typing import Optional, List, Dict, Any

logger = logging.getLogger("fan_ctrl.temp")


class TempCollector:
    """温度采集器 — 动态硬盘识别 + CPU/hwmon 采集."""

    def __init__(self):
        self._cache: Dict[str, Any] = {}
        self._cache_time: float = 0.0
        # 缓存 TTL: 硬盘 SMART 真采一次 3~8s, 12s 缓存窗口必定命中
        self._cache_ttl: float = 12.0

    # ============================================================
    #  对外主入口
    # ============================================================
    async def collect_all(self) -> Dict[str, Any]:
        """采集全部温度 (带 12s 缓存).

        返回 dict, 包含 cpu / max_ssd_temp / max_hdd_temp / disk_details 等.
        """
        import time
        now = time.time()
        if now - self._cache_time < self._cache_ttl and self._cache:
            logger.debug(f"温度缓存命中 (age={now - self._cache_time:.1f}s)")
            return self._cache

        result: Dict[str, Any] = {
            "cpu": None,
            "max_ssd_temp": None,
            "max_hdd_temp": None,
            "ambient_temp": None,
            "disk_details": [],
            "hwmon": {},
            "sensors": {},
        }

        # 1. CPU 温度 (hwmon/sensors/thermal 三路兜底)
        try:
            result["cpu"] = await self._collect_cpu_temp()
        except Exception as e:
            logger.warning(f"CPU 温度采集失败: {e}")

        # 2. 硬盘温度 (动态 lsblk 识别 + smartctl timeout=5)
        try:
            disk_data = await self._collect_disks()
            result["max_ssd_temp"] = disk_data["max_ssd"]
            result["max_hdd_temp"] = disk_data["max_hdd"]
            result["disk_details"] = disk_data["details"]
        except Exception as e:
            logger.warning(f"硬盘温度采集失败: {e}")

        # 3. hwmon 原始数据 (调试用)
        try:
            result["hwmon"] = await self._collect_hwmon_raw()
        except Exception as e:
            logger.debug(f"hwmon 采集失败: {e}")

        self._cache = result
        self._cache_time = now
        return result

    # ============================================================
    #  CPU 温度 (hwmon/sensors/thermal 三路兜底)
    # ============================================================
    async def _collect_cpu_temp(self) -> Optional[float]:
        """采集 CPU 温度.

        优先级:
          1. hwmon_i915 (Intel SoC 温度, 与飞牛 NAS 系统显示一致)
          2. coretemp Package (lm-sensors 或 hwmon)
          3. /sys/class/thermal thermal_zone
        """
        # 1. i915 (Intel 集显/SoC 温度)
        i915 = self._read_hwmon_temp("i915")
        if i915 is not None:
            return i915

        # 2. coretemp
        for name in ("coretemp", "k10temp"):
            vals = self._read_hwmon_all_temps(name)
            if vals:
                return round(max(vals), 1)

        # 3. sensors CLI
        try:
            sensors_temp = await self._collect_sensors_cpu()
            if sensors_temp is not None:
                return sensors_temp
        except Exception:
            pass

        # 4. thermal zone 兜底
        for path in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
            try:
                with open(path, "r") as f:
                    val = int(f.read().strip())
                if 0 < val < 150000:
                    return round(val / 1000, 1)
            except Exception:
                continue
        return None

    def _read_hwmon_temp(self, name_keyword: str) -> Optional[float]:
        """从 /sys/class/hwmon 找 name 含 keyword 的设备, 返回 temp1_input."""
        base = "/sys/class/hwmon"
        if not os.path.isdir(base):
            return None
        for hwmon in sorted(os.listdir(base)):
            hwmon_path = os.path.join(base, hwmon)
            name_file = os.path.join(hwmon_path, "name")
            try:
                with open(name_file, "r") as f:
                    name = f.read().strip()
            except Exception:
                continue
            if name_keyword.lower() in name.lower():
                temp_file = os.path.join(hwmon_path, "temp1_input")
                try:
                    with open(temp_file, "r") as f:
                        return round(int(f.read().strip()) / 1000, 1)
                except Exception:
                    pass
        return None

    def _read_hwmon_all_temps(self, name_keyword: str) -> List[float]:
        """读取某 hwmon 设备所有 temp*_input 值."""
        base = "/sys/class/hwmon"
        if not os.path.isdir(base):
            return []
        result = []
        for hwmon in sorted(os.listdir(base)):
            hwmon_path = os.path.join(base, hwmon)
            name_file = os.path.join(hwmon_path, "name")
            try:
                with open(name_file, "r") as f:
                    name = f.read().strip()
            except Exception:
                continue
            if name_keyword.lower() in name.lower():
                for f in sorted(os.listdir(hwmon_path)):
                    if f.startswith("temp") and f.endswith("_input"):
                        try:
                            with open(os.path.join(hwmon_path, f), "r") as fobj:
                                val = int(fobj.read().strip())
                            if 0 < val < 150000:
                                result.append(val / 1000)
                        except Exception:
                            pass
        return result

    async def _collect_sensors_cpu(self) -> Optional[float]:
        """通过 sensors CLI 采集 CPU Package 温度."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "sensors",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            text = stdout.decode("utf-8", errors="replace")
        except Exception:
            return None
        # 找 Package id 0 行
        for line in text.split("\n"):
            m = re.search(r"Package id 0:\s*[+\\-]?(\d+\.?\d*)°C", line)
            if m:
                return round(float(m.group(1)), 1)
        return None

    # ============================================================
    #  硬盘温度 (动态识别 — 核心)
    # ============================================================
    async def _collect_disks(self) -> Dict[str, Any]:
        """动态识别所有硬盘并采集温度.

        流程:
          1. lsblk -d -n -o NAME,ROTA 列出块设备 (排除 loop/rom)
          2. ROTA=0 → SSD/NVMe, ROTA=1 → HDD
          3. 对每个设备调 smartctl -A (timeout=5) 取温度
          4. 同类取 MAX, 收集明细
        """
        max_ssd: Optional[float] = None
        max_hdd: Optional[float] = None
        details: List[Dict[str, Any]] = []

        # ── Step 1: 动态获取设备列表 ──
        ssd_devs, hdd_devs = await self._list_disk_devices()
        logger.info(f"动态识别硬盘: SSD={ssd_devs} HDD={hdd_devs}")

        # ── Step 2: 逐个采集温度 ──
        for dev in ssd_devs:
            temp = await self._get_disk_temp(dev, is_nvme=dev.startswith("/dev/nvme"))
            details.append({"device": dev, "type": "SSD", "temp": temp})
            if temp is not None and (max_ssd is None or temp > max_ssd):
                max_ssd = temp

        for dev in hdd_devs:
            temp = await self._get_disk_temp(dev, is_nvme=False)
            details.append({"device": dev, "type": "HDD", "temp": temp})
            if temp is not None and (max_hdd is None or temp > max_hdd):
                max_hdd = temp

        return {"max_ssd": max_ssd, "max_hdd": max_hdd, "details": details}

    async def _list_disk_devices(self) -> tuple:
        """用 lsblk 动态列出所有 SSD 和 HDD 设备路径.

        返回: (ssd_devs: list[str], hdd_devs: list[str])
        """
        ssd_devs: List[str] = []
        hdd_devs: List[str] = []
        try:
            # lsblk -d (只列磁盘, 不分区) -n (无表头) -o NAME,ROTA
            # 输出示例:
            #   sda   1
            #   sdb   0
            #   nvme0n1 0
            result = subprocess.run(
                ["lsblk", "-d", "-n", "-o", "NAME,ROTA"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode != 0:
                logger.warning(f"lsblk 失败: {result.stderr.strip()[:100]}")
                return ssd_devs, hdd_devs
            for line in result.stdout.strip().split("\n"):
                line = line.strip()
                if not line:
                    continue
                parts = line.split()
                if len(parts) < 2:
                    continue
                name = parts[0]
                try:
                    rota = int(parts[1])
                except ValueError:
                    continue
                # 排除 loop / rom / zram 等非物理磁盘
                if name.startswith(("loop", "ram", "zram", "sr")):
                    continue
                dev_path = f"/dev/{name}"
                if rota == 0:
                    ssd_devs.append(dev_path)
                else:
                    hdd_devs.append(dev_path)
        except subprocess.TimeoutExpired:
            logger.warning("lsblk 超时 (5s)")
        except FileNotFoundError:
            logger.warning("lsblk 命令不存在")
        except Exception as e:
            logger.warning(f"lsblk 异常: {e}")
        return ssd_devs, hdd_devs

    async def _get_disk_temp(self, dev_path: str, is_nvme: bool = False) -> Optional[float]:
        """采集单个硬盘温度 (smartctl -A, timeout=5).

        兼容格式:
          - HDD: "194 Temperature_Celsius ... 36 (Min/Max 20/50)"
            取第 10 列 → "36" → tr -cd '0-9' → 36
          - SSD: "194 Temperature_Celsius ... 42"
            取第 10 列 → "42" → 42
          - NVMe: "Temperature: 45 Celsius"
            取 "Temperature:" 后面的数字 (第 2 列)
        失败返回 None.
        """
        try:
            # 选择 smartctl 设备类型
            if is_nvme:
                dev_type = "nvme"
            else:
                dev_type = "sat"
            # ★ 必须 timeout=5, 防止休眠硬盘卡死
            result = subprocess.run(
                ["timeout", "5", "smartctl", "-A", "-d", dev_type, dev_path],
                capture_output=True, text=True, timeout=8
            )
            text = result.stdout
            if not text:
                # sat 不行试 ata
                if not is_nvme:
                    result2 = subprocess.run(
                        ["timeout", "5", "smartctl", "-A", "-d", "ata", dev_path],
                        capture_output=True, text=True, timeout=8
                    )
                    text = result2.stdout
            temp = self._parse_smartctl_temp(text, is_nvme)
            return temp
        except subprocess.TimeoutExpired:
            logger.warning(f"smartctl 超时: {dev_path}")
            return None
        except FileNotFoundError:
            logger.warning("smartctl 命令不存在")
            return None
        except Exception as e:
            logger.warning(f"smartctl {dev_path} 异常: {e}")
            return None

    def _parse_smartctl_temp(self, text: str, is_nvme: bool) -> Optional[float]:
        """解析 smartctl -A 输出, 提取温度.

        HDD/SSD 走 Temperature_Celsius 行:
          取空白分割后的第 10 列 (索引 9), 用 tr -cd '0-9' 过滤数字.
        NVMe 走 Temperature: 行:
          取冒号后第一个数字.
        """
        if not text:
            return None
        if is_nvme:
            # NVMe: "Temperature:                        45 Celsius"
            m = re.search(r"Temperature:\s+(\d+)", text)
            if m:
                val = int(m.group(1))
                if 0 < val < 150:
                    return float(val)
            return None
        # SATA HDD/SSD: 找 Temperature_Celsius 行
        for line in text.split("\n"):
            if "Temperature_Celsius" in line:
                parts = line.split()
                # smartctl 输出固定格式: ID# ATTRIBUTE_NAME FLAG VALUE WORST THRESH TYPE UPDATED WHEN_FAILED RAW_VALUE
                # 第 10 列 (索引 9) 是 RAW_VALUE, HDD 可能是 "36 (Min/Max 20/50)", SSD 是 "42"
                if len(parts) >= 10:
                    raw_val = parts[9]
                    # 模拟 tr -cd '0-9': 只保留数字
                    digits = re.sub(r"[^0-9]", "", raw_val)
                    if digits:
                        val = int(digits)
                        if 0 < val < 150:
                            return float(val)
                # 兜底: 正则抓行内最后一个数字
                nums = re.findall(r"\d+", line)
                if nums:
                    val = int(nums[-1])
                    if 0 < val < 150:
                        return float(val)
        return None

    # ============================================================
    #  hwmon 原始数据 (调试用)
    # ============================================================
    async def _collect_hwmon_raw(self) -> Dict[str, Any]:
        """采集所有 hwmon 设备的温度 (调试用)."""
        result = {}
        base = "/sys/class/hwmon"
        if not os.path.isdir(base):
            return result
        for hwmon in sorted(os.listdir(base)):
            hwmon_path = os.path.join(base, hwmon)
            name_file = os.path.join(hwmon_path, "name")
            try:
                with open(name_file, "r") as f:
                    name = f.read().strip()
            except Exception:
                name = "unknown"
            temps = {}
            for f in sorted(os.listdir(hwmon_path)):
                if f.startswith("temp") and f.endswith("_input"):
                    try:
                        with open(os.path.join(hwmon_path, f), "r") as fobj:
                            temps[f] = round(int(fobj.read().strip()) / 1000, 1)
                    except Exception:
                        pass
            if temps:
                result[f"hwmon_{name}"] = temps
        return result
