"""Scene/view/image selection only; never deletes stimulus files."""
from pathlib import Path
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QTreeWidget,
    QTreeWidgetItem, QLabel, QPushButton, QLineEdit)
from .experiment.scenes import read_scene_catalog


class SceneSelector(QWidget):
    changed = Signal()

    def __init__(self, manifest, selected_ids=None, parent=None):
        super().__init__(parent)
        self.setObjectName('sceneSelector')
        self.stimuli, self.metadata = read_scene_catalog(manifest)
        self.paths = {s.image_id: s.file_path for s in self.stimuli}
        self.manifest_dir = Path(manifest).resolve().parent
        self.items = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        note = QLabel('默认全选。勾选场景或视角可批量选择；取消勾选仅排除本次采集，不删除文件。')
        note.setWordWrap(True); layout.addWidget(note)
        self.search = QLineEdit(); self.search.setPlaceholderText('搜索物体编号、名称、场景或文件名（仅筛选显示）')
        layout.addWidget(self.search)
        controls = QHBoxLayout()
        for text, checked in [('全部选中', True), ('全部取消', False)]:
            button = QPushButton(text); button.clicked.connect(lambda _, value=checked: self.set_all(value))
            controls.addWidget(button)
        self.count = QLabel(); controls.addWidget(self.count, 1);layout.addLayout(controls)
        row = QHBoxLayout(); self.tree = QTreeWidget(); self.tree.setHeaderLabels(['场景 / 视角 / 图片', '物品名称'])
        self.tree.setColumnWidth(0, 330); self.tree.setMinimumHeight(330)
        row.addWidget(self.tree, 3)
        self.preview = QLabel('点击图片查看预览');self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setFixedSize(220, 160);self.preview.setWordWrap(True);row.addWidget(self.preview, 1)
        layout.addLayout(row)
        scenes, views = {}, {}
        selected = set(self.metadata) if selected_ids is None else set(selected_ids)
        for s in self.stimuli:
            m = self.metadata[s.image_id];scene, view = m['scene'], m['view']
            if scene not in scenes:
                scenes[scene] = self.parent_item(self.tree, scene)
            if (scene, view) not in views:
                views[scene, view] = self.parent_item(scenes[scene], view)
            item = QTreeWidgetItem(views[scene, view], [s.file_path.name, m['object_name']])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setData(0, Qt.ItemDataRole.UserRole, s.image_id)
            item.setCheckState(0, Qt.CheckState.Checked if s.image_id in selected else Qt.CheckState.Unchecked)
            self.items[s.image_id] = item
        self.tree.expandToDepth(0)
        self.tree.itemChanged.connect(self.on_changed)
        self.tree.currentItemChanged.connect(self.show_preview)
        self.search.textChanged.connect(self.filter_items)
        self.update_count()

    @staticmethod
    def parent_item(parent, text):
        item = QTreeWidgetItem(parent, [text])
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsAutoTristate | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(0, Qt.CheckState.Unchecked)
        return item

    def selected_ids(self):
        return [identity for identity, item in self.items.items() if item.checkState(0) == Qt.CheckState.Checked]

    def update_count(self):
        self.count.setText(f'已选 {len(self.selected_ids())} / {len(self.items)} 张')

    def on_changed(self, *_):
        self.update_count();self.changed.emit()

    def set_all(self, checked):
        self.tree.blockSignals(True)
        for item in self.items.values():item.setCheckState(0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.tree.blockSignals(False);self.on_changed()

    def filter_items(self, text):
        text = text.strip().casefold()
        for identity, item in self.items.items():
            m = self.metadata[identity]
            item.setHidden(text not in ' '.join([identity, item.text(0), m['scene'], m['view'], m['object_name']]).casefold())
        if text:self.tree.expandAll()
        for i in range(self.tree.topLevelItemCount()):
            scene = self.tree.topLevelItem(i)
            for j in range(scene.childCount()):
                view = scene.child(j);view.setHidden(all(view.child(k).isHidden() for k in range(view.childCount())))
            scene.setHidden(all(scene.child(j).isHidden() for j in range(scene.childCount())))

    def show_preview(self, item, *_):
        identity = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if identity not in self.paths:return
        thumbnail = self.manifest_dir / 'thumbnails' / f'{identity}.jpg'
        pixmap = QPixmap(str(thumbnail if thumbnail.is_file() else self.paths[identity]))
        self.preview.setPixmap(pixmap.scaled(self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                            Qt.TransformationMode.SmoothTransformation))
