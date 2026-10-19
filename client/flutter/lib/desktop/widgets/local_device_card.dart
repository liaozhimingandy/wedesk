// ToDesk style "my device" card shown on the left pane of the desktop home
// page: device code on a blue gradient header, temporary password below with
// visible affordances (reveal / refresh / change) and a service status row.

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_hbb/common.dart';
import 'package:flutter_hbb/common/widgets/animated_rotation_widget.dart';
import 'package:flutter_hbb/desktop/pages/desktop_setting_page.dart';
import 'package:flutter_hbb/common/widgets/wedesign.dart';
import 'package:flutter_hbb/models/platform_model.dart';
import 'package:flutter_hbb/models/server_model.dart';
import 'package:flutter_hbb/models/state_model.dart';
import 'package:get/get.dart';
import 'package:provider/provider.dart';

class LocalDeviceCard extends StatefulWidget {
  const LocalDeviceCard({Key? key, this.onSvcStatusChanged}) : super(key: key);

  /// Called whenever the service status is rendered, the home page uses it to
  /// re-measure the window size in "incoming only" mode.
  final VoidCallback? onSvcStatusChanged;

  @override
  State<LocalDeviceCard> createState() => _LocalDeviceCardState();
}

class _LocalDeviceCardState extends State<LocalDeviceCard> {
  bool _passwordVisible = true;

  @override
  Widget build(BuildContext context) {
    return ChangeNotifierProvider.value(
      value: gFFI.serverModel,
      child: Consumer<ServerModel>(
        builder: (context, model, child) => _buildCard(context, model),
      ),
    );
  }

