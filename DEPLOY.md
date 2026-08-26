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

<details><summary>💡 quick-start.sh 自动帮你做了什么?(三保险·比 GUI 更稳)</summary>

| 步骤 | 自动处理什么 | 避免的坑 |
|---|---|---|
| 0 | 自动要求 root 执行 | 普通用户 ajima/其他人报 docker.sock permission denied |
| 1 | 自动检查 docker 命令 + 8780 端口冲突 | 端口占用导致启动失败无提示 |
| 2 | 自动拉 Docker Hub，超时自动 Fallback 尝试 GHCR，最后兜底本地镜像 | GUI 拉取超时直接卡死 |
| 3 | 「⭐ 三保险·控制器识别」<br>**保险 A**: --privileged 特权模式<br>**保险 B**: 动态扫描宿主机真实存在的 ttyACM0~9/ttyUSB0~9，只映射真实存在的节点，不存在的完全跳过（不会因设备不存在启动失败！）<br>**保险 C**: `-v /dev:/dev-host-dev:ro` 宿主机整个 /dev 只读挂载进容器 → 兜底中的兜底！启动时没插控制器也行，之后任何时候插、任何 USB 口、任何 tty 编号、拔插随便切都能识别 | 控制器插到其他口变成 ttyACM2/ttyUSB3、启动时控制器没插导致 --device 映射失败 → 这些坑全部避免 |
| 4 | 万能参数补齐（root用户/数据卷/时区/日志轮转/健康检查 30s 宽限期）| GUI 漏勾特权模式、漏挂 /dev-host-dev、健康检查太急误判「启动失败」|
| 5 | 自动等 60 秒健康检查通过 + 验证 `/api/info` 响应 | 还没启动完就手忙脚乱点进去以为炸了 |
| 6 | 最后输出部署指南 + 日常运维命令 + 升级方法 | 不用查文档了 |
</details>

---

## ⏱️ 3 种部署方法 · 1-5 分钟搞定 (任选一种, 首推方法零)

### ⭐⭐⭐ 方法一 · NAS 纯 GUI 点鼠标 (不用传脚本, 方法零脚本上传嫌麻烦用这个)
**不用 SSH、不用传文件、全程点 NAS 浏览器管理页面。** 但我们强烈推荐**「懒人兜底填法」：设备映射列表可以完全留空！只要下面 3 个红色必填 + 2 个必挂卷做到位，三保险就能覆盖任何情况。**

| 步骤 | 操作 (以飞牛 NAS 为例, 群晖/绿联/极空间流程完全类似) |
|---|---|
| 1 | 打开 NAS Web 管理 → **容器** → **镜像仓库** (Registry) 搜索框 → 搜 `zxggh/fnsmartfan` |
| 2 | 找到 `zxggh/fnsmartfan` (Tag: `latest`, Size ~220MB) → 点右侧 **【拉取】** → 等 1-3 分钟拉完 |
| 3 | 切到 **镜像** (Images) 列表 → 点 `zxggh/fnsmartfan:latest` 这一行右侧 **【创建容器】** |
| 4 | **⭐ 懒人兜底填法（照着抄就行，漏一个红色必填直接炸）**： <ul><li>**名称**: `smartfan`</li><li>**端口映射**: 主机 `8780` → 容器 `8780` (TCP)</li><li>⚠️ **红色必填 1 · ✅ 勾特权模式 (Privileged)** <span style="color:red">(保险 A·USB 控制器 + HDD 温度必须，不勾必炸!)</span></li><li>⚠️ **红色必填 2 · 用户/UID**: `root` (或 UID=0) <span style="color:red">(避免 sg 密码提示导致容器无限重启)</span></li><li>**重启策略**: `always` (开机自启)</li><li>⚠️ **红色必填 3 · 2 个必挂卷 (漏一个 99% 功能异常!)** <br>① 新建卷名 `smartfan-data` → 容器路径 `/data` (默认「读写」, 配置持久化，删容器不丢)<br>② 宿主机路径 **`/dev`** → 容器路径 **`/dev-host-dev`** → **模式必须选 只读 (ro / Read-Only)** <span style="color:red">(保险 C·核心! HDD SMART 温度 + 任何 USB 口拔插兜底)</span></li><li>**环境变量**: `TZ` = `Asia/Shanghai` (日志/温控时区正确)</li><li>✅ **(可以完全不填!) 设备映射**: 想手动加就加 ttyACM0~9 / ttyUSB0~9；懒人直接**跳过留空就行**，上面「特权模式 + /dev→/dev-host-dev」已经兜底到任何 USB 口/任何编号了！</li></ul> |
| 5 | 点 **【创建并启动】** → **等 1~2 分钟后再刷新容器列表**（健康检查 start_period=30s，加上首次启动 pip 补装依赖，飞牛 GUI **前 90 秒会误判「启动失败」**，千万别删容器！等 90 秒一刷新就自动变「运行中（健康）」）|
| 6 | 浏览器打开 **http://你的NAS_IP:8780** → 完成 🎉 |

<details><summary>💡 常见问题: GUI 报「启动失败，查看日志」？任何口都识别不到控制器？怎么办</summary>

