#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("xiaomi14_builder")


PRESET_ANDROID = "android14"
PRESET_KERNEL = "6.1"
PRESET_SUBLEVEL = "138"
PRESET_OS_PATCH = "2025-06"
PRESET_CUSTOM_VERSION = "-android14-11-g965475777129-mi"
DEFAULT_KERNELSU_REF = "v4.1.3"

SUSFS_BRANCH = "gki-android14-6.1"
MANIFEST_BRANCH = f"common-{PRESET_ANDROID}-{PRESET_KERNEL}-{PRESET_OS_PATCH}"

REPO_TOOL_URL = "https://storage.googleapis.com/git-repo-downloads/repo"
SUKISU_REPO = "https://github.com/SukiSU-Ultra/SukiSU-Ultra.git"
SUKISU_SETUP_MAIN = "https://raw.githubusercontent.com/SukiSU-Ultra/SukiSU-Ultra/main/kernel/setup.sh"
SUSFS_REPO = "https://github.com/ShirkNeko/susfs4ksu.git"
SUKISU_PATCH_REPO = "https://github.com/ShirkNeko/SukiSU_patch.git"
ANYKERNEL_REPO = "https://github.com/WildPlusKernel/AnyKernel3.git"
ANYKERNEL_BRANCH = "gki-2.0"
AOSP_MIRROR = "https://android.googlesource.com"
BUILD_TOOLS_BRANCH = "main-kernel-build-2024"
MKBOOTIMG_BRANCH = "main-kernel-build-2024"

STAGES = (
    "clone_support_repos",
    "prepare_toolchain",
    "sync_kernel_source",
    "integrate_kernelsu",
    "integrate_susfs",
    "apply_compat_fixes",
    "configure_kernel",
    "build_kernel",
    "package_artifacts",
)

KERNEL_CONFIGS = {
    "CONFIG_KSU": "y",
    "CONFIG_KPM": "n",
    "CONFIG_KSU_SUSFS": "y",
    "CONFIG_KSU_SUSFS_SUS_SU": "n",
    "CONFIG_KSU_SUSFS_SUS_MAP": "y",
    "CONFIG_KSU_SUSFS_SUS_MOUNT": "y",
    "CONFIG_KSU_SUSFS_AUTO_ADD_SUS_KSU_DEFAULT_MOUNT": "y",
    "CONFIG_KSU_SUSFS_AUTO_ADD_SUS_BIND_MOUNT": "y",
    "CONFIG_KSU_SUSFS_SUS_KSTAT": "y",
    "CONFIG_KSU_SUSFS_TRY_UMOUNT": "y",
    "CONFIG_KSU_SUSFS_AUTO_ADD_TRY_UMOUNT_FOR_BIND_MOUNT": "y",
    "CONFIG_KSU_SUSFS_SPOOF_UNAME": "y",
    "CONFIG_KSU_SUSFS_ENABLE_LOG": "y",
    "CONFIG_KSU_SUSFS_HIDE_KSU_SUSFS_SYMBOLS": "y",
    "CONFIG_KSU_SUSFS_SPOOF_CMDLINE_OR_BOOTCONFIG": "y",
    "CONFIG_KSU_SUSFS_OPEN_REDIRECT": "y",
    "CONFIG_KSU_SUSFS_SUS_PATH": "y",
    "CONFIG_TMPFS_XATTR": "y",
    "CONFIG_TMPFS_POSIX_ACL": "y",
    "CONFIG_IP_NF_TARGET_TTL": "y",
    "CONFIG_IP6_NF_TARGET_HL": "y",
    "CONFIG_IP6_NF_MATCH_HL": "y",
}


class BuildFailure(RuntimeError):
    pass


@dataclass
class BuildConfig:
    workspace: str
    kernelsu_ref: str = DEFAULT_KERNELSU_REF
    susfs_ref: str = ""
    boot_sign_key_path: str = ""

    @property
    def release_version(self) -> str:
        return f"{PRESET_KERNEL}.{PRESET_SUBLEVEL}{PRESET_CUSTOM_VERSION}"

    @property
    def work_id(self) -> str:
        return f"{PRESET_ANDROID}-{PRESET_KERNEL}-{PRESET_SUBLEVEL}"


