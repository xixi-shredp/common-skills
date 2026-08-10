---
name: make-xs-checkpoints
description: 为 XiangShan EMU/NEMU/xs-gem5 工作流构建 OpenSBI+Linux workload/checkpoints
---

# 香山 checkpoint workload 制作

基于香山官方教程执行。

将 OpenSBI、Linux、rootfs、nemu_board 和 Linux 内核压缩包放在同一个父目录；不要假定仓库版本或本机工具链前缀。优先复用机器上已有且经验证的基础仓库，但绝不让多个切片 workflow 共享同一个可写构建目录

## 先确认输入与环境

1. 确认目标：仅验证最简启动，还是制作可供 SimPoint/checkpoint 使用的 `gcpt.bin`；后者使用 SPEC initramfs 和 GCPT 链接流程。
2. 确认 RISC-V Linux 交叉编译器可执行
3. 设置绝对路径，避免当前目录和 shell 会话变化造成误构建：

```bash
export RISCV_LINUX_HOME=/absolute/path/linux-<version>
export RISCV_ROOTFS_HOME=/absolute/path/riscv-rootfs
export WORKLOAD_BUILD_ENV_HOME=/absolute/path/nemu_board
export OPENSBI_HOME=/absolute/path/opensbi
export RISCV=/absolute/path/riscv-gnu-toolchain-install
export ARCH=riscv
export CROSS_COMPILE="$RISCV/bin/riscv64-unknown-linux-gnu-"
export PATH="$RISCV/bin:$PATH"
```

## 复用基础仓库与隔离切片

开始下载前，检查用户提供的环境变量、常用工作区和候选目录是否已有 OpenSBI、Linux、`riscv-rootfs`、`nemu_board` 及需要时的 LibCheckpointAlpha。对每个候选仓库确认预期文件存在、记录 `git rev-parse HEAD`，并检查 `git status --short`；仅复用版本和修改状态均符合本次构建要求的仓库。不要因发现已有仓库而覆盖、清理或更新它。

将已有的干净仓库作为**只读源仓库**。单个切片可以直接复用它；只要两个或以上切片会并行构建，或 workflow 会修改配置、initramfs、设备树链接或 `build/`，就在该 workflow 的私有目录建立隔离副本。Linux 内核必须隔离，因为 `.config`、生成头文件、`arch/riscv/boot/Image` 和编译中间产物会互相覆盖；会写入构建产物的 OpenSBI、rootfs 和 nemu_board 也应隔离

对 Git 仓库优先使用 worktree，而不是重新下载对象：

```bash
# 每个 slice 使用唯一且新的 $SLICE_WORKDIR；源仓库必须干净。
export LINUX_SOURCE=/absolute/path/existing/linux
export SLICE_WORKDIR=/absolute/path/worktrees/slice-<unique-id>
git -C "$LINUX_SOURCE" worktree add --detach "$SLICE_WORKDIR/linux" HEAD
export RISCV_LINUX_HOME="$SLICE_WORKDIR/linux"
```

为 rootfs、nemu_board 和 OpenSBI 同样建立各自的 worktree；若其来源不是 Git 仓库，先在私有 `$SLICE_WORKDIR` 创建完整副本。不要共享任何 slice 的 `build/`、`.config`、`dts/build/`、`platform.dtsi` 或输出 Image。完成后仅在用户授权时执行 `git worktree remove` 或删除私有目录。

## 按剩余资源并行

在启动构建前调用 `<skill-directory>/scripts/host_parallelism.py --slices <并行切片数>`，读取机器可用 CPU、当前负载和 `MemAvailable`，获得建议的总并发编译数与每个切片的 `make -j` 值。将每个 workflow 的 `make -j` 限制为该值；不要让每个切片都使用 `-j$(nproc)`。若输出的每切片并行度为 1、内存紧张、磁盘空间不足或已有高负载，减少并行 slice 数或顺序执行。

构建层次按依赖划分：不同 slice 的「rootfs → dtb → Linux → OpenSBI → GCPT」链彼此并行；同一 slice 内保持该顺序，避免 OpenSBI 在 Image/dtb 尚未完成时读取不完整输入。记录实际并行数、每个 slice 的私有工作目录以及所有源仓库 commit。

## 获取并准备源代码

仅在不存在可复用的合格基础仓库时，按教程获取所需源码；允许替换 Linux 版本，但要相应更新目录名和补丁/配置兼容性。

```bash
git clone https://github.com/riscv-software-src/opensbi.git
wget https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.10.3.tar.xz
git clone https://github.com/OpenXiangShan/riscv-rootfs -b checkpoint
git clone https://github.com/OpenXiangShan/nemu_board.git
tar -xf linux-6.10.3.tar.xz
```

