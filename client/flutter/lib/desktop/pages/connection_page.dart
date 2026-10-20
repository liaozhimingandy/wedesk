// main window right pane

import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_hbb/consts.dart';
import 'package:flutter_hbb/desktop/widgets/connect_panel.dart';
import 'package:flutter_hbb/common/widgets/wedesign.dart';
import 'package:flutter_hbb/models/state_model.dart';
import 'package:get/get.dart';
import 'package:url_launcher/url_launcher_string.dart';
import 'package:window_manager/window_manager.dart';

import '../../common.dart';
import '../../common/formatter/id_formatter.dart';
import '../../common/widgets/peer_tab_page.dart';
import '../../common/widgets/autocomplete.dart';
import '../../models/platform_model.dart';

class OnlineStatusWidget extends StatefulWidget {
  const OnlineStatusWidget({Key? key, this.onSvcStatusChanged})
      : super(key: key);

  final VoidCallback? onSvcStatusChanged;

  @override
  State<OnlineStatusWidget> createState() => _OnlineStatusWidgetState();
}

/// State for the connection page.
class _OnlineStatusWidgetState extends State<OnlineStatusWidget> {
  final _svcStopped = Get.find<RxBool>(tag: 'stop-service');
  final _svcIsUsingPublicServer = true.obs;
  Timer? _updateTimer;

  void onUsePublicServerGuide() {
    const url = "https://rustdesk.com/pricing";
    canLaunchUrlString(url).then((can) {
      if (can) {
        launchUrlString(url);
      }
    });
  }

  @override
  void initState() {
    super.initState();
    _updateTimer = periodic_immediate(Duration(seconds: 1), () async {
      updateStatus();
    });
  }

  @override
  void dispose() {
    _updateTimer?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final colors = WeColors.of(context);
    final isIncomingOnly = bind.isIncomingOnly();
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: WeSpace.l, vertical: 8),
      decoration: BoxDecoration(
        color: colors.surfaceAlt,
        border: Border(top: BorderSide(color: colors.border, width: 1)),
      ),
      child: Obx(() {
        // Keeps "incoming only" window auto sizing in sync with the content.
        widget.onSvcStatusChanged?.call();
        final svcStopped = _svcStopped.value;
        final status = stateGlobal.svcStatus.value;
        final statusColor = svcStopped
            ? colors.textTertiary
            : status == SvcStatus.ready
                ? colors.success
                : status == SvcStatus.connecting
                    ? colors.warn
                    : colors.danger;
        final statusText = svcStopped
            ? translate("Service is not running")
            : status == SvcStatus.connecting
                ? translate("connecting_status")
                : status == SvcStatus.notReady
                    ? translate("not_ready_status")
                    : translate('Ready');
        final canStart = svcStopped;
        return Wrap(
          crossAxisAlignment: WrapCrossAlignment.center,
          spacing: WeSpace.s,
          runSpacing: WeSpace.xs,
          children: [
            Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Container(
                  width: 8,
                  height: 8,
                  decoration:
                      BoxDecoration(color: statusColor, shape: BoxShape.circle),
                ),
                const SizedBox(width: 6),
                Text(
                  statusText,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    fontSize: 12.5,
                    color: colors.textSecondary,
                    fontWeight: FontWeight.w500,
                  ),
                ),
              ],
            ),
            if (canStart)
              InkWell(
                onTap: () async {
                  await start_service(true);
                },
                child: Text(
                  translate("Start service"),
                  style: TextStyle(
                    fontSize: 12.5,
                    color: colors.brand,
                    decoration: TextDecoration.underline,
                    fontWeight: FontWeight.w500,
                  ),
                ),
              ),
            if (!isIncomingOnly &&
                !svcStopped &&
                status == SvcStatus.ready &&
                _svcIsUsingPublicServer.value)
              InkWell(
                onTap: onUsePublicServerGuide,
                child: Text(
                  translate('setup_server_tip'),
                  style: TextStyle(
                    fontSize: 12.5,
                    color: colors.brand,
                    decoration: TextDecoration.underline,
                  ),
                ),
              ),
            if (!isIncomingOnly && stateGlobal.videoConnCount.value > 0)
              Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Icon(Icons.desktop_windows_outlined,
                      size: 14, color: colors.textTertiary),
                  const SizedBox(width: 4),
                  Text(
                    '${translate('Active sessions')}: ${stateGlobal.videoConnCount.value}',
                    style:
                        TextStyle(fontSize: 12.5, color: colors.textTertiary),
                  ),
                ],
              ),
          ],
        );
      }),
    );
  }

  updateStatus() async {
    final status =
        jsonDecode(await bind.mainGetConnectStatus()) as Map<String, dynamic>;
    final statusNum = status['status_num'] as int;
    if (statusNum == 0) {
      stateGlobal.svcStatus.value = SvcStatus.connecting;
    } else if (statusNum == -1) {
      stateGlobal.svcStatus.value = SvcStatus.notReady;
    } else if (statusNum == 1) {
      stateGlobal.svcStatus.value = SvcStatus.ready;
    } else {
      stateGlobal.svcStatus.value = SvcStatus.notReady;
    }
    _svcIsUsingPublicServer.value = await bind.mainIsUsingPublicServer();
    try {
      stateGlobal.videoConnCount.value = status['video_conn_count'] as int;
    } catch (_) {}
  }
}

