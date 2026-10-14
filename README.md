# 私有远程桌面套件（基于 RustDesk 二次开发）

一套面向**团队内部使用**的远程桌面私有化方案：自建服务器保证数据不外流，定制客户端让员工免配置直连。

---

## 一、这套东西解决什么问题

直接用官方 RustDesk 客户端，团队会遇到三个麻烦：

| 问题 | 后果 | 本方案的解法 |
|---|---|---|
| 默认连官方服务器 `rs-ny.rustdesk.com` | 员工忘记配置 → 流量走公网，违反数据不出内网要求 | 编译期把服务器改成你的，忘记配置也不会漏出去 |
| 每个员工手动填服务器地址+公钥 | 几十台机器配置成本极高，还容易填错 | 客户端开箱即连，界面里什么都不用填 |
| 品牌是 RustDesk、链接指向官网 | 内部推广时像是第三方工具 | 改成你自己的产品名与官网 |

**关键在于：改动的是"兜底默认值"，不是硬编码锁定。** RustDesk 解析服务器地址的优先级是：

```
命令行参数 > 用户界面设置的 custom-rendezvous-server > 编译注入 > 本地配置 > 兜底常量 ← 我们改这里
```

好处是员工（或你）仍可在界面里临时切服务器做调试，但默认值永远指向你的私有服务器。

---

## 二、目录说明

```
rd-suite/
├── server/                  自建服务器端
│   ├── docker-compose.yml   hbbs(ID/注册) + hbbr(中继)
│   ├── .env.example         配置项模板，复制为 .env 后填写
│   └── rdctl.py             跨平台管理工具（启停/查公钥/备份）
│
├── build/                   客户端定制与构建
│   ├── custom.toml          ★ 唯一需要你改的配置文件
│   ├── customize.py         源码注入器（本地 source 定制用）
│   ├── patch-ci.py          CI 补丁工具（GitHub Actions 云编译用）
│   └── branding/            放你的图标、logo（替换客户端图标）
│
└── docs/
    ├── ARCHITECTURE.md      定制点原理图 —— 想改更多东西请看这里
    └── IMPLEMENTATION.md    分阶段落地路线图
```

---

## 三、快速开始（推荐路线：GitHub Actions 云编译）

你本机没有 Rust / Flutter / vcpkg 工具链，**在 Windows 上配齐这套环境要数小时且极易失败**。
用 GitHub Actions 免费云编译是更靠谱的选择——浏览器点几下就能拿到 exe。

### 第 1 步：起服务器（`server/`）

```bash
cd server
cp .env.example .env          # 编辑 .env，把 RELAY_HOST 改成你的 IP/域名
python rdctl.py start
```

首次启动成功后会打印**服务器公钥**，类似这样：

```
[OK] 服务器就绪。以下是你的服务器公钥，客户端定制时需要写入：

    GbRXxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxS8=
```

> **⚠️ 端口与防火墙**：必须在服务器/防火墙放行
> `TCP 21115, 21116, 21117` 和 `UDP 21116`。
> 只开 TCP 不开 UDP 会导致"ID 能注册但连不上"——这是最常见的坑。

### 第 2 步：改配置（`build/custom.toml`）

```toml
[server]
rendezvous_host = "你的域名或IP"        # 客户端真正能连到的地址
key            = "上一步拿到的公钥"      # 必须完全一致，否则连不上

[brand]
app_name  = "企业网管助手"
exe_name  = "nethelper"
company_name = "你的公司"
website   = "https://你的内网地址"
```

### 第 3 步：fork RustDesk 并打补丁

```bash
git clone --recursive https://github.com/rustdesk/rustdesk.git
# ... 在 GitHub 上 fork 到你自己的账号，然后 clone 你的 fork ↓

cd <你的fork目录>
python <rd-suite>/build/patch-ci.py --source .
```

> `git clone` 一定要带 `--recursive`：`libs/hbb_common` 是独立 submodule，
> 服务器地址和公钥就定义在里面，漏了它定制会失败。

### 第 4 步：推送并触发构建

```bash
git add . && git commit -m "customize client" && git push
```

然后到 GitHub 仓库页面 → **Actions** → `Custom Client Build` → **Run workflow**。

构建约 20–40 分钟，完成后在 Artifacts 下载 `rustdesk-unsigned-windows-x86_64`。

### 第 5 步：分发

把整个 `rustdesk` 目录打包发给员工即可（便携版，无需安装）。

---

## 四、在本地构建（可选）

如果你确实想在本机构建：

```bash
python build/customize.py --source <rustdesk源码> --config custom.toml --dry-run   # 先预演
python build/customize.py --source <rustdesk源码> --config custom.toml             # 正式写入
python build/customize.py --source <rustdesk源码> --config custom.toml --restore    # 后悔了还原
```

前置依赖（Windows）：Rust 1.75、Flutter 3.24.5、LLVM 15.0.6、vcpkg（commit `9e59...`）、MSVC。
版本要求严，请以 `docs/IMPLEMENTATION.md` 为准。

---

## 五、⚠️ 许可证提醒（务必先看）

**RustDesk 使用 AGPL-3.0 许可证**，这一点直接影响你的使用方式：

| 场景 | 是否合规 |
|---|---|
| 公司内部使用，不对外分发 | ✅ 完全没问题，AGPL 不限制内部使用 |
| 改了代码，只在内部用，不外传 | ✅ 无需开源 |
| **把修改后的客户端发给公司以外的人** | ⚠️ 属于"分发"，**必须向接收者提供完整源码** |
| 作为商业产品卖给客户 | ⚠️ 需要遵守 AGPL 或向官方购买商业授权 |

如果合规要求严格且涉及对外分发，建议考虑 RustDesk 官方商业授权，
或改用 Apache/MIT 许可的替代方案。

---

## 六、已知问题与排查

**Docker 拉镜像失败：`connecting to 127.0.0.1:7890 ... actively refused`**

你的 Docker Desktop 配置了代理，它转发的 7890 端口（通常是科学上网客户端）当前没运行。
三种解法任选其一：
1. 启动你的代理软件；
2. Docker Desktop → Settings → Resources → Proxies → 关闭代理；
3. 给 Docker daemon 单独配置一个可用的镜像加速器。

**客户端连不上，提示 key 不匹配**
`server.key` 与服务器上 `data/id_ed25519.pub` 必须逐字节一致。用 `python rdctl.py key` 重新取。

**ID 能注册但连接失败**
99% 是 UDP 21116 没放行，或 relay 地址配错，检查 `server/.env` 的 `RELAY_HOST`。

**定制没生效**
检查 `git clone` 是否带了 `--recursive`；再看 CI 日志里是否有定制步骤的输出。

---

## 七、验证状态

本套件的定制锚点已对 RustDesk master 分支（v1.5.1）**实测验证**：

- 17 项替换规则在真实源码上 **17/17 命中，0 失败**
- `--restore` 回滚验证通过，8 个文件完整还原且备份自动清理
- 生成的 GitHub Actions 配置经 YAML 解析器校验，语法有效
- `docker-compose.yml` 通过 `docker compose config` 校验

验证环境的实际源码哈希可能随上游更新变化，若某条规则未命中，
`customize.py` 会明确报出**哪一项失败及原因**，不会静默放过。
