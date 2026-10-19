import 'package:flutter/material.dart';
import 'package:flutter_hbb/common.dart';
import 'package:flutter_hbb/common/shared_state.dart';
import 'package:get/get.dart';

/// 连接方式的常驻角标：**P2P 直连** / **中继转发**。
///
/// 数据源是 Rust 侧 `connection_ready` 事件写入的 [ConnectionTypeState]
/// （见 `client/src/client/io_loop.rs` 的 `set_connection_type`），因此：
/// - 连接刚建立、类型还没确定时不显示；
/// - 会话重连后连接方式变了（比如 P2P 失败退回中继），角标会跟着变。
///
/// 放进远程画面的 `Stack` 里即可，位置由 [alignment] + [margin] 决定。
/// 整块是 `IgnorePointer`，不会挡住对远端的鼠标/触摸操作。
class ConnectionTypeBadge extends StatelessWidget {
  final String id;
  final AlignmentGeometry alignment;
  final EdgeInsetsGeometry margin;

  const ConnectionTypeBadge({
    Key? key,
    required this.id,
    this.alignment = Alignment.topLeft,
    this.margin = const EdgeInsets.only(left: 10, top: 10),
  }) : super(key: key);

  @override
  Widget build(BuildContext context) {
    return Obx(() {
      // 会话还没建立、或状态已随窗口销毁时被清理，直接不显示。
      // 不能用 try/catch 兜底：Obx 在 build 期间抛异常会被当成 widget 错误。
      if (!Get.isRegistered<ConnectionType>(tag: ConnectionTypeState.tag(id))) {
        return const Offstage();
      }
      final type = ConnectionTypeState.find(id);
      if (!type.isValid()) {
        return const Offstage();
      }

      final secure = type.secure.value == ConnectionType.strSecure;
      final direct = type.direct.value == ConnectionType.strDirect;

      // 中继用琥珀色提示"流量经过服务器"，P2P 用绿色表示直连。
      final color = direct ? Colors.greenAccent : Colors.amberAccent;
      final label = direct
          ? translate('P2P direct connection')
          : translate('Relayed connection');

      return IgnorePointer(
        child: Align(
          alignment: alignment,
          child: Container(
            margin: margin,
            padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
            decoration: BoxDecoration(
              color: Colors.black.withOpacity(0.55),
              borderRadius: BorderRadius.circular(10),
            ),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Container(
                  width: 7,
                  height: 7,
                  decoration: BoxDecoration(
                    color: color,
                    shape: BoxShape.circle,
                  ),
                ),
                const SizedBox(width: 5),
                Text(
                  label,
                  style: const TextStyle(color: Colors.white, fontSize: 12),
                ),
                if (!secure) ...[
                  const SizedBox(width: 4),
                  const Icon(Icons.no_encryption, size: 12, color: Colors.redAccent),
                ],
              ],
            ),
          ),
        ),
      );
    });
  }
}
