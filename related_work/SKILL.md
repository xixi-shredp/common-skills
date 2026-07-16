---
name: related_work
description: 根据某一个主题检索查找相关工作（论文和专利）并生成一份报告
---

# Related Work 查找相关工作

根据某一个主题检索查找相关工作（论文和专利）并生成一份报告

## workflow SOP

1. 根据主题在检索源下进行论文/专利检索
2. 对于检索出的每一篇论文/专利，启动一个 subagent(model: gpt-5.3-codex-spark, effort: xhigh)，按照"检索候选项判定标准"进行判定
3. 对于论文候选项，启动一个 subagent (model: gpt-5.3-codex-spark, effort: xhigh) 阅读该论文的参考文献部分，找出参考文献中和论文主题比较符合的论文也纳入本次检索的候选项
4. 如果论文候选项是来自 arxiv 的预印本，再尝试检索，尽量找到真正发表的版本
5. 将搜集到的相关的论文和专利整理到用户工作目录下的 `related_work.md`
    - markdown 内容格式参考 [related_work.md](examples/related_work.md)
6. 启动一个新的 subagent (model: gpt-5.3-codex-spark, effort: xhigh) 检查当前的搜索结果是否有遗漏的论文和专利，如果有，重复步骤 1-6，直到没有遗漏的相关工作
7. 停下来询问用户：是否下载相关工作的文件到用户工作目录下的 sources/ 下

## 检索源

论文检索来源：
- [DBLP](https://dblp.org/)
- [Google Scholar](https://scholar.google.com/)
- [IEEE Xplore](https://ieeexplore.ieee.org)
- [ACM](https://www.acm.org/publications/proceedings)
- 论文发表的会议/期刊的网站
- 根据论文名、主题、领域、作者名进行论文检索
- 论文作者主页、实验室主页等

专利检索来源：
[Google Patents](https://patents.google.com/)

## 检索候选项判定标准
- 阅读论文/专利的摘要部分，大致判断这篇论文针对的领域是否和主题一致相关，相关则判定论文为检索候选项，不相关则放弃
- 对于不相关的论文，不要纳入候选项，不要为了凑数就加入候选项

## Output Rules

输出的报告文件 `related_work.md` 要求：
1. 内容简介部分主要使用中文
2. 专业术语，标题，作者信息等使用英文
3. 所有的相关工作必须在互联网上可以检索到
4. 内容格式严格按照模板：[related_work.md](examples/related_work.md)
    - 尤其是表格，不要擅自增加、修改表格字段

