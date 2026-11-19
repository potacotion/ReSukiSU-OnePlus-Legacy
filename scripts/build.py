"""Experimental Linux compile; a successful compile is not device validation."""
import argparse
import datetime
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
from kernel_release import SUFFIX, branded_release
from resukisu_source import resolve as resolve_resukisu
from toolchain import configure, configure_legacy_linker
from prepare_protocol import prepare
from module_trust import install as install_module_trust, verify_embedded
from project import ROOT, PATCHES, device, run, save, sha256

def checkout(repo, commit, dest, shallow=True):
    run("git", "init", "--quiet", dest)
    run("git", "remote", "add", "origin", "https://github.com/" + repo + ".git", cwd=dest)
    command = ["git", "fetch", "--no-tags"]
    if shallow:
        command += ["--depth=1"]
    run(*command, "origin", commit, cwd=dest)
    run("git", "checkout", "--detach", "FETCH_HEAD", cwd=dest)
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=dest, text=True).strip()
    if actual != commit:
        raise ValueError("Source commit mismatch")

# The parallel compile buries the first failure under thousands of later lines,
# so every make invocation that can fail is logged and summarised on failure.
BUILD_ERROR = re.compile(r"error:|Error \d+|undefined reference|No rule to make target")


def report_errors(log, context=5, limit=200):
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    hits = [index for index, line in enumerate(lines) if BUILD_ERROR.search(line)]
    print("\n===== build error summary (" + str(log) + ") =====", flush=True)
    if not hits:
        print("No matching error line; see the log above.", flush=True)
        print("===== end of build error summary =====", flush=True)
        return
    selected = []
    covered = 0
    for hit in hits:
        selected.extend(range(max(hit - context, 0), min(hit + context + 1, len(lines))))
        covered += 1
        if len(selected) >= limit:
            break
    previous = -1
    for number in dict.fromkeys(selected):
        if number != previous + 1:
            print("--", flush=True)
        print("{:6d}: {}".format(number + 1, lines[number]), flush=True)
        previous = number
    if covered < len(hits):
        print("... {} matching lines elided".format(len(hits) - covered), flush=True)
    print("===== end of build error summary =====", flush=True)


