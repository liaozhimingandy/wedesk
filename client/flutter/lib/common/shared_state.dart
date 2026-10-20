import 'package:flutter_hbb/common.dart';
import 'package:get/get.dart';

import '../consts.dart';

// TODO: A lot of dup code.

class PrivacyModeState {
  static String tag(String id) => 'privacy_mode_$id';

  static void init(String id) {
    final key = tag(id);
    if (!Get.isRegistered<RxString>(tag: key)) {
      final RxString state = ''.obs;
      Get.put<RxString>(state, tag: key);
    }
  }

  static void delete(String id) {
    final key = tag(id);
    if (Get.isRegistered<RxString>(tag: key)) {
      Get.delete<RxString>(tag: key);
    } else {
      Get.find<RxString>(tag: key).value = '';
    }
  }

  static RxString find(String id) => Get.find<RxString>(tag: tag(id));
}

class BlockInputState {
  static String tag(String id) => 'block_input_$id';

  static void init(String id) {
    final key = tag(id);
    if (!Get.isRegistered<RxBool>(tag: key)) {
      final RxBool state = false.obs;
      Get.put<RxBool>(state, tag: key);
    } else {
      Get.find<RxBool>(tag: key).value = false;
    }
  }

  static void delete(String id) {
    final key = tag(id);
    if (Get.isRegistered<RxBool>(tag: key)) {
      Get.delete<RxBool>(tag: key);
    }
  }

  static RxBool find(String id) => Get.find<RxBool>(tag: tag(id));
}

class CurrentDisplayState {
  static String tag(String id) => 'current_display_$id';

  static void init(String id) {
    final key = tag(id);
    if (!Get.isRegistered<RxInt>(tag: key)) {
      final RxInt state = RxInt(0);
      Get.put<RxInt>(state, tag: key);
    } else {
      Get.find<RxInt>(tag: key).value = 0;
    }
  }

  static void delete(String id) {
    final key = tag(id);
    if (Get.isRegistered<RxInt>(tag: key)) {
      Get.delete<RxInt>(tag: key);
    }
  }

  static RxInt find(String id) => Get.find<RxInt>(tag: tag(id));
}

/// 连接建立过程的**阶段进度**（"正在接入服务器" → "正在尝试 P2P 直连"
/// → 必要时"正在通过中继连接" → "正在建立加密通道"）。
///
/// 数据源是 Rust 侧 `connection_stage` 事件（见 `client/src/client.rs` 的
/// `set_connection_stage`）。它存在的理由：连接过程在跨网打洞失败回退中继时
/// 可能要十几秒，这段时间界面上只有一个"正在连接…"，用户既不知道卡在哪，
/// 也分不清是"慢"还是"坏了"。
class ConnectionProgress {
  final Rx<String> _stage = ''.obs;
  final Rx<String> _detail = ''.obs;

  Rx<String> get stage => _stage;
  Rx<String> get detail => _detail;

  void set(String stage, String detail) {
    _stage.value = stage;
    _detail.value = detail;
  }

  void reset() => set('', '');
}

class ConnectionProgressState {
  static String tag(String id) => 'connection_progress_$id';

  static void init(String id) {
    final key = tag(id);
    if (!Get.isRegistered<ConnectionProgress>(tag: key)) {
      Get.put<ConnectionProgress>(ConnectionProgress(), tag: key);
    } else {
      Get.find<ConnectionProgress>(tag: key).reset();
    }
  }

  static void delete(String id) {
    final key = tag(id);
    if (Get.isRegistered<ConnectionProgress>(tag: key)) {
      Get.delete<ConnectionProgress>(tag: key);
    }
  }

  static ConnectionProgress find(String id) =>
      Get.find<ConnectionProgress>(tag: tag(id));
}

class ConnectionType {
  final Rx<String> _secure = kInvalidValueStr.obs;
  final Rx<String> _direct = kInvalidValueStr.obs;
  final Rx<String> _stream_type = kInvalidValueStr.obs;

  Rx<String> get secure => _secure;
  Rx<String> get direct => _direct;
  Rx<String> get stream_type => _stream_type;

