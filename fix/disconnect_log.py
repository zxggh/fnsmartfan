"""
控制器断连记录 — 持久化到 /data/disconnect_log.json (掉电不丢失).

记录结构:
  {"start": "2026-08-06 10:22:30", "end": "2026-08-06 10:25:10",
   "duration_s": 160, "status": "recovered"}
  未恢复时: {"start": "...", "end": null, "duration_s": null, "status": "disconnected"}
"""

import json
import time
import logging
from pathlib import Path
from datetime import datetime

logger = logging.getLogger("fan_ctrl.disconnect")

LOG_FILE = Path("/data/disconnect_log.json")
MAX_ENTRIES = 100


class DisconnectLogger:
    def __init__(self, log_file: Path = LOG_FILE, max_entries: int = MAX_ENTRIES):
        self._file = Path(log_file)
        self._max = max_entries
        self._entries = []
        self._open_entry = None
        self._load()

    def _load(self):
        try:
            if self._file.exists():
                data = json.loads(self._file.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self._entries = data
                    if self._entries and self._entries[-1].get("status") == "disconnected":
                        self._open_entry = self._entries[-1]
                logger.info(f"已加载 {len(self._entries)} 条断连记录")
        except Exception as e:
            logger.warning(f"加载断连记录失败: {e}")

    def _save(self):
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            self._file.write_text(
                json.dumps(self._entries, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"保存断连记录失败: {e}")

    def start(self):
        if self._open_entry:
            return
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._open_entry = {
            "start": now, "end": None, "duration_s": None, "status": "disconnected",
        }
        self._entries.append(self._open_entry)
        if len(self._entries) > self._max:
            self._entries = self._entries[-self._max:]
        self._save()
        logger.info(f"断连开始: {now}")

    def end(self):
        if not self._open_entry:
            return
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            start_dt = datetime.strptime(self._open_entry["start"], "%Y-%m-%d %H:%M:%S")
            dur = int((datetime.now() - start_dt).total_seconds())
        except Exception:
            dur = 0
        self._open_entry["end"] = now
        self._open_entry["duration_s"] = dur
        self._open_entry["status"] = "recovered"
        self._open_entry = None
        self._save()
        logger.info(f"断连恢复: {now} (持续 {dur}s)")

    def get_entries(self, limit: int = 50) -> list:
        return list(reversed(self._entries[-limit:]))

    def is_disconnected(self) -> bool:
        return self._open_entry is not None
