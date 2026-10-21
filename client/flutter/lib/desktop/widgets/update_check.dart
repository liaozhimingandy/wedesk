// 客户端检查更新。
//
// 数据源是官网静态目录下的 update.json：
//   https://www.alsoapp.com/static/wedesk/update.json
// 由运维在发布新版本时更新该文件，客户端启动时（勾选"自动更新版本"后）
// 或用户在"关于"页点"检查更新"时拉取。
//
// 刻意不复用 Rust 侧 updater.rs 那条链路：那是为官方 api.rustdesk.com +
// GitHub Release（allowlist / sha256 / MSI 提权安装）设计的，与自有 JSON
// 格式不匹配；wedesk 私有部署客户端也不允许访问外网更新源
// （common.rs 的 do_check_software_update 对 custom client 直接早退）。
// 这里做成纯 Dart 的轻量检查：比较版本号 → 有更新就引导用户去下载页。
library;

import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_hbb/common.dart';
import 'package:flutter_hbb/common/widgets/wedesign.dart';
import 'package:flutter_hbb/consts.dart';
import 'package:flutter_hbb/models/platform_model.dart';
import 'package:http/http.dart' as http;
import 'package:url_launcher/url_launcher.dart';

/// 更新信息 JSON 的固定地址。
const String kUpdateJsonUrl = 'https://www.alsoapp.com/static/wedesk/update.json';

/// 单次检查的最长等待时间。官网静态目录挂了的话不至于让按钮一直转圈。
const Duration _kCheckTimeout = Duration(seconds: 10);

enum UpdateCheckStatus {
  /// 服务端版本 <= 当前版本。
  upToDate,

  /// 服务端版本 > 当前版本。
  hasUpdate,

  /// 网络 / 解析失败。
  failed,
}

/// 一条更新记录（已按当前平台选好下载地址）。
class WedeskUpdateInfo {
  final String version;

  /// 当前平台的安装包地址；JSON 里没给时为空（此时只提示版本，不提供下载）。
  final String? downloadUrl;

  /// 更新说明，支持字符串或 {"zh-cn": "...", "en": "..."} 多语言对象。
  final String notes;

  /// 强制更新：弹窗不可关闭，只留"立即更新"。
  final bool mandatory;

  /// 发布日期（原样展示）。
  final String date;

  const WedeskUpdateInfo({
    required this.version,
    this.downloadUrl,
    this.notes = '',
    this.mandatory = false,
    this.date = '',
  });
}

class UpdateCheckResult {
  final UpdateCheckStatus status;
  final WedeskUpdateInfo? info;

  /// status == failed 时的原因（已翻译成用户可读文案）。
  final String error;

  const UpdateCheckResult(this.status, this.info, this.error);

  const UpdateCheckResult.upToDate()
      : status = UpdateCheckStatus.upToDate,
        info = null,
        error = '';

  const UpdateCheckResult.hasUpdate(WedeskUpdateInfo updateInfo)
      : status = UpdateCheckStatus.hasUpdate,
        info = updateInfo,
        error = '';

  const UpdateCheckResult.failed(this.error)
      : status = UpdateCheckStatus.failed,
        info = null;
}

String _currentPlatformKey() {
  if (Platform.isWindows) return 'windows';
  if (Platform.isMacOS) return 'macos';
  if (Platform.isLinux) return 'linux';
  if (Platform.isAndroid) return 'android';
  if (Platform.isIOS) return 'ios';
  return 'unknown';
}

/// 按当前平台取下载地址：platforms.<平台>.url 优先，顶层 url 兜底。
String? _pickDownloadUrl(Map<String, dynamic> json) {
  final platforms = json['platforms'];
  if (platforms is Map) {
    final entry = platforms[_currentPlatformKey()];
    if (entry is Map) {
      final url = entry['url'];
      if (url is String && url.trim().isNotEmpty) return url.trim();
    }
  }
  final top = json['url'];
  if (top is String && top.trim().isNotEmpty) return top.trim();
  return null;
}

