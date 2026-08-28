# SmartFan 智能风扇控制器 — 部署文档 (v6 最新)

> 适用版本：≥ commit `c9d5716`（含 NAS 部署坑修复 + 温度历史功能）  
> 控制器固件：≥ v6（USB CDC 死锁检测 + NTC 温度基准同步）

---

## 🎯 部署方式推荐顺序（从稳 → 折腾）

| 排名 | 方式 | 难度 | 推荐度 | 适用场景 |
|:---:|---|---|:---:|---|
| 🥇 1 | **Docker Hub 拉取 + Shell 一键脚本** | ⭐ | ⭐⭐⭐⭐⭐ | 绝大多数用户，飞牛/群晖 NAS，零配置 1 分钟好 |
| 🥈 2 | **Docker Hub 拉取 + Docker Compose** | ⭐⭐ | ⭐⭐⭐⭐ | 喜欢 Compose 管理多容器的用户 |
| 🥉 3 | **本地构建镜像 (build-on-nas.sh)** | ⭐⭐⭐ | ⭐⭐⭐ | Docker Hub 访问受限 / 需要修改源码后自建镜像 |
| 4 | **裸机脚本安装 (smartfan-install.sh)** | ⭐⭐⭐ | ⭐⭐ | 非 Docker 环境，物理机/虚拟机 Linux 部署 |
| ❌ | **飞牛 NAS GUI 手动创建容器** | ⭐⭐ | ⭐ | **不推荐** — 有两大坑（健康检查误判 + 参数漏填），见文末排错 |

---

## 🥇 方式 1：Docker Hub 拉取（99% 用户首选）

