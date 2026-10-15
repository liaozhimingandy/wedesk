# 文档索引

按下面的顺序读，从"这是什么"到"怎么改它"。

---

## 一、理解项目

| # | 文档 | 回答什么问题 |
|---|---|---|
| 01 | [架构总览](01-architecture.md) | 分几层？各目录干什么？exe 是怎么拼出来的？ |
| 02 | [运行原理](02-runtime.md) | 输入 ID 按下连接，中间发生了什么？打洞和中继怎么工作的？ |

> **先看这两篇。** 跳过它们直接改代码，很容易出现"改了但没反应"——
> 因为不知道 Rust 和 Flutter 之间隔着一层自动生成的代码桥。

---

## 二、构建与部署

| # | 文档 | 回答什么问题 |
|---|---|---|
| 03 | [本地构建](03-build.md) | 怎么在本机编译出 exe？依赖怎么装？ |
| 03b | [GitHub Actions 云构建](03b-ci.md) | 不想装工具链，怎么在云端编译？ |
| 04 | [部署指南](04-deploy.md) | 服务端怎么起？密钥怎么管？客户端怎么发给员工？ |
| 09 | [服务端自编译](09-server-build.md) | 服务端源码在哪？怎么自己编译 hbbs/hbbr？ |
| 10 | [端口与网络规划](10-ports.md) | 用到哪些端口？怎么统一规划？改端口要动哪几处？ |
| 11 | [服务端运维与客户端接入](11-server-ops.md) | 两个容器各干什么？日志每行什么意思？客户端怎么连？日常怎么运维？ |

> **没有工具链或不想折腾环境** → 走 03b（云构建）。
> **要频繁调试、改代码** → 走 03（本地构建）。
> **不想用官方服务端镜像** → 走 09，源码已在本仓库里，一次 `docker build` 就有自己的镜像。
> **要上公网 / 想换端口 / 要开 host 网络** → 先读 10，端口是从主端口推导的，漏一个派生端口就是"连不上"。
> **服务端已经跑起来了，想知道每个容器在干什么、怎么接客户端、怎么运维** → 走 11。

---

## 三、定制与维护

| # | 文档 | 回答什么问题 |
|---|---|---|
| 05 | [定制指南](05-customize.md) | 换 Logo、改服务器、裁剪功能在哪改？ |
| 06 | [维护手册](06-maintenance.md) | 加一个新功能要动哪些文件？怎么升上游？ |
| 07 | [故障排查](07-faq.md) | 构建挂了 / 连不上 / 定制没生效怎么办？ |
| 08 | [性能调优](08-performance.md) | 怎么让画面最流畅、CPU 最省、服务端占地最小？ |

> **想让软件跑得更快更省** → 先看 08。它的结论是：
> 别在画质参数上较劲，**硬件编解码开没开**才是量级差距。

---

## 五、上游相关

| 文档 | 内容 |
|---|---|
| [`../upstream/UPSTREAM.md`](../upstream/UPSTREAM.md) | 上游基线与改动登记。**升级上游前必读** |
| [`../upstream/custom.patch`](../upstream/custom.patch) | 客户端定制的完整 diff，可从干净上游重放 |
| [`../upstream/server.patch`](../upstream/server.patch) | 服务端定制 diff（关闭官方版本检查） |
| [`../upstream/verify_patch.py`](../upstream/verify_patch.py) | 验证客户端补丁仍可完整重建定制状态 |
| [`upstream/`](upstream/) | 上游的 AI 指令文件（AGENTS.md 等）与上游 README |
| [`upstream-i18n/`](upstream-i18n/) | 上游的 60 份多语言 README/CONTRIBUTING 等，**与本项目无关**，仅留档 |

---

## 六、三条最重要的规则

改这个项目前，请把这三条记住 —— 它们是过去踩坑的总结：

1. **改了 `src/flutter_ffi.rs` 必须重跑 `python build/generate-bridge.py`**
   否则 Dart 侧看不到新函数。这是"改了但没反应"的头号原因。

2. **改了 `customize.py` 覆盖的 13 个文件，必须重跑 `python upstream/verify_patch.py --regen`**
   否则下次升级上游时改动会静默丢失，而且没人会发现。

3. **带 `if` 判断 / 新增函数 / 改控制流的改动，要登记到 `upstream/UPSTREAM.md`**
   这类改动塞不进字符串替换规则表，升级时脚本不会帮你重建，必须手动重做。

---

## 七、一分钟速查

```bash
python build/check-env.py                  # 环境体检（构建失败时第一个跑）
python build/customize.py --dry-run        # 看品牌注入命中情况
python build/generate-bridge.py --check    # bridge 是否就绪
python upstream/verify_patch.py            # 补丁是否仍可重放
cd server && python rdctl.py status        # 服务端状态
python rdctl.py build         # 服务端：从源码构建镜像（首次）

build\tag-release.bat --dry                # 预演发版（看将要打什么标签）
build\tag-release.bat                      # 发版：打标签 → CI 自动发布 Release
                                           #        同时产出客户端 exe/MSI 与服务端 hbbs/hbbr
```