def run_logged(options, targets, env, log):
    command = list(map(str, options)) + list(map(str, targets))
    print("+", " ".join(command), flush=True)
    with log.open("w", encoding="utf-8", errors="replace") as stream:
        process = subprocess.Popen(command, env=env, text=True, errors="replace",
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for line in process.stdout:
            stream.write(line)
            sys.stdout.write(line)
        process.stdout.close()
        code = process.wait()
    if code != 0:
        report_errors(log)
        raise subprocess.CalledProcessError(code, command)

# Kbuild records every inline hook ReSukiSU insists on in this file; the set
# moves with ReSukiSU HEAD, so read it instead of hard-coding the list.
INLINE_HOOK_RULE = re.compile(
    r"\$\(eval \$\(call (check_ksu_hook|check_ksu_hook_incompatible),"
    r"([A-Za-z0-9_]+),\$\(srctree\)/([^\s)]+)\)\)")


def verify_inline_hooks(source):
    """Reject a kernel tree whose inline hooks do not match ReSukiSU's rules."""
    rules = source / "KernelSU/kernel/tools/inline_hook_check.mk"
    if not rules.is_file():
        raise RuntimeError("Missing ReSukiSU inline hook rules: " + str(rules))
    wanted = {}
    for kind, symbol, path in INLINE_HOOK_RULE.findall(rules.read_text(encoding="utf-8")):
        wanted.setdefault((kind, path), []).append(symbol)
    if not any(kind == "check_ksu_hook" for kind, _ in wanted):
        raise RuntimeError("Could not parse the ReSukiSU inline hook rules")
    for (kind, path), symbols in sorted(wanted.items()):
        target = source / path
        if not target.is_file():
            raise RuntimeError("SUSFS inline hook target is missing: " + path)
        text = target.read_text(encoding="utf-8", errors="replace")
        for symbol in symbols:
            present = re.search(r"\b" + symbol + r"\b", text) is not None
            if kind == "check_ksu_hook" and not present:
                raise RuntimeError("Missing SUSFS inline hook: " + symbol + " in " + path)
            if kind == "check_ksu_hook_incompatible" and present:
                raise RuntimeError("Incompatible SUSFS inline hook: " + symbol + " in " + path)
    return sorted({symbol for symbols in wanted.values() for symbol in symbols})


def build(name, config=None, rom='coloros', diagnostics_only=False):
    if os.name != "posix":
        raise RuntimeError("Build on Linux (GitHub Actions or WSL)")
    data = device(name)
    data["resukisu"], resukisu_selection = resolve_resukisu(data["resukisu"])
    print("Latest ReSukiSU:", data["resukisu"]["repository"], data["resukisu"]["commit"], flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
            summary.write("Latest ReSukiSU: [" + data["resukisu"]["commit"] + "](https://github.com/" + data["resukisu"]["repository"] + "/commit/" + data["resukisu"]["commit"] + ")\n\n")
    firmware = data["firmware"][rom]
    compiler_lock = configure(data["platform"])
    stock_config = config is None
    if config is None:
        if not firmware.get("config"):
            raise RuntimeError("No verified stock config for " + name + "/" + rom)
        config = ROOT / firmware["config"]
        if sha256(config) != firmware["config_sha256"]:
            raise ValueError("Registered stock config checksum mismatch")
    for program in ("git", "make", "clang", "ld.lld", "aarch64-linux-gnu-gcc", "arm-linux-gnueabi-gcc"):
        if not shutil.which(program):
            raise RuntimeError("Missing build dependency: " + program)
    work = ROOT / ".work" / (name + "-" + datetime.datetime.now().strftime("%Y%m%d%H%M%S"))
    work.mkdir(parents=True, exist_ok=False)
    source, output = work / "kernel" / ("msm-" + ".".join(data["kernel"]["version"].split(".")[:2])), work / "obj"
    checkout(data["kernel"]["repository"], data["kernel"]["commit"], source)
    modules = work / "modules"
    checkout(data["vendor"]["repository"], data["vendor"]["commit"], modules)
    vendor_patch = ROOT / "patches" / ("vendor-" + {"oneplus-7-pro": "oneplus-7", "oneplus-7t": "oneplus-7", "oneplus-7t-pro": "oneplus-7", "oneplus-8-pro": "oneplus-8", "oneplus-9-pro": "oneplus-9"}.get(name, name) + ".patch")
    if vendor_patch.exists():
        run("git", "apply", "--check", vendor_patch, cwd=modules)
        run("git", "apply", vendor_patch, cwd=modules)
    module_trust = install_module_trust(firmware, source)
    protocol = prepare(modules, data) if data["platform"] != "sm8150" else None
    if data["platform"] == "sm8150" and (modules / "vendor/oplus/kernel/network/data_module").exists():
        raise ValueError("SM8150 source unexpectedly contains a protobuf data module")
    (work / "vendor").symlink_to(modules / "vendor", target_is_directory=True)
    overlay = modules / "kernel" / source.name
    for item in overlay.rglob("*"):
        if item.is_dir() and not item.is_symlink():
            continue
        target = source / item.relative_to(overlay)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            if item.is_symlink() and target.is_symlink() and os.readlink(item) == os.readlink(target):
                continue
            if not item.is_symlink() and not target.is_symlink() and sha256(item) == sha256(target):
                continue
            raise RuntimeError("Vendor overlay conflicts with kernel source: " + str(target))
        if item.is_symlink():
            target.symlink_to(os.readlink(item))
        else:
            shutil.copyfile(item, target)
    patch = ROOT / "patches" / (PATCHES[name] + ".patch")
    run("git", "apply", "--check", patch, cwd=source)
    run("git", "apply", patch, cwd=source)
    compat_patch = ROOT / "patches" / ("compat-" + data["platform"] + ".patch")
    if compat_patch.exists():
        run("git", "apply", "--check", compat_patch, cwd=source)
        run("git", "apply", compat_patch, cwd=source)
    # SUSFS is only integrated when the Kconfig fragment asks for it, so the
    # stock hook path stays byte-identical for every other configuration.
    susfs_fragment = (ROOT / "configs/resukisu.config").read_text(encoding="utf-8")
    susfs_wanted = "CONFIG_KSU_SUSFS=y" in susfs_fragment.splitlines()
    susfs_patches = [ROOT / "patches" / item for item in ("susfs-4.14.patch", "susfs-4.14-ksu.patch")]
    if susfs_wanted:
        # The SUSFS sources are vendored instead of fetched at build time so the
        # tree_hash stays reproducible; provenance is recorded in docs/susfs.md.
        for item in ("fs/susfs.c", "include/linux/susfs.h", "include/linux/susfs_def.h"):
            shutil.copyfile(ROOT / "susfs" / item, source / item)
        for item in susfs_patches:
            if not item.is_file():
                raise RuntimeError("Missing SUSFS patch: " + item.name)
            # The vendored 4.14 port carries upstream indentation that trips the
            # default whitespace gate.
            run("git", "apply", "--whitespace=nowarn", "--check", item, cwd=source)
            run("git", "apply", "--whitespace=nowarn", item, cwd=source)
    checkout(data["resukisu"]["repository"], data["resukisu"]["commit"], source / "KernelSU", False)
    (source / "drivers/kernelsu").symlink_to("../KernelSU/kernel", target_is_directory=True)
    for path, addition in (
        ("drivers/Makefile", "\nobj-$(CONFIG_KSU) += kernelsu/\n"),
        ("drivers/Kconfig", '\nsource "drivers/kernelsu/Kconfig"\n'),
    ):
        with (source / path).open("a", encoding="utf-8") as stream:
            stream.write(addition)
    output.mkdir()
    env = dict(os.environ, ARCH="arm64", SUBARCH="arm64")
    native_features = {}
    if data["platform"] in ("sm8150", "sm8250"):
        # Android normally exports these variables to every recursive make.
        # Reading the include in the top-level make alone does not export them.
        feature_file = source / "oplus_native_features.mk"
        for line in feature_file.read_text().splitlines():
            match = re.fullmatch(r"([A-Z][A-Z0-9_]*)=(.*)", line.strip())
            if match:
                native_features[match[1]] = match[2]
        if not native_features:
            raise ValueError("Official native feature assignments are missing")
        env.update(native_features)
    linker_lock = None
    if data["platform"] == "sm8150":
        linker, linker_lock = configure_legacy_linker()
        env["PATH"] = os.environ["PATH"]
    else:
        linker = "aarch64-linux-gnu-ld" if data["platform"] == "sm8250" else "ld.lld"
    # Versioned stock modules retain their CRC and signature checks.
    stock_release = firmware.get("kernel_release")
    release = branded_release(stock_release)
    if release:
        if not release.startswith(data["kernel"]["version"]):
            raise RuntimeError("Stock kernel version differs from source baseline")
        (source / ".scmversion").write_text("", encoding="utf-8")

    options = ["make", "-C", str(source), "O=" + str(output), "ARCH=arm64",
               "CC=clang", "LD=" + linker, "AR=llvm-ar", "NM=llvm-nm",
               "OBJCOPY=" + ("aarch64-linux-gnu-objcopy" if data["platform"] in ("sm8150", "sm8250") else "llvm-objcopy"), "OBJDUMP=llvm-objdump", "STRIP=llvm-strip",
               "CLANG_TRIPLE=aarch64-linux-gnu-", "CROSS_COMPILE=aarch64-linux-gnu-",
               "CROSS_COMPILE_ARM32=arm-linux-gnueabi-"]
    options += ["OPLUS_FEATURE_SECURE_GUARD=" + ("yes" if data["platform"] in ("sm8150", "sm8250") else "no"), "OPLUS_FEATURE_SECURE_ROOTGUARD=no",
                "OPLUS_FEATURE_SECURE_MOUNTGUARD=no", "OPLUS_FEATURE_SECURE_EXECGUARD=no",
                "OPLUS_FEATURE_SECURE_KEYINTERFACESGUARD=no"]
    stock_abi_header = ROOT / "configs/sm8150-stock-abi.h" if data["platform"] == "sm8150" else None
    if data["platform"] in ("sm8150", "sm8250"):
        cflags = "-gdwarf-4"
        if stock_abi_header:
            # Old Kbuild compiler probes run before kernel include directories
            # are available, so this must be an absolute forced include.
            cflags += " -include " + str(stock_abi_header)
        options += ["KCFLAGS=" + cflags, "BRAND_SHOW_FLAG=oneplus"]
    if config:
        shutil.copyfile(config, output / ".config")
    else:
        target = data["kernel"]["defconfig"]
        if data["platform"] == "sm8350":
            target = "vendor/lahaina-qgki_defconfig"
            run("bash", "scripts/gki/generate_defconfig.sh", target, cwd=source, env=env)
        run(*options, target, env=env)
    translations = {}
    if data["platform"] == "sm8150":
        # The released 4.14 sources renamed these stock OPPO Kconfig symbols.
        renamed = ("FG_IO_OPT", "MEM_MONITOR", "NFC", "MOTOR", "HANS", "ARCH_FILE",
                   "COMMON_SOFT", "DEVICE_IFNO", "HEALTHINFO", "JANK_INFO")
        stock_values = dict(line.split("=", 1) for line in (output / ".config").read_text().splitlines()
                            if line.startswith("CONFIG_") and "=" in line)
        with (output / ".config").open("a") as stream:
            for suffix in renamed:
                old, new = "CONFIG_OPPO_" + suffix, "CONFIG_OPLUS_" + suffix
                if old in stock_values:
                    translations[old] = new
                    stream.write(new + "=" + stock_values[old] + "\n")
            # This new vendor default is absent from every target stock kernel.
            stream.write("# CONFIG_TOUCHPANEL_NT_PEN_SUPPORT is not set\n")
        env["TARGET_BUILD_VARIANT"] = "user"
    if data["platform"] == "sm8350":
        stock_lines = (output / ".config").read_text().splitlines()
        for line in stock_lines:
            if line.startswith("CONFIG_OPPO_FINGERPRINT") and "=" in line:
                key, value = line.split("=", 1)
                translations[key] = key.replace("CONFIG_OPPO_", "CONFIG_OPLUS_", 1)
                with (output / ".config").open("a") as stream:
                    stream.write(translations[key] + "=" + value + "\n")
    fragment = (ROOT / "configs/resukisu.config").read_text(encoding="utf-8")
    with (output / ".config").open("a", encoding="utf-8") as stream:
        stream.write("\n" + fragment)
        if module_trust:
            stream.write('CONFIG_SYSTEM_TRUSTED_KEYS="certs/stock-trusted.pem"\n')
        if release:
            stream.write('CONFIG_LOCALVERSION="' + release[len(data["kernel"]["version"]):] + '"\n')
            stream.write("# CONFIG_LOCALVERSION_AUTO is not set\n")

    run(*options, "olddefconfig", env=env)
    final_config = (output / ".config").read_text()
    if data["platform"] == "sm8150":
        for old, value in stock_values.items():
            if old in ("CONFIG_LOCALVERSION", "CONFIG_SYSTEM_TRUSTED_KEYS"):
                continue
            new = translations.get(old, old)
            if new + "=" + value not in final_config.splitlines():
                raise RuntimeError("Kconfig changed stock option: " + new)
        if "CONFIG_TOUCHPANEL_NT_PEN_SUPPORT=y" in final_config.splitlines():
            raise RuntimeError("Unexpected non-stock stylus support enabled")
    required = ["CONFIG_KSU=y", "CONFIG_KALLSYMS_ALL=y", "CONFIG_MODVERSIONS=y"]
    susfs_hooks = []
    if susfs_wanted:
        # CONFIG_KSU_SUSFS replaces CONFIG_KSU_MANUAL_HOOK in ReSukiSU's hook
        # method choice, so only one of the two can survive olddefconfig.
        required += ["CONFIG_KSU_SUSFS=y"]
        if "CONFIG_KSU_MANUAL_HOOK=y" in final_config.splitlines():
            raise RuntimeError("SUSFS and the manual hook method are mutually exclusive")
        for feature in ("CONFIG_KSU_SUSFS_SUS_PATH", "CONFIG_KSU_SUSFS_SUS_KSTAT",
                        "CONFIG_KSU_SUSFS_SPOOF_UNAME", "CONFIG_KSU_SUSFS_ENABLE_LOG",
                        "CONFIG_KSU_SUSFS_SPOOF_CMDLINE_OR_BOOTCONFIG"):
            if feature + "=y" not in final_config.splitlines():
                raise RuntimeError("Kconfig dropped required SUSFS option: " + feature)
        for path, symbol in (
                ("kernel/sys.c", "ksu_handle_setresuid"),
                ("fs/exec.c", "ksu_handle_execveat"),
                ("fs/open.c", "ksu_handle_faccessat"),
                ("fs/read_write.c", "ksu_handle_sys_read"),
                ("fs/stat.c", "ksu_handle_stat"),
                ("fs/stat.c", "ksu_handle_vfs_fstat"),
                ("kernel/reboot.c", "ksu_handle_sys_reboot"),
                ("drivers/input/input.c", "ksu_handle_input_handle_event")):
            # Mirrors the fail-fast checks in ReSukiSU's tools/inline_hook_check.mk.
            if not re.search(r"\b" + symbol + r"\b", (source / path).read_text(encoding="utf-8", errors="replace")):
                raise RuntimeError("Missing SUSFS inline hook: " + symbol + " in " + path)
        susfs_hooks = verify_inline_hooks(source)
        for path in ("fs/susfs.c", "include/linux/susfs.h", "include/linux/susfs_def.h"):
            if not (source / path).is_file():
                raise RuntimeError("SUSFS source missing from kernel tree: " + path)
    else:
        required += ["CONFIG_KSU_MANUAL_HOOK=y"]
    for required_option in required:
        if required_option not in final_config.splitlines():
            raise RuntimeError("Kconfig dropped required option: " + required_option)
    if data["platform"] == "sm8150" and diagnostics_only:
        # Use the exporting translation units: a declaration in another .i file
        # may see incomplete types and produce a different genksyms graph.
        core_targets = ("kernel/module", "kernel/locking/mutex", "kernel/sched/core",
                        "drivers/base/platform", "mm/slub", "drivers/misc/motor",
                        "fs/namespace",
                        "drivers/input/touchscreen/oplus_touchscreen/touchpanel_healthinfo",
                        "drivers/gpu/drm/msm/dsi-staging/dsi_display")
        core_dest = ROOT / "out" / name / rom / "abi-types"
        core_dest.mkdir(parents=True, exist_ok=True)
        for core_target in core_targets:
            # DRM's nested objects belong to the parent composite Makefile.
            if core_target.startswith("drivers/gpu/drm/msm/"):
                run(*options, "-j" + str(os.cpu_count() or 2), "KBUILD_SYMTYPES=1",
                    "drivers/gpu/drm/msm/", env=env)
            else:
                run(*options, "KBUILD_SYMTYPES=1", core_target + ".o", env=env)
            obj = output / (core_target + ".o")
            saved = obj.with_name("." + obj.name + ".cmd").read_text().splitlines()[0]
            if not saved.startswith("cmd_") or ":=" not in saved:
                raise ValueError("Unexpected Kbuild command record")
            command = shlex.split(saved.split(":=", 1)[1].replace("$(pound)", "#").replace("$$", "$"))
            if command[0] != "clang" or command.count("-c") != 1 or command.count("-o") != 1:
                raise ValueError("Unexpected diagnostic compiler command")
            command[command.index("-c")] = "-E"
            command[command.index("-o") + 1] = str(output / (core_target + ".i"))
            command.append("-D__GENKSYMS__")
            run(*command, cwd=output, env=env)
            for suffix in (".i", ".symtypes"):
                shutil.copyfile(output / (core_target + suffix),
                                core_dest / (core_target.replace("/", "_") + suffix))
        target = "drivers/platform/msm/gsi/gsi"
        run(*options, "KBUILD_SYMTYPES=1", target + ".o", env=env)
        run(*options, "KCPPFLAGS=-D__GENKSYMS__", target + ".i", env=env)
        dest = ROOT / "out" / name / rom / "gsi-diagnostics"
        dest.mkdir(parents=True, exist_ok=True)
        for suffix in (".o", ".i", ".symtypes"):
            shutil.copyfile(output / (target + suffix), dest / ("gsi" + suffix))
        shutil.copyfile(output / ".config", dest / "kernel.config")
        (dest / "gsi-symbols.txt").write_text(subprocess.check_output(
            ["llvm-nm", str(output / (target + ".o"))], text=True))
        with (output / (target + ".i")).open("rb") as input_file, \
                (dest / "genksyms-crcs.txt").open("wb") as stdout, \
                (dest / "genksyms-warnings.txt").open("wb") as stderr:
            result = subprocess.run([str(output / "scripts/genksyms/genksyms"), "-w"],
                                    stdin=input_file, stdout=stdout, stderr=stderr)
        save(dest / "diagnostic.json", {"genksyms_returncode": result.returncode,
                                       "compile_verified": False})
        print("GSI symbol-version diagnostics only; no kernel image produced.")
        return
    if protocol is not None:
        run(*options, "-j" + str(os.cpu_count() or 2), "KBUILD_SYMTYPES=1", "net/oplus_modules/data_module/", env=env)
    if data["platform"] == "sm8150":
        # Diagnose vendor -Werror failures before the expensive full Image build.
        run(*options, "-j" + str(os.cpu_count() or 2), "drivers/misc/oplus_motor/",
            "drivers/power/oplus/", "drivers/vservices/", env=env)
    if name == "oneplus-8t":
        run(*options, "-j" + str(os.cpu_count() or 2), "net/ipv4/route.o",
            "net/ipv6/route.o", "net/xfrm/xfrm_policy.o", env=env)
    if diagnostics_only:
        if data["platform"] == "sm8250":
            run(*options, "KBUILD_SYMTYPES=1", "drivers/input/touchscreen/touch.o", env=env)
        dest = ROOT / "out" / name / rom / "abi-types"
        dest.mkdir(parents=True, exist_ok=True)
        for item in output.rglob("*.symtypes"):
            target = dest / item.relative_to(output)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item, target)
        for item in (output / "net/oplus_modules/data_module").rglob("*.symversions"):
            print(item.read_text())
        shutil.copyfile(modules / "vendor/oplus/kernel/network/data_module/proto-src/netlink_msg.pb-c.h", dest / "netlink_msg.pb-c.h")
        print("ABI type diagnostics only; no kernel image produced.")
        return
    run_logged(options, ["-j" + str(os.cpu_count() or 2), "Image"], env, work / "image-build.log")
    actual_release = (output / "include/config/kernel.release").read_text().strip()
    if actual_release != release:
        raise ValueError("Compiled kernel release differs from the requested daxiaamu release")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
            summary.write("Kernel release: `" + actual_release + "`\n\n")
    image = output / "arch/arm64/boot/Image"
    if not image.is_file() or image.stat().st_size < 1024:
        raise RuntimeError("Kernel image missing")
    dest = ROOT / "out" / name / rom
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(image, dest / "Image")
    shutil.copyfile(output / ".config", dest / "kernel.config")
    for diagnostic in ("Module.symvers", "System.map"):
        if (output / diagnostic).is_file():
            shutil.copyfile(output / diagnostic, dest / diagnostic)
    compiler = subprocess.check_output(["clang", "--version"], text=True)
    actions = None
    if os.environ.get("GITHUB_ACTIONS") == "true":
        actions = {key: os.environ[variable] for key, variable in (
            ("repository", "GITHUB_REPOSITORY"), ("run_id", "GITHUB_RUN_ID"),
            ("run_attempt", "GITHUB_RUN_ATTEMPT"), ("ref", "GITHUB_REF"),
            ("commit", "GITHUB_SHA"))}
        actions["url"] = "https://github.com/" + actions["repository"] + "/actions/runs/" + actions["run_id"]
    save(dest / "build.json", {"device": name, "os": rom, "firmware": firmware["build_id"], "kernel_release": actual_release, "stock_kernel_release": stock_release, "kernel_release_suffix": SUFFIX, "kernel": data["kernel"], "resukisu": data["resukisu"], "resukisu_selection": resukisu_selection,
        "compat_patch_sha256": sha256(compat_patch) if compat_patch.exists() else None, "stock_abi_header_sha256": sha256(stock_abi_header) if stock_abi_header else None, "vendor_patch_sha256": sha256(vendor_patch) if vendor_patch.exists() else None, "vendor": data["vendor"], "patch_sha256": sha256(patch), "image_sha256": sha256(image), "compiler": compiler, "compiler_lock": compiler_lock, "stock_protocol": protocol, "linker": subprocess.check_output([linker, "--version"], text=True).splitlines()[0], "native_features": native_features,
        "module_trust": module_trust, "config_sha256": sha256(output / ".config"), "stock_config_supplied": stock_config, "config_translations": translations,
        "susfs_inline_hooks": susfs_hooks,
        "linker_lock": linker_lock, "github_actions": actions, "compile_verified": True, "device_verified": False})
    verify_embedded(firmware, dest)
    print("Compile complete; boot packaging and on-device checks remain.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", required=True, choices=list(PATCHES))
    parser.add_argument("--os", choices=("coloros", "oxygenos"), default="coloros")
    parser.add_argument("--config", type=pathlib.Path, help="Uncompressed config from the matching stock kernel")
    parser.add_argument("--diagnostics-only", action="store_true")
    args = parser.parse_args()
    build(args.device, args.config, args.os, args.diagnostics_only)
