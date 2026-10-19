// WeDesign: the design language of the wedesk desktop client.
//
// It borrows the visual grammar of ToDesk: a blue brand color, generous
// rounded cards, clear hierarchy between "my device" and "remote device",
// and affordances (copy / reveal / refresh) that are visible instead of
// hidden behind double taps.
//
// Everything here is theme aware: pick the palette with [WeColors.of] so the
// light and dark themes stay in sync without duplicating code.

import 'package:flutter/material.dart';

import 'package:flutter_hbb/common/formatter/id_formatter.dart';

/// Brand and semantic colors.
class WeColors {
  // 注意：这里**不能**写 `WeColors._();` —— 类里有 final 实例字段时，
  // 空的生成构造函数无法初始化它们，Dart 会报
  // "Final field 'brand' is not initialized by this constructor"。
  // 想禁止直接实例化就只留下面那个 const 构造。

  /// ToDesk-like blue. Used as the primary action color.
  static const Color brandLight = Color(0xFF1E71FF);
  static const Color brandDark = Color(0xFF3E86FF);

  /// Second stop of the brand gradient (used on the device card header).
  static const Color brandEndLight = Color(0xFF4C9BFF);
  static const Color brandEndDark = Color(0xFF1B5FE0);

  static const Color successLight = Color(0xFF17B26A);
  static const Color successDark = Color(0xFF2BCF80);

  static const Color warnLight = Color(0xFFF79009);
  static const Color warnDark = Color(0xFFFFA733);

  static const Color dangerLight = Color(0xFFF04438);
  static const Color dangerDark = Color(0xFFFF5C50);

  static const Color _textPrimaryLight = Color(0xFF1D2939);
  static const Color _textSecondaryLight = Color(0xFF667085);
  static const Color _textTertiaryLight = Color(0xFF98A2B3);
  static const Color _textPrimaryDark = Color(0xFFE4E7EC);
  static const Color _textSecondaryDark = Color(0xFFA6ADB8);
  static const Color _textTertiaryDark = Color(0xFF7A8290);

  static const Color _surfaceLight = Color(0xFFFFFFFF);
  static const Color _surfaceAltLight = Color(0xFFF5F7FA);
  static const Color _borderLight = Color(0xFFE4E7EC);
  static const Color _surfaceDark = Color(0xFF1E2026);
  static const Color _surfaceAltDark = Color(0xFF26282F);
  static const Color _borderDark = Color(0xFF35383F);

  /// Resolved palette for the current brightness.
  final Color brand;
  final Color brandEnd;
  final Color brandSoft;
  final Color success;
  final Color warn;
  final Color danger;
  final Color textPrimary;
  final Color textSecondary;
  final Color textTertiary;
  final Color surface;
  final Color surfaceAlt;
  final Color border;

  const WeColors({
    required this.brand,
    required this.brandEnd,
    required this.brandSoft,
    required this.success,
    required this.warn,
    required this.danger,
    required this.textPrimary,
    required this.textSecondary,
    required this.textTertiary,
    required this.surface,
    required this.surfaceAlt,
    required this.border,
  });

  static final WeColors light = const WeColors(
    brand: brandLight,
    brandEnd: brandEndLight,
    brandSoft: Color(0x141E71FF),
    success: successLight,
    warn: warnLight,
    danger: dangerLight,
    textPrimary: _textPrimaryLight,
    textSecondary: _textSecondaryLight,
    textTertiary: _textTertiaryLight,
    surface: _surfaceLight,
    surfaceAlt: _surfaceAltLight,
    border: _borderLight,
  );

  static final WeColors dark = const WeColors(
    brand: brandDark,
    brandEnd: brandEndDark,
    brandSoft: Color(0x243E86FF),
    success: successDark,
    warn: warnDark,
    danger: dangerDark,
    textPrimary: _textPrimaryDark,
    textSecondary: _textSecondaryDark,
    textTertiary: _textTertiaryDark,
    surface: _surfaceDark,
    surfaceAlt: _surfaceAltDark,
    border: _borderDark,
  );

