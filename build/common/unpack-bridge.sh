#!/usr/bin/env bash
# 解包 wedesk-bridge artifact，并立刻校验文件落在了正确的位置。
#
# 为什么不能"下载即用"（2026-10-17 Windows 构建连挂的真因）：
#   upload-artifact@v4 在 path 收到【多个路径】时，会用它们的最近公共父目录
#   当 artifact 的根目录。那六个 bridge 文件的公共父目录是 client/，
#   于是 client/ 这一层被剥掉 —— 下载到仓库根之后文件落进 ./src/，
#   而不是 ./client/src/。cargo 要的是 client/src/bridge_generated.rs，
#   拿不到就报
#     error[E0583]: file not found for module `bridge_generated`
#   而这个错误要等几百个依赖全部编译完才出现：白等十分钟，且报错信息指向
#   "模块文件不存在"，指不到"artifact 解包位置错了"这个真因。
#
#   客户端整棵树收进 client/ 之前，公共父目录正好是仓库根，剥掉后路径依然
#   正确 —— 所以这个坑只在重构之后才显形，之前一直是好的。
#
# 对策：生成侧打成 tar 包（包内结构由 tar 定义，与 action 的路径推断无关），
# 这一侧解包后立刻校验 —— 位置错了就在几秒内报错，不用等编译。
#
# 用法（各平台 job 在 download-artifact 之后执行）：
#   bash build/common/unpack-bridge.sh
set -e

if [ ! -f bridge.tar.gz ]; then
  echo "::error::没找到 bridge.tar.gz —— wedesk-bridge artifact 的内容与预期不符"
  echo "::error::当前目录内容: $(ls -A | tr '\n' ' ')"
  exit 1
fi

# 包内是相对仓库根的 client/src/... 与 client/flutter/...
tar -xzf bridge.tar.gz
rm -f bridge.tar.gz

missing=0
# 这两个缺了一定编译不过：lib.rs 在 flutter feature 下要 mod bridge_generated，
# flutter 侧要 import generated_bridge.dart
for f in \
  client/src/bridge_generated.rs \
  client/flutter/lib/generated_bridge.dart; do
  if [ -f "$f" ]; then
    echo "  [OK] $f"
  else
    echo "::error::bridge 文件未落到 $f"
    missing=1
  fi
done

# 这几个是可选的：frb 只在对应目标/版本下才生成，缺了不算错
for f in \
  client/src/bridge_generated.io.rs \
  client/flutter/lib/generated_bridge.freezed.dart \
  client/flutter/macos/Runner/bridge_generated.h \
  client/flutter/ios/Runner/bridge_generated.h; do
  if [ -f "$f" ]; then
    echo "  [OK] $f"
  else
    echo "  [--] $f（未生成，跳过）"
  fi
done

if [ "$missing" -ne 0 ]; then
  echo "::error::bridge 文件缺失会让 cargo 报 E0583（file not found for module bridge_generated）。先查 generate-bridge 里 tar 打包的路径，再看这里的解包目录。"
  echo "::error::仓库根下实际有的 bridge 文件：$(find . -maxdepth 4 -name 'bridge_generated*' -o -maxdepth 4 -name 'generated_bridge*' | head -n 10 | tr '\n' ' ')"
  exit 1
fi

echo "✓ bridge 文件已就位"