@dataclass
class BuildResult:
    success: bool
    message: str
    stage: str
    artifacts: list[str] = field(default_factory=list)
    build_time: Optional[float] = None


class Xiaomi14Builder:
    def __init__(self, config: BuildConfig):
        self.config = config
        self.workspace = Path(config.workspace).resolve()
        self.root_dir = self.workspace / config.work_id
        self.kernel_root = self.root_dir
        self.common_dir = self.kernel_root / "common"
        self.output_dir = self.root_dir / "out"
        self.repo_tool_dir = self.workspace / "repo-tool"
        self.repo_tool_path = self.repo_tool_dir / "repo"
        self.susfs_dir = self.workspace / "susfs4ksu"
        self.sukisu_patch_dir = self.workspace / "SukiSU_patch"
        self.anykernel_dir = self.workspace / "AnyKernel3"
        self.build_tools_dir = self.workspace / "build-tools"
        self.mkbootimg_dir = self.workspace / "mkbootimg"
        self.env = os.environ.copy()
        self.env["CCACHE_DIR"] = self.env.get("CCACHE_DIR", str(Path.home() / ".ccache"))
        self.env["CCACHE_COMPILERCHECK"] = "%compiler% -dumpmachine; %compiler% -dumpversion"
        self.env["CCACHE_NOHASHDIR"] = "true"
        self.env["CCACHE_HARDLINK"] = "true"
        self.env["PYTHONUNBUFFERED"] = "1"
        self.workspace.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def list_stages() -> tuple[str, ...]:
        return STAGES

    @staticmethod
    def resolve_stage_range(from_stage: Optional[str], until_stage: Optional[str]) -> list[str]:
        start = STAGES.index(from_stage) if from_stage else 0
        end = STAGES.index(until_stage) + 1 if until_stage else len(STAGES)
        if start >= end:
            raise BuildFailure("--from-stage 不能晚于 --until-stage")
        return list(STAGES[start:end])

    def _run(
        self,
        cmd: str,
        cwd: Optional[Path] = None,
        capture_output: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        run_cwd = str(cwd or self.workspace)
        logger.info("执行命令: %s", cmd)
        try:
            return subprocess.run(
                cmd,
                cwd=run_cwd,
                env=self.env,
                shell=True,
                executable="/bin/bash",
                capture_output=capture_output,
                text=True,
                check=check,
            )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or str(exc)).strip()
            raise BuildFailure(detail) from exc

    def _ensure_command(self, name: str):
        if shutil.which(name) is None:
            raise BuildFailure(f"缺少依赖命令: {name}")

    def preflight(self, selected_stages: list[str]):
        if os.name == "nt":
            raise BuildFailure("该构建器只能在 Linux/WSL 环境运行。")

        required = {"bash", "git", "curl", "patch", "python3", "zip", "gzip", "unzip"}
        if "build_kernel" in selected_stages:
            required.add("ccache")
        for command in sorted(required):
            self._ensure_command(command)

    def clone_support_repos(self):
        repos = [
            (self.susfs_dir, f"git clone --depth 1 -b {SUSFS_BRANCH} {SUSFS_REPO} {self.susfs_dir}"),
            (self.sukisu_patch_dir, f"git clone --depth 1 {SUKISU_PATCH_REPO} {self.sukisu_patch_dir}"),
            (self.anykernel_dir, f"git clone --depth 1 -b {ANYKERNEL_BRANCH} {ANYKERNEL_REPO} {self.anykernel_dir}"),
        ]
        for repo_dir, cmd in repos:
            if repo_dir.exists():
                logger.info("已存在，跳过: %s", repo_dir)
                continue
            self._run(cmd)

        if self.config.susfs_ref:
            self._run("git fetch origin", cwd=self.susfs_dir)
            self._run(f"git checkout {self.config.susfs_ref}", cwd=self.susfs_dir)

    def prepare_toolchain(self):
        self.repo_tool_dir.mkdir(parents=True, exist_ok=True)
        if not self.repo_tool_path.exists():
            self._run(f"curl -Lo {self.repo_tool_path} {REPO_TOOL_URL}")
            self._run(f"chmod a+rx {self.repo_tool_path}")

        if not self.build_tools_dir.exists():
            self._run(
                f"git clone --depth 1 -b {BUILD_TOOLS_BRANCH} "
                f"{AOSP_MIRROR}/kernel/prebuilts/build-tools {self.build_tools_dir}"
            )

        if not self.mkbootimg_dir.exists():
            self._run(
                f"git clone --depth 1 -b {MKBOOTIMG_BRANCH} "
                f"{AOSP_MIRROR}/platform/system/tools/mkbootimg {self.mkbootimg_dir}"
            )

        self.env["REPO"] = str(self.repo_tool_path)
        self.env["AVBTOOL"] = str(self.build_tools_dir / "linux-x86/bin/avbtool")
        self.env["MKBOOTIMG"] = str(self.mkbootimg_dir / "mkbootimg.py")

        if self.config.boot_sign_key_path:
            self.env["BOOT_SIGN_KEY_PATH"] = self.config.boot_sign_key_path

    def sync_kernel_source(self):
        self.root_dir.mkdir(parents=True, exist_ok=True)
        if not (self.root_dir / ".repo").exists():
            self._run(
                f"{self.repo_tool_path} init --depth=1 "
                f"-u https://android.googlesource.com/kernel/manifest "
                f"-b {MANIFEST_BRANCH} --repo-rev=v2.16",
                cwd=self.root_dir,
            )

        remote = self._run(
            f"git ls-remote https://android.googlesource.com/kernel/common refs/heads/{PRESET_ANDROID}-{PRESET_KERNEL}-{PRESET_OS_PATCH}",
            capture_output=True,
        ).stdout.strip()
        if not remote:
            deprecated = f"deprecated/{PRESET_ANDROID}-{PRESET_KERNEL}-{PRESET_OS_PATCH}"
            remote = self._run(
                f"git ls-remote https://android.googlesource.com/kernel/common refs/heads/{deprecated}",
                capture_output=True,
            ).stdout.strip()
            if not remote:
                raise BuildFailure(f"找不到 AOSP 分支: {PRESET_ANDROID}-{PRESET_KERNEL}-{PRESET_OS_PATCH}")
            manifest = self.root_dir / ".repo/manifests/default.xml"
            content = manifest.read_text(encoding="utf-8")
            content = content.replace(
                f'"{PRESET_ANDROID}-{PRESET_KERNEL}-{PRESET_OS_PATCH}"',
                f'"{deprecated}"',
            )
            manifest.write_text(content, encoding="utf-8")

        self._run(f"{self.repo_tool_path} --trace sync -c -j$(nproc --all) --no-tags --fail-fast", cwd=self.root_dir)
        if not self.common_dir.exists():
            raise BuildFailure("repo sync 完成后未找到 common 目录")

    def integrate_kernelsu(self):
        setup_url = (
            f"https://raw.githubusercontent.com/SukiSU-Ultra/SukiSU-Ultra/{self.config.kernelsu_ref}/kernel/setup.sh"
            if self.config.kernelsu_ref
            else SUKISU_SETUP_MAIN
        )
        setup_ref = self.config.kernelsu_ref or ""
        if setup_ref:
            self._run(f"curl -LSs {setup_url} | bash -s -- {setup_ref}", cwd=self.root_dir)
        else:
            self._run(f"curl -LSs {setup_url} | bash -s --", cwd=self.root_dir)

        kernelsu_dir = self.root_dir / "KernelSU"
        if self.config.kernelsu_ref and kernelsu_dir.exists():
            self._run(f"git checkout {self.config.kernelsu_ref}", cwd=kernelsu_dir)

    def integrate_susfs(self):
        patch_name = f"50_add_susfs_in_gki-{PRESET_ANDROID}-{PRESET_KERNEL}.patch"
        susfs_patch = self.susfs_dir / "kernel_patches" / patch_name
        if not susfs_patch.exists():
            raise BuildFailure(f"缺少 SUSFS 补丁: {susfs_patch}")

        self._run(f"cp {susfs_patch} {self.common_dir}/")

        for src, dst in [
            (self.susfs_dir / "kernel_patches/fs", self.common_dir / "fs"),
            (self.susfs_dir / "kernel_patches/include/linux", self.common_dir / "include/linux"),
        ]:
            self._run(f"cp -r {src}/* {dst}/")

        self._run(f"patch -p1 --fuzz=3 < {patch_name}", cwd=self.common_dir)

        hide_patch = self.sukisu_patch_dir / "69_hide_stuff.patch"
        if hide_patch.exists():
            self._run(f"cp {hide_patch} {self.common_dir}/")
            self._run("patch -p1 -F 3 < 69_hide_stuff.patch", cwd=self.common_dir)

    def apply_compat_fixes(self):
        self._fix_task_mmu()
        self._fix_base_c()
        self._prepare_bazel_tree()

    def _fix_task_mmu(self):
        task_mmu = self.common_dir / "fs/proc/task_mmu.c"
        content = task_mmu.read_text(encoding="utf-8")

        if "if (!vma_pages(vma))" not in content and "goto show_pad;" in content:
            content = content.replace("goto show_pad;", "return 0;")

        content = re.sub(r"^(\s*)struct dentry \*dentry;\n", "", content, flags=re.MULTILINE)
        if "goto bypass;" not in content:
            content = re.sub(r"^(\s*)bypass:\n", "", content, flags=re.MULTILINE)

        task_mmu.write_text(content, encoding="utf-8")

    def _fix_base_c(self):
        base_c = self.common_dir / "fs/proc/base.c"
        content = base_c.read_text(encoding="utf-8")
        if "#include <linux/dma-buf.h>" not in content:
            content = content.replace(
                "#include <linux/cpufreq_times.h>",
                "#include <linux/cpufreq_times.h>\n#include <linux/dma-buf.h>",
            )
            base_c.write_text(content, encoding="utf-8")

    def _prepare_bazel_tree(self):
        build_bazel = self.common_dir / "BUILD.bazel"
        if build_bazel.exists():
            lines = build_bazel.read_text(encoding="utf-8").splitlines()
            lines = [
                line
                for line in lines
                if '"protected_exports_list"' not in line
                and "android/abi_gki_protected_exports_aarch64" not in line
            ]
            build_bazel.write_text("\n".join(lines) + "\n", encoding="utf-8")

        abi_file = self.common_dir / "android/abi_gki_protected_exports_aarch64"
        if abi_file.exists():
            if abi_file.is_dir():
                shutil.rmtree(abi_file)
            else:
                abi_file.unlink()

        stamp_bzl = self.kernel_root / "build/kernel/kleaf/impl/stamp.bzl"
        if stamp_bzl.exists():
            content = stamp_bzl.read_text(encoding="utf-8").replace("-maybe-dirty", "")
            stamp_bzl.write_text(content, encoding="utf-8")

        build_config = self.common_dir / "build.config.gki"
        if build_config.exists():
            content = build_config.read_text(encoding="utf-8").replace("check_defconfig", "")
            build_config.write_text(content, encoding="utf-8")

        build_config_aarch64 = self.common_dir / "build.config.gki.aarch64"
        if build_config_aarch64.exists():
            content = build_config_aarch64.read_text(encoding="utf-8")
            content = content.replace("BUILD_SYSTEM_DLKM=1", "BUILD_SYSTEM_DLKM=0")
            content = "\n".join(
                line
                for line in content.splitlines()
                if "MODULES_ORDER=android/gki_aarch64_modules" not in line
                and "KMI_SYMBOL_LIST_STRICT_MODE" not in line
            )
            build_config_aarch64.write_text(content + "\n", encoding="utf-8")

        setlocalversion = self.common_dir / "scripts/setlocalversion"
        if setlocalversion.exists():
            content = setlocalversion.read_text(encoding="utf-8").replace("-dirty", "")
            setlocalversion.write_text(content, encoding="utf-8")

    def configure_kernel(self):
        defconfig = self.common_dir / "arch/arm64/configs/gki_defconfig"
        content = defconfig.read_text(encoding="utf-8")

        for key, value in KERNEL_CONFIGS.items():
            pattern = re.compile(rf"^{re.escape(key)}=.*$", flags=re.MULTILINE)
            line = f'{key}={value}'
            if key == "CONFIG_LOCALVERSION":
                line = f'{key}="{value}"'
            if pattern.search(content):
                content = pattern.sub(line, content)
            else:
                content += f"\n{line}"

        localversion_pattern = re.compile(r'^CONFIG_LOCALVERSION=".*"$', flags=re.MULTILINE)
        localversion_line = f'CONFIG_LOCALVERSION="{PRESET_CUSTOM_VERSION}"'
        if localversion_pattern.search(content):
            content = localversion_pattern.sub(localversion_line, content)
        else:
            content += f"\n{localversion_line}"

        defconfig.write_text(content.strip() + "\n", encoding="utf-8")

    def build_kernel(self):
        self._run(
            "tools/bazel build --disk_cache=/home/runner/.cache/bazel "
            "--config=fast --lto=thin //common:kernel_aarch64_dist",
            cwd=self.root_dir,
        )

    def package_artifacts(self) -> list[str]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        artifacts: list[str] = []
        image_dir = self.root_dir / "bazel-bin/common/kernel_aarch64"

        for image_name in ("Image", "Image.lz4"):
            src = image_dir / image_name
            if src.exists():
                dest = self.output_dir / image_name
                shutil.copy2(src, dest)
                artifacts.append(str(dest))

        image = self.output_dir / "Image"
        if image.exists():
            self._run("gzip -n -k -f -9 Image", cwd=self.output_dir)
            image_gz = self.output_dir / "Image.gz"
            if image_gz.exists():
                artifacts.append(str(image_gz))

        artifacts.extend(self._create_anykernel_zips())
        artifacts.extend(self._create_boot_images())
        self._write_checksums()
        return artifacts

    def _artifact_prefix(self) -> str:
        return f"xiaomi14-{self.config.release_version}"

    def _create_anykernel_zips(self) -> list[str]:
        produced: list[str] = []
        mappings = [
            ("Image", ""),
            ("Image.gz", "-gz"),
            ("Image.lz4", "-lz4"),
        ]

        for image_name, suffix in mappings:
            src = self.output_dir / image_name
            if not src.exists():
                continue

            for leftover in ("Image", "Image.gz", "Image.lz4"):
                leftover_path = self.anykernel_dir / leftover
                if leftover_path.exists():
                    leftover_path.unlink()

            shutil.copy2(src, self.anykernel_dir / image_name)
            zip_name = f"{self._artifact_prefix()}-AnyKernel3{suffix}.zip"
            self._run(f"zip -r {self.output_dir / zip_name} ./* -x .git/*", cwd=self.anykernel_dir)
            (self.anykernel_dir / image_name).unlink(missing_ok=True)
            produced.append(str(self.output_dir / zip_name))

        return produced

    def _create_boot_images(self) -> list[str]:
        key_path = self.env.get("BOOT_SIGN_KEY_PATH", "").strip()
        if not key_path:
            logger.warning("未提供 BOOT_SIGN_KEY_PATH，跳过 boot.img 打包，仅保留 AnyKernel3 ZIP。")
            return []

        produced: list[str] = []
        for kernel_name, output_name in [
            ("Image", "boot.img"),
            ("Image.gz", "boot-gz.img"),
            ("Image.lz4", "boot-lz4.img"),
        ]:
            kernel_path = self.output_dir / kernel_name
            if not kernel_path.exists():
                continue

            self._run(
                f"{self.env['MKBOOTIMG']} --header_version 4 --kernel {kernel_name} --output {output_name}",
                cwd=self.output_dir,
            )
            self._run(
                f"{self.env['AVBTOOL']} add_hash_footer "
                f"--partition_name boot --partition_size $((64 * 1024 * 1024)) "
                f"--image {output_name} --algorithm SHA256_RSA2048 --key {key_path}",
                cwd=self.output_dir,
            )

            final_name = f"{self._artifact_prefix()}-{output_name}"
            final_path = self.output_dir / final_name
            shutil.move(self.output_dir / output_name, final_path)
            produced.append(str(final_path))

        return produced

    def _write_checksums(self):
        self._run("sha256sum * > SHA256SUMS.txt", cwd=self.output_dir)

    def build(
        self,
        from_stage: Optional[str] = None,
        until_stage: Optional[str] = None,
        preflight_only: bool = False,
    ) -> BuildResult:
        started = time.time()
        current_stage = "preflight"
        selected_stages = self.resolve_stage_range(from_stage, until_stage)
        handlers = {
            "clone_support_repos": self.clone_support_repos,
            "prepare_toolchain": self.prepare_toolchain,
            "sync_kernel_source": self.sync_kernel_source,
            "integrate_kernelsu": self.integrate_kernelsu,
            "integrate_susfs": self.integrate_susfs,
            "apply_compat_fixes": self.apply_compat_fixes,
            "configure_kernel": self.configure_kernel,
            "build_kernel": self.build_kernel,
            "package_artifacts": self.package_artifacts,
        }

        try:
            self.preflight(selected_stages)
            if preflight_only:
                return BuildResult(
                    success=True,
                    message="预检通过",
                    stage=selected_stages[-1],
                    build_time=time.time() - started,
                )

            artifacts: list[str] = []
            logger.info("执行阶段: %s", " -> ".join(selected_stages))
            for current_stage in selected_stages:
                logger.info("--- 阶段开始: %s ---", current_stage)
                result = handlers[current_stage]()
                if current_stage == "package_artifacts" and result:
                    artifacts.extend(result)
                logger.info("--- 阶段完成: %s ---", current_stage)

            return BuildResult(
                success=True,
                message="构建完成",
                stage=selected_stages[-1],
                artifacts=artifacts,
                build_time=time.time() - started,
            )
        except Exception as exc:
            logger.error("构建失败 [%s]: %s", current_stage, exc)
            return BuildResult(
                success=False,
                message=str(exc),
                stage=current_stage,
                build_time=time.time() - started,
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Xiaomi 14 Android14 6.1.138 kernel builder")
    parser.add_argument("--workspace", default=os.environ.get("GKI_WORKSPACE", "/tmp/gki-build"))
    parser.add_argument("--kernelsu-ref", default=DEFAULT_KERNELSU_REF)
    parser.add_argument("--susfs-ref", default="")
    parser.add_argument("--boot-sign-key-path", default=os.environ.get("BOOT_SIGN_KEY_PATH", ""))
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--list-stages", action="store_true")
    parser.add_argument("--from-stage", choices=Xiaomi14Builder.list_stages())
    parser.add_argument("--until-stage", choices=Xiaomi14Builder.list_stages())
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    if args.list_stages:
        for stage in Xiaomi14Builder.list_stages():
            print(stage)
        return 0

    config = BuildConfig(
        workspace=args.workspace,
        kernelsu_ref=args.kernelsu_ref,
        susfs_ref=args.susfs_ref,
        boot_sign_key_path=args.boot_sign_key_path,
    )
    result = Xiaomi14Builder(config).build(
        from_stage=args.from_stage,
        until_stage=args.until_stage,
        preflight_only=args.preflight_only,
    )

    print("=" * 60)
    print("Xiaomi 14 Build Summary")
    print("=" * 60)
    print(f"Success : {result.success}")
    print(f"Stage   : {result.stage}")
    print(f"Message : {result.message}")
    if result.artifacts:
        print("Artifacts:")
        for artifact in result.artifacts:
            print(f"  - {artifact}")
    print("=" * 60)

    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