  static WeColors of(BuildContext context) {
    final brightness = Theme.of(context).brightness;
    return brightness == Brightness.dark ? dark : light;
  }

  static bool isDark(BuildContext context) =>
      Theme.of(context).brightness == Brightness.dark;

  /// Brand gradient used on headers and primary buttons.
  static LinearGradient brandGradient(BuildContext context,
      [AlignmentGeometry begin = Alignment.centerLeft,
      AlignmentGeometry end = Alignment.centerRight]) {
    final c = of(context);
    return LinearGradient(
      begin: begin,
      end: end,
      colors: [c.brand, c.brandEnd],
    );
  }
}

/// Corner radii.
class WeRadii {
  WeRadii._();

  static const double sm = 6.0;
  static const double md = 10.0;
  static const double lg = 14.0;
  static const double xl = 20.0;

  static BorderRadius get smRadius => BorderRadius.circular(sm);
  static BorderRadius get mdRadius => BorderRadius.circular(md);
  static BorderRadius get lgRadius => BorderRadius.circular(lg);
  static BorderRadius get xlRadius => BorderRadius.circular(xl);
}

/// Spacing scale.
class WeSpace {
  WeSpace._();

  static const double xs = 4.0;
  static const double s = 8.0;
  static const double m = 12.0;
  static const double l = 16.0;
  static const double xl = 20.0;
  static const double xxl = 24.0;
}

/// Shadows.
class WeShadow {
  WeShadow._();

  static List<BoxShadow> card(BuildContext context) => [
        BoxShadow(
          color: Colors.black.withOpacity(WeColors.isDark(context) ? 0.36 : 0.06),
          blurRadius: 12,
          offset: const Offset(0, 2),
        ),
      ];

  static List<BoxShadow> popup(BuildContext context) => [
        BoxShadow(
          color: Colors.black.withOpacity(WeColors.isDark(context) ? 0.5 : 0.16),
          blurRadius: 20,
          offset: const Offset(0, 6),
        ),
      ];
}

/// A rounded, theme aware surface with an optional tap handler.
class WeCard extends StatelessWidget {
  const WeCard({
    Key? key,
    required this.child,
    this.padding,
    this.margin,
    this.color,
    this.borderRadius,
    this.border,
    this.onTap,
    this.elevation = true,
  }) : super(key: key);

  final Widget child;
  final EdgeInsetsGeometry? padding;
  final EdgeInsetsGeometry? margin;
  final Color? color;
  final BorderRadius? borderRadius;
  final BoxBorder? border;
  final VoidCallback? onTap;
  final bool elevation;

  @override
  Widget build(BuildContext context) {
    final colors = WeColors.of(context);
    final radius = borderRadius ?? WeRadii.lgRadius;
    Widget content = Container(
      padding: padding,
      decoration: BoxDecoration(
        color: color ?? colors.surface,
        borderRadius: radius,
        border: border ?? Border.all(color: colors.border, width: 1),
        boxShadow: elevation ? WeShadow.card(context) : null,
      ),
      child: child,
    );
    if (onTap != null) {
      content = InkWell(
        borderRadius: radius,
        onTap: onTap,
        child: content,
      );
    }
    if (margin != null) {
      content = Padding(padding: margin!, child: content);
    }
    return content;
  }
}

/// Square / circular icon button with hover feedback and tooltip.
class WeIconButton extends StatefulWidget {
  const WeIconButton({
    Key? key,
    required this.icon,
    required this.onTap,
    this.tooltip,
    this.size = 32,
    this.iconSize = 17,
    this.color,
    this.hoverColor,
    this.activeColor,
    this.active = false,
    this.onTapDown,
  }) : super(key: key);

