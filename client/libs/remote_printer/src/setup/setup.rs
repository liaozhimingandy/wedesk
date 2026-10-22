use super::{
    driver::{get_installed_driver_version, install_driver, uninstall_driver},
    port::{check_add_local_port, check_delete_local_port},
    printer::{add_printer, delete_printer},
};
use hbb_common::{allow_err, bail, lazy_static, log, ResultType};
use std::{path::PathBuf, sync::Mutex};
use windows_strings::PCWSTR;

lazy_static::lazy_static!(
    static ref SETUP_MTX: Mutex<()> = Mutex::new(());
);

/// 在 `drivers/` 下递归找 .inf。
///
/// 兼容历史打包布局：0628219 及更早的构建把驱动 zip 自带的父目录一起拷了
/// 进来，inf 实际落在 `drivers/<X>/<X>/*.inf`（多一层）；改名后目录/文件名
/// 也从 RustDeskPrinterDriver 变成 WeDeskPrinterDriver。驱动包**内容**始终
/// 没变（目录签名按内容算哈希、不认路径），所以布局不同也照样能装。
fn find_inf_recursive(dir: &std::path::Path, found: &mut Vec<PathBuf>) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            find_inf_recursive(&path, found);
        } else if path
            .extension()
            .map(|ext| ext.eq_ignore_ascii_case("inf"))
            .unwrap_or(false)
        {
            found.push(path);
        }
    }
}

fn get_driver_inf_abs_path() -> ResultType<PathBuf> {
    use crate::RD_DRIVER_INF_PATH;

    let exe_file = std::env::current_exe()?;
    let parent = match exe_file.parent() {
        Some(parent) => parent,
        None => bail!(
            "Invalid exe parent for {}",
            exe_file.to_string_lossy().as_ref()
        ),
    };
    let abs_path = parent.join(RD_DRIVER_INF_PATH);
    if abs_path.exists() {
        return Ok(abs_path);
    }

    // 精确路径没有 → 退而在 drivers/ 里递归找一个 .inf（优先名字带
    // "printerdriver" 的），把旧布局/旧目录名的包也救回来。
    let mut candidates = Vec::new();
    find_inf_recursive(&parent.join("drivers"), &mut candidates);
    candidates.sort();
    let preferred = candidates
        .iter()
        .find(|p| {
            p.file_name()
                .map(|n| n.to_string_lossy().to_lowercase().contains("printerdriver"))
                .unwrap_or(false)
        })
        .or_else(|| candidates.first());
    if let Some(path) = preferred {
        log::warn!(
            "Driver inf not at \"{}\", falling back to \"{}\" (packed layout differs)",
            RD_DRIVER_INF_PATH,
            path.display()
        );
        return Ok(path.clone());
    }

    // 带上解析后的绝对路径：便携版与安装版的 exe 目录不同（MSI 装到
    // Program Files\<Product>），只报相对路径时看不出它到底去哪找的。
    // 保持单行——这条消息会直接弹到 Flutter UI 上（install-printer-res）。
    bail!(
        "The driver inf file \"{}\" does not exist (resolved: \"{}\", drivers dir: {}), please make sure the driver folder ships next to the executable",
        RD_DRIVER_INF_PATH,
        abs_path.display(),
        if parent.join("drivers").exists() { "exists but has no .inf" } else { "missing" },
    )
}

// Note: This function must be called in a separate thread.
// Because many functions in this module are blocking or synchronous.
// Calling this function from a thread that manages interaction with the user interface could make the application appear to be unresponsive.
// Steps:
// 1. Add the local port.
// 2. Check if the driver is installed.
//    Uninstall the existing driver if it is installed.
//    We should not check the driver version because the driver is deployed with the application.
//    It's better to uninstall the existing driver and install the driver from the application.
// 3. Add the printer.
pub fn install_update_printer(app_name: &str) -> ResultType<()> {
    let printer_name = crate::get_printer_name(app_name);
    let driver_name = crate::get_driver_name();
    let port = crate::get_port_name(app_name);
    let rd_printer_name = PCWSTR::from_raw(printer_name.as_ptr());
    let rd_printer_driver_name = PCWSTR::from_raw(driver_name.as_ptr());
    let rd_printer_port = PCWSTR::from_raw(port.as_ptr());

    let inf_file = get_driver_inf_abs_path()?;
    let inf_file: Vec<u16> = inf_file
        .to_string_lossy()
        .as_ref()
        .encode_utf16()
        .chain(Some(0).into_iter())
        .collect();
    let _lock = SETUP_MTX.lock().unwrap();

    check_add_local_port(&rd_printer_port)?;

    let should_install_driver = match get_installed_driver_version(&rd_printer_driver_name)? {
        Some(_version) => {
            delete_printer(&rd_printer_name)?;
            allow_err!(uninstall_driver(&rd_printer_driver_name));
            true
        }
        None => true,
    };

    if should_install_driver {
        allow_err!(install_driver(&rd_printer_driver_name, inf_file.as_ptr()));
    }

    add_printer(&rd_printer_name, &rd_printer_driver_name, &rd_printer_port)?;

    Ok(())
}

pub fn uninstall_printer(app_name: &str) {
    let printer_name = crate::get_printer_name(app_name);
    let driver_name = crate::get_driver_name();
    let port = crate::get_port_name(app_name);
    let rd_printer_name = PCWSTR::from_raw(printer_name.as_ptr());
    let rd_printer_driver_name = PCWSTR::from_raw(driver_name.as_ptr());
    let rd_printer_port = PCWSTR::from_raw(port.as_ptr());

    let _lock = SETUP_MTX.lock().unwrap();

    allow_err!(delete_printer(&rd_printer_name));
    allow_err!(uninstall_driver(&rd_printer_driver_name));
    allow_err!(check_delete_local_port(&rd_printer_port));
}