/// notes 支持纯字符串或按语言键的对象；取不到就用英文，再退化到第一个值。
String _pickNotes(dynamic raw) {
  if (raw is String) return raw.trim();
  if (raw is Map) {
    // localeName 形如 "zh_CN"，JSON 里按 lang key 习惯写成 "zh-cn"。
    final localeKey = localeName.replaceAll('_', '-');
    final byLocale = raw[localeKey] ?? raw['en'];
    if (byLocale is String && byLocale.trim().isNotEmpty) {
      return byLocale.trim();
    }
    for (final v in raw.values) {
      if (v is String && v.trim().isNotEmpty) return v.trim();
    }
  }
  return '';
}

/// 拉取 update.json 并与当前版本比较。
///
/// 任何异常都收敛成 [UpdateCheckResult.failed]，由调用方决定怎么提示——
/// 检查更新失败不应该打断正常使用。
Future<UpdateCheckResult> checkWedeskUpdate() async {
  try {
    final resp = await http
        .get(Uri.parse(kUpdateJsonUrl))
        .timeout(_kCheckTimeout);
    if (resp.statusCode != 200) {
      return UpdateCheckResult.failed('HTTP ${resp.statusCode}');
    }
    final Object? decoded = jsonDecode(resp.body);
    if (decoded is! Map<String, dynamic>) {
      return UpdateCheckResult.failed('bad json');
    }
    final version = decoded['version'];
    if (version is! String || version.trim().isEmpty) {
      return UpdateCheckResult.failed('no version');
    }
    final info = WedeskUpdateInfo(
      // 容忍运维写 "v1.2.3"：versionToNumber 只认纯数字段。
      version: version.trim().replaceFirst(RegExp(r'^[vV]'), ''),
      downloadUrl: _pickDownloadUrl(decoded),
      notes: _pickNotes(decoded['notes']),
      mandatory: decoded['mandatory'] == true,
      date: decoded['date'] is String ? decoded['date'] as String : '',
    );
    final current = await bind.mainGetVersion();
    if (versionCmp(info.version, current) > 0) {
      return UpdateCheckResult.hasUpdate(info);
    }
    return const UpdateCheckResult.upToDate();
  } catch (e) {
    return UpdateCheckResult.failed(e.toString());
  }
}

Future<void> _openDownloadUrl(String url) async {
  final uri = Uri.tryParse(url);
  if (uri == null) {
    showToast('Invalid url: $url');
    return;
  }
  // externalApplication：让系统浏览器接管，客户端内不嵌网页。
  if (!await launchUrl(uri, mode: LaunchMode.externalApplication)) {
    showToast('Failed to open $url');
  }
}

/// "发现新版本"弹窗。mandatory 时不可关闭（barrierDismissible=false、
/// 不显示"稍后"、Esc/点遮罩不关）。
Future<void> showWedeskUpdateDialog(
    BuildContext context, WedeskUpdateInfo info) async {
  final colors = WeColors.of(context);
  final current = await bind.mainGetVersion();
  final notes = info.notes.isNotEmpty
      ? info.notes
      : (info.date.isNotEmpty ? info.date : translate('No release notes'));
  final canDownload = info.downloadUrl != null;

  final actions = <Widget>[
    if (!info.mandatory)
      WeOutlineButton(
        text: translate('Later'),
        onPressed: () => Navigator.of(context).pop(),
      ),
    if (canDownload)
      WePrimaryButton(
        text: translate('Update now'),
        onPressed: () {
          Navigator.of(context).pop();
          _openDownloadUrl(info.downloadUrl!);
        },
      ),
  ];

  await showDialog<void>(
    context: context,
    // 强制更新时不允许点遮罩关闭。
    barrierDismissible: !info.mandatory,
    builder: (_) => CustomAlertDialog(
      title: Row(
        children: [
          Icon(Icons.system_update_rounded, color: colors.brand, size: 22),
          const SizedBox(width: 8),
          Text('${translate('New version available')}  V${info.version}'),
        ],
      ),
      content: ConstrainedBox(
        constraints: const BoxConstraints(minWidth: 380),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(
              '${translate('Current version')} V$current  →  V${info.version}',
              style: TextStyle(fontSize: 13, color: colors.textSecondary),
            ),
            const SizedBox(height: 10),
            Text(translate('Release notes'),
                style: const TextStyle(
                    fontSize: 13, fontWeight: FontWeight.w600)),
            const SizedBox(height: 4),
            // 说明文案由服务端下发，必须可滚动 + 可选中，长文本不能撑破弹窗。
            Flexible(
              child: SingleChildScrollView(
                child: SelectionArea(child: Text(notes)),
              ),
            ),
            if (!canDownload) ...[
              const SizedBox(height: 10),
              Text(
                translate('Download url is not provided for this platform'),
                style: TextStyle(fontSize: 12.5, color: colors.textSecondary),
              ),
            ],
          ],
        ),
      ),
      actions: actions.isEmpty
          ? [
              WePrimaryButton(
                text: translate('OK'),
                onPressed: () => Navigator.of(context).pop(),
              )
            ]
          : actions,
    ),
  );
}

