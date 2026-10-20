// ToDesk style "allow control" section: a switch row (mapped to the
// stop-service option) followed by a light device card with the device code
// on the left and the connection password on the right, divided by a hair
// line — mirroring the reference layout (设备代码 | 连接模式).

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_hbb/common.dart';
import 'package:flutter_hbb/common/widgets/animated_rotation_widget.dart';
import 'package:flutter_hbb/desktop/pages/desktop_setting_page.dart';
import 'package:flutter_hbb/common/widgets/wedesign.dart';
import 'package:flutter_hbb/consts.dart';
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
        builder: (context, model, child) => _buildSection(context, model),
      ),
    );
  }

  Widget _buildSection(BuildContext context, ServerModel model) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _buildAllowRow(context, model),
        const SizedBox(height: WeSpace.m),
        _buildCard(context, model),
      ],
    );
  }

  /// "Allow control of this device" + switch + service status pill.
  Widget _buildAllowRow(BuildContext context, ServerModel model) {
    final svcStoppedRx = _svcStoppedRx();
    return Obx(() {
      final svcStopped = svcStoppedRx?.value ?? false;
      final status = stateGlobal.svcStatus.value;
      final ready = !svcStopped && status == SvcStatus.ready;
      final statusText = svcStopped
          ? translate("Service is not running")
          : status == SvcStatus.connecting
              ? translate("connecting_status")
              : status == SvcStatus.notReady
                  ? translate("not_ready_status")
                  : translate('Ready');
      final statusColor = ready
          ? WeColors.of(context).success
          : WeColors.of(context).warn;
      widget.onSvcStatusChanged?.call();
      return Row(
        children: [
          Text(
            translate("Allow remote control"),
            style: TextStyle(
              fontSize: 15,
              fontWeight: FontWeight.w600,
              color: WeColors.of(context).textPrimary,
            ),
          ),
          const SizedBox(width: WeSpace.s),
          Switch(
            value: !svcStopped,
            activeColor: WeColors.of(context).brand,
            materialTapTargetSize: MaterialTapTargetSize.shrinkWrap,
            onChanged: (allow) async {
              await mainSetBoolOption(kOptionStopService, !allow);
              if (svcStoppedRx != null) {
                svcStoppedRx.value = !allow;
              }
            },
          ),
          const SizedBox(width: WeSpace.s),
          WeStatusPill(text: statusText, color: statusColor),
        ],
      );
    });
  }

  Widget _buildCard(BuildContext context, ServerModel model) {
    final colors = WeColors.of(context);
    return WeCard(
      padding: const EdgeInsets.symmetric(
          horizontal: WeSpace.xl, vertical: WeSpace.l),
      color: colors.surfaceAlt,
      elevation: false,
      border: Border.all(color: Colors.transparent, width: 1),
      borderRadius: WeRadii.lgRadius,
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.center,
        children: [
          Expanded(child: _buildIdColumn(context, model)),
          Padding(
            padding:
                const EdgeInsets.symmetric(horizontal: WeSpace.xl),
            child: Container(width: 1, height: 56, color: colors.border),
          ),
          Expanded(child: _buildPasswordColumn(context, model)),
        ],
      ),
    );
  }

  Widget _buildIdColumn(BuildContext context, ServerModel model) {
    final colors = WeColors.of(context);
    final id = model.serverId.text.trim();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          translate("Device ID"),
          style: TextStyle(
            color: colors.textTertiary,
            fontSize: 12.5,
            fontWeight: FontWeight.w500,
          ),
        ),
        const SizedBox(height: 6),
        Row(
          children: [
            Expanded(
              child: FittedBox(
                alignment: Alignment.centerLeft,
                fit: BoxFit.scaleDown,
                child: Text(
                  groupId(id),
                  style: TextStyle(
                    color: colors.textPrimary,
                    fontSize: 26,
                    fontWeight: FontWeight.w700,
                    letterSpacing: 1.2,
                    height: 1.2,
                  ),
                ),
              ),
            ),
            WeIconButton(
              icon: Icons.content_copy,
              tooltip: translate("Copy"),
              size: 30,
              iconSize: 17,
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
      ],
    );
  }

  Widget _buildPasswordColumn(BuildContext context, ServerModel model) {
    final colors = WeColors.of(context);
    final showOneTime = model.approveMode != 'click' &&
        model.verificationMethod != kUsePermanentPassword;
    final passwd = model.serverPasswd.text.trim();
    final masked =
        List<String>.filled(passwd.isEmpty ? 6 : passwd.length, '•').join();
    final display =
        !showOneTime ? '-' : (_passwordVisible ? passwd : masked);

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          translate("Connection mode"),
          style: TextStyle(
            color: colors.textTertiary,
            fontSize: 12.5,
            fontWeight: FontWeight.w500,
          ),
        ),
        const SizedBox(height: 6),
        Row(
          children: [
            Expanded(
              child: Text(
                display,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(
                  color: colors.textPrimary,
                  fontSize: 20,
                  fontWeight: FontWeight.w600,
                  letterSpacing: 2,
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
                tooltip:
                    _passwordVisible ? translate("Hide") : translate("Show"),
                size: 28,
                iconSize: 16,
                onTap: () {
                  setState(() => _passwordVisible = !_passwordVisible);
                },
              ),
            if (showOneTime)
              AnimatedRotationWidget(
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
      ],
    );
  }

  RxBool? _svcStoppedRx() {
    if (!Get.isRegistered<RxBool>(tag: 'stop-service')) {
      return null;
    }
    return Get.find<RxBool>(tag: 'stop-service');
  }
}
