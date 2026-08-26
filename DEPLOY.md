# SmartFan 智能风扇控制器 · 公共部署指南

> 默认镜像: **`zxggh/fnsmartfan:latest`** (Docker Hub 公共仓库, **匿名拉取, 无需 PAT/登录, 所有 NAS 品牌通用**)
>
> 备用镜像: `ghcr.io/zxggh/fnsmartfan:latest` (GHCR, 国内部分 NAS 可切换到此)
>
> 锁死版本: `zxggh/fnsmartfan:2.2.0` (每次升级需手动改 tag, 适合生产环境求稳)

---

## 🚀 方法零 · 一键脚本部署 (⭐⭐⭐⭐⭐ 强烈推荐, 100% 不踩坑, 万能启动参数)
> **完美解决飞牛 NAS GUI「启动失败误判 / 参数漏填」问题，脚本内置命令行 100% 成功的「万能启动参数」**，**不需要懂 docker 命令，不需要手动填任何参数**。

1. **下载一键脚本**：下载仓库根目录的 **[quick-start.sh](./quick-start.sh)** 到本地电脑
2. **上传到 NAS**：把 `quick-start.sh` 上传到 NAS 任意共享文件夹（例如 `/vol1/1000/个人/smartfan`）
3. **SSH 登录 NAS（或 webSSH）** → 执行：
   ```bash
   # ① 切到 root (必须, 普通用户没 docker 权限)
   su - root
   #    输入你的 root 密码, 提示符变成 root@xxx:~# 就对了

   # ② 进入你放脚本的目录 (就是刚才上传 quick-start.sh 那个共享文件夹路径)
   cd "/vol1/1000/个人/smartfan"     ← 改这里, 用你自己的真实路径

   # ③ 一键执行 (自动完成: 检查环境 → 拉镜像 → 清旧容器 → 万能参数启动 → 健康验证)
   bash quick-start.sh
   ```
4. **等 1~3 分钟**，脚本最后会输出 🎉 部署完成 + 浏览器访问地址 `http://NAS_IP:8780`，复制打开即可使用！

<details><summary>💡 quick-start.sh 自动帮你做了什么?(比 GUI 更稳)</summary>

| 步骤 | 自动处理什么 | 避免的坑 |
|---|---|---|
| 0 | 自动要求 root 执行 | 普通用户 ajima/其他人报 docker.sock permission denied |
| 1 | 自动检查 docker 命令 + 8780 端口冲突 | 端口占用导致启动失败无提示 |
| 2 | 自动拉 Docker Hub，超时自动 Fallback 尝试 GHCR，最后兜底本地镜像 | GUI 拉取超时直接卡死 |
| 3 | 自动删除同名旧容器 + **万能启动参数**（特权/root/卷/设备映射/时区/日志轮转/健康检查 30s 宽限期）| GUI 漏勾「特权模式」、漏挂 `/dev-host-dev`、健康检查太急误判「启动失败」|
| 4 | 自动等 60 秒健康检查通过 + 验证 `/api/info` 响应 | 还没启动完就手忙脚乱点进去以为炸了 |
| 5 | 最后输出部署指南 + 日常运维命令 + 升级方法 | 不用查文档了 |
</details>

---

## ⏱️ 3 种部署方法 · 1-5 分钟搞定 (任选一种, 首推方法零)

### ⭐⭐⭐ 方法一 · NAS 纯 GUI 点鼠标 (不用传脚本, 方法零脚本上传嫌麻烦用这个)
**不用 SSH、不用传文件、全程点 NAS 浏览器管理页面。但请严格按下面表格参数填，漏项会踩坑。**