镜像地址：[`zxggh/fnsmartfan:latest`](https://hub.docker.com/r/zxggh/fnsmartfan)（自动支持 `linux/amd64` + `linux/arm64` 双架构，飞牛 x86/ARM NAS 通吃）

### 方式 1-A：Shell 一键脚本（最稳，强烈推荐）
SSH 登录飞牛 NAS → **切 root** → 执行**一条命令**（自动清旧容器 + 拉镜像 + 正确参数 + 状态轮询提示）：

```bash
su - root
bash <(curl -s https://raw.githubusercontent.com/zxggh/fnsmartfan/main/deploy-on-fnnas.sh)
```

脚本会自动：
1. 验证必须是 root 用户执行
2. 删除同名旧容器
3. 拉取 `zxggh/fnsmartfan:latest`
4. 自动创建 `smartfan-data` 持久化卷
5. 启动容器（100% 正确参数：`--user root --privileged -p 8780:8780 -v smartfan-data:/data -v /dev:/dev:rw -e TZ=Asia/Shanghai`）
6. 2 分钟内每 5 秒轮询健康状态，**健康后打印中文成功提示 + 访问 URL**

<details>
<summary>如果飞牛 NAS 访问 GitHub raw 慢 → 手动执行脚本文件</summary>

本地下载 `deploy-on-fnnas.sh` 上传到 NAS 后执行：
```bash
su - root
chmod +x deploy-on-fnnas.sh
./deploy-on-fnnas.sh
```
</details>

---

### 方式 1-B：Docker Compose（喜欢 Compose 的用户）

把仓库根目录的 **`docker-compose-offline.yml`** 上传到 NAS 任意目录（例如 `/vol1/1000/docker/smartfan-fix/`），执行：

```bash
su - root
cd /你的目录
# 先拉最新镜像 + 后台启动
docker compose -f docker-compose-offline.yml pull
docker compose -f docker-compose-offline.yml up -d

# 查看状态（等 30~90 秒健康检查）
docker compose -f docker-compose-offline.yml ps
```

Compose 文件里已经包含**所有正确参数**：
- `user: root` + `privileged: true`
- 4 个常用串口 `--device` 兜底（ttyUSB0/1、ttyACM0/1）+ `/dev` 全映射
- `smartfan-data` 卷自动创建（持久化配置 + 温度历史）
- 健康检查 `start-period: 90s`（不会被 GUI 误判）
- 日志 `10m × 5 文件` 限制（不撑爆 NAS 硬盘）

---

### 方式 1-C：一行版 docker run（高级用户，零续行零反斜杠）

SSH 粘贴单条命令，直接跑：

```bash
su - root
docker rm -f smartfan 2>/dev/null; docker run -d --name smartfan --user root --privileged --restart always -p 8780:8780 -v smartfan-data:/data -v /dev:/dev:rw -e TZ=Asia/Shanghai --log-driver json-file --log-opt max-size=10m --log-opt max-file=5 zxggh/fnsmartfan:latest
```

然后检查状态：
```bash
sleep 8; docker ps -a | grep smartfan
```

---

### ❌ 不推荐：飞牛 NAS GUI 手动创建容器（两大坑必须绕过）

如果你一定要在图形界面创建：

| 必须设置项 | 值 | 为什么必须 |
|---|---|---|
| **用户** | **root** | NAS 串口访问 + `/data` 卷写入必须 |
| **特权模式 / 最高权限** | ✅ 勾选 | 自动获得所有 `/dev/ttyUSB*` 设备权限 |
| 端口映射 | 本地 `8780` → 容器 `8780` TCP | Web 控制台端口 |
| 存储卷 1 | 命名卷 `smartfan-data` → 容器 `/data` | 配置文件 + 温度历史 JSONL 持久化 |
| 存储卷 2 | 宿主机 `/dev` → 容器 `/dev`（读写） | 控制器 USB 串口访问 |
| 环境变量 | `TZ=Asia/Shanghai` | 曲线 X 轴时间正确 |
| 重启策略 | Always | NAS 重启后自动拉起 |

⚠️ **飞牛 GUI 健康检查误判坑**：容器启动后 GUI 可能显示「启动失败，请查看运行日志」**千万不要删除容器**！首次启动需要 pip install 补装依赖约 30~60 秒，健康检查宽限期 90 秒，等 1~2 分钟**刷新容器列表页面**，99% 会自动变为「运行中（健康）」。

---

## 🥈 方式 2：NAS 本地构建镜像（Docker Hub 访问受限时用）

适用于：NAS 在纯内网环境、Docker Hub 访问太慢/被屏蔽、自己修改了源码想直接打包。

**步骤**：
1. 上传仓库根目录的 `build-on-nas.sh` 和 `Dockerfile` + `smartfan-install.sh` 到 NAS 同一目录
2. 执行：
   ```bash
   su - root
   chmod +x build-on-nas.sh
   ./build-on-nas.sh
   ```
3. 构建完用上面的「方式 1-C 一行版 docker run」启动（把镜像名从 `zxggh/fnsmartfan:latest` 改成你本地构建的 tag 即可）

---

## 🥉 方式 3：裸机脚本安装 smartfan-install.sh（非 Docker）

适用于：物理机 Linux / LXC 容器 / 不想用 Docker 的虚拟机。

```bash
su - root
wget https://raw.githubusercontent.com/zxggh/fnsmartfan/main/smartfan-install.sh -O /tmp/install.sh
chmod +x /tmp/install.sh
bash /tmp/install.sh
```

脚本会自动：
- 把所有程序文件释放到 `/opt/fan-controller/`
- 创建 Python venv + 安装依赖
- 注册 systemd 服务 `fan-controller.service` + 开机自启
- 安装 smartmontools（HDD 温度采集）

访问：`http://服务器IP:8780`

---

## ✅ 验证部署成功（三条命令）

```bash
# 1. 容器状态：看到 Up (healthy) 就成功
docker ps -a --filter name=smartfan

# 2. 启动日志环境自检：3 项 OK（UID=0 / 有串口 / /data 可写）
docker logs smartfan 2>&1 | grep -E "环境自检|UID|串口|写入权限|deps ok|Uvicorn running"

# 3. 页面返回 JSON（不打命令直接浏览器打开也 OK）
curl -s http://127.0.0.1:8780/api/info | python3 -m json.tool
```

打开浏览器 → **http://你的NAS-IP:8780** → 看到温控仪表盘就全部 OK。

---

## ⚠️ 飞牛 NAS 常见坑 & 排错清单（v6 已内置防护，但仍可能遇到）

| # | 坑 | 现象 | 解决方法 |
|---|---|---|---|
| 1 | **GUI 健康检查误判「启动失败」** | 创建后 GUI 弹窗失败，但容器仍在运行中 | ❌ 不要删容器！等 1~2 分钟刷新「容器列表」，自动变为运行中（健康）。v6 已把 start-period 从 10s 拉到 90s，大幅降低误判率。 |
| 2 | **参数漏填导致启动失败/无限重启** | 容器启动后 1 秒退出 / 重启循环 | 改用 🥇 Shell 一键脚本，所有参数全自动配齐，不会漏填。v6 镜像启动时还会打印环境自检 WARNING：非 root / 无串口 / 卷无写权限。 |
| 3 | **PermissionError: /data/temp_history.jsonl** | 容器日志里报权限不足，服务起不来 | 两种办法：① 执行 `chmod -R 777 /vol1/docker/volumes/smartfan-data/_data`；② 确保启动时加 `--user root`（一键脚本已包含）。v6 代码层即使失败也自动降级内存模式，不崩服务。 |
| 4 | **端口 8780 被占用** | `docker run` 报 "port is already allocated" | 查占用：`netstat -tlnp \| grep 8780`，停掉占用程序，或把本地端口改成别的（例如 `-p 8781:8780`）。 |
| 5 | **控制器串口未识别** | 日志里「检测到的串口设备数：0」 | ① 确认 USB 线插好了，重新插拔控制器；② 确保 `--privileged` 已开启 + `-v /dev:/dev:rw`（一键脚本已包含）；③ NAS 执行 `ls /dev/ttyUSB* /dev/ttyACM*` 看设备文件是否存在。 |
| 6 | **Docker Hub 拉取太慢 / 超时** | `docker pull` 长时间卡住 | 飞牛「容器管理 → 设置 → 镜像加速」打开国内加速器（阿里云、网易等）；或用 🥈 本地构建方式。 |
| 7 | **容器删不掉 (No such container / name conflict)** | `docker rm` 失败 / 名字冲突 | 执行：`docker rm -f $(docker ps -aq --filter name=smartfan) 2>/dev/null; docker container prune -f`；仍不行就 `systemctl restart docker` 清僵尸。 |

---

## 🔧 日常维护命令

```bash
# 查看最新 50 行日志
docker logs --tail 50 smartfan

# 实时跟随日志（排障用）
docker logs -f smartfan

# 重启服务（改完配置后）
docker restart smartfan

# 停 + 删容器（保留数据卷 smartfan-data）
docker rm -f smartfan

# 升级到最新镜像（保留数据）
docker pull zxggh/fnsmartfan:latest
docker rm -f smartfan
# 然后用「方式 1-A / 1-C」重新启动（数据卷 smartfan-data 会保留配置和温度历史）

# 备份温度历史和配置（复制到 ~/smartfan-backup-日期.tar.gz）
docker run --rm -v smartfan-data:/data -v ~:/backup alpine tar czf /backup/smartfan-backup-$(date +%Y%m%d).tar.gz -C /data .
```

---

## 📂 数据目录结构（smartfan-data 卷）

容器内 `/data` 对应卷 `smartfan-data`，NAS 宿主机路径：
```
/vol1/docker/volumes/smartfan-data/_data/
├── config.yaml              # 温控阈值 / 串口 / 日志 配置
├── temp_history.jsonl       # 温度历史持久化 (JSON Lines, 7 天自动保留)
├── auto_cmd_state.json      # 自动命令开关持久化状态
└── disconnect_log.jsonl     # 控制器断连日志（排障用）
```

**升级 / 重建容器完全不影响这些文件**，所有配置和温度历史都会保留。
