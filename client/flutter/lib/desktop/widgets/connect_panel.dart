// ToDesk style connect panel: one wide search field for the remote device
// code with autocomplete, a dominant "remote control" action and small quick
// entries for the secondary connection types.

import 'package:flutter/material.dart';
import 'package:flutter_hbb/common.dart';
import 'package:flutter_hbb/common/formatter/id_formatter.dart';
import 'package:flutter_hbb/common/widgets/autocomplete.dart';
import 'package:flutter_hbb/common/widgets/connection_page_title.dart';
import 'package:flutter_hbb/consts.dart';
import 'package:flutter_hbb/desktop/widgets/material_mod_popup_menu.dart'
    as mod_menu;
import 'package:flutter_hbb/desktop/widgets/popup_menu.dart';
import 'package:flutter_hbb/common/widgets/wedesign.dart';
import 'package:flutter_hbb/models/peer_model.dart';
import 'package:get/get.dart';

typedef ConnectCallback = void Function({
  bool isFileTransfer,
  bool isViewCamera,
  bool isTerminal,
  bool isTcpTunneling,
});

class ConnectPanel extends StatefulWidget {
  const ConnectPanel({
    Key? key,
    required this.idController,
    required this.idEditingController,
    required this.focusNode,
    required this.idInputFocused,
    required this.allPeersLoader,
    required this.onConnect,
  }) : super(key: key);

  final IDTextEditingController idController;
  final TextEditingController idEditingController;
  final FocusNode focusNode;
  final RxBool idInputFocused;
  final AllPeersLoader allPeersLoader;
  final ConnectCallback onConnect;

  @override
  State<ConnectPanel> createState() => _ConnectPanelState();
}

class _ConnectPanelState extends State<ConnectPanel> {
  // https://github.com/flutter/flutter/issues/157244
  Iterable<Peer> _autocompleteOpts = [];
  final _menuOpen = false.obs;
  Offset _menuOffset = Offset.zero;