/// "已是最新版本 / 检查失败"提示。轻量场景，一个确定按钮即可。
Future<void> showUpdateCheckInfoDialog(
    BuildContext context, String message) async {
  await showDialog<void>(
    context: context,
    builder: (_) => CustomAlertDialog(
      title: Text(translate('Check update')),
      content: Text(message),
      actions: [
        WePrimaryButton(
          text: translate('OK'),
          onPressed: () => Navigator.of(context).pop(),
        ),
      ],
    ),
  );
}

/// 设置页"检查更新"按钮：自带进行中态，点击后按结果弹窗 / 提示。
class CheckUpdateButton extends StatefulWidget {
  const CheckUpdateButton({Key? key}) : super(key: key);

  @override
  State<CheckUpdateButton> createState() => _CheckUpdateButtonState();
}

class _CheckUpdateButtonState extends State<CheckUpdateButton> {
  bool _checking = false;

  Future<void> _onPressed() async {
    if (_checking) return;
    setState(() => _checking = true);
    final result = await checkWedeskUpdate();
    if (!mounted) return;
    setState(() => _checking = false);
    switch (result.status) {
      case UpdateCheckStatus.hasUpdate:
        await showWedeskUpdateDialog(context, result.info!);
        break;
      case UpdateCheckStatus.upToDate:
        showToast(translate('You are up to date'));
        break;
      case UpdateCheckStatus.failed:
        await showUpdateCheckInfoDialog(
            context, '${translate('Check update failed')}\n${result.error}');
        break;
    }
  }

  @override
  Widget build(BuildContext context) {
    // WePrimaryButton 没有 loading 形参，检查中直接把文案换成"正在检查…"，
    // 同时禁用点击，避免连点产生并发请求。
    return WePrimaryButton(
      text: _checking
          ? translate('Checking for updates...')
          : translate('Check update'),
      height: 32,
      fontSize: 13,
      enabled: !_checking,
      onPressed: _onPressed,
      padding: const EdgeInsets.symmetric(horizontal: 16),
    );
  }
}

/// 启动时自动检查：仅在"自动更新版本"勾选时触发。
///
/// 延迟 [delay] 是为了避开首屏渲染和登录/连接流程，避免和用户操作抢带宽、
/// 抢对话框。发现新版本时用全局 navigator 弹窗（此时用户可能不在设置页）。
Future<void> autoCheckUpdateOnStartup({
  Duration delay = const Duration(seconds: 8),
}) async {
  await Future.delayed(delay);
  bool enabled = false;
  try {
    enabled = await mainGetBoolOption(kOptionAllowAutoUpdate);
  } catch (_) {
    // 配置读不到（极端情况）时按未勾选处理，不弹窗。
    return;
  }
  if (!enabled) return;
  final result = await checkWedeskUpdate();
  if (result.status != UpdateCheckStatus.hasUpdate || result.info == null) {
    return;
  }
  final ctx = globalKey.currentContext;
  if (ctx == null) return;
  await showWedeskUpdateDialog(ctx, result.info!);
}
