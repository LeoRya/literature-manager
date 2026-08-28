from __future__ import annotations

import html
import queue
import subprocess
import sys
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .config import APP_NAME, APP_VERSION, DEFAULT_DB_PATH
from .database import Database
from .import_export import export_file, import_file
from .scanner import LibraryScanner
from .search import FIELDS, SearchCondition, SearchService


STATUS_LABELS = {"ready": "已识别", "review": "待确认", "error": "识别失败"}


class FilterRow(QWidget):
    removed = Signal(object)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        self.field = QComboBox()
        for label, value in FIELDS.items():
            self.field.addItem(label, value)
        self.values = QLineEdit()
        self.values.setPlaceholderText("多个值用分号分隔")
        self.logic = QComboBox()
        self.logic.addItem("任一值（OR）", "OR")
        self.logic.addItem("全部值（AND）", "AND")
        remove = QPushButton("移除")
        remove.setObjectName("secondaryButton")
        remove.clicked.connect(lambda: self.removed.emit(self))
        layout.addWidget(self.field)
        layout.addWidget(self.values, 1)
        layout.addWidget(self.logic)
        layout.addWidget(remove)

    def condition(self) -> SearchCondition | None:
        values = [part.strip() for part in self.values.text().split(";") if part.strip()]
        if not values:
            return None
        return SearchCondition(self.field.currentData(), values, self.logic.currentData())


