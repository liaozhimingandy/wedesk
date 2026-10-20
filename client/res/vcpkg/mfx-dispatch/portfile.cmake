# 注意：早期版本这里用 vcpkg_download_distfile 从
# https://github.com/lu-zero/mfx_dispatch/commit/d624124...patch?full_index=1
# 现下载这个补丁 —— GitHub 时不时对这种 commit patch 链接返回 503
# （2026-10-20 CI 构建因此失败：重试 3 次全部 503，vcpkg 直接 halt）。
# 补丁只有 2.8 KB 且内容恒定，已改为随 overlay port 一起入库
# （内容与官方下载件的 SHA512 5d2ffc4e...b61e44 逐字节一致），
# 构建过程不再为它联网。
vcpkg_from_github(
    OUT_SOURCE_PATH SOURCE_PATH
    REPO lu-zero/mfx_dispatch
    REF "${VERSION}"
    SHA512 12517338342d3e653043a57e290eb9cffd190aede0c3a3948956f1c7f12f0ea859361cf3e534ab066b96b1c211f68409c67ef21fd6d76b68cc31daef541941b0
    HEAD_REF master
    PATCHES
        fix-unresolved-symbol.patch
        fix-pkgconf.patch
        0003-upgrade-cmake-3.14.patch
        fix-missing-cstdint-import.patch
)

if(VCPKG_TARGET_IS_WINDOWS AND NOT VCPKG_TARGET_IS_MINGW)
    vcpkg_cmake_configure(
        SOURCE_PATH "${SOURCE_PATH}" 
    )
    vcpkg_cmake_install()
    vcpkg_copy_pdbs()
else()
    if(VCPKG_TARGET_IS_MINGW)
        vcpkg_check_linkage(ONLY_STATIC_LIBRARY)
    endif()
    vcpkg_configure_make(
        SOURCE_PATH "${SOURCE_PATH}"
        AUTOCONFIG
    )
    vcpkg_install_make()
endif()
vcpkg_fixup_pkgconfig()
  
file(REMOVE_RECURSE "${CURRENT_PACKAGES_DIR}/debug/include")
vcpkg_install_copyright(FILE_LIST "${SOURCE_PATH}/LICENSE")