| 步骤 | 操作 (以飞牛 NAS 为例, 群晖/绿联/极空间流程完全类似) |
|---|---|
| 1 | 打开 NAS Web 管理 → **容器** → **镜像仓库** (Registry) 搜索框 → 搜 `zxggh/fnsmartfan` |
| 2 | 找到 `zxggh/fnsmartfan` (Tag: `latest`, Size ~220MB) → 点右侧 **【拉取】** → 等 1-3 分钟拉完 |
| 3 | 切到 **镜像** (Images) 列表 → 点 `zxggh/fnsmartfan:latest` 这一行右侧 **【创建容器】** |
| 4 | **参数严格照抄（3 个红色必填 ⚠️ 必填，漏一个直接启动失败）**： <ul><li>**名称**: `smartfan`</li><li>**端口映射**: 主机 `8780` → 容器 `8780` (TCP)</li><li>⚠️ **必填·✅ 勾特权模式 (Privileged)** <span style="color:red">(USB 控制器 + HDD 温度 必须, 不勾必炸)</span></li><li>⚠️ **必填·用户/UID**: `root` (或 UID=0) <span style="color:red">(避免 sg 密码提示导致容器无限重启)</span></li><li>**重启策略**: `always` (开机自启)</li><li>⚠️ **必填·卷 (Volumes)** <span style="color:red">(漏挂配置/HDD 温度不显示)</span>： <br>① 新建卷名 `smartfan-data` → 挂载到容器路径 `/data` (配置持久化，删容器不丢)<br>② 挂载宿主机路径 `/dev` → 容器路径 `/dev-host-dev`，**模式只读 (ro/Read-Only)** (读 HDD SMART 温度必须)</li><li>**环境变量**: `TZ` = `Asia/Shanghai` (日志/温控时区正确)</li><li>(可选) **设备映射**: `/dev/ttyACM0`→`/dev/ttyACM0`，同理 `ttyACM1`、`ttyUSB0`、`ttyUSB1`（没有设备映射入口就跳过，特权模式 + 上面 /dev 卷已兜底拔插识别）</li></ul> |
| 5 | 点 **【创建并启动】** → **等 1~2 分钟后再刷新容器列表**（健康检查 start_period=30s，飞牛 GUI 前 30s 会误判「启动失败」，别删容器，等一下自动变「运行中（健康）」）|
| 6 | 浏览器打开 **http://你的NAS_IP:8780** → 完成 🎉 |

<details><summary>💡 常见问题: GUI 报「启动失败，查看日志」？怎么办</summary>

**90% 不是真的启动失败，是以下 2 种误判：**
1. **飞牛健康检查太急躁**：首次启动要 `pip install -r requirements.txt` 补装依赖 + 健康检查有 30 秒宽限期，**前 1 分钟健康检查显示 starting / unhealthy，GUI 就会误判成失败**。解决：**别删容器！等 90 秒再刷新容器列表页面，90% 会自动变成「运行中（健康）」**
2. **参数漏填**（真的启动失败）：回到步骤 4 检查 3 个红色必填项（特权模式？用户 root？挂了 smartfan-data + /dev → /dev-host-dev 只读？）

如果上面都做到了 2 分钟后还是失败，**直接换「方法零·一键脚本部署」**，脚本内置万能参数+健康检查宽限，100% 一次成功。
</details>

---

### ⭐⭐ 方法二 · Compose 文件一键部署 (推荐, 适合所有 NAS)
适合想一键启停/备份配置、或者 NAS 有 Compose/Stack 菜单的用户。

1. 下载仓库根目录的 **[docker-compose.remote.yml](./docker-compose.remote.yml)** 到本地电脑
2. 打开 NAS Web 管理 → **容器** → **Compose / Stack** → **【+ 创建项目】**
3. 填写:
   - 项目名称: `smartfan`
   - 路径: 随便选一个你可读写的共享文件夹 (例如 `/vol1/1000/个人/smart fan`)
   - 上传模式: 上传刚才下载的 `docker-compose.remote.yml`
4. 点 **【确定】** → NAS 会自动 `docker pull zxggh/fnsmartfan:latest` → 创建卷 → 启动容器
5. 浏览器打开 **http://NAS_IP:8780** → 完成 🎉

<details><summary>💡 想命令行跑? 直接复制下面这个最小版 docker-compose.yml 也行</summary>