/// Connection page for connecting to a remote peer.
class ConnectionPage extends StatefulWidget {
  const ConnectionPage({
    Key? key,
    this.navIndex = 0,
    this.header = const [],
  }) : super(key: key);

  /// 0 = remote control page, 1 = device list page. Switched with an
  /// [IndexedStack] so both pages keep their state while navigating.
  final int navIndex;

  /// Widgets rendered above the connect panel on the remote control page
  /// (the local device card, help cards, ...).
  final List<Widget> header;

  @override
  State<ConnectionPage> createState() => _ConnectionPageState();
}

/// State for the connection page.
class _ConnectionPageState extends State<ConnectionPage>
    with SingleTickerProviderStateMixin, WindowListener {
  /// Controller for the id input bar.
  final _idController = IDTextEditingController();

  final RxBool _idInputFocused = false.obs;
  final FocusNode _idFocusNode = FocusNode();
  final TextEditingController _idEditingController = TextEditingController();

  bool isWindowMinimized = false;

  final AllPeersLoader _allPeersLoader = AllPeersLoader();

  @override
  void initState() {
    super.initState();
    _allPeersLoader.init(setState);
    _idFocusNode.addListener(onFocusChanged);
    if (_idController.text.isEmpty) {
      WidgetsBinding.instance.addPostFrameCallback((_) async {
        final lastRemoteId = await bind.mainGetLastRemoteId();
        if (lastRemoteId != _idController.id) {
          setState(() {
            _idController.id = lastRemoteId;
          });
        }
      });
    }
    Get.put<TextEditingController>(_idEditingController);
    Get.put<IDTextEditingController>(_idController);
    windowManager.addListener(this);
  }

  @override
  void dispose() {
    _idController.dispose();
    windowManager.removeListener(this);
    _allPeersLoader.clear();
    _idFocusNode.removeListener(onFocusChanged);
    _idFocusNode.dispose();
    _idEditingController.dispose();
    if (Get.isRegistered<IDTextEditingController>()) {
      Get.delete<IDTextEditingController>();
    }
    if (Get.isRegistered<TextEditingController>()) {
      Get.delete<TextEditingController>();
    }
    super.dispose();
  }

  @override
  void onWindowEvent(String eventName) {
    super.onWindowEvent(eventName);
    if (eventName == 'minimize') {
      isWindowMinimized = true;
    } else if (eventName == 'maximize' || eventName == 'restore') {
      if (isWindowMinimized && isWindows) {
        // windows can't update when minimized.
        Get.forceAppUpdate();
      }
      isWindowMinimized = false;
    }
  }

  @override
  void onWindowEnterFullScreen() {
    // Remove edge border by setting the value to zero.
    stateGlobal.resizeEdgeSize.value = 0;
  }

  @override
  void onWindowLeaveFullScreen() {
    // Restore edge border to default edge size.
    stateGlobal.resizeEdgeSize.value = stateGlobal.isMaximized.isTrue
        ? kMaximizeEdgeSize
        : windowResizeEdgeSize;
  }

  @override
  void onWindowClose() {
    super.onWindowClose();
    bind.mainOnMainWindowClose();
  }

  void onFocusChanged() {
    _idInputFocused.value = _idFocusNode.hasFocus;
    if (_idFocusNode.hasFocus) {
      if (_allPeersLoader.needLoad) {
        _allPeersLoader.getAllPeers();
      }

      final textLength = _idEditingController.value.text.length;
      // Select all to facilitate removing text, just following the behavior of address input of chrome.
      _idEditingController.selection =
          TextSelection(baseOffset: 0, extentOffset: textLength);
    }
  }

  @override
  Widget build(BuildContext context) {
    final isOutgoingOnly = bind.isOutgoingOnly();
    return Column(
      children: [
        // The native title bar is hidden; this slim bar gives the window a
        // drag area plus minimize / close buttons (the main window is fixed
        // in size, so there is intentionally no maximize button).
        _buildWindowBar(context),
        Expanded(
          child: IndexedStack(
            index: widget.navIndex == 1 ? 1 : 0,
            children: [
              SingleChildScrollView(
                padding: const EdgeInsets.fromLTRB(
                    WeSpace.xl, WeSpace.l, WeSpace.xl, WeSpace.xl),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    ...widget.header,
                    if (widget.header.isNotEmpty)
                      const SizedBox(height: WeSpace.l),
                    ConnectPanel(
                      idController: _idController,
                      idEditingController: _idEditingController,
                      focusNode: _idFocusNode,
                      idInputFocused: _idInputFocused,
                      allPeersLoader: _allPeersLoader,
                      onConnect: onConnect,
                    ),
                  ],
                ),
              ),
              PeerTabPage(),
            ],
          ),
        ),
        if (!isOutgoingOnly) ...[
          const Divider(height: 1),
          OnlineStatusWidget()
        ],
      ],
    );
  }

  /// A slim bar at the top of the window: drag area + window buttons.
  Widget _buildWindowBar(BuildContext context) {
    final colors = WeColors.of(context);
    return Row(
      children: [
        Expanded(
          child: GestureDetector(
            behavior: HitTestBehavior.translucent,
            onPanStart: (_) => windowManager.startDragging(),
            child: const SizedBox(height: 34),
          ),
        ),
        _WindowButton(
          icon: Icons.horizontal_rule,
          tooltip: translate('Minimize'),
          hoverColor: colors.border,
          onTap: () => windowManager.minimize(),
        ),
        _WindowButton(
          icon: Icons.close,
          tooltip: translate('Close'),
          hoverColor: const Color(0xFFE81123),
          onTap: () => windowManager.close(),
        ),
      ],
    );
  }

  /// Callback for the connect button.
  /// Connects to the selected peer.
  void onConnect(
      {bool isFileTransfer = false,
      bool isViewCamera = false,
      bool isTerminal = false,
      bool isTcpTunneling = false}) {
    var id = _idController.id;
    connect(context, id,
        isFileTransfer: isFileTransfer,
        isViewCamera: isViewCamera,
        isTerminal: isTerminal,
        isTcpTunneling: isTcpTunneling);
  }

}

/// One caption-button of the custom window bar (minimize / close).
class _WindowButton extends StatefulWidget {
  const _WindowButton({
    Key? key,
    required this.icon,
    required this.tooltip,
    required this.onTap,
    required this.hoverColor,
  }) : super(key: key);

  final IconData icon;
  final String tooltip;
  final VoidCallback onTap;
  final Color hoverColor;

  @override
  State<_WindowButton> createState() => _WindowButtonState();
}

class _WindowButtonState extends State<_WindowButton> {
  bool _hover = false;

  @override
  Widget build(BuildContext context) {
    final colors = WeColors.of(context);
    return Tooltip(
      message: widget.tooltip,
      waitDuration: const Duration(milliseconds: 400),
      child: InkWell(
        onTap: widget.onTap,
        onHover: (v) {
          if (_hover != v) {
            setState(() => _hover = v);
          }
        },
        child: Container(
          width: 42,
          height: 34,
          decoration: BoxDecoration(
            color: _hover ? widget.hoverColor : Colors.transparent,
          ),
          child: Icon(
            widget.icon,
            size: 16,
            color: _hover && widget.hoverColor == const Color(0xFFE81123)
                ? Colors.white
                : colors.textSecondary,
          ),
        ),
      ),
    );
  }
}
