// ToDesk style "allow control" section: a switch row (mapped to the
// stop-service option) followed by a light device card with the device code
// on the left and the connection password on the right, divided by a hair
// line — mirroring the reference layout (设备代码 | 连接模式).

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_hbb/common.dart';
import 'package:flutter_hbb/common/widgets/animated_rotation_widget.dart';
import 'package:flutter_hbb/desktop/pages/desktop_home_page.dart';
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

  /// "permanent-password-set" 的查询 future。缓存住是为了让 FutureBuilder
  /// 不在每次 rebuild 时重新发 FFI 请求（future 实例不变才不重跑）。
  Future<String>? _permSetFuture;

  Future<String> get _permSet =>
      _permSetFuture ??= bind.mainGetCommon(key: "permanent-password-set");

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
      // 竖向内边距收紧（l→m）：右侧列本身较高（下拉+密码行），
      // 左侧 ID 垂直居中后上下留白随 padding 放大，观感空旷。
      padding: const EdgeInsets.symmetric(
          horizontal: WeSpace.xl, vertical: WeSpace.m),
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
            child: Container(width: 1, height: 60, color: colors.border),
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
        const SizedBox(height: 4),
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
                    fontSize: 30,
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
              size: 32,
              iconSize: 18,
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
    final method = model.verificationMethod;
    final noPassword = model.approveMode == 'click';
    final showOneTime = !noPassword &&
        (method == kUseTemporaryPassword || method == kUseBothPasswords);
    final showPermanent = !noPassword &&
        (method == kUsePermanentPassword || method == kUseBothPasswords);

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
        const SizedBox(height: 4),
        // 连接模式（一次性密码 / 固定密码 / 两者都用）直接在这里切，
        // 不用再进设置页。写的是 kOptionVerificationMethod，与设置页同一个选项。
        ComboBox(
          enabled: !bind.isDisableSettings(),
          keys: const [
            kUseTemporaryPassword,
            kUsePermanentPassword,
            kUseBothPasswords,
          ],
          values: [
            translate('Use one-time password'),
            translate('Use permanent password'),
            translate('Use both passwords'),
          ],
          initialKey: method,
          onChanged: (key) => _onMethodChanged(model, key),
        ),
        const SizedBox(height: 8),
        if (showOneTime) _buildOneTimeRow(context, model),
        if (showPermanent) _buildPermanentRow(context, model),
        if (noPassword)
          Text(
            '-',
            style: TextStyle(
              color: colors.textPrimary,
              fontSize: 20,
              fontWeight: FontWeight.w600,
            ),
          ),
      ],
    );
  }

  /// 切换连接模式。与设置页同规则：选到固定密码但还没设过时，先弹设置框。
  Future<void> _onMethodChanged(ServerModel model, String key) async {
    Future<void> apply() async {
      await model.setVerificationMethod(key);
      await model.updatePasswordModel();
      _permSetFuture = null;
    }

    if (key == kUsePermanentPassword || key == kUseBothPasswords) {
      final set = await bind.mainGetCommon(key: "permanent-password-set");
      if (set != "true" && !isChangePermanentPasswordDisabled()) {
        setPasswordDialog(notEmptyCallback: apply);
        return;
      }
    }
    await apply();
  }

  Widget _buildOneTimeRow(BuildContext context, ServerModel model) {
    final colors = WeColors.of(context);
    final passwd = model.serverPasswd.text.trim();
    final masked =
        List<String>.filled(passwd.isEmpty ? 6 : passwd.length, '•').join();
    final display = _passwordVisible ? passwd : masked;

    return Row(
      children: [
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                translate('Use one-time password'),
                style: TextStyle(
                  color: colors.textTertiary,
                  fontSize: 11.5,
                  fontWeight: FontWeight.w500,
                ),
              ),
              const SizedBox(height: 2),
              Text(
                display,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(
                  color: colors.textPrimary,
                  fontSize: 20,
                  fontWeight: FontWeight.w600,
                  letterSpacing: 2,
                ),
              ),
            ],
          ),
        ),
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
        WeIconButton(
          icon: _passwordVisible
              ? Icons.visibility_outlined
              : Icons.visibility_off_outlined,
          tooltip: _passwordVisible ? translate("Hide") : translate("Show"),
          size: 28,
          iconSize: 16,
          onTap: () {
            setState(() => _passwordVisible = !_passwordVisible);
          },
        ),
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
      ],
    );
  }

  /// 固定密码只能写不能读（Rust 侧只暴露 "permanent-password-set" 布尔），
  /// 所以这里显示"已设置/未设置" + 设置入口，不显示明文。
  Widget _buildPermanentRow(BuildContext context, ServerModel model) {
    final colors = WeColors.of(context);
    return Padding(
      padding: const EdgeInsets.only(top: WeSpace.s),
      child: FutureBuilder<String>(
        future: _permSet,
        builder: (context, snapshot) {
          final isSet = snapshot.data == "true";
          return Row(
            children: [
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      translate('Permanent password'),
                      style: TextStyle(
                        color: colors.textTertiary,
                        fontSize: 11.5,
                        fontWeight: FontWeight.w500,
                      ),
                    ),
                    const SizedBox(height: 2),
                    Text(
                      isSet ? '••••••' : translate('Not set'),
                      style: TextStyle(
                        color: isSet ? colors.textPrimary : colors.textTertiary,
                        fontSize: 20,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  ],
                ),
              ),
              WeIconButton(
                icon: Icons.edit_outlined,
                tooltip: translate('Set permanent password'),
                size: 28,
                iconSize: 16,
                onTap: () {
                  setPasswordDialog(notEmptyCallback: () {
                    _permSetFuture = null;
                    setState(() {});
                  });
                },
              ),
            ],
          );
        },
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