```yaml
services:
  smartfan:
    container_name: smartfan
    image: zxggh/fnsmartfan:latest
    restart: always
    ports:
      - "8780:8780"
    privileged: true
    devices:
      - "/dev/ttyACM0:/dev/ttyACM0"
      - "/dev/ttyACM1:/dev/ttyACM1"
      - "/dev/ttyUSB0:/dev/ttyUSB0"
      - "/dev/ttyUSB1:/dev/ttyUSB1"
    volumes:
      - smartfan-data:/data
      - /dev:/dev-host-dev:ro
    user: root
    environment:
      - TZ=Asia/Shanghai
    healthcheck:
      test: ["CMD", "curl", "-s", "-m", "3", "http://127.0.0.1:8780/api/info"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "5"
volumes:
  smartfan-data:
    driver: local
```
保存成 `docker-compose.yml`，目录里执行 `docker compose up -d` 就行。
</details>

---

### ⭐⭐⭐ 方法三 · 离线部署 (NAS 完全没外网 / Docker Hub 拉不下来)
在一台**能联网的 Windows / macOS / Linux 电脑**上把镜像导出成 tar，拷到 NAS 导入：

```bash
# ========== 能联网的电脑上执行 ==========
# 1. 拉镜像
docker pull zxggh/fnsmartfan:latest

# 2. 导出成 tar (约 220MB)
docker save zxggh/fnsmartfan:latest -o fnsmartfan-latest.tar

# ========== 拷 fnsmartfan-latest.tar 到 NAS ==========
# 在 NAS Web 管理 → 容器 → 镜像 → 【导入镜像】 → 选这个 tar 导入
# 导入成功后镜像会显示: zxggh/fnsmartfan:latest

# ========== 之后就按方法一 GUI 创建容器, 或方法二 compose up 即可 ==========
```

---

## ✅ 部署后验证清单 (浏览器打开 Web UI 后检查 4 件事)
| 检查项 | 预期结果 | 不通过时检查 |
|---|---|---|
| ① 系统状态卡片 | 显示 **CPU/HDD 温度有数值** (不是 N/A / `--`) | 首次启动等 15 秒自动采集; 确认勾选了**特权模式**、挂了 `/dev` → `/dev-host-dev` **只读** |
| ② 控制器连接状态 | 显示 **已连接 (绿色)** | 确认 USB CDC 控制器插入; 检查端口是否 `/dev/ttyACM0` |
| ③ 「🖥️ 原始命令」卡顶 | 有绿色 **『⏸ 已启用』** 按钮 (v2.2 新增开关，点一下可切红色『▶ 已停用』停止自动下发) | 没这按钮说明拉到了旧镜像，重拉 `docker pull zxggh/fnsmartfan:latest` |
| ④ 手动发命令测试 | 在原始命令输入框输 **`F1CPD=30`** 回车 → 风扇1 进度条立刻变 30%; 再输 **`F1CPD=0`** 立刻 0% | 进度条未立即更新不影响核心功能 (温控自动下发始终正常)，后续版本会修复 |

---

## 🛠️ 日常运维命令 (懂 SSH 的话, 不懂就用 GUI 重启就行)
```bash
# 重启容器
docker restart smartfan

# 看最近 50 行日志 (排查控制器断连/温控)
docker logs --tail 50 -f smartfan

# 升级到最新镜像 (永久保留配置卷 smartfan-data, 不会丢)
docker pull zxggh/fnsmartfan:latest
docker rm -f smartfan
# 然后按"方法一"重新创建容器即可 (compose 用户直接 docker compose up -d)

# 备份配置 (config.yaml) 到本地
docker cp smartfan:/app/config.yaml ./smartfan-config-backup.yaml

# 完全卸载 (⚠️ 最后那条会删除配置! 慎用)
docker rm -f smartfan
docker rmi zxggh/fnsmartfan:latest
docker volume rm smartfan-data   # ⚠️ 这行才是真的删 config.yaml
```

---

## 🆗 已预装依赖 (容器镜像已自带, 不需要你再 apt install / pip install)
- ✅ `smartmontools` → HDD/SSD SMART 温度检测
- ✅ `python3-pip` → Python 运行环境 (pyyaml / fastapi / uvicorn 等按需自动补装)
- ✅ `curl` / `tzdata` / `ca-certificates` → Web 健康检查 + 时区 + HTTPS CA
