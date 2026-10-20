// Global Flutter error capture.
//
// Release builds render any widget whose build/layout threw as a blank area
// (the default ErrorWidget paints nothing visible), which cost us a full
// debugging round on the redesigned main window. These handlers make every
// uncaught error observable:
//   - appended to `<app support dir>/flutter_error.log` with a timestamp,
//   - framework errors (build/layout/paint) additionally rendered inline as a
//     small red box carrying the exception summary, so a user screenshot is
//     already a usable bug report.
//
// Desktop only; mobile keeps the default handlers.

import 'dart:async';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:path_provider/path_provider.dart';

/// Append-only diagnostics file. Created lazily on the first error; failures
/// here are swallowed — error reporting must never crash the app.
class FlutterErrorLog {
  FlutterErrorLog._();

  static File? _file;
  static bool _initFailed = false;

  static Future<File?> _resolve() async {
    if (_file != null || _initFailed) return _file;
    try {
      final dir = await getApplicationSupportDirectory();
      final f = File('${dir.path}${Platform.pathSeparator}flutter_error.log');
      _file = f;
    } catch (_) {
      _initFailed = true;
    }
    return _file;
  }

  static Future<void> write(String message) async {
    try {
      final f = await _resolve();
      if (f == null) return;
      final ts = DateTime.now().toIso8601String();
      await f.writeAsString('\n[$ts] $message\n', mode: FileMode.append);
    } catch (_) {
      // Never let the logger take the app down.
    }
  }
}

String _summarize(Object error, StackTrace? stack) {
  final first =
      (stack?.toString().split('\n') ?? const <String>[]).take(6).join('\n');
  return '$error\n$first';
}

/// Install the global handlers. Call once from `main()` before `runApp`.
Future<void> installGlobalErrorHandlers() async {
  FlutterError.onError = (details) {
    FlutterErrorLog.write(
        'FlutterError: ${_summarize(details.exception, details.stack)}');
    // Keep the default console dumping for debug sessions.
    FlutterError.presentError(details);
  };
  PlatformDispatcher.instance.onError = (error, stack) {
    FlutterErrorLog.write('Uncaught: ${_summarize(error, stack)}');
    return true;
  };
  if (kReleaseMode) {
    ErrorWidget.builder = (details) {
      FlutterErrorLog.write('Widget build error: ${_summarize(
          details.exception,
          details.stack)}');
      // A small, always-visible marker instead of a silent blank area. Kept
      // deliberately compact so it cannot break surrounding layouts.
      return Container(
        padding: const EdgeInsets.all(4),
        margin: const EdgeInsets.symmetric(vertical: 2),
        decoration: BoxDecoration(
          border: Border.all(color: const Color(0xFFD32F2F), width: 1),
          color: const Color(0xFFFFEBEE),
        ),
        child: Text(
          'UI error: ${details.exception}',
          maxLines: 4,
          overflow: TextOverflow.ellipsis,
          style: const TextStyle(fontSize: 11, color: Color(0xFFB71C1C)),
        ),
      );
    };
  }
}
