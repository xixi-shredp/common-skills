---
name: gem5-compile
description: Gem5 修改和编译约束，在需要编译或者验证修改 Gem5 时使用。
---

# Gem5 修改和编译约束

- 在编译 Gem5 时，直接使用 `scons -j$(nproc) build/RISCV/gem5.opt`，编译过程中不检查中间产物的编译状态，必须等待最终 scons 编译的后台进程完全退出才能继续动作
- 编译或链接过程中出现错误时，暂停交由用户处理
- 后续任何需要使用 gem5.opt 前必须确保 scons 编译进程完全退出，不能 scons 未完全退出时运行 gem5.opt.
- 测试验证 gem5 的功能时，不使用 gtest ，直接编译最终的 gem5.opt 来验证功能。
