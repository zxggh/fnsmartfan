"""
温控配置模块 — 持久化到 /data/config.json

配置项:
  - start_temp:  风扇启动温度 (°C)
  - max_temp:    风扇全速温度 (°C)
  - fan_mode:    auto / manual
  - manual_duty: 手动模式占空比 (0-100)
  - hysteresis:  温度回差 (°C)
"""

import json
import logging
from pathlib import Path
from typing import Dict, Any

logger = logging.getLogger("fan_ctrl.config")

CONFIG_FILE = Path("/data/config.json")

DEFAULT_CONFIG: Dict[str, Any] = {
    "start_temp": 35,
    "max_temp": 60,
    "fan_mode": "auto",       # auto | manual
    "manual_duty": 50,
    "hysteresis": 2,
}


class ConfigManager:
    """温控配置管理器 — 读写 /data/config.json."""

    def __init__(self):
        self._config: Dict[str, Any] = dict(DEFAULT_CONFIG)
        self._load()

    def _load(self):
        """从 config.json 加载配置."""
        try:
            if CONFIG_FILE.exists():
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    # 合并默认值, 保证新字段有默认值
                    merged = dict(DEFAULT_CONFIG)
                    merged.update(data)
                    self._config = merged
                    logger.info(f"已加载温控配置: {self._config}")
        except Exception as e:
            logger.warning(f"加载配置失败, 使用默认: {e}")

    def _save(self):
        """保存配置到 config.json."""
        try:
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(
                json.dumps(self._config, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception as e:
            logger.error(f"保存配置失败: {e}")

    @property
    def config(self) -> Dict[str, Any]:
        return dict(self._config)

    def update(self, **kwargs) -> Dict[str, Any]:
        """更新配置项并持久化."""
        for k, v in kwargs.items():
            if k in DEFAULT_CONFIG:
                self._config[k] = v
        self._save()
        return self.config

    def reset(self) -> Dict[str, Any]:
        """恢复默认配置."""
        self._config = dict(DEFAULT_CONFIG)
        self._save()
        return self.config

    def calc_target_speed(self, max_temp: float) -> int:
        """根据最高温计算目标转速 (线性映射).

        speed = clamp((max_temp - start) / (max - start) * 100, 0, 100)
        """
        start = self._config.get("start_temp", 35)
        max_t = self._config.get("max_temp", 60)
        if max_temp is None:
            return 0
        if max_temp <= start:
            return 0
        if max_temp >= max_t:
            return 100
        ratio = (max_temp - start) / (max_t - start)
        return int(round(ratio * 100))
