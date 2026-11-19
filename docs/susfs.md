# SUSFS 4.14 integration (OnePlus 7T Pro / HD1910, sm8150)

This branch integrates **SUSFS v2.2.0 (NON-GKI branch)** into the
`OnePlusOSS/android_kernel_oneplus_sm8150` 4.14.180 tree so that ReSukiSU
(`ReSukiSU/ReSukiSU`, hook method `CONFIG_KSU_SUSFS`) can talk to it.

## Why the old `susfs4ksu` 4.14 patch does not work here

`simonpunk/susfs4ksu` `kernel-4.14` (v1.5.x) is **API-incompatible** with
ReSukiSU. ReSukiSU's kernel side (`feature/sucompat.c`,
`feature/kernel_umount.c`, `supercall/dispatch.c`) expects the modern entry
points:

* `susfs_is_current_proc_no_su()` / `susfs_set_current_proc_no_su()` /
  `susfs_clear_current_proc_no_su()` (inline, TIF flag based)
* `susfs_set_current_proc_umounted[_for_zygote_next]()`, `susfs_extra_works`
* `susfs_set_hide_sus_mnts_for_non_su_procs()`,
  `susfs_set_avc_log_spoofing()`, `susfs_add_sus_path_loop()`,
  `susfs_add_sus_map()`, `susfs_show_version()`, `susfs_show_variant()`
* command ABI `ksu_handle_susfs_cmd(unsigned int cmd, void __user **arg)`
  (`SUSFS_MAGIC 0xFAFAFAFA`, `CMD_SUSFS_* 0x555*`/`0x600*`)

The v1.5.5 4.14 patch exposes `susfs_add_sus_path(struct st_susfs_sus_path __user*)`,
a `prctl()` interface and no TIF helpers at all, so enabling
`CONFIG_KSU_SUSFS` against it cannot compile.

The 4.14-compatible v2.x port used here is the one maintained in
`FlopKernel-Series/flop_trinket-mi_kernel` (4.14.357, SUSFS v2.2.0, NON-GKI).
Only the device-independent parts are vendored:

| file | origin |
|---|---|
| `susfs/fs/susfs.c` | `flop_trinket-mi_kernel:fs/susfs.c` (v2.2.0) |
| `susfs/include/linux/susfs.h` | same tree |
| `susfs/include/linux/susfs_def.h` | same tree |

Both are GPL-2.0, matching the kernel tree they are applied to.

## What `scripts/build.py` does

When `configs/resukisu.config` contains `CONFIG_KSU_SUSFS=y`:

1. copies the three vendored files into the checked-out kernel tree,
2. applies `patches/susfs-4.14.patch` (VFS hooks) and
   `patches/susfs-4.14-ksu.patch` (KSU inline-hook wiring) with
   `git apply --whitespace=nowarn`,
3. after `olddefconfig` it re-verifies, and *fails the build* if
   `CONFIG_KSU_SUSFS` or any requested `CONFIG_KSU_SUSFS_*` feature was dropped,
4. re-verifies the seven inline hook sites that ReSukiSU's
   `tools/inline_hook_check.mk` greps for, so a silently-broken integration
   cannot produce a kernel.

`CONFIG_KSU_SUSFS` lives in the same Kconfig `choice` as
`CONFIG_KSU_MANUAL_HOOK`, so the two are mutually exclusive; the SUSFS patch
therefore widens the guard around the existing `op7.patch` hook sites to
`#if defined(CONFIG_KSU_MANUAL_HOOK) || defined(CONFIG_KSU_SUSFS)` and adds the
three sites the manual path covered with auto hooks (`ksu_handle_setresuid`,
`ksu_handle_sys_read`, `ksu_handle_input_handle_event`).

## Enabled / deferred feature set

Enabled: `SUS_PATH`, `SUS_KSTAT`, `SPOOF_UNAME`,
`SPOOF_CMDLINE_OR_BOOTCONFIG`, `ENABLE_LOG`, AVC log spoofing.

Deferred (`=n` in the fragment, hooks staged in the patch but compiled out):
`SUS_MOUNT`, `OPEN_REDIRECT`, `SUS_MAP`, `SUS_MEMFD`, `TRY_UMOUNT`.
Their hooks in `fs/namespace.c`, `fs/proc_namespace.c`,
`fs/notify/fdinfo.c`, `fs/proc/fd.c`, `fs/statfs.c`, `fs/open.c`,
`fs/proc/base.c` depend on 4.14.3xx mount-data plumbing
(`mnt->mnt.data`, `mount_fs(..., &mnt->mnt, ...)`, `ida_alloc`) that does not
exist in this 4.14.180 vendor tree and must be re-written against the
`ida_pre_get`/`ida_get_new_above`/`ida_remove` API before it can be enabled.

## ABI / `Module.symvers` CRC analysis (MODVERSIONS)

The device kernel enforces `CONFIG_MODVERSIONS=y` and
`CONFIG_MODULE_SIG_FORCE=y`, and the stock `.ko` set (32 modules, e.g.
`qca_cld3_wlan.ko`) carries a `__versions` CRC table. Any layout change in a
struct that appears in an exported prototype changes the CRC that `genksyms`
records in `Module.symvers`, and the stock module then refuses to load
(`disagrees about version of symbol ...`). `scripts/check_module_abi.py`
compares all 11,964 stock export CRCs against a built `Image`, which is how the
previous baseline was validated.

SUSFS v2.x is deliberately designed to be ABI-neutral, and this port keeps it
that way:

* task state is stored in **spare `TIF_*` bits of `thread_info.flags`**
  (`TIF_PROC_UMOUNTED 33`, `TIF_PROC_NO_SU 34`,
  `TIF_PROC_UMOUNTED_FOR_ZYGOTE_NEXT 35`) — `struct task_struct` is unchanged;
* mount state uses `vfsmount.mnt_flags` bit 31 and `inode->i_state` bits 33+ —
  existing fields, no new members;
* nameidata state uses `nd->state` / `nd->flags` spare bits.

The upstream 4.14 v2.2.0 code still *declares* two legacy members that are
never referenced by any translation unit:

```
include/linux/sched.h : u64 susfs_task_state; u64 susfs_last_fake_mnt_id;
include/linux/mount.h : u64 susfs_mnt_id_backup;
```

They are **not** part of `patches/susfs-4.14.patch` on purpose. Adding them
would grow `struct task_struct` / `struct vfsmount` and therefore perturb the
genksyms CRC of every export that mentions those types, breaking stock `.ko`
loading. Omitting them costs nothing because no code reads them.

The remaining hooks only add new non-exported symbols (`fs/susfs.c`) and calls
inside existing functions, so the exported symbol table stays identical. The
expected result of the ABI check is therefore "all 11,964 exports unchanged";
any mismatch reported by `scripts/check_module_abi.py` after a SUSFS build is a
porting bug, not an expected cost.

## Verifying locally

```
git apply --check patches/op7.patch
git apply --whitespace=nowarn --check patches/susfs-4.14.patch
```

(`patches/susfs-4.14-ksu.patch` applies on top of both.)