  static String get strSecure => 'secure';
  static String get strInsecure => 'insecure';
  static String get strDirect => '';
  static String get strIndirect => '_relay';

  void setSecure(bool v) {
    _secure.value = v ? strSecure : strInsecure;
  }

  void setDirect(bool v) {
    _direct.value = v ? strDirect : strIndirect;
  }

  void setStreamType(String v) {
    _stream_type.value = v;
  }

  bool isValid() {
    return _secure.value != kInvalidValueStr &&
        _direct.value != kInvalidValueStr &&
        _stream_type.value != kInvalidValueStr;
  }
}

class ConnectionTypeState {
  static String tag(String id) => 'connection_type_$id';

  static void init(String id) {
    final key = tag(id);
    if (!Get.isRegistered<ConnectionType>(tag: key)) {
      final ConnectionType collectionType = ConnectionType();
      Get.put<ConnectionType>(collectionType, tag: key);
    }
  }

  static void delete(String id) {
    final key = tag(id);
    if (Get.isRegistered<ConnectionType>(tag: key)) {
      Get.delete<ConnectionType>(tag: key);
    }
  }

  static ConnectionType find(String id) =>
      Get.find<ConnectionType>(tag: tag(id));
}

class FingerprintState {
  static String tag(String id) => 'fingerprint_$id';

  static void init(String id) {
    final key = tag(id);
    if (!Get.isRegistered<RxString>(tag: key)) {
      final RxString state = ''.obs;
      Get.put<RxString>(state, tag: key);
    } else {
      Get.find<RxString>(tag: key).value = '';
    }
  }

  static void delete(String id) {
    final key = tag(id);
    if (Get.isRegistered<RxString>(tag: key)) {
      Get.delete<RxString>(tag: key);
    }
  }

  static RxString find(String id) => Get.find<RxString>(tag: tag(id));
}

class ShowRemoteCursorState {
  static String tag(String id) => 'show_remote_cursor_$id';

  static void init(String id) {
    final key = tag(id);
    if (!Get.isRegistered<RxBool>(tag: key)) {
      final RxBool state = false.obs;
      Get.put<RxBool>(state, tag: key);
    } else {
      Get.find<RxBool>(tag: key).value = false;
    }
  }

  static void delete(String id) {
    final key = tag(id);
    if (Get.isRegistered<RxBool>(tag: key)) {
      Get.delete<RxBool>(tag: key);
    }
  }

  static RxBool find(String id) => Get.find<RxBool>(tag: tag(id));
}

class ShowRemoteCursorLockState {
  static String tag(String id) => 'show_remote_cursor_lock_$id';

  static void init(String id) {
    final key = tag(id);
    if (!Get.isRegistered<RxBool>(tag: key)) {
      final RxBool state = false.obs;
      Get.put<RxBool>(state, tag: key);
    } else {
      Get.find<RxBool>(tag: key).value = false;
    }
  }

  static void delete(String id) {
    final key = tag(id);
    if (Get.isRegistered<RxBool>(tag: key)) {
      Get.delete<RxBool>(tag: key);
    }
  }

  static RxBool find(String id) => Get.find<RxBool>(tag: tag(id));
}

class KeyboardEnabledState {
  static String tag(String id) => 'keyboard_enabled_$id';

  static void init(String id) {
    final key = tag(id);
    if (!Get.isRegistered<RxBool>(tag: key)) {
      // Server side, default true
      final RxBool state = true.obs;
      Get.put<RxBool>(state, tag: key);
    } else {
      Get.find<RxBool>(tag: key).value = true;
    }
  }

  static void delete(String id) {
    final key = tag(id);
    if (Get.isRegistered<RxBool>(tag: key)) {
      Get.delete<RxBool>(tag: key);
    }
  }

  static RxBool find(String id) => Get.find<RxBool>(tag: tag(id));
}

class RemoteCursorMovedState {
  static String tag(String id) => 'remote_cursor_moved_$id';

  static void init(String id) {
    final key = tag(id);
    if (!Get.isRegistered<RxBool>(tag: key)) {
      final RxBool state = false.obs;
      Get.put<RxBool>(state, tag: key);
    } else {
      Get.find<RxBool>(tag: key).value = false;
    }
  }

