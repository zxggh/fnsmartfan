"""
温度历史持久化模块 (v6 新增)

解决痛点: 前端内存只存最多几十分钟数据, 浏览器刷新就丢, 无法查看全天/全周温度走势.

设计:
  - 存储格式: JSON Lines (每行一条记录, 便于追加与按时间扫描):
      {"t": 1724800000, "cpu": 38.5, "ssd": 42.0, "hdd": 36.0, "ntc": 28.2}
  - 存储位置: /data/temp_history.jsonl
      注意: /data 是 docker 挂载卷, 容器重启 / 镜像更新后不会丢失,
            而 /app 目录不应该存数据 (项目硬性约束).
  - 采样间隔: 60 秒 1 条 → 1 天 1440 条 → 7 天 10080 条, 单条约 70B,
      7 天数据约 700KB, 对 NAS 硬盘完全零压力.
  - 保留策略: 启动时 + 每小时自动清理超过 RETAIN_DAYS 天的记录.
  - 查询降采样: 当请求范围内原始点数 > MAX_POINTS 时, 按时间桶合并
      (桶内取各温度平均值, 整数时间戳), 确保图表点数始终可控.
"""

import json
import os
import time
import logging
from pathlib import Path

logger = logging.getLogger("fan_ctrl.history")

# ======== 可配置参数 (按需调整) ========
# 采样间隔(秒). 1分钟 = 精度够用 + 数据量可控.
SAMPLE_INTERVAL_S = 60
# 保留天数. 7 天 = 约 1 万行, 700KB.
RETAIN_DAYS = 7
# 返回前端的最大点数 (降采样阈值). 360 点画图毫无压力.
MAX_POINTS = 360
# 历史文件路径 (/data 是挂载卷, 容器更新不丢数据)
HISTORY_FILE = Path("/data/temp_history.jsonl")


