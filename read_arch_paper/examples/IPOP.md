---
title: "I-POP (2026 HPCA): Ignite Positive Prefetchers"
date: 2026-03-22 23:41:34
categories:
description: "2026 HPCA 论文阅读：I-POP 多预取器管理"
tags: [HPCA, 2026]
---

整体上，这篇论文不是在发明一个新的 prefetcher，而是在解决一个更“系统级”的问题：**多个 prefetcher 同时存在时，怎样动态决定谁该开、谁该关、谁该更激进**

TLDR:
1. 现有多预取器管理策略存在问题， Static， RL-based，Performance-counter-based 
2. 提出新的训练参考指标 PE 代表预取器真实能带来的收益（accuracy 和 coverage 并不适合作为训练目标）
3. 硬件上通过两个表收集计算 PE 需要的信息，然后根据 PE 调整子预取器的开关和激进度

# 背景与动机

...

结论：...

---

## 相关工作: 已有的 multi-prefetcher management

### 1. Static Schemes

...

### 2. RL-based Schemes

...

### 3. Performance-counter-based Schemes

...

---

# Insight：提出 Prefetch Effectiveness（PE）

...

## 1. useful prefetch 的收益 $I_{UPF}$

...

## 2. cache pollution 的损失 $I_{POLL}$

...

## 3. 竞争导致的延迟损失 $I_{LAT}$

...

---

# 核心设计：I-POP 硬件设计

I-POP 由两个大模块构成：

### 1. Metric Collector

...

### 2. Control Engine

...

---

# 实验 setup

实验平台: ChampSim

workload 选择：Spec2017

系统设置：
1. 4 个 state-of-the-art prefetcher   
2. I-POP
  - ...

---

# 实验结果

## 实验 1. 总体性能

...

## 实验 2. coverage 与 overprediction

...

---

# Limits

1. PE 仍然是近似，不是真正因果测量
    - ...
2. ...
...
