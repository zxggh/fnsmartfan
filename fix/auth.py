"""
用户认证模块 (JWT + bcrypt)

功能:
  - users.json 持久化用户 (bcrypt 哈希存储, cost>=10)
  - 首次启动自动初始化 admin/admin
  - JWT 签发 (7 天有效期) + 校验
  - 防暴力破解: 连续 3 次失败后延迟 5 秒
  - 修改密码后所有 JWT 立即失效 (版本号机制)
  - 登出 (前端清 token 即可, 后端可选黑名单)

JWT 结构:
  payload: { "sub": "admin", "iat": ts, "exp": ts+7d, "ver": 1 }
  ver 字段 = 用户密码版本号, 改密后 ver+1, 旧 token 校验失败.
"""

import json
import time
import hmac
import hashlib
import base64
import logging
import os
from pathlib import Path
from typing import Optional, Dict, Any

logger = logging.getLogger("fan_ctrl.auth")

# 用户数据文件 (放在 /data 卷, 容器更新不丢失)
USERS_FILE = Path("/data/users.json")
# JWT 密钥 (首次启动随机生成, 存 /data 卷; 容器重启不丢)
SECRET_FILE = Path("/data/.jwt_secret")
# JWT 有效期: 7 天
JWT_EXPIRE_SECONDS = 7 * 24 * 3600
# bcrypt cost 因子 (>= 10)
BCRYPT_COST = 12
# 防暴力破解: 失败阈值 + 延迟秒数
MAX_FAILS = 3
FAIL_DELAY_S = 5


