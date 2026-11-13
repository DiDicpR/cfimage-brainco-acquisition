"""NAS actions run outside the Qt GUI thread."""
from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QProgressBar, QPushButton, QMessageBox, QInputDialog, QLineEdit


class TransferWorker(QThread):
    message = Signal(str)
    def __init__(self, callback, parent):
        super().__init__(parent);self.callback=callback;self.result=None;self.error=''
    def run(self):
        try:self.result=self.callback(self.message.emit)
        except Exception as e:self.error=f'{type(e).__name__}: {e}'


class TransferDialog(QDialog):
    def __init__(self, title, callback, parent=None, *, auto_close=False):
        super().__init__(parent);self.auto_close=auto_close;self.setWindowTitle(title);self.resize(650,230)
        layout=QVBoxLayout(self);self.status=QLabel('正在准备…');self.status.setWordWrap(True);layout.addWidget(self.status)
        self.bar=QProgressBar();self.bar.setRange(0,0);layout.addWidget(self.bar)
        self.button=QPushButton('关闭');self.button.setEnabled(False);self.button.clicked.connect(self.accept);layout.addWidget(self.button)
        self.worker=TransferWorker(callback,self);self.worker.message.connect(self.status.setText);self.worker.finished.connect(self.done_transfer);self.worker.start()
    def done_transfer(self):
        self.bar.setRange(0,1);self.bar.setValue(1);self.button.setEnabled(True)
        if self.worker.error:self.status.setText('未完成：'+self.worker.error)
        else:
            self.status.setText('完成，已核验保存内容。\n'+str(self.worker.result))
            if self.auto_close:self.accept()
    def reject(self):
        if not self.worker.isRunning():super().reject()
    def closeEvent(self,event):
        if self.worker.isRunning():event.ignore()
        else:super().closeEvent(event)


def connect_smb(parent=None):
    """Credentials stay in Windows' current logon session, never in repo files."""
    import os,ctypes
    from ctypes import wintypes
    if os.name!='nt':raise RuntimeError('SMB login helper requires Windows')
    username,ok=QInputDialog.getText(parent,'连接 NAS','SMB 用户名（或 DOMAIN\\username）')
    if not ok:return
    password,ok=QInputDialog.getText(parent,'连接 NAS','SMB 密码（仅用于当前 Windows 会话）',QLineEdit.EchoMode.Password)
    if not ok:return
    class Resource(ctypes.Structure):
        _fields_=[('scope',wintypes.DWORD),('type',wintypes.DWORD),('display',wintypes.DWORD),('usage',wintypes.DWORD),('local',wintypes.LPWSTR),('remote',wintypes.LPWSTR),('comment',wintypes.LPWSTR),('provider',wintypes.LPWSTR)]
    resource=Resource();resource.type=1;resource.remote=r'\\172.16.4.2\aim'
    function=ctypes.WinDLL('mpr').WNetAddConnection2W
    function.argtypes=[ctypes.POINTER(Resource),wintypes.LPCWSTR,wintypes.LPCWSTR,wintypes.DWORD];function.restype=wintypes.DWORD
    code=function(ctypes.byref(resource),password,username,0);password=''
    if code:raise RuntimeError(f'Windows SMB 登录失败（{code}）。1219 表示本机已有其他账号连接此 NAS，请先在 Windows 中处理连接。')
    QMessageBox.information(parent,'NAS','已连接；密码未写入项目。')


def upload_from_operator(dialog):
    from .nas_transfer import upload_subject
    participant=dialog.participant.text().strip()
    if dialog.busy or not participant:return
    # Modal transfer keeps acquisition controls inaccessible while taking a snapshot.
    TransferDialog('上传当前被试全部数据：'+participant,
        lambda progress:upload_subject(dialog.output_root,participant,progress=progress),dialog).exec()