class EditPaperDialog(QDialog):
    def __init__(self, paper: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("编辑并确认文献信息")
        self.resize(720, 720)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.title_edit = QLineEdit(paper.get("title") or "")
        self.authors_edit = QLineEdit(paper.get("authors") or "")
        self.doi_edit = QLineEdit(paper.get("doi") or "")
        self.journal_edit = QLineEdit(paper.get("journal") or "")
        self.year_edit = QLineEdit(paper.get("year") or "")
        self.date_edit = QLineEdit(paper.get("publication_date") or "")
        self.keywords_edit = QLineEdit(paper.get("keywords_auto") or "")
        self.tags_edit = QLineEdit("; ".join(paper.get("tags") or []))
        self.abstract_edit = QPlainTextEdit(paper.get("abstract") or "")
        self.abstract_edit.setMaximumHeight(150)
        self.notes_edit = QPlainTextEdit(paper.get("notes") or "")
        self.notes_edit.setMaximumHeight(120)
        self.verified = QCheckBox("标记为已人工确认（后续扫描不覆盖这些元数据）")
        self.verified.setChecked(bool(paper.get("is_verified")))
        form.addRow("标题", self.title_edit)
        form.addRow("作者（分号分隔）", self.authors_edit)
        form.addRow("DOI", self.doi_edit)
        form.addRow("期刊", self.journal_edit)
        form.addRow("年份", self.year_edit)
        form.addRow("发表日期", self.date_edit)
        form.addRow("关键词（分号分隔）", self.keywords_edit)
        form.addRow("自定义标签（分号分隔）", self.tags_edit)
        form.addRow("摘要", self.abstract_edit)
        form.addRow("备注", self.notes_edit)
        form.addRow("", self.verified)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def result_values(self) -> tuple[dict, list[str]]:
        values = {
            "title": self.title_edit.text().strip(),
            "authors": self.authors_edit.text().strip(),
            "doi": self.doi_edit.text().strip(),
            "journal": self.journal_edit.text().strip(),
            "year": self.year_edit.text().strip(),
            "publication_date": self.date_edit.text().strip(),
            "keywords_auto": self.keywords_edit.text().strip(),
            "abstract": self.abstract_edit.toPlainText().strip(),
            "notes": self.notes_edit.toPlainText().strip(),
            "is_verified": int(self.verified.isChecked()),
            "status": "ready" if self.verified.isChecked() else "review",
        }
        tags = [item.strip() for item in self.tags_edit.text().split(";") if item.strip()]
        return values, tags


class MainWindow(QMainWindow):
    def __init__(self, database: Database | None = None):
        super().__init__()
        self.database = database or Database(DEFAULT_DB_PATH)
        self.search_service = SearchService(self.database)
        self.current_results: list[dict] = []
        self.current_paper_id: int | None = None
        self.filter_rows: list[FilterRow] = []
        self.scan_thread: threading.Thread | None = None
        self.scan_queue: queue.Queue = queue.Queue()
        self.setWindowTitle(f"{APP_NAME} · v{APP_VERSION}")
        self.resize(1460, 900)
        self.setMinimumSize(1100, 700)
        self._build_menu()
        self._build_ui()
        self._apply_style()
        self._load_roots()
        self.refresh_results()

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("文件")
        import_action = QAction("导入 CSV / BibTeX / RIS…", self)
        import_action.triggered.connect(self.import_records)
        export_action = QAction("导出当前结果…", self)
        export_action.triggered.connect(self.export_records)
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(import_action)
        file_menu.addAction(export_action)
        file_menu.addSeparator()
        file_menu.addAction(quit_action)

        help_menu = self.menuBar().addMenu("帮助")
        about_action = QAction("关于", self)
        about_action.triggered.connect(
            lambda: QMessageBox.about(
                self,
                "关于",
                f"{APP_NAME} v{APP_VERSION}\n\n本地索引 PDF，不移动原文件。\n数据库：{self.database.path}",
            )
        )
        help_menu.addAction(about_action)

    def _build_ui(self) -> None:
        central = QWidget()
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(18, 14, 18, 14)
        root_layout.setSpacing(10)

        title_row = QHBoxLayout()
        title = QLabel("文献库")
        title.setObjectName("pageTitle")
        self.count_label = QLabel()
        self.count_label.setObjectName("muted")
        title_row.addWidget(title)
        title_row.addWidget(self.count_label)
        title_row.addStretch()
        root_layout.addLayout(title_row)

        scan_box = QGroupBox("整理文献")
        scan_layout = QGridLayout(scan_box)
        self.root_combo = QComboBox()
        self.root_combo.setEditable(True)
        self.root_combo.setMinimumWidth(480)
        choose_button = QPushButton("选择文件夹…")
        choose_button.setObjectName("secondaryButton")
        choose_button.clicked.connect(self.choose_folder)
        self.online_check = QCheckBox("联网补全元数据")
        self.online_check.setChecked(True)
        self.scan_button = QPushButton("整理文献")
        self.scan_button.clicked.connect(self.start_scan)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.scan_status = QLabel("只建立索引，不会移动或改名 PDF")
        self.scan_status.setObjectName("muted")
        scan_layout.addWidget(self.root_combo, 0, 0)
        scan_layout.addWidget(choose_button, 0, 1)
        scan_layout.addWidget(self.online_check, 0, 2)
        scan_layout.addWidget(self.scan_button, 0, 3)
        scan_layout.addWidget(self.progress, 1, 0, 1, 2)
        scan_layout.addWidget(self.scan_status, 1, 2, 1, 2)
        root_layout.addWidget(scan_box)

        search_box = QGroupBox("搜索与组合筛选")
        search_layout = QVBoxLayout(search_box)
        global_row = QHBoxLayout()
        self.global_search = QLineEdit()
        self.global_search.setPlaceholderText("搜索标题、作者、DOI、期刊、标签、路径或 PDF 正文…")
        self.global_search.setClearButtonEnabled(True)
        self.global_search.textChanged.connect(self._schedule_search)
        self.condition_logic = QComboBox()
        self.condition_logic.addItem("条件全部满足（AND）", "AND")
        self.condition_logic.addItem("条件任一满足（OR）", "OR")
        add_filter = QPushButton("添加筛选条件")
        add_filter.setObjectName("secondaryButton")
        add_filter.clicked.connect(self.add_filter_row)
        apply_filter = QPushButton("应用筛选")
        apply_filter.clicked.connect(self.refresh_results)
        clear_filter = QPushButton("清空")
        clear_filter.setObjectName("secondaryButton")
        clear_filter.clicked.connect(self.clear_filters)
        global_row.addWidget(self.global_search, 1)
        global_row.addWidget(self.condition_logic)
        global_row.addWidget(add_filter)
        global_row.addWidget(apply_filter)
        global_row.addWidget(clear_filter)
        search_layout.addLayout(global_row)
        self.filters_container = QWidget()
        self.filters_layout = QVBoxLayout(self.filters_container)
        self.filters_layout.setContentsMargins(0, 0, 0, 0)
        search_layout.addWidget(self.filters_container)
        root_layout.addWidget(search_box)

        splitter = QSplitter(Qt.Horizontal)
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["状态", "标题", "作者", "年份", "期刊", "DOI", "标签", "位置"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self.show_selected_detail)
        self.table.cellDoubleClicked.connect(lambda _row, _col: self.open_pdf())
        splitter.addWidget(self.table)

        detail_frame = QFrame()
        detail_frame.setObjectName("detailPanel")
        detail_layout = QVBoxLayout(detail_frame)
        detail_title = QLabel("文献详情")
        detail_title.setObjectName("sectionTitle")
        self.detail = QTextBrowser()
        self.detail.setOpenExternalLinks(True)
        button_row = QHBoxLayout()
        self.open_button = QPushButton("打开 PDF")
        self.open_button.clicked.connect(self.open_pdf)
        self.reveal_button = QPushButton("在文件夹中显示")
        self.reveal_button.setObjectName("secondaryButton")
        self.reveal_button.clicked.connect(self.reveal_file)
        self.edit_button = QPushButton("编辑 / 确认")
        self.edit_button.setObjectName("secondaryButton")
        self.edit_button.clicked.connect(self.edit_selected)
        for button in (self.open_button, self.reveal_button, self.edit_button):
            button.setEnabled(False)
            button_row.addWidget(button)
        detail_layout.addWidget(detail_title)
        detail_layout.addWidget(self.detail, 1)
        detail_layout.addLayout(button_row)
        splitter.addWidget(detail_frame)
        splitter.setSizes([1000, 420])
        splitter.setStretchFactor(0, 1)
        root_layout.addWidget(splitter, 1)
        self.setCentralWidget(central)
        self.statusBar().showMessage("就绪")

        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(250)
        self.search_timer.timeout.connect(self.refresh_results)
        self.scan_poll_timer = QTimer(self)
        self.scan_poll_timer.setInterval(80)
        self.scan_poll_timer.timeout.connect(self._poll_scan_queue)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #f6f7f9; color: #20242b; font-size: 13px; }
            QMenuBar { background: #ffffff; border-bottom: 1px solid #e2e5e9; }
            QGroupBox { background: #ffffff; border: 1px solid #dde1e6; border-radius: 8px;
                        margin-top: 10px; padding: 12px 10px 8px 10px; font-weight: 600; }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; }
            QLineEdit, QComboBox, QPlainTextEdit { background: #ffffff; border: 1px solid #cfd5dc;
                        border-radius: 6px; padding: 7px; selection-background-color: #2f6fed; }
            QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus { border: 1px solid #2f6fed; }
            QPushButton { background: #2f6fed; color: white; border: 0; border-radius: 6px;
                          padding: 8px 14px; font-weight: 600; }
            QPushButton:hover { background: #245fce; }
            QPushButton:disabled { background: #aeb7c5; }
            QPushButton#secondaryButton { background: #edf1f7; color: #27364b; }
            QPushButton#secondaryButton:hover { background: #dfe6f0; }
            QLabel#pageTitle { font-size: 26px; font-weight: 700; color: #172033; }
            QLabel#sectionTitle { font-size: 17px; font-weight: 700; }
            QLabel#muted { color: #667085; }
            QTableWidget { background: white; border: 1px solid #dde1e6; border-radius: 8px;
                           gridline-color: #edf0f3; alternate-background-color: #fafbfc; }
            QHeaderView::section { background: #eef2f6; border: 0; border-bottom: 1px solid #d8dde4;
                                   padding: 8px; font-weight: 600; }
            QFrame#detailPanel { background: white; border: 1px solid #dde1e6; border-radius: 8px; }
            QTextBrowser { background: white; border: 0; }
            QProgressBar { border: 1px solid #cfd5dc; border-radius: 5px; text-align: center; background: white; }
            QProgressBar::chunk { background: #2f6fed; border-radius: 4px; }
            """
        )

    def _load_roots(self) -> None:
        current = self.root_combo.currentText()
        self.root_combo.clear()
        self.root_combo.addItems(self.database.recent_roots())
        if current:
            self.root_combo.setCurrentText(current)

    def choose_folder(self) -> None:
        initial = self.root_combo.currentText() or str(Path.home())
        path = QFileDialog.getExistingDirectory(self, "选择需要索引的 PDF 文件夹", initial)
        if path:
            self.root_combo.setCurrentText(path)

    def start_scan(self) -> None:
        root = self.root_combo.currentText().strip()
        if not root or not Path(root).expanduser().is_dir():
            QMessageBox.warning(self, "无法扫描", "请先选择一个有效文件夹。")
            return
        self.scan_button.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.scan_status.setText("正在发现 PDF…")
        self.scan_thread = threading.Thread(
            target=self._run_scan_background,
            args=(root, self.online_check.isChecked()),
            name="literature-scanner",
            daemon=True,
        )
        self.scan_thread.start()
        self.scan_poll_timer.start()

    def _run_scan_background(self, root: str, online: bool) -> None:
        try:
            result = LibraryScanner(self.database).scan(
                root,
                online=online,
                progress=lambda name, current, total: self.scan_queue.put(
                    ("progress", name, current, total)
                ),
            )
            self.scan_queue.put(("finished", result))
        except Exception as exc:
            self.scan_queue.put(("failed", str(exc)))

    def _poll_scan_queue(self) -> None:
        try:
            while True:
                event = self.scan_queue.get_nowait()
                if event[0] == "progress":
                    self._scan_progress(event[1], event[2], event[3])
                elif event[0] == "finished":
                    self._scan_finished(event[1])
                    self._scan_cleanup()
                elif event[0] == "failed":
                    self._scan_failed(event[1])
                    self._scan_cleanup()
        except queue.Empty:
            pass

    @Slot(str, int, int)
    def _scan_progress(self, name: str, current: int, total: int) -> None:
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(current)
        self.scan_status.setText(f"{current}/{total} · {name}")

    @Slot(dict)
    def _scan_finished(self, result: dict) -> None:
        message = (
            f"扫描完成：新增 {result['added']}，更新 {result['updated']}，跳过 {result['skipped']}，"
            f"缺失 {result['missing']}，失败 {result['failed']}"
        )
        self.scan_status.setText(message)
        self.statusBar().showMessage(message, 15000)
        self._load_roots()
        self.refresh_results()
        if result.get("failed"):
            details = result.get("errors") or "部分文件无法读取。"
            QMessageBox.warning(self, "扫描已完成，但有失败项", message + "\n\n" + str(details)[:5000])

    @Slot(str)
    def _scan_failed(self, error: str) -> None:
        self.scan_status.setText("扫描失败")
        QMessageBox.critical(self, "扫描失败", error)

    @Slot()
    def _scan_cleanup(self) -> None:
        self.scan_button.setEnabled(True)
        self.progress.setVisible(False)
        self.scan_poll_timer.stop()
        self.scan_thread = None

    def _schedule_search(self) -> None:
        self.search_timer.start()

    def add_filter_row(self) -> None:
        row = FilterRow()
        row.removed.connect(self.remove_filter_row)
        self.filter_rows.append(row)
        self.filters_layout.addWidget(row)

    @Slot(object)
    def remove_filter_row(self, row: FilterRow) -> None:
        if row in self.filter_rows:
            self.filter_rows.remove(row)
        row.deleteLater()
        self.refresh_results()

    def clear_filters(self) -> None:
        self.global_search.clear()
        for row in self.filter_rows:
            row.deleteLater()
        self.filter_rows.clear()
        self.refresh_results()

    def refresh_results(self) -> None:
        conditions = [condition for row in self.filter_rows if (condition := row.condition())]
        try:
            self.current_results = self.search_service.search(
                self.global_search.text(), conditions, self.condition_logic.currentData()
            )
        except Exception as exc:
            QMessageBox.critical(self, "搜索失败", str(exc))
            return
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(self.current_results))
        for row_index, paper in enumerate(self.current_results):
            status = STATUS_LABELS.get(paper.get("status"), paper.get("status") or "待确认")
            if paper.get("is_verified"):
                status = "已确认"
            if not paper.get("active_files"):
                status += " · 无文件"
            paths = (paper.get("file_paths") or "").splitlines()
            values = [
                status, paper.get("title") or paper.get("filename") or "（无标题）",
                paper.get("authors") or "", paper.get("year") or "", paper.get("journal") or "",
                paper.get("doi") or "", paper.get("custom_tags") or "",
                f"{paper.get('active_files', 0)}/{len(paths)}",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                if column == 0:
                    item.setData(Qt.UserRole, paper["id"])
                self.table.setItem(row_index, column, item)
        self.table.setSortingEnabled(True)
        self.count_label.setText(f"{len(self.current_results)} 篇")
        self.statusBar().showMessage(f"当前显示 {len(self.current_results)} 篇文献")
        if self.current_results:
            self.table.selectRow(0)
        else:
            self.current_paper_id = None
            self.detail.setHtml("<p style='color:#667085'>没有符合条件的文献。</p>")
            for button in (self.open_button, self.reveal_button, self.edit_button):
                button.setEnabled(False)

    def show_selected_detail(self) -> None:
        selected = self.table.selectedItems()
        if not selected:
            return
        row = selected[0].row()
        id_item = self.table.item(row, 0)
        paper_id = id_item.data(Qt.UserRole) if id_item else None
        if not paper_id:
            return
        self.current_paper_id = int(paper_id)
        paper = self.database.get_paper(self.current_paper_id)
        if not paper:
            return
        tags = " · ".join(html.escape(tag) for tag in paper["tags"]) or "无"
        files = []
        for item in paper["files"]:
            flag = "可用" if item["exists_flag"] else "缺失"
            text_flag = "可检索正文" if item["has_text"] else "无文字层"
            error = (
                f"<br><span style='color:#b42318'>{html.escape(item.get('error_message') or '')}</span>"
                if item.get("error_message") else ""
            )
            files.append(f"<li><b>{flag}</b> · {text_flag}<br>{html.escape(item['path'])}{error}</li>")
        status = "已人工确认" if paper.get("is_verified") else STATUS_LABELS.get(paper.get("status"), "待确认")
        abstract = html.escape(paper.get("abstract") or "无").replace("\n", "<br>")
        notes = html.escape(paper.get("notes") or "无").replace("\n", "<br>")
        self.detail.setHtml(
            f"""
            <style>body {{ color:#20242b; line-height:1.45; }} h2 {{ margin-bottom:6px; }}
            dt {{ color:#667085; font-weight:600; margin-top:10px; }} dd {{ margin-left:0; }}</style>
            <h2>{html.escape(paper.get('title') or '（无标题）')}</h2>
            <p style="color:#667085">{html.escape(status)} · 可信度 {float(paper.get('confidence') or 0):.0%} ·
            {html.escape(paper.get('metadata_source') or '未知来源')}</p>
            <dl>
              <dt>作者</dt><dd>{html.escape(paper.get('authors') or '无')}</dd>
              <dt>期刊与时间</dt><dd>{html.escape(paper.get('journal') or '无')} ·
                {html.escape(paper.get('publication_date') or paper.get('year') or '无')}</dd>
              <dt>DOI</dt><dd>{html.escape(paper.get('doi') or '无')}</dd>
              <dt>关键词</dt><dd>{html.escape(paper.get('keywords_auto') or '无')}</dd>
              <dt>自定义标签</dt><dd>{tags}</dd>
              <dt>摘要</dt><dd>{abstract}</dd>
              <dt>备注</dt><dd>{notes}</dd>
              <dt>文件位置</dt><dd><ul>{''.join(files) or '<li>尚未关联本地 PDF</li>'}</ul></dd>
            </dl>
            """
        )
        valid_file = any(item["exists_flag"] and Path(item["path"]).exists() for item in paper["files"])
        self.open_button.setEnabled(valid_file)
        self.reveal_button.setEnabled(valid_file)
        self.edit_button.setEnabled(True)

    def _first_valid_file(self) -> Path | None:
        if not self.current_paper_id:
            return None
        paper = self.database.get_paper(self.current_paper_id)
        if not paper:
            return None
        for item in paper["files"]:
            path = Path(item["path"])
            if item["exists_flag"] and path.exists():
                return path
        return None

    def open_pdf(self) -> None:
        path = self._first_valid_file()
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def reveal_file(self) -> None:
        path = self._first_valid_file()
        if not path:
            return
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", "-R", str(path)])
            elif sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path.parent)])
        except OSError as exc:
            QMessageBox.warning(self, "无法显示文件", str(exc))

    def edit_selected(self) -> None:
        if not self.current_paper_id:
            return
        paper = self.database.get_paper(self.current_paper_id)
        if not paper:
            return
        dialog = EditPaperDialog(paper, self)
        if dialog.exec() == QDialog.Accepted:
            values, tags = dialog.result_values()
            self.database.update_paper(self.current_paper_id, values, tags)
            self.refresh_results()
            self.statusBar().showMessage("文献信息已保存", 5000)

    def import_records(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "导入文献记录", "", "文献数据 (*.csv *.bib *.bibtex *.ris)"
        )
        if not path:
            return
        try:
            created, matched = import_file(self.database, path)
            self.refresh_results()
            QMessageBox.information(self, "导入完成", f"新增 {created} 条，匹配已有记录 {matched} 条。")
        except Exception as exc:
            QMessageBox.critical(self, "导入失败", str(exc))

    def export_records(self) -> None:
        path, selected_filter = QFileDialog.getSaveFileName(
            self, "导出当前搜索结果", "literature_export.csv",
            "CSV (*.csv);;BibTeX (*.bib);;RIS (*.ris)",
        )
        if not path:
            return
        suffixes = {"CSV (*.csv)": ".csv", "BibTeX (*.bib)": ".bib", "RIS (*.ris)": ".ris"}
        if not Path(path).suffix:
            path += suffixes.get(selected_filter, ".csv")
        try:
            count = export_file(self.database, path, [row["id"] for row in self.current_results])
            QMessageBox.information(self, "导出完成", f"已导出 {count} 条记录到：\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "导出失败", str(exc))


def run() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    font = QFont()
    font.setPointSize(13)
    app.setFont(font)
    window = MainWindow()
    window.show()
    return app.exec()