  static void delete(String id) {
    final key = tag(id);
    if (Get.isRegistered<RxBool>(tag: key)) {
      Get.delete<RxBool>(tag: key);
    }
  }

  static RxBool find(String id) => Get.find<RxBool>(tag: tag(id));
}

class RemoteCountState {
  static String tag() => 'remote_count_';

  static void init() {
    final key = tag();
    if (!Get.isRegistered<RxInt>(tag: key)) {
      final RxInt state = 1.obs;
      Get.put<RxInt>(state, tag: key);
    } else {
      Get.find<RxInt>(tag: key).value = 1;
    }
  }

  static void delete() {
    final key = tag();
    if (Get.isRegistered<RxInt>(tag: key)) {
      Get.delete<RxInt>(tag: key);
    }
  }

  static RxInt find() => Get.find<RxInt>(tag: tag());
}

class PeerBoolOption {
  static String tag(String id, String opt) => 'peer_{$opt}_$id';

  static void init(String id, String opt, bool Function() init_getter) {
    final key = tag(id, opt);
    if (!Get.isRegistered<RxBool>(tag: key)) {
      final RxBool value = RxBool(init_getter());
      Get.put<RxBool>(value, tag: key);
    } else {
      Get.find<RxBool>(tag: key).value = init_getter();
    }
  }

  static void delete(String id, String opt) {
    final key = tag(id, opt);
    if (Get.isRegistered<RxBool>(tag: key)) {
      Get.delete<RxBool>(tag: key);
    }
  }

  static RxBool find(String id, String opt) =>
      Get.find<RxBool>(tag: tag(id, opt));
}

class PeerStringOption {
  static String tag(String id, String opt) => 'peer_{$opt}_$id';

  static void init(String id, String opt, String Function() init_getter) {
    final key = tag(id, opt);
    if (!Get.isRegistered<RxString>(tag: key)) {
      final RxString value = RxString(init_getter());
      Get.put<RxString>(value, tag: key);
    } else {
      Get.find<RxString>(tag: key).value = init_getter();
    }
  }

  static void delete(String id, String opt) {
    final key = tag(id, opt);
    if (Get.isRegistered<RxString>(tag: key)) {
      Get.delete<RxString>(tag: key);
    }
  }

  static RxString find(String id, String opt) =>
      Get.find<RxString>(tag: tag(id, opt));
}

class UnreadChatCountState {
  static String tag(id) => 'unread_chat_count_$id';

  static void init(String id) {
    final key = tag(id);
    if (!Get.isRegistered<RxInt>(tag: key)) {
      final RxInt state = RxInt(0);
      Get.put<RxInt>(state, tag: key);
    } else {
      Get.find<RxInt>(tag: key).value = 0;
    }
  }

  static void delete(String id) {
    final key = tag(id);
    if (Get.isRegistered<RxInt>(tag: key)) {
      Get.delete<RxInt>(tag: key);
    }
  }

  static RxInt find(String id) => Get.find<RxInt>(tag: tag(id));
}

initSharedStates(String id) {
  PrivacyModeState.init(id);
  BlockInputState.init(id);
  CurrentDisplayState.init(id);
  KeyboardEnabledState.init(id);
  ShowRemoteCursorState.init(id);
  ShowRemoteCursorLockState.init(id);
  RemoteCursorMovedState.init(id);
  FingerprintState.init(id);
  PeerBoolOption.init(id, kOptionZoomCursor, () => false);
  UnreadChatCountState.init(id);
  if (isMobile) ConnectionTypeState.init(id); // desktop in other places
}

removeSharedStates(String id) {
  PrivacyModeState.delete(id);
  BlockInputState.delete(id);
  CurrentDisplayState.delete(id);
  ShowRemoteCursorState.delete(id);
  ShowRemoteCursorLockState.delete(id);
  KeyboardEnabledState.delete(id);
  RemoteCursorMovedState.delete(id);
  FingerprintState.delete(id);
  PeerBoolOption.delete(id, kOptionZoomCursor);
  UnreadChatCountState.delete(id);
  ConnectionProgressState.delete(id);
  if (isMobile) ConnectionTypeState.delete(id);
}
