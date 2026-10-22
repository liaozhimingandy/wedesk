#[cfg(target_os = "windows")]
mod setup;
#[cfg(target_os = "windows")]
pub use setup::{
    is_rd_printer_installed,
    setup::{install_update_printer, uninstall_printer},
};

#[cfg(target_os = "windows")]
// 打包时的落盘布局是 drivers/WeDeskPrinterDriver/WeDeskPrinterDriver.inf，
// 但目录里除 .inf 文件名外的所有内容必须**逐字节保持上游原样**：
// 这个驱动包带微软 WHQL 目录签名（包里的 *.cat，签名者 Microsoft Windows
// Hardware Compatibility Publisher）。
// 目录签名按【文件内容】算哈希：
//   - 改任意一个文件的内容（包括 .inf 里的哪怕一个字符）
//     → pnputil /add-driver 直接报
//       "The hash for the file is not present in the specified catalog file"
//       （2026-10-22 实测）；
//   - 只改目录名 / .inf 文件名（内容不动）→ 安装完全正常（同批实测）。
// 所以"把 RustDesk 字样全换成 WeDesk"只能做到文件名这一层，
// 驱动在系统里显示的名字（下面的 get_driver_name）仍受限于 INF，详见该函数注释。
const RD_DRIVER_INF_PATH: &str = "drivers/WeDeskPrinterDriver/WeDeskPrinterDriver.inf";

#[cfg(target_os = "windows")]
fn get_printer_name(app_name: &str) -> Vec<u16> {
    format!("{} Printer", app_name)
        .encode_utf16()
        .chain(Some(0))
        .collect()
}

#[cfg(target_os = "windows")]
// 这一串必须等于 .inf 的 [Models] 段里声明的驱动名：
// 安装走的是 UploadPrinterDriverPackage + InstallPrinterDriverFromPackage，
// 后者是按名字在驱动仓库里找包的，写成别的会直接报
//   "The specified driver does not exist in the driver store."
// （Add-PrinterDriver -Name "WeDesk v4 Printer Driver" 实测复现，2026-10-22）。
// 而 .inf 的内容改不动（目录签名），所以这个名字只能跟着上游走。
// 用户能看到的是打印机队列名（get_printer_name，上面那个）与端口名，
// 这两个都是 "WeDesk Printer"，已经完全是我们自己的品牌。
fn get_driver_name() -> Vec<u16> {
    "RustDesk v4 Printer Driver"
        .encode_utf16()
        .chain(Some(0))
        .collect()
}

#[cfg(target_os = "windows")]
fn get_port_name(app_name: &str) -> Vec<u16> {
    format!("{} Printer", app_name)
        .encode_utf16()
        .chain(Some(0))
        .collect()
}