class TempHistory:
    def __init__(self):
        # ★ v6 加固: 任何 /data 卷权限问题都不要让整个服务崩溃,
        #   降级为 "内存模式" — 仅在本次运行内存中保存历史数据,
        #   容器重启会丢失, 但核心温控/实时曲线完全正常, 避免 PermissionError
        #   导致的 FastAPI lifespan 直接抛出 → 容器狂重启.
        self._memory_mode = False
        self._in_memory_records = []   # 内存模式下暂存记录 (list[dict])
        self._last_cleanup_time = time.time()
        try:
            HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            if not HISTORY_FILE.exists():
                HISTORY_FILE.touch()
                logger.info(f"温度历史文件创建: {HISTORY_FILE}")
            else:
                # 只读权限就先尝试读一行测试, 行数计算失败也不致命
                try:
                    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                        lines = sum(1 for _ in f)
                    logger.info(f"温度历史文件已存在: {HISTORY_FILE} ({lines} 行记录)")
                except Exception as le:
                    logger.warning(f"读取历史文件行数失败(继续尝试写入模式): {le}")
            # 启动时先清理一次过期数据
            self._cleanup_expired()
        except (PermissionError, OSError, IOError) as pe:
            self._memory_mode = True
            logger.error(
                f"⚠️  温度历史持久化关闭(权限不足, 降级为内存模式): {pe}. "
                f"本次运行内存中仍会保留采样数据, 但容器重启后会丢失. "
                f"修复方法: 宿主机执行 chmod -R 777 /你的卷目录 或 容器以 root 用户启动."
            )
        except Exception as e:
            self._memory_mode = True
            logger.error(f"⚠️  温度历史初始化失败(降级为内存模式): {e}", exc_info=False)

    # ------------------ 写入 ------------------
    def add_record(self, cpu=None, ssd=None, hdd=None, ntc=None):
        """追加一条温度记录. 温度值为 None 时写 null(JSON 兼容).

        ★ v6 加固: 内存模式就追加到内存 list, 不写文件; 任何写入异常都不抛出.
        """
        rec = {
            "t": int(time.time()),       # 秒级 UNIX 时间戳 (便于 JSON 传输)
            "cpu": cpu,
            "ssd": ssd,
            "hdd": hdd,
            "ntc": ntc,
        }
        # ---- 内存模式: 直接 append, 同时按 7 天截断内存容量 ----
        if self._memory_mode:
            try:
                self._in_memory_records.append(rec)
                # 内存里最多保留 10080 条 (7 天 * 1440), 避免长周期运行占内存
                if len(self._in_memory_records) > RETAIN_DAYS * 1440 + 100:
                    self._in_memory_records[:] = self._in_memory_records[-(RETAIN_DAYS * 1440):]
            except Exception as e:
                logger.warning(f"温度历史内存写入失败: {e}")
            return

        # ---- 持久化模式: 追加写 JSONL ----
        try:
            line = json.dumps(rec, ensure_ascii=False) + "\n"
            # a+ 追加: 写入量极小(<100B/min), 不用 fsync 也 OK, NAS 掉电概率极低.
            with open(HISTORY_FILE, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception as e:
            logger.warning(f"温度历史写入失败: {e}")

        # 每小时清理一次过期数据 (60 分钟 = 3600s)
        if time.time() - self._last_cleanup_time > 3600:
            try:
                self._cleanup_expired()
            except Exception as e:
                logger.warning(f"温度历史清理失败: {e}")
            self._last_cleanup_time = time.time()

    # ------------------ 过期清理 ------------------
    def _cleanup_expired(self):
        """扫描整个 JSONL, 把 RETAIN_DAYS 天内的记录重写到新文件, 然后原子替换.

        RETAIN_DAYS=7 时约一万行, 完全可以承受; 如果有几十万行再考虑更高效滚动文件.
        """
        cutoff = time.time() - RETAIN_DAYS * 86400
        kept_lines = []
        removed = 0
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line: continue
                    try:
                        rec = json.loads(line)
                        if rec.get("t", 0) >= cutoff:
                            kept_lines.append(line)
                        else:
                            removed += 1
                    except (json.JSONDecodeError, ValueError):
                        removed += 1  # 损坏行直接丢弃
            if removed == 0:
                return
            # 原子写: 先写临时文件再 rename (覆盖)
            tmp = HISTORY_FILE.with_suffix(".jsonl.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                for ln in kept_lines:
                    f.write(ln + "\n")
            os.replace(tmp, HISTORY_FILE)
            logger.info(f"温度历史清理: 移除 {removed} 条过期/损坏行, 剩余 {len(kept_lines)} 条")
        except Exception as e:
            logger.warning(f"_cleanup_expired 异常: {e}")
            # 临时文件残留就清一下
            tmp = HISTORY_FILE.with_suffix(".jsonl.tmp")
            try:
                if tmp.exists(): tmp.unlink()
            except Exception:
                pass

    # ------------------ 查询 + 降采样 ------------------
    def query_range_seconds(self, seconds_ago: int):
        """查询从 now-seconds_ago 到 now 的温度数据, 并按 MAX_POINTS 做降采样.

        ★ v6 加固: 内存模式时从 self._in_memory_records 读取, 否则读 JSONL 文件.
        返回:
            list[dict] — 元素: {"t": int_ts, "cpu":float|None, "ssd":..., "hdd":..., "ntc":...}
                         已按 t 升序排列.
        """
        start_ts = time.time() - seconds_ago
        raw = []

        # ---- 内存模式: 遍历内存列表 ----
        if self._memory_mode:
            try:
                for rec in self._in_memory_records:
                    ts = rec.get("t", 0)
                    if ts >= start_ts:
                        raw.append(rec)
            except Exception as e:
                logger.warning(f"内存模式历史查询失败: {e}")
                return []
        # ---- 持久化模式: 读 JSONL ----
        else:
            try:
                with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line: continue
                        try:
                            rec = json.loads(line)
                        except (json.JSONDecodeError, ValueError):
                            continue
                        ts = rec.get("t", 0)
                        if ts >= start_ts:
                            raw.append(rec)
            except Exception as e:
                logger.warning(f"历史查询失败: {e}")
                return []

        if len(raw) <= MAX_POINTS:
            return raw  # 量少直接返回, 不降采样

        # 降采样: 按桶合并, 每个桶求四个温度字段的平均值(跳过 None)
        bucket_count = MAX_POINTS
        bucket_span = seconds_ago / bucket_count  # 秒 / 桶
        buckets = [None] * bucket_count  # 每个元素: dict{count, sum_cpu, sum_ssd, sum_hdd, sum_ntc, bucket_ts}
        # 计算每个桶的起始时间戳 (整数对齐, 方便前端显示)
        bucket_base = int(start_ts)
        for r in raw:
            offset = (r["t"] - bucket_base)
            if offset < 0: offset = 0
            idx = int(offset / bucket_span)
            if idx >= bucket_count: idx = bucket_count - 1
            b = buckets[idx]
            if b is None:
                buckets[idx] = {
                    "n": 0,
                    "cpu_s": 0.0, "cpu_n": 0,
                    "ssd_s": 0.0, "ssd_n": 0,
                    "hdd_s": 0.0, "hdd_n": 0,
                    "ntc_s": 0.0, "ntc_n": 0,
                    "t": int(bucket_base + (idx + 0.5) * bucket_span),  # 桶中心时间
                }
                b = buckets[idx]
            b["n"] += 1
            for fld in ("cpu", "ssd", "hdd", "ntc"):
                v = r.get(fld)
                if v is not None and isinstance(v, (int, float)):
                    b[f"{fld}_s"] += float(v)
                    b[f"{fld}_n"] += 1

        # 合并桶 -> 输出记录
        out = []
        for b in buckets:
            if b is None or b["n"] == 0: continue
            out.append({
                "t": b["t"],
                "cpu": (b["cpu_s"] / b["cpu_n"]) if b["cpu_n"] else None,
                "ssd": (b["ssd_s"] / b["ssd_n"]) if b["ssd_n"] else None,
                "hdd": (b["hdd_s"] / b["hdd_n"]) if b["hdd_n"] else None,
                "ntc": (b["ntc_s"] / b["ntc_n"]) if b["ntc_n"] else None,
            })
        return out


# -------- range 字符串 → 秒数 --------
RANGE_MAP = {
    "1h":  3600,
    "6h":  6 * 3600,
    "12h": 12 * 3600,
    "24h": 24 * 3600,
    "3d":  3 * 86400,
    "7d":  7 * 86400,
}


def parse_range(range_str: str) -> int:
    """解析 range 参数, 非法时默认 24h."""
    if not range_str: return RANGE_MAP["24h"]
    r = range_str.strip().lower()
    return RANGE_MAP.get(r, RANGE_MAP["24h"])


def fmt_timestamp(ts: int, range_seconds: int) -> str:
    """把 UNIX 时间戳格式化成前端 X 轴文字.
    - range ≤ 24h → HH:MM
    - range > 24h → MM/DD HH:MM  (区分跨日)
    """
    t = time.localtime(ts)
    if range_seconds <= 24 * 3600:
        return f"{t.tm_hour:02d}:{t.tm_min:02d}"
    return f"{t.tm_mon:02d}/{t.tm_mday:02d} {t.tm_hour:02d}:{t.tm_min:02d}"
