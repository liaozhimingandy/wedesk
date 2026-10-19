import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

class IDTextEditingController extends TextEditingController {
  IDTextEditingController({String? text}) : super(text: text);

  String get id => trimID(value.text);

  set id(String newID) => text = formatID(newID);
}

class IDTextInputFormatter extends TextInputFormatter {
  @override
  TextEditingValue formatEditUpdate(
      TextEditingValue oldValue, TextEditingValue newValue) {
    if (newValue.text.isEmpty) {
      return newValue.copyWith(text: '');
    } else if (newValue.text.compareTo(oldValue.text) == 0) {
      return newValue;
    } else {
      int selectionIndexFromTheRight =
          newValue.text.length - newValue.selection.extentOffset;
      String newID = formatID(newValue.text);
      return TextEditingValue(
        text: newID,
        selection: TextSelection.collapsed(
          offset: newID.length - selectionIndexFromTheRight,
        ),
        // https://github.com/flutter/flutter/issues/78066#issuecomment-797869906
        composing: newValue.composing,
      );
    }
  }
}

/// ID 每组的位数。
///
/// ID 已统一为 12 位纯数字（见 Rust 侧 `Config::ID_LENGTH`），按 4 位一组展示成
/// `1234 5678 9012`。此前按 3 位分组是为了适配上游那种 9/10 位不定长 ID。
const int kIdGroupSize = 4;

String formatID(String id) {
  String id2 = id.replaceAll(' ', '');
  String suffix = '';
  if (id2.endsWith(r'\r') || id2.endsWith(r'/r')) {
    suffix = id2.substring(id2.length - 2, id2.length);
    id2 = id2.substring(0, id2.length - 2);
  }
  if (int.tryParse(id2) == null) return id;
  String newID = '';
  if (id2.length <= kIdGroupSize) {
    newID = id2;
  } else {
    var n = id2.length;
    var a = n % kIdGroupSize != 0 ? n % kIdGroupSize : kIdGroupSize;
    newID = id2.substring(0, a);
    for (var i = a; i < n; i += kIdGroupSize) {
      newID += " ${id2.substring(i, i + kIdGroupSize)}";
    }
  }
  return newID + suffix;
}

String trimID(String id) {
  return id.replaceAll(' ', '');
}