  final IconData icon;
  final VoidCallback onTap;
  final String? tooltip;
  final double size;
  final double iconSize;
  final Color? color;
  final Color? hoverColor;
  final Color? activeColor;
  final bool active;
  final void Function(TapDownDetails details)? onTapDown;

  @override
  State<WeIconButton> createState() => _WeIconButtonState();
}

class _WeIconButtonState extends State<WeIconButton> {
  bool _hover = false;

  @override
  Widget build(BuildContext context) {
    final colors = WeColors.of(context);
    final base = widget.color ?? colors.textSecondary;
    final highlight =
        widget.activeColor ?? (widget.hoverColor ?? colors.brand);
    Widget child = InkWell(
      borderRadius: BorderRadius.circular(widget.size / 2),
      onTap: widget.onTap,
      onTapDown: widget.onTapDown,
      onHover: (v) {
        if (_hover != v) {
          setState(() => _hover = v);
        }
      },
      child: SizedBox(
        width: widget.size,
        height: widget.size,
        child: Icon(
          widget.icon,
          size: widget.iconSize,
          color: (_hover || widget.active) ? highlight : base,
        ),
      ),
    );
    if (widget.tooltip != null) {
      child = Tooltip(message: widget.tooltip!, child: child);
    }
    return child;
  }
}

/// Primary action button with the brand gradient.
class WePrimaryButton extends StatefulWidget {
  const WePrimaryButton({
    Key? key,
    required this.text,
    required this.onPressed,
    this.icon,
    this.height = 40,
    this.width,
    this.padding = const EdgeInsets.symmetric(horizontal: 20),
    this.radius = WeRadii.md,
    this.enabled = true,
    this.fontSize = 15,
  }) : super(key: key);

  final String text;
  final VoidCallback? onPressed;
  final IconData? icon;
  final double height;
  final double? width;
  final EdgeInsetsGeometry padding;
  final double radius;
  final bool enabled;
  final double fontSize;

  @override
  State<WePrimaryButton> createState() => _WePrimaryButtonState();
}

class _WePrimaryButtonState extends State<WePrimaryButton> {
  bool _hover = false;

  @override
  Widget build(BuildContext context) {
    final colors = WeColors.of(context);
    final onTap = widget.enabled ? widget.onPressed : null;
    final decoration = BoxDecoration(
      borderRadius: BorderRadius.circular(widget.radius),
      gradient: widget.enabled
          ? WeColors.brandGradient(
              context, Alignment.topLeft, Alignment.bottomRight)
          : null,
      color: widget.enabled
          ? null
          : (WeColors.isDark(context)
              ? Colors.white.withOpacity(0.12)
              : const Color(0xFFE4E7EC)),
      boxShadow: widget.enabled && _hover
          ? [
              BoxShadow(
                color: colors.brand.withOpacity(0.32),
                blurRadius: 10,
                offset: const Offset(0, 3),
              ),
            ]
          : null,
    );
    final textColor = widget.enabled
        ? Colors.white
        : (WeColors.isDark(context)
            ? Colors.white.withOpacity(0.4)
            : const Color(0xFF98A2B3));
    Widget child = Row(
      mainAxisAlignment: MainAxisAlignment.center,
      mainAxisSize: MainAxisSize.min,
      children: [
        if (widget.icon != null)
          Icon(widget.icon, size: widget.fontSize + 2, color: textColor),
        if (widget.icon != null) const SizedBox(width: 6),
        Flexible(
          child: Text(
            widget.text,
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: TextStyle(
              color: textColor,
              fontSize: widget.fontSize,
              fontWeight: FontWeight.w600,
            ),
          ),
        ),
      ],
    );
    child = Container(
      height: widget.height,
      width: widget.width,
      padding: widget.padding,
      decoration: decoration,
      child: child,
    );
    return InkWell(
      borderRadius: BorderRadius.circular(widget.radius),
      onTap: onTap,
      onHover: (v) {
        if (_hover != v) {
          setState(() => _hover = v);
        }
      },
      child: child,
    );
  }
}

