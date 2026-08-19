# Skills

本目录包含面向计算机体系结构研究、gem5 开发与技术文档产出的实用技能。各技能的简要功能如下：

| Skill | 功能简介 |
| --- | --- |
| [`arch-image-gen`](./arch-image-gen/) | 为计算机体系结构论文或专利生成流水线架构图、硬件结构图等技术插图。 |
| [`gem5-compile`](./gem5-compile/) | 规范 gem5 修改后的编译与验证流程，帮助选择合适的构建目标和检查方式。 |
| [`gem5-perf-debug`](./gem5-perf-debug/) | 指导定位在 gem5 中新增或修改设计后出现的性能问题。 |
| [`gem5-reproduce`](./gem5-reproduce/) | 将论文或专利中的体系结构设计复现到 gem5，并按流程进行实现与复核。 |
| [`gem5-stats-analysis`](./gem5-stats-analysis/) | 分析 gem5 的 `stats.txt` 输出，提取关键统计指标并生成中文 Markdown 报告。 |
| [`gen-typst-slide`](./gen-typst-slide/) | 使用 Typst 模板生成结构清晰、适合展示的演示文稿。 |
| [`idea2spec`](./idea2spec/) | 将初步想法整理、扩展为一份或多份可执行的设计文档。 |
| [`make-xs-checkpoints`](./make-xs-checkpoints/) | 为香山 EMU、NEMU 和 xs-gem5 工作流构建 OpenSBI、Linux workload 与 checkpoint。 |
| [`png2drawio`](./png2drawio/) | 将流程图、架构图等 PNG 示意图重建为可编辑的 draw.io 文件，并通过渲染对比迭代校准。 |
| [`read_multi_works`](./read_multi_works/) | 阅读并综合多篇计算机体系结构论文或专利，形成结构化阅读报告。 |
| [`read_single_work`](./read_single_work/) | 阅读单篇计算机体系结构论文或专利，并按统一模板输出精炼的阅读报告。 |
| [`related_work`](./related_work/) | 围绕指定主题检索相关论文和专利，筛选候选工作并生成调研报告。 |
| [`write_spec_docs`](./write-spec-docs/) | 根据代码或已有文档说明指定设计，输出一套重点明确的 spec 文档。 |

每个子目录中的 `SKILL.md` 包含该技能的适用场景、执行步骤和具体约束；部分技能还提供模板、示例、参考资料或辅助脚本。
