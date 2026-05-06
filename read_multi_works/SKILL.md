---
name: read_multi_works
description: 需要阅读多篇计算机体系结构相关的论文或者专利并总结精炼出阅读报告时使用
---

# Read Multi Works

1. 首先检查本机是否存在 read_single_work skill
2. 如果不存在，暂停，请用户输入 read_single_work skill 的路径
3. 针对每一篇论文/专利，单独启动一个 subagent ，每个 subagent 使用 read_single_work skill 阅读总结对应的论文/专利，并输出对应的阅读报告