### ❓ GUI 先提示「启动失败」→ 90 秒后刷新变「运行中」
**99% 是 GUI 健康检查误判**：首次启动要 `pip install -r requirements.txt` 补装依赖 + 健康检查 `start_period=30s`，**前 60 秒容器健康状态是 starting，飞牛 GUI 会把 starting/健康检查中 误判成「启动失败」**，实际上容器正常在启动。
✅ 解决：**别删容器！别点停止！等 90 秒刷新容器列表页面，90% 就会自动变成「运行中（健康）」**

### ❓ 真的启动失败（容器直接退出，或者等了 2 分钟状态还是 Exited）
回到上面步骤 4 检查「懒人兜底填法」红色必填 3 项有没有做到位：
- 【特权模式】有没有打勾？
- 用户是不是填了 root（UID=0）？
- 2 个必挂卷是不是都挂了？特别是宿主机 `/dev` → 容器 `/dev-host-dev`，模式是**只读**！
如果上面 3 项有一项漏了，删容器按懒人兜底填法重来一次；或者直接换**方法零 quick-start.sh 一键脚本**（100% 参数齐全，零漏项）。

### ❓ 容器显示 running，但任何 USB 口插控制器 Web UI 里都显示「未连接」
99% 是**没勾【特权模式】或者漏挂 `/dev` → `/dev-host-dev` 只读卷**，导致容器里看不到宿主机 USB 设备节点。
✅ 快速自检（root 下跑）：
```bash
# 只要 /dev-host-dev 下面有 ttyACM*/ttyUSB*，就证明容器参数是对的
docker exec smartfan ls -l /dev-host-dev/ttyACM* /dev-host-dev/ttyUSB* 2>&1
```
如果上面命令输出还是「No such file or directory」，那就 100% 是卷没挂对 → 删容器按懒人兜底填法重来一次。
</details>

---

### ⭐⭐ 方法二 · Compose 文件一键部署 (推荐, 适合所有 NAS)
适合想一键启停/备份配置、或者 NAS 有 Compose/Stack 菜单的用户。**本方法已内置「三保险」：devices 列表扩展到 ttyACM0~9 + ttyUSB0~9（Compose 自动跳过不存在的节点，不会报错！）+ 特权模式 + /dev-host-dev 兜底。**

1. 下载仓库根目录的 **[docker-compose.remote.yml](./docker-compose.remote.yml)** 到本地电脑
2. 打开 NAS Web 管理 → **容器** → **Compose / Stack** → **【+ 创建项目】**
3. 填写:
   - 项目名称: `smartfan`
   - 路径: 随便选一个你可读写的共享文件夹 (例如 `/vol1/1000/个人/smart fan`)
   - 上传模式: 上传刚才下载的 `docker-compose.remote.yml`
4. 点 **【确定】** → NAS 会自动 `docker pull zxggh/fnsmartfan:latest` → 创建卷 → 启动容器
5. 浏览器打开 **http://NAS_IP:8780** → 完成 🎉

<details><summary>💡 想命令行跑? 直接复制下面这个「三保险完整版」docker-compose.yml 也行（0~9 全覆盖）</summary>

```yaml
services:
  smartfan:
    container_name: smartfan
    image: zxggh/fnsmartfan:latest
    restart: always
    ports:
      - "8780:8780"
    # 保险 A: 特权模式 (给容器完整权限访问宿主机 /dev)
    privileged: true
    # 保险 B: ttyACM0~9 + ttyUSB0~9 全覆盖
    #         Docker Compose 自动跳过不存在的节点, 不会报错!
    devices:
      - "/dev/ttyACM0:/dev/ttyACM0"
      - "/dev/ttyACM1:/dev/ttyACM1"
      - "/dev/ttyACM2:/dev/ttyACM2"
      - "/dev/ttyACM3:/dev/ttyACM3"
      - "/dev/ttyACM4:/dev/ttyACM4"
      - "/dev/ttyACM5:/dev/ttyACM5"
      - "/dev/ttyACM6:/dev/ttyACM6"
      - "/dev/ttyACM7:/dev/ttyACM7"
      - "/dev/ttyACM8:/dev/ttyACM8"
      - "/dev/ttyACM9:/dev/ttyACM9"
      - "/dev/ttyUSB0:/dev/ttyUSB0"
      - "/dev/ttyUSB1:/dev/ttyUSB1"
      - "/dev/ttyUSB2:/dev/ttyUSB2"
      - "/dev/ttyUSB3:/dev/ttyUSB3"
      - "/dev/ttyUSB4:/dev/ttyUSB4"
      - "/dev/ttyUSB5:/dev/ttyUSB5"
      - "/dev/ttyUSB6:/dev/ttyUSB6"
      - "/dev/ttyUSB7:/dev/ttyUSB7"
      - "/dev/ttyUSB8:/dev/ttyUSB8"
      - "/dev/ttyUSB9:/dev/ttyUSB9"
    volumes:
      - smartfan-data:/data
      # 保险 C · 必挂核心: 宿主机 /dev 整卷只读挂载进容器 (兜底中的兜底)
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
