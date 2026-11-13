"""Read-only participant coverage by within-category image position."""
from copy import deepcopy
import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QHBoxLayout, QHeaderView,
    QLineEdit, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from .operator_ui import STYLE, PreflightWorker, label
from .experiment.positions import query_positions


class ProgressDialog(QDialog):
    def __init__(self, experiment, modes, participant='', mode='neuracle', parent=None):
        super().__init__(parent)
        self.experiment = deepcopy(experiment)
        self.worker = None
        self.report = None
        self.setObjectName('workbench')
        self.setWindowTitle('THINGS EEG · 被试图片覆盖查询')
        self.setMinimumSize(900, 650)
        self.resize(1080, 820)
        self.setStyleSheet(STYLE + '''
            QTableWidget { background: white; alternate-background-color: #f3f7f6; border: 1px solid #dce5e5; gridline-color: #e5eceb; }
            QTableWidget::item:selected { background: #237f73; color: white; }
            QHeaderView::section { background: #e7efed; color: #244b4a; padding: 9px; border: none; }
        ''')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(25, 24, 25, 24)
        layout.setSpacing(12)
        layout.addWidget(label('被试图片覆盖查询', 'pageTitle'))
        layout.addWidget(label('按全部训练类别核对第 1–10 张；仅查询已保存数据，不连接设备、不修改进度。', 'muted', True))
        inputs = QHBoxLayout()
        self.participant = QLineEdit(participant)
        self.participant.setPlaceholderText('被试编号，如 sub-112')
        self.mode = QComboBox()
        self.mode.addItems(modes)
        self.mode.setCurrentText(mode)
        self.repeat = QSpinBox()
        self.repeat.setRange(1, 100)
        self.repeat.setValue(int(experiment['collection'].get('target_repetitions', 10)))
        self.test_repeat = QSpinBox()
        self.test_repeat.setRange(1, 100)
        self.test_repeat.setValue(int(experiment['collection'].get('test_repetitions', 10)))
        for text, widget in [('被试编号', self.participant), ('采集模式', self.mode),
                             ('训练图达标次数', self.repeat), ('测试图达标次数', self.test_repeat)]:
            column = QVBoxLayout()
            column.addWidget(label(text, 'field'))
            column.addWidget(widget)
            inputs.addLayout(column, 1)
        self.query_button = QPushButton('查询全部序号')
        self.query_button.setObjectName('startButton')
        self.query_button.clicked.connect(self.query)
        inputs.addWidget(self.query_button)
        layout.addLayout(inputs)
        self.status = label('输入被试编号与累计目标次数后查询。', 'muted', True)
        layout.addWidget(self.status)
        self.table = self.make_table(['图片范围', '类别总数', '至少采过 1 次', '达到目标次数', '还需有效试次', '结果'])
        self.table.setMaximumHeight(330)
        self.table.setMinimumHeight(315)
        self.table.verticalHeader().setMinimumSectionSize(24)
        self.table.verticalHeader().setDefaultSectionSize(24)
        self.table.itemSelectionChanged.connect(self.show_details)
        layout.addWidget(self.table, 1)
        detail_header = QHBoxLayout()
        self.detail_title = label('选择上方一行，查看具体类别与图片', 'field')
        detail_header.addWidget(self.detail_title, 1)
        self.only_missing = QCheckBox('仅显示未达标类别')
        self.only_missing.setChecked(True)
        self.only_missing.toggled.connect(self.show_details)
        detail_header.addWidget(self.only_missing)
        layout.addLayout(detail_header)
        self.details = self.make_table(['类别', '图片文件', '已有效次数', '目标次数', '尚缺次数'])
        layout.addWidget(self.details, 1)
        layout.addWidget(label('“全部达标”要求每个类别均达到当前指定次数。模拟数据不计入正式设备模式；历史次数不会因调整目标而清零。', 'small', True))
        for signal in (self.participant.textChanged, self.mode.currentIndexChanged,
                       self.repeat.valueChanged, self.test_repeat.valueChanged):
            signal.connect(self.clear_result)

    @staticmethod
    def make_table(headers):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setAlternatingRowColors(True)
        table.verticalHeader().hide()
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        return table

    def clear_result(self):
        self.report = None
        self.table.setRowCount(0)
        self.details.setRowCount(0)
        self.status.setText('查询条件已变化，请重新查询。')

    def query(self):
        if self.worker:
            return
        participant = self.participant.text().strip()
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', participant) or participant == 'sub-':
            self.status.setText('请输入有效的匿名被试编号。')
            return
        self.clear_result()
        experiment = deepcopy(self.experiment)
        experiment['collection'].update(positions=[1], target_repetitions=self.repeat.value(),
                                        test_repetitions=self.test_repeat.value())
        self.status.setText('正在核对全部序号的有效试次及已保存 EEG / Trigger，请稍候…')
        for widget in (self.participant, self.mode, self.repeat, self.test_repeat, self.query_button):
            widget.setEnabled(False)
        self.worker = PreflightWorker(lambda mode, *_: query_positions(experiment, participant, mode),
                                      self.mode.currentText(), {}, 0, self)
        self.worker.finished.connect(self.query_finished)
        self.worker.start()

    def query_finished(self):
        result, error = self.worker.result, self.worker.error
        self.worker.deleteLater()
        self.worker = None
        for widget in (self.participant, self.mode, self.repeat, self.test_repeat, self.query_button):
            widget.setEnabled(True)
        if error:
            self.status.setText('查询失败：' + error)
            return
        self.report = result
        self.status.setText(f"被试 {result['participant_id']} · 模式 {result['run_mode']} · 训练目标 {result['target_repetitions']} 次/图；"
                            f"另有 {result['unverified_trials']} 条历史有效记录未通过落盘核对，未计入。")
        self.table.setRowCount(len(result['coverage']))
        for index, row in enumerate(result['coverage']):
            values = [f"训练第 {row['position']} 张" if row['partition'] == 'training' else '测试集（独立）',
                      row['total_categories'], row['acquired_categories'], row['completed_categories'],
                      row['remaining_trials'], '全部达标' if row['completed_categories'] == row['total_categories'] else '未全部达标']
            self.fill_row(self.table, index, values)
        if result['coverage']:
            self.table.selectRow(0)

    @staticmethod
    def fill_row(table, index, values):
        for column, value in enumerate(values):
            item = QTableWidgetItem()
            item.setData(Qt.ItemDataRole.DisplayRole, value)
            table.setItem(index, column, item)

    def show_details(self):
        index = self.table.currentRow()
        self.details.setRowCount(0)
        if not self.report or index < 0:
            return
        row = self.report['coverage'][index]
        details = [d for d in row['details'] if not self.only_missing.isChecked() or d['missing'] > 0]
        self.detail_title.setText(f"{'训练第 ' + str(row['position']) + ' 张' if row['partition']=='training' else '测试集'} · 显示 {len(details)} 个类别")
        self.details.setRowCount(len(details))
        for index, detail in enumerate(details):
            self.fill_row(self.details, index, [detail['category'], detail['filename'], detail['valid'], detail['target'], detail['missing']])

    def reject(self):
        if not self.worker:
            super().reject()

    def closeEvent(self, event):
        if self.worker:
            event.ignore()
        else:
            super().closeEvent(event)
