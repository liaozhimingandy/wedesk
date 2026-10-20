// ToDesk style left sidebar: logo on top, vertical navigation below
// ("remote control" / "device list"), settings pinned to the bottom.
//
// The sidebar replaces the old content column: everything that used to live
// on the left pane (device card, help cards, tips) moves into the main
// content area, and this widget becomes pure navigation.

import 'package:flutter/material.dart';
import 'package:flutter_hbb/common.dart';
import 'package:flutter_hbb/common/widgets/wedesign.dart';
import 'package:flutter_hbb/models/platform_model.dart';

class WeSideMenu extends StatefulWidget {
  const WeSideMenu({
    Key? key,
    required this.selectedIndex,
    required this.onSelect,
    this.onSettings,
  }) : super(key: key);

  final int selectedIndex;
  final ValueChanged<int> onSelect;
  final VoidCallback? onSettings;

  @override
  State<WeSideMenu> createState() => _WeSideMenuState();
}

class _WeSideMenuState extends State<WeSideMenu> {
  @override
  Widget build(BuildContext context) {
    final colors = WeColors.of(context);
    final items = [
      (Icons.desktop_windows_outlined, translate('Remote control')),
      (Icons.devices_outlined, translate('Device list')),
    ];
    return Container(
      width: 208,
      color: colors.surfaceAlt,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          SizedBox(height: WeSpace.l),
          Align(
            alignment: Alignment.center,
            child: loadLogo(),
          ),
          SizedBox(height: WeSpace.xl),
          for (var i = 0; i < items.length; i++)
            _NavTile(
              icon: items[i].$1,
              label: items[i].$2,
              selected: widget.selectedIndex == i,
              onTap: () => widget.onSelect(i),
            ),
          const Spacer(),
          if (widget.onSettings != null)
            _NavTile(
              icon: Icons.settings_outlined,
              label: translate('Settings'),
              selected: false,
              onTap: widget.onSettings!,
            ),
          if (bind.isCustomClient())
            Padding(
              padding: const EdgeInsets.only(bottom: WeSpace.s),
              child: Align(
                alignment: Alignment.center,
                child: loadPowered(context),
              ),
            ),
        ],
      ),
    );
  }
}

class _NavTile extends StatefulWidget {
  const _NavTile({
    Key? key,
    required this.icon,
    required this.label,
    required this.selected,
    required this.onTap,
  }) : super(key: key);

  final IconData icon;
  final String label;
  final bool selected;
  final VoidCallback onTap;

  @override
  State<_NavTile> createState() => _NavTileState();
}

class _NavTileState extends State<_NavTile> {
  bool _hover = false;

  @override
  Widget build(BuildContext context) {
    final colors = WeColors.of(context);
    final fg = widget.selected
        ? colors.brand
        : (_hover ? colors.textPrimary : colors.textSecondary);
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: WeSpace.m, vertical: 3),
      child: InkWell(
        borderRadius: WeRadii.mdRadius,
        onTap: widget.onTap,
        onHover: (v) {
          if (_hover != v) {
            setState(() => _hover = v);
          }
        },
        child: Container(
          height: 42,
          padding: const EdgeInsets.symmetric(horizontal: WeSpace.m),
          decoration: BoxDecoration(
            borderRadius: WeRadii.mdRadius,
            color: widget.selected
                ? colors.brandSoft
                : (_hover ? colors.surface : Colors.transparent),
            border: widget.selected
                ? Border.all(color: colors.brand.withOpacity(0.35), width: 1)
                : null,
          ),
          child: Row(
            children: [
              Icon(widget.icon, size: 20, color: fg),
              const SizedBox(width: WeSpace.m),
              Expanded(
                child: Text(
                  widget.label,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    fontSize: 14,
                    fontWeight:
                        widget.selected ? FontWeight.w600 : FontWeight.w500,
                    color: fg,
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