  Widget _buildCard(BuildContext context, ServerModel model) {
    final colors = WeColors.of(context);
    return WeCard(
      padding: EdgeInsets.zero,
      margin: const EdgeInsets.fromLTRB(WeSpace.l, WeSpace.s, WeSpace.l, 0),
      borderRadius: WeRadii.lgRadius,
      border: Border.all(color: colors.border, width: 1),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          _buildHeader(context, model),
          _buildPasswordRow(context, model),
        ],
      ),
    );
  }

  Widget _buildHeader(BuildContext context, ServerModel model) {
    final id = model.serverId.text.trim();
    final svcStoppedRx = _svcStoppedRx();

    return Container(
      padding: const EdgeInsets.fromLTRB(WeSpace.l, WeSpace.m, WeSpace.m,
          WeSpace.m),
      decoration: BoxDecoration(
        borderRadius: const BorderRadius.only(
          topLeft: Radius.circular(WeRadii.lg),
          topRight: Radius.circular(WeRadii.lg),
        ),
        gradient: WeColors.brandGradient(
            context, Alignment.topLeft, Alignment.bottomRight),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  translate("Your Desktop"),
                  style: const TextStyle(
                    color: Colors.white70,
                    fontSize: 12,
                    fontWeight: FontWeight.w500,
                  ),
                ),
              ),
              _HeaderAction(
                icon: Icons.content_copy,
                tooltip: translate("Copy"),
                onTap: () {
                  if (id.isEmpty) {
                    return;
                  }
                  Clipboard.setData(ClipboardData(text: id));
                  showToast(translate("Copied"));
                },
              ),
            ],
          ),
          const SizedBox(height: 6),
          FittedBox(
            alignment: Alignment.centerLeft,
            fit: BoxFit.scaleDown,
            child: Text(
              groupId(id),
              style: const TextStyle(
                color: Colors.white,
                fontSize: 24,
                fontWeight: FontWeight.w700,
                letterSpacing: 1.2,
                height: 1.25,
              ),
            ),
          ),
          const SizedBox(height: WeSpace.s),
          Obx(() {
            final svcStopped = svcStoppedRx?.value ?? false;
            final status = stateGlobal.svcStatus.value;
            final statusText = svcStopped
                ? translate("Service is not running")
                : status == SvcStatus.connecting
                    ? translate("connecting_status")
                    : status == SvcStatus.notReady
                        ? translate("not_ready_status")
                        : translate('Ready');
            final statusColor = (!svcStopped && status == SvcStatus.ready)
                ? const Color(0xFF7DF0BE)
                : const Color(0xFFFFD066);
            widget.onSvcStatusChanged?.call();
            return Row(
              children: [
                Container(
                  width: 7,
                  height: 7,
                  decoration: BoxDecoration(
                    color: statusColor,
                    shape: BoxShape.circle,
                  ),
                ),
                const SizedBox(width: 6),
                Expanded(
                  child: Text(
                    statusText,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(
                      color: Colors.white,
                      fontSize: 12,
                      fontWeight: FontWeight.w500,
                    ),
                  ),
                ),
                if (svcStopped)
                  _HeaderAction(
                    icon: Icons.play_arrow_rounded,
                    tooltip: translate("Start service"),
                    onTap: () async {
                      await start_service(true);
                    },
                  ),
              ],
            );
          }),
        ],
      ),
    );
  }

  Widget _buildPasswordRow(BuildContext context, ServerModel model) {
    final colors = WeColors.of(context);
    final showOneTime = model.approveMode != 'click' &&
        model.verificationMethod != kUsePermanentPassword;
    final passwd = model.serverPasswd.text.trim();
    final masked =
        List<String>.filled(passwd.isEmpty ? 6 : passwd.length, '•').join();
    final display =
        !showOneTime ? '-' : (_passwordVisible ? passwd : masked);

    return Container(
      padding: const EdgeInsets.fromLTRB(WeSpace.l, WeSpace.m, WeSpace.s,
          WeSpace.m),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  translate("One-time Password"),
                  style: TextStyle(
                    color: colors.textTertiary,
                    fontSize: 12,
                    fontWeight: FontWeight.w500,
                  ),
                ),
              ),
              if (showOneTime)
                WeIconButton(
                  icon: Icons.content_copy,
                  tooltip: translate("Copy"),
                  size: 28,
                  iconSize: 16,
                  onTap: () {
                    if (passwd.isEmpty) {
                      return;
                    }
                    Clipboard.setData(ClipboardData(text: passwd));
                    showToast(translate("Copied"));
                  },
                ),
              if (showOneTime)
                WeIconButton(
                  icon: _passwordVisible
                      ? Icons.visibility_outlined
                      : Icons.visibility_off_outlined,
                  tooltip: _passwordVisible
                      ? translate("Hide")
                      : translate("Show"),
                  size: 28,
                  iconSize: 16,
                  onTap: () {
                    setState(() => _passwordVisible = !_passwordVisible);
                  },
                ),
              if (showOneTime)
                Padding(
                  padding: const EdgeInsets.only(left: 2),
                  child: AnimatedRotationWidget(
                    onPressed: () => bind.mainUpdateTemporaryPassword(),
                    child: Tooltip(
                      message: translate('Refresh Password'),
                      child: Icon(
                        Icons.refresh,
                        size: 17,
                        color: colors.textTertiary,
                      ),
                    ),
                  ),
                ),
              if (!bind.isDisableSettings())
                WeIconButton(
                  icon: Icons.edit_outlined,
                  tooltip: translate('Change Password'),
                  size: 28,
                  iconSize: 16,
                  onTap: () => DesktopSettingPage.switch2page(
                      SettingsTabKey.safety),
                ),
            ],
          ),
          const SizedBox(height: 2),
          GestureDetector(
            onDoubleTap: () {
              if (!showOneTime || passwd.isEmpty) {
                return;
              }
              Clipboard.setData(ClipboardData(text: passwd));
              showToast(translate("Copied"));
            },
            child: Text(
              display,
              style: TextStyle(
                color: colors.textPrimary,
                fontSize: 20,
                fontWeight: FontWeight.w600,
                letterSpacing: 2,
              ),
            ),
          ),
        ],
      ),
    );
  }

  RxBool? _svcStoppedRx() {
    if (!Get.isRegistered<RxBool>(tag: 'stop-service')) {
      return null;
    }
    return Get.find<RxBool>(tag: 'stop-service');
  }
}

/// Icon button living on the blue gradient header.
class _HeaderAction extends StatefulWidget {
  const _HeaderAction({
    Key? key,
    required this.icon,
    required this.tooltip,
    required this.onTap,
  }) : super(key: key);

  final IconData icon;
  final String tooltip;
  final VoidCallback onTap;

  @override
  State<_HeaderAction> createState() => _HeaderActionState();
}

class _HeaderActionState extends State<_HeaderAction> {
  bool _hover = false;

  @override
  Widget build(BuildContext context) {
    return Tooltip(
      message: widget.tooltip,
      child: InkWell(
        borderRadius: BorderRadius.circular(14),
        onTap: widget.onTap,
        onHover: (v) {
          if (_hover != v) {
            setState(() => _hover = v);
          }
        },
        child: Container(
          width: 28,
          height: 28,
          decoration: BoxDecoration(
            color: _hover ? Colors.white.withOpacity(0.22) : Colors.white12,
            borderRadius: BorderRadius.circular(14),
          ),
          child: Icon(widget.icon, size: 16, color: Colors.white),
        ),
      ),
    );
  }
}
