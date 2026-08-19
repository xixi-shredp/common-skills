---
name: gem5-stats-analysis
description: gem5 输出结果统计分析，并写一份 markdown 中文报告
---

# Gem5 stats.txt 分析

## 输出要求

1. 报告使用中文，关键术语保留英文
2. 语气面向专业的 CPU 体系结构工程师
3. 文档严格参考模板
4. 分析范围尽量全面，覆盖 CPU 各子系统的所有部件

## 报告的内容要求

针对当前 gem5 的运行结果进行统计分析，输出一个报告到 rpt.md ，至少包括：
1. 整体性能对比（IPC simpoint 加权聚合对比），各个配置的绝对 ipc 与各配置相对于 Baseline 的配置
2. Top-Down 分析结果的瓶颈变化(列出不同配置下的各个瓶颈，分层级列出所有瓶颈占比统计)；
3. 针对不同的瓶颈占比，分析对应的子系统指标:
    - 对于前端瓶颈 Frontend Bound: 分析前端每周期取指数、ICache hit rate，Latency , 指令预取器，FTQ 占用率, 前端空闲周期占总周期比值等指标
    - 对于预测瓶颈 Bad Speculation，分析分支预测准确率、方向预测准确率、BTB/RAS/ITTAGE 准确率和 miss rate 等指标
    - 对于后端瓶颈 Backend Bound, 进一步细分分析
      - 对于 Core Bound： 分析 IssueQueue, Dispatch Queue, LSQ 等相关的指标
      - 对于 Memory Bound：分析各级 Cache 的 hit rate, 数据预取器，Latency 等指标

每一类指标都需要列表对比（列出每个配置的指标具体数值和相对于 baseline 的提升/降低）

## 分析要求

列表统计之后进行分析

1. 需要结合不同的实验配置、对应的子系统的参数和配置进行分析
2. 要分析出当前性能提升的方向和性能受限的原因

