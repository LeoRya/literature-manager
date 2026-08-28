# 本地文献管理系统

一个只建立索引、不改动原始 PDF 的本地桌面文献库。完整需求见 [docs/requirements-v0.1.md](docs/requirements-v0.1.md)。

## 主要功能

- 递归扫描文件夹中的 PDF，并增量更新索引
- 从 PDF 提取正文与元数据，使用 Crossref/OpenAlex 联网补全
- DOI、文件哈希、标题与年份联合去重；一篇文献可关联多个位置
- 自定义标签、待确认队列和人工校对
- 元数据、标签、路径及 PDF 正文的全局搜索
- 条件之间、同字段多个值之间均支持 AND/OR
- 直接打开 PDF 或在 Finder/资源管理器中显示
- CSV、BibTeX、RIS 导入与导出
- 保留并迁移已有 `paper_library.db` 中的文献与标签

## 启动

建议使用项目自己的虚拟环境：

```bash
cd literature-manager
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python run_literature_manager.py
```

macOS 也可以双击 `启动文献管理系统.command`。首次运行会创建虚拟环境并安装依赖，因此需要联网。

## 数据位置

默认使用项目根目录中的 `paper_library.db`。数据库迁移是增量的，不会删除旧记录。建议像备份其他论文资料一样定期备份这个文件。

联网元数据查询只发送 DOI 或候选标题；PDF 文件与全文不会上传。关闭“联网补全元数据”后，扫描可以完全离线运行。

## 测试

```bash
PYTHONPYCACHEPREFIX=/tmp/literature-manager-pycache .venv/bin/python -m unittest discover -s tests -v
```
