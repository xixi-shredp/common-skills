---
name: related_work
description: 根据某一个主题检索查找相关工作（论文和专利）并生成一份报告
---

# Related Work 查找相关工作

根据某一个主题检索查找相关工作（论文和专利）并生成一份报告

## workflow

1. 询问用户：是否下载相关工作的文件到用户工作目录下的 sources/ 下
2. 根据指定的主题和领域，在 [Google Scholar](https://scholar.google.com/) 和 [Google Patents](https://patents.google.com/) 中查找相关领域的论文和专利
    - 如果找到的论文是来自 arxiv 的预印本，再尝试检索，尽量找到真正发表的版本
3. 将搜集到的相关的论文和专利整理到用户工作目录下的 `related_work.md`
    - markdown 内容格式参考 [related_work.md](examples/related_work.md)
4. 如果步骤 1 中用户要求下载相关工作，则按照用户的要求下载文件
5. 启动一个新的 subagent 检查当前的搜索结果是否有遗漏的论文和专利，如果有，重复步骤 2-5，直到没有遗漏的相关工作

## Output Rules

输出的报告文件 `related_work.md` 要求：
1. 内容简介部分主要使用中文
2. 专业术语，标题，作者信息等使用英文
3. 所有的相关工作必须在互联网上可以检索到