  @override
  Widget build(BuildContext context) {
    final colors = WeColors.of(context);
    return ConstrainedBox(
      constraints: const BoxConstraints(maxWidth: 720),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          getConnectionPageTitle(context, false),
          const SizedBox(height: WeSpace.xs),
          Text(
            translate('remote_control_hint'),
            style: TextStyle(fontSize: 12.5, color: colors.textTertiary),
          ),
          const SizedBox(height: WeSpace.m),
          Row(
            crossAxisAlignment: CrossAxisAlignment.center,
            children: [
              Expanded(child: _buildSearchField(context)),
              const SizedBox(width: WeSpace.m),
              WePrimaryButton(
                text: translate('Remote control'),
                icon: Icons.desktop_windows_outlined,
                height: 46,
                width: 152,
                onPressed: () => widget.onConnect(),
              ),
              const SizedBox(width: WeSpace.xs),
              _buildMoreButton(context),
            ],
          ),
          const SizedBox(height: WeSpace.xs),
          Wrap(
            crossAxisAlignment: WrapCrossAlignment.center,
            spacing: WeSpace.xs,
            children: [
              _buildQuickAction(
                context,
                icon: Icons.swap_vert_circle_outlined,
                text: translate('Transfer file'),
                onTap: () => widget.onConnect(isFileTransfer: true),
              ),
              _buildQuickAction(
                context,
                icon: Icons.videocam_outlined,
                text: translate('View camera'),
                onTap: () => widget.onConnect(isViewCamera: true),
              ),
            ],
          ),
        ],
      ),
    );
  }

  Widget _buildSearchField(BuildContext context) {
    final colors = WeColors.of(context);
    return Obx(() {
      final focused = widget.idInputFocused.value;
      return Container(
        height: 46,
        decoration: BoxDecoration(
          borderRadius: WeRadii.mdRadius,
          color: WeColors.isDark(context) ? colors.surfaceAlt : Colors.white,
          border: Border.all(
            color: focused ? colors.brand : colors.border,
            width: focused ? 1.6 : 1,
          ),
          boxShadow: focused
              ? [
                  BoxShadow(
                    color: colors.brand.withOpacity(0.14),
                    blurRadius: 8,
                    offset: const Offset(0, 1),
                  ),
                ]
              : null,
        ),
        child: Row(
          children: [
            Padding(
              padding: const EdgeInsets.only(left: 12, right: 8),
              child: Icon(
                Icons.search_rounded,
                size: 20,
                color: focused ? colors.brand : colors.textTertiary,
              ),
            ),
            Expanded(child: _buildAutocomplete(context)),
            ValueListenableBuilder<TextEditingValue>(
              valueListenable: widget.idEditingController,
              builder: (context, value, child) {
                if (value.text.isEmpty) {
                  return const SizedBox.shrink();
                }
                return Padding(
                  padding: const EdgeInsets.only(right: 6),
                  child: WeIconButton(
                    icon: Icons.close_rounded,
                    tooltip: translate('Clear'),
                    size: 28,
                    iconSize: 16,
                    onTap: () {
                      widget.idEditingController.clear();
                      widget.idController.id = '';
                      widget.focusNode.requestFocus();
                    },
                  ),
                );
              },
            ),
          ],
        ),
      );
    });
  }

  Widget _buildAutocomplete(BuildContext context) {
    return RawAutocomplete<Peer>(
      optionsBuilder: (TextEditingValue textEditingValue) {
        if (textEditingValue.text == '') {
          _autocompleteOpts = const Iterable<Peer>.empty();
        } else if (widget.allPeersLoader.peers.isEmpty &&
            !widget.allPeersLoader.isPeersLoaded) {
          Peer emptyPeer = Peer(
            id: '',
            username: '',
            hostname: '',
            alias: '',
            platform: '',
            tags: [],
            hash: '',
            password: '',
            forceAlwaysRelay: false,
            rdpPort: '',
            rdpUsername: '',
            loginName: '',
            device_group_name: '',
            note: '',
          );
          _autocompleteOpts = [emptyPeer];
        } else {
          String textWithoutSpaces = textEditingValue.text.replaceAll(" ", "");
          if (int.tryParse(textWithoutSpaces) != null) {
            textEditingValue = TextEditingValue(
              text: textWithoutSpaces,
              selection: textEditingValue.selection,
            );
          }
          String textToFind = textEditingValue.text.toLowerCase();
          _autocompleteOpts = widget.allPeersLoader.peers
              .where((peer) =>
                  peer.id.toLowerCase().contains(textToFind) ||
                  peer.username.toLowerCase().contains(textToFind) ||
                  peer.hostname.toLowerCase().contains(textToFind) ||
                  peer.alias.toLowerCase().contains(textToFind))
              .toList();
          widget.allPeersLoader.queryOnlines(_autocompleteOpts);
        }
        return _autocompleteOpts;
      },
      focusNode: widget.focusNode,
      textEditingController: widget.idEditingController,
      fieldViewBuilder: (
        BuildContext context,
        TextEditingController fieldTextEditingController,
        FocusNode fieldFocusNode,
        VoidCallback onFieldSubmitted,
      ) {
        updateTextAndPreserveSelection(
            fieldTextEditingController, widget.idController.text);
        return TextField(
          autocorrect: false,
          enableSuggestions: false,
          keyboardType: TextInputType.visiblePassword,
          focusNode: fieldFocusNode,
          style: TextStyle(
            fontSize: 17,
            height: 1.25,
            color: WeColors.of(context).textPrimary,
          ),
          maxLines: 1,
          cursorColor: WeColors.of(context).brand,
          decoration: InputDecoration(
            filled: false,
            counterText: '',
            isDense: true,
            contentPadding: EdgeInsets.zero,
            border: InputBorder.none,
            focusedBorder: InputBorder.none,
            enabledBorder: InputBorder.none,
            hintText: translate('Enter Remote ID'),
            hintStyle: TextStyle(
              fontSize: 15,
              color: WeColors.of(context).textTertiary,
            ),
          ),
          controller: fieldTextEditingController,
          inputFormatters: [IDTextInputFormatter()],
          onChanged: (v) {
            widget.idController.id = v;
          },
          onSubmitted: (_) {
            widget.onConnect();
          },
        ).workaroundFreezeLinuxMint();
      },
      onSelected: (option) {
        setState(() {
          widget.idController.id = option.id;
          FocusScope.of(context).unfocus();
        });
      },
      optionsViewBuilder: (BuildContext context,
          AutocompleteOnSelected<Peer> onSelected, Iterable<Peer> options) {
        options = _autocompleteOpts;
        final colors = WeColors.of(context);
        return Align(
          alignment: Alignment.topLeft,
          child: Container(
            decoration: BoxDecoration(
              borderRadius: WeRadii.mdRadius,
              boxShadow: WeShadow.popup(context),
            ),
            child: ClipRRect(
              borderRadius: WeRadii.mdRadius,
              child: Material(
                color: colors.surface,
                elevation: 0,
                child: Container(
                  decoration: BoxDecoration(
                    border: Border.all(color: colors.border),
                    borderRadius: WeRadii.mdRadius,
                  ),
                  child: ConstrainedBox(
                    constraints: const BoxConstraints(
                      maxHeight: 200,
                      maxWidth: 420,
                    ),
                    child: widget.allPeersLoader.peers.isEmpty &&
                            !widget.allPeersLoader.isPeersLoaded
                        ? const SizedBox(
                            height: 80,
                            child: Center(
                              child: CircularProgressIndicator(strokeWidth: 2),
                            ),
                          )
                        : Padding(
                            padding: const EdgeInsets.symmetric(vertical: 5),
                            child: ListView(
                              children: options
                                  .map((peer) => AutocompletePeerTile(
                                      onSelect: () => onSelected(peer),
                                      peer: peer))
                                  .toList(),
                            ),
                          ),
                  ),
                ),
              ),
            ),
          ),
        );
      },
    );
  }

  Widget _buildMoreButton(BuildContext context) {
    return Obx(
      () => WeIconButton(
        icon: _menuOpen.value
            ? Icons.keyboard_arrow_up_rounded
            : Icons.more_vert_rounded,
        tooltip: translate('More'),
        size: 34,
        iconSize: 20,
        active: _menuOpen.value,
        onTapDown: (details) {
          _menuOffset = details.globalPosition;
        },
        onTap: () async {
          _menuOpen.value = true;
          final x = _menuOffset.dx;
          final y = _menuOffset.dy;
          await mod_menu
              .showMenu(
            context: context,
            position: RelativeRect.fromLTRB(x, y, x, y),
            items: [
              ('Transfer file', () => widget.onConnect(isFileTransfer: true)),
              ('View camera', () => widget.onConnect(isViewCamera: true)),
              (
                '${translate('Terminal')} (beta)',
                () => widget.onConnect(isTerminal: true)
              ),
              if (isDesktop)
                (
                  'TCP tunneling',
                  () => widget.onConnect(isTcpTunneling: true)
                ),
            ]
                .map((e) => MenuEntryButton<String>(
                      childBuilder: (TextStyle? style) =>
                          Text(translate(e.$1), style: style),
                      proc: () => e.$2(),
                      padding: EdgeInsets.symmetric(
                          horizontal: kDesktopMenuPadding.left),
                      dismissOnClicked: true,
                    ))
                .map((e) => e.build(
                    context,
                    const MenuConfig(
                        commonColor: CustomPopupMenuTheme.commonColor,
                        height: CustomPopupMenuTheme.height,
                        dividerHeight: CustomPopupMenuTheme.dividerHeight)))
                .expand((i) => i)
                .toList(),
            elevation: 8,
          )
              .then((_) {
            _menuOpen.value = false;
          });
        },
      ),
    );
  }

  Widget _buildQuickAction(
    BuildContext context, {
    required IconData icon,
    required String text,
    required VoidCallback onTap,
  }) {
    final colors = WeColors.of(context);
    return InkWell(
      borderRadius: WeRadii.smRadius,
      onTap: onTap,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 4),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(icon, size: 15, color: colors.textSecondary),
            const SizedBox(width: 4),
            Text(
              text,
              style: TextStyle(fontSize: 12.5, color: colors.textSecondary),
            ),
          ],
        ),
      ),
    );
  }
}
