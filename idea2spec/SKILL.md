---
name: idea2spec
description: 将用户的 idea 转换成一份或多份设计文档
---

# Idea 转换成设计文档

负责将用户的 idea 转换成一份设计文档。

## workflow

1. 根据用户输入的 idea 陈述，在 [Google Scholar](https://scholar.google.com/) 和 [Google Patents](https://patents.google.com/) 中查找相关领域的论文和专利
2. 如果没有找到相关的论文和专利，跳过
3. 将搜集到的相关的论文和专利整理到用户工作目录下的 `related_work.md`
    - markdown 内容格式参考 [related_work.md](examples/related_work.md)
4. 根据找到的专利和论文，以及用户的 idea 输入，思考出几种可能的设计方案
5. 将每一种可能的设计方案写入到一份设计文档中：`spec_1.md`, `spec_2.md`, ...
    - 设计文档需要包含：方案的理论依据，具体的实现路径，建议的实验方案