class AuthManager:
    """认证管理器 — 用户管理 + JWT 签发校验."""

    def __init__(self):
        self._users: Dict[str, Dict[str, Any]] = {}
        self._secret: str = ""
        self._fail_count: Dict[str, int] = {}   # 用户名 → 连续失败次数
        self._lock_until: Dict[str, float] = {}  # 用户名 → 解锁时间戳
        self._load_or_init()

    # ============================================================
    #  初始化 / 持久化
    # ============================================================
    def _load_or_init(self):
        """加载用户数据 + JWT 密钥; 首次启动自动初始化."""
        # 确保 /data 目录存在
        try:
            USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        # 1. JWT 密钥
        if SECRET_FILE.exists():
            try:
                self._secret = SECRET_FILE.read_text(encoding="utf-8").strip()
            except Exception as e:
                logger.warning(f"读取 JWT 密钥失败: {e}")
        if not self._secret:
            # 生成 32 字节随机密钥并持久化
            self._secret = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
            try:
                SECRET_FILE.write_text(self._secret, encoding="utf-8")
                os.chmod(SECRET_FILE, 0o600)
                logger.info("已生成新的 JWT 密钥并存盘")
            except Exception as e:
                logger.warning(f"保存 JWT 密钥失败: {e}")

        # 2. 用户数据
        if USERS_FILE.exists():
            try:
                self._users = json.loads(USERS_FILE.read_text(encoding="utf-8"))
                logger.info(f"已加载 {len(self._users)} 个用户")
            except Exception as e:
                logger.warning(f"加载 users.json 失败, 重新初始化: {e}")
                self._users = {}
        if not self._users:
            # 首次启动: 创建 admin/admin
            self._create_user("admin", "admin")
            logger.info("首次启动: 已创建默认用户 admin/admin")

    def _save_users(self):
        """保存用户数据到 users.json."""
        try:
            USERS_FILE.write_text(
                json.dumps(self._users, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception as e:
            logger.error(f"保存 users.json 失败: {e}")

    # ============================================================
    #  密码哈希 (bcrypt)
    # ============================================================
    def _hash_password(self, password: str) -> str:
        """用 bcrypt 哈希密码 (cost=12)."""
        try:
            import bcrypt
            hashed = bcrypt.hashpw(
                password.encode("utf-8"),
                bcrypt.gensalt(rounds=BCRYPT_COST)
            )
            return hashed.decode("utf-8")
        except ImportError:
            # 兜底: 容器未装 bcrypt 时用 PBKDF2 (不推荐, 但保证能跑)
            logger.warning("bcrypt 未安装, 降级使用 PBKDF2 哈希 (安全性降低)")
            salt = os.urandom(16)
            dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100000)
            return "pbkdf2$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()

    def _verify_password(self, password: str, hashed: str) -> bool:
        """验证密码."""
        if hashed.startswith("pbkdf2$"):
            try:
                _, salt_b64, dk_b64 = hashed.split("$")
                salt = base64.b64decode(salt_b64)
                dk = base64.b64decode(dk_b64)
                check = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100000)
                return hmac.compare_digest(check, dk)
            except Exception:
                return False
        try:
            import bcrypt
            return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
        except ImportError:
            return False
        except Exception:
            return False

    # ============================================================
    #  用户管理
    # ============================================================
    def _create_user(self, username: str, password: str):
        """创建用户 (内部用, 首次初始化)."""
        self._users[username] = {
            "password_hash": self._hash_password(password),
            "password_version": 1,   # 改密时 +1, 使旧 JWT 失效
            "created_at": int(time.time()),
        }
        self._save_users()

    def change_password(self, username: str, old_password: str, new_password: str) -> bool:
        """修改密码. 成功后 password_version+1, 所有旧 JWT 失效."""
        user = self._users.get(username)
        if not user:
            return False
        if not self._verify_password(old_password, user["password_hash"]):
            return False
        user["password_hash"] = self._hash_password(new_password)
        user["password_version"] = user.get("password_version", 1) + 1
        self._save_users()
        logger.info(f"用户 {username} 密码已修改, 版本号升至 {user['password_version']}")
        return True

    def get_password_version(self, username: str) -> int:
        """获取用户密码版本号 (改密后递增)."""
        user = self._users.get(username)
        if not user:
            return 0
        return user.get("password_version", 1)

    # ============================================================
    #  登录 + 防暴力破解
    # ============================================================
    def login(self, username: str, password: str) -> Optional[str]:
        """登录验证. 成功返回 JWT, 失败返回 None.

        防暴力破解: 连续 MAX_FAILS 次失败后, 该账号锁定 FAIL_DELAY_S 秒.
        """
        # 检查账号锁定
        now = time.time()
        lock_until = self._lock_until.get(username, 0)
        if now < lock_until:
            remaining = int(lock_until - now)
            logger.info(f"账号 {username} 已锁定, 还需 {remaining}s")
            return None

        user = self._users.get(username)
        if not user or not self._verify_password(password, user["password_hash"]):
            # 登录失败
            self._fail_count[username] = self._fail_count.get(username, 0) + 1
            if self._fail_count[username] >= MAX_FAILS:
                self._lock_until[username] = now + FAIL_DELAY_S
                logger.warning(f"账号 {username} 连续 {MAX_FAILS} 次登录失败, 锁定 {FAIL_DELAY_S}s")
                self._fail_count[username] = 0
            return None

        # 登录成功, 清零失败计数
        self._fail_count[username] = 0
        self._lock_until.pop(username, None)
        token = self._create_token(username)
        logger.info(f"用户 {username} 登录成功")
        return token

    def _create_token(self, username: str) -> str:
        """签发 JWT (HS256, 7 天有效期, 含密码版本号)."""
        now = int(time.time())
        payload = {
            "sub": username,
            "iat": now,
            "exp": now + JWT_EXPIRE_SECONDS,
            "ver": self.get_password_version(username),
        }
        return self._jwt_encode(payload)

    def verify_token(self, token: str) -> Optional[str]:
        """校验 JWT. 成功返回用户名, 失败返回 None."""
        payload = self._jwt_decode(token)
        if not payload:
            return None
        # 检查过期
        if payload.get("exp", 0) < int(time.time()):
            return None
        # 检查密码版本号 (改密后旧 token 失效)
        username = payload.get("sub")
        if not username:
            return None
        current_ver = self.get_password_version(username)
        if payload.get("ver", 0) != current_ver:
            return None
        return username

    # ============================================================
    #  JWT 编解码 (纯 Python 实现, 不依赖 pyjwt)
    # ============================================================
    def _b64url_encode(self, data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    def _b64url_decode(self, data: str) -> bytes:
        padding = "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(data + padding)

    def _jwt_encode(self, payload: Dict[str, Any]) -> str:
        """JWT HS256 编码."""
        header = {"alg": "HS256", "typ": "JWT"}
        header_b64 = self._b64url_encode(json.dumps(header, separators=(",", ":")).encode())
        payload_b64 = self._b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
        signing_input = f"{header_b64}.{payload_b64}".encode()
        signature = hmac.new(
            self._secret.encode("utf-8"),
            signing_input,
            hashlib.sha256
        ).digest()
        sig_b64 = self._b64url_encode(signature)
        return f"{header_b64}.{payload_b64}.{sig_b64}"

    def _jwt_decode(self, token: str) -> Optional[Dict[str, Any]]:
        """JWT HS256 解码 + 签名校验."""
        try:
            parts = token.split(".")
            if len(parts) != 3:
                return None
            header_b64, payload_b64, sig_b64 = parts
            signing_input = f"{header_b64}.{payload_b64}".encode()
            expected_sig = hmac.new(
                self._secret.encode("utf-8"),
                signing_input,
                hashlib.sha256
            ).digest()
            actual_sig = self._b64url_decode(sig_b64)
            if not hmac.compare_digest(expected_sig, actual_sig):
                return None
            payload = json.loads(self._b64url_decode(payload_b64))
            return payload
        except Exception:
            return None


# 全局单例 (main.py 中初始化)
auth_manager: Optional[AuthManager] = None


def init_auth() -> AuthManager:
    """初始化全局认证管理器."""
    global auth_manager
    auth_manager = AuthManager()
    return auth_manager