在 rootfs 仓库按其 Makefile 构建要打包进 initramfs 的应用。为单核 NEMU/EMU 设备树，将 `${WORKLOAD_BUILD_ENV_HOME}/dts/platform.dtsi` 链接到对应的 `platform_noop.dtsi`，再在该仓库运行 `build_single_core_for_nemu.sh`。确认产物为 `dts/build/xiangshan.dtb`。

## 构建最简 OpenSBI + Linux

先用该路径验证工具链、内核配置和设备树。复制 `nemu_board/configs/xiangshan_defconfig` 到内核的 `arch/riscv/configs/`，然后：

```bash
cd "$RISCV_LINUX_HOME"
make xiangshan_defconfig
make -j"$(nproc)"

cd "$OPENSBI_HOME"
make PLATFORM=generic \
  FW_PAYLOAD_PATH="$RISCV_LINUX_HOME/arch/riscv/boot/Image" \
  FW_FDT_PATH="$WORKLOAD_BUILD_ENV_HOME/dts/build/xiangshan.dtb" \
  FW_PAYLOAD_OFFSET=0x200000
```

仅在确有配置需求时运行 `make menuconfig`；最简启动的 initramfs 默认是 `rootfsimg/initramfs-emu.txt`。成功产物是 `build/platform/generic/firmware/fw_payload.bin`。

## 制作 SPEC / checkpoint workload

1. 在 Linux `make menuconfig` 中，将 initramfs source 从 `rootfsimg/initramfs-emu.txt` 改为 `rootfsimg/initramfs-spec.txt`。
2. 按 workload 需求编辑 `initramfs-spec.txt`；不修改该文件不能直接得到可用的 SPEC workload。
3. 重新构建内核。
4. 清理 OpenSBI 的旧构建目录，避免复用旧 Image 或设备树。
5. 对内核 Image 调用下列脚本；它根据官方的「Image 超过 32 MiB 时」规则计算对齐后的 `FW_PAYLOAD_FDT_ADDR`。

```bash
rm -rf "$OPENSBI_HOME/build"
python3 <skill-directory>/scripts/opensbi_fdt_addr.py \
  "$RISCV_LINUX_HOME/arch/riscv/boot/Image"
```

若脚本报告不需要地址，构建：

```bash
make -C "$OPENSBI_HOME" PLATFORM=generic \
  FW_PAYLOAD_PATH="$RISCV_LINUX_HOME/arch/riscv/boot/Image" \
  FW_FDT_PATH="$WORKLOAD_BUILD_ENV_HOME/dts/build/xiangshan.dtb" \
  FW_PAYLOAD_OFFSET=0x100000 -j10
```

若脚本输出十六进制地址（例如 `0x...`），在上面的命令中追加 `FW_PAYLOAD_FDT_ADDR=<脚本输出>`。不要把 shell 文本原样当作 Make 变量；使用 `--format value` 取得仅地址值。

## 生成 GCPT 输入

获取并构建 LibCheckpointAlpha，将 OpenSBI payload 作为输入：

```bash
git clone https://github.com/OpenXiangShan/LibCheckpointAlpha.git
export GCPT_HOME=/absolute/path/LibCheckpointAlpha
make -C "$GCPT_HOME" \
  GCPT_PAYLOAD_PATH="$OPENSBI_HOME/build/platform/generic/firmware/fw_payload.bin"
```

交付前验证 `$GCPT_HOME/build/gcpt.bin` 存在且非空，并记录 Linux、OpenSBI、rootfs、nemu_board、LibCheckpointAlpha 的 commit 与完整构建命令。该 `gcpt.bin` 可用于直接启动，也可作为 SimPoint profiling 和 checkpoint 的 workload。

## 排障与约束

- 遇到 `riscv64-unknown-linux-gnu-gcc: command not found`，优先检查 `${CROSS_COMPILE}gcc`、`RISCV` 和 `PATH`；仅在确认工具链采用发行版命名前缀时替换为 `riscv64-linux-gnu-`。
- 设备树问题先检查 `platform.dtsi` 的链接目标和 `dts/build/xiangshan.dtb` 的更新时间。
- 每次改变 initramfs、Image 或 dtb 后，重新构建 OpenSBI；特别是 checkpoint 路径必须清掉旧的 `$OPENSBI_HOME/build`。
- 仅在 Image **大于** 32 MiB 时传递 `FW_PAYLOAD_FDT_ADDR`；地址为 `align_up(0x80000000 + Image大小 + 2 MiB, 1 MiB)`。
- 不擅自下载、编译或删除外部工程；先取得用户授权并在执行前确认目标目录。

官方依据：<https://docs.xiangshan.cc/zh-cn/latest/workloads/opensbi-kernel-for-xs/>