/// Secondary (outlined) button paired with [WePrimaryButton].
class WeOutlineButton extends StatefulWidget {
  const WeOutlineButton({
    Key? key,
    required this.text,
    required this.onPressed,
    this.icon,
    this.height = 40,
    this.width,
    this.radius = WeRadii.md,
    this.fontSize = 14,
  }) : super(key: key);

  final String text;
  final VoidCallback? onPressed;
  final IconData? icon;
  final double height;
  final double? width;
  final double radius;
  final double fontSize;

  @override
  State<WeOutlineButton> createState() => _WeOutlineButtonState();
}

class _WeOutlineButtonState extends State<WeOutlineButton> {
  bool _hover = false;

  @override
  Widget build(BuildContext context) {
    final colors = WeColors.of(context);
    final borderColor = _hover ? colors.brand : colors.border;
    final textColor = _hover ? colors.brand : colors.textSecondary;
    return InkWell(
      borderRadius: BorderRadius.circular(widget.radius),
      onTap: widget.onPressed,
      onHover: (v) {
        if (_hover != v) {
          setState(() => _hover = v);
        }
      },
      child: Container(
        height: widget.height,
        width: widget.width,
        padding: const EdgeInsets.symmetric(horizontal: 16),
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(widget.radius),
          border: Border.all(color: borderColor, width: 1),
          color: _hover ? colors.brandSoft : Colors.transparent,
        ),
        child: Row(
          mainAxisAlignment: MainAxisAlignment.center,
          mainAxisSize: MainAxisSize.min,
          children: [
            if (widget.icon != null)
              Icon(widget.icon, size: widget.fontSize + 2, color: textColor),
            if (widget.icon != null) const SizedBox(width: 6),
            Flexible(
              child: Text(
                widget.text,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(
                  color: textColor,
                  fontSize: widget.fontSize,
                  fontWeight: FontWeight.w500,
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

/// Small rounded status pill, e.g. "Online" / "Ready".
class WeStatusPill extends StatelessWidget {
  const WeStatusPill({
    Key? key,
    required this.text,
    required this.color,
    this.icon,
    this.filled = false,
  }) : super(key: key);

  final String text;
  final Color color;
  final IconData? icon;
  final bool filled;

  @override
  Widget build(BuildContext context) {
    final bg = filled ? color : color.withOpacity(0.12);
    final fg = filled ? Colors.white : color;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
      decoration: BoxDecoration(
        color: bg,
        borderRadius: BorderRadius.circular(20),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          if (icon != null)
            Padding(
              padding: const EdgeInsets.only(right: 4),
              child: Icon(icon, size: 12, color: fg),
            )
          else
            Container(
              width: 6,
              height: 6,
              margin: const EdgeInsets.only(right: 5),
              decoration: BoxDecoration(color: fg, shape: BoxShape.circle),
            ),
          Text(
            text,
            style: TextStyle(
              color: fg,
              fontSize: 12,
              fontWeight: FontWeight.w500,
            ),
          ),
        ],
      ),
    );
  }
}

/// Section title used above card groups.
class WeSectionTitle extends StatelessWidget {
  const WeSectionTitle(this.text, {Key? key, this.trailing}) : super(key: key);

  final String text;
  final Widget? trailing;

  @override
  Widget build(BuildContext context) {
    final colors = WeColors.of(context);
    return Row(
      children: [
        Text(
          text,
          style: TextStyle(
            fontSize: 14,
            fontWeight: FontWeight.w600,
            color: colors.textPrimary,
          ),
        ),
        const Spacer(),
        if (trailing != null) trailing!,
      ],
    );
  }
}

/// Split an id into groups of four characters: `123456789012` -> `1234 5678 9012`.
///
/// Reuses the shared [formatID] so that the card and the input field can never
/// disagree on the grouping.
String groupId(String id) => formatID(id);
