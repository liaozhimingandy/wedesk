// ToDesk style connect section: a "remote control device" header with a
// readiness pill, one wide search field for the remote device code with
// autocomplete, a dominant "connect" action, connection mode radios and a
// row of feature cards for the secondary connection types.

import 'package:flutter/material.dart';
import 'package:flutter_hbb/common.dart';
import 'package:flutter_hbb/common/formatter/id_formatter.dart';
import 'package:flutter_hbb/common/widgets/autocomplete.dart';
import 'package:flutter_hbb/common/widgets/wedesign.dart';
import 'package:flutter_hbb/models/peer_model.dart';
import 'package:flutter_hbb/models/state_model.dart';
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
  bool _fileTransferMode = false;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _buildSectionHeader(context),
        const SizedBox(height: WeSpace.l),
        Row(
          crossAxisAlignment: CrossAxisAlignment.center,
          children: [
            Expanded(child: _buildSearchField(context)),
            const SizedBox(width: WeSpace.m),
            WePrimaryButton(
              text: translate('Connect'),
              height: 46,
              width: 128,
              onPressed: () =>
                  widget.onConnect(isFileTransfer: _fileTransferMode),
            ),
          ],
        ),
        const SizedBox(height: WeSpace.s),
        Wrap(
          crossAxisAlignment: WrapCrossAlignment.center,
          spacing: WeSpace.xl,
          children: [
            _buildModeRadio(
              context,
              text: translate('Remote control'),
              selected: !_fileTransferMode,
              onTap: () => setState(() => _fileTransferMode = false),
            ),
            _buildModeRadio(
              context,
              text: translate('Transfer file'),
              selected: _fileTransferMode,
              onTap: () => setState(() => _fileTransferMode = true),
            ),
          ],
        ),
        const SizedBox(height: WeSpace.l),
        Row(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Expanded(
              child: _buildFeatureCard(
                context,
                icon: Icons.folder_copy_outlined,
                label: translate('Transfer file'),
                onTap: () => widget.onConnect(isFileTransfer: true),
              ),
            ),
            const SizedBox(width: WeSpace.m),
            Expanded(
              child: _buildFeatureCard(
                context,
                icon: Icons.videocam_outlined,
                label: translate('View camera'),
                onTap: () => widget.onConnect(isViewCamera: true),
              ),
            ),
            const SizedBox(width: WeSpace.m),
            Expanded(
              child: _buildFeatureCard(
                context,
                icon: Icons.terminal_outlined,
                label: '${translate('Terminal')} (beta)',
                onTap: () => widget.onConnect(isTerminal: true),
              ),
            ),
            if (isDesktop) ...[
              const SizedBox(width: WeSpace.m),
              Expanded(
                child: _buildFeatureCard(
                  context,
                  icon: Icons.swap_horiz_outlined,
                  label: translate('TCP tunneling'),
                  onTap: () => widget.onConnect(isTcpTunneling: true),
                ),
              ),
            ],
          ],
        ),
      ],
    );
  }

  Widget _buildSectionHeader(BuildContext context) {
    final colors = WeColors.of(context);
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
                  : translate('Ready to connect');
      return Row(
        children: [
          Text(
            translate('Remote control device'),
            style: TextStyle(
              fontSize: 15,
              fontWeight: FontWeight.w600,
              color: colors.textPrimary,
            ),
          ),
          const SizedBox(width: WeSpace.s),
          WeStatusPill(
            text: statusText,
            color: ready ? colors.success : colors.warn,
          ),
        ],
      );
    });
  }

  Widget _buildModeRadio(
    BuildContext context, {
    required String text,
    required bool selected,
    required VoidCallback onTap,
  }) {
    final colors = WeColors.of(context);
    return InkWell(
      borderRadius: WeRadii.smRadius,
      onTap: onTap,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 2, vertical: 4),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(
              selected ? Icons.radio_button_checked : Icons.radio_button_off,
              size: 18,
              color: selected ? colors.brand : colors.textTertiary,
            ),
            const SizedBox(width: 6),
            Text(
              text,
              style: TextStyle(
                fontSize: 13.5,
                fontWeight: selected ? FontWeight.w600 : FontWeight.w400,
                color: selected ? colors.textPrimary : colors.textSecondary,
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildFeatureCard(
    BuildContext context, {
    required IconData icon,
    required String label,
    required VoidCallback onTap,
  }) {
    final colors = WeColors.of(context);
    return InkWell(
      borderRadius: WeRadii.mdRadius,
      onTap: onTap,
      child: Container(
        height: 84,
        decoration: BoxDecoration(
          borderRadius: WeRadii.mdRadius,
          color: colors.surfaceAlt,
          border: Border.all(color: colors.border, width: 1),
        ),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Icon(icon, size: 26, color: colors.brand),
            const SizedBox(height: WeSpace.s),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 4),
              child: Text(
                label,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(
                  fontSize: 12.5,
                  color: colors.textSecondary,
                  fontWeight: FontWeight.w500,
                ),
              ),
            ),
          ],
        ),
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
            widget.onConnect(isFileTransfer: _fileTransferMode);
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

  RxBool? _svcStoppedRx() {
    if (!Get.isRegistered<RxBool>(tag: 'stop-service')) {
      return null;
    }
    return Get.find<RxBool>(tag: 'stop-service');
  }
}
