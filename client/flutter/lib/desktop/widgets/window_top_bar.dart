// ToDesk style window top bar: brand on the left, related operations on the
// right (quick start / help / settings / minimize / close).
//
// The native title bar is fully removed (window_manager's
// `TitleBarStyle.hidden` keeps the native caption buttons — see main.dart's
// `setAsFrameless` call), so this bar also doubles as the window's drag area.

import 'package:flutter/material.dart';
import 'package:flutter_hbb/common.dart';
import 'package:flutter_hbb/common/widgets/wedesign.dart';
import 'package:flutter_hbb/models/platform_model.dart';
import 'package:url_launcher/url_launcher.dart';
import 'package:window_manager/window_manager.dart';

class WeWindowTopBar extends StatelessWidget {
  const WeWindowTopBar({Key? key, this.onSettings}) : super(key: key);

  /// Opens the settings window; hidden entirely when settings are disabled.
  final VoidCallback? onSettings;

  static const _website = 'https://wedesk.alsoapp.com';

  @override
  Widget build(BuildContext context) {
    final colors = WeColors.of(context);
    return GestureDetector(
      behavior: HitTestBehavior.translucent,
      onPanStart: (_) => windowManager.startDragging(),
      child: Container(
        height: 46,
        decoration: BoxDecoration(
          color: colors.surface,
          border: Border(bottom: BorderSide(color: colors.border, width: 1)),
        ),
        padding: const EdgeInsets.symmetric(horizontal: WeSpace.l),
        child: Row(
          children: [
            loadIcon(22),
            const SizedBox(width: WeSpace.s),
            Text(
              bind.mainGetAppNameSync(),
              style: TextStyle(
                color: colors.textPrimary,
                fontSize: 15,
                fontWeight: FontWeight.w600,
              ),
            ),
            const Spacer(),
            _TopBarAction(
              icon: Icons.rocket_launch_outlined,
              tooltip: translate('Quick start'),
              onTap: () => launchUrl(Uri.parse(_website)),
            ),
            _TopBarAction(
              icon: Icons.help_outline_rounded,
              tooltip: translate('Help'),
              onTap: () => launchUrl(Uri.parse(_website)),
            ),
            if (onSettings != null)
              _TopBarAction(
                icon: Icons.settings_outlined,
                tooltip: translate('Settings'),
                onTap: onSettings!,
              ),
            _TopBarAction(
              icon: Icons.remove,
              tooltip: translate('Minimize'),
              onTap: () => windowManager.minimize(),
            ),
            _TopBarAction(
              icon: Icons.close,
              tooltip: translate('Close'),
              hoverColor: const Color(0xFFE81123),
              onTap: () => windowManager.close(),
            ),
          ],
        ),
      ),
    );
  }
}

/// One icon button of the top bar; turns white-on-red when hovered (close).
class _TopBarAction extends StatefulWidget {
  const _TopBarAction({
    Key? key,
    required this.icon,
    required this.tooltip,
    required this.onTap,
    this.hoverColor,
  }) : super(key: key);

  final IconData icon;
  final String tooltip;
  final VoidCallback onTap;

  /// Background while hovered; defaults to a neutral surface.
  final Color? hoverColor;

  @override
  State<_TopBarAction> createState() => _TopBarActionState();
}

class _TopBarActionState extends State<_TopBarAction> {
  bool _hover = false;

  @override
  Widget build(BuildContext context) {
    final colors = WeColors.of(context);
    final isClose = widget.hoverColor == const Color(0xFFE81123);
    return Tooltip(
      message: widget.tooltip,
      waitDuration: const Duration(milliseconds: 400),
      child: InkWell(
        borderRadius: BorderRadius.circular(6),
        onTap: widget.onTap,
        onHover: (v) {
          if (_hover != v) {
            setState(() => _hover = v);
          }
        },
        child: Container(
          width: 36,
          height: 32,
          margin: const EdgeInsets.only(left: 2),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(6),
            color: _hover
                ? (widget.hoverColor ?? colors.surfaceAlt)
                : Colors.transparent,
          ),
          child: Icon(
            widget.icon,
            size: 17,
            color: _hover && isClose
                ? Colors.white
                : (_hover ? colors.textPrimary : colors.textSecondary),
          ),
        ),
      ),
    );
  }
}
