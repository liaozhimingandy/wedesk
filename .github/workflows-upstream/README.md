# 上游工作流存档区

GitHub 只加载 `.github/workflows/*.yml`。放到这里的 yml **不会被执行**，
纯粹是为了保留上游 rustdesk/rustdesk 的原始 CI 作为参照。

## 为什么挪走

这些工作流是为「上游要同时发布 Android / iOS / macOS / Linux / Flatpak / AppImage / F-Droid /
 nightly / 平台 Release / Lepton-com role 同步恳求」设计的，还依赖一大批
 `secrets.*`（签名证书、Apple 开发者账号、Android keystore 等）。

本仓库的实际诉求只是一条：**从本仓库直接编译出可用的 Windows 客户端**。
留着它们的结果就是每次 push 都触发一堆必然失败的 job，真正的产物被噪音掩盖。

## 现在唯一的工作流

`.github/workflows/build-windows.yml`
—— preflight 校验 → 生成 bridge → Windows x64 构建 → 上传产物。详见 `docs/CI.md`。

## 想对照上游时怎么办

升级上游版本时，用这里的 `flutter-build.yml` 比对游戏的 steps 有没有变
（尤其是 vcpkg commit、Flutter 版本、engine 下载地址这三处最容易变）。
升级流程见 `upstream/UPSTREAM.md`。

## 恢复某个工作流

```bash
git mv .github/workflows-upstream/<name>.yml .github/workflows/
```
恢复后记得检查它依赖的 secrets / reusable workflow 是否还在 —— 上面这些文件
互相之间有 `workflow_call` 引用（例如 `ci.yml` → `flutter-build.yml` → `bridge.yml`），
只单独搬回来一个通常跑不起来。
