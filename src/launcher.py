"""One GUI for first-time assets, NAS login, and explicit acquisition launch."""
import sys
from PySide6.QtCore import QProcess,QTimer
from PySide6.QtWidgets import QApplication, QDialog, QVBoxLayout, QLabel, QPushButton, QMessageBox
from .nas_transfer import ROOT,download_assets
from .transfer_ui import TransferDialog,connect_smb


class Launcher(QDialog):
    def __init__(self):
        super().__init__();self.setWindowTitle('CFIMAGE · 强脑采集启动器 · 内网加速版');self.resize(560,340)
        layout=QVBoxLayout(self)
        hint=QLabel('自动从 NAS 下载并校验图片，无需输入账号密码，完成后自动打开采集工作台。\n请在工作台填写真实被试编号，再测试设备并开始采集。')
        hint.setWordWrap(True);layout.addWidget(hint)
        for text,callback in [('重新下载 / 校验全部图片',self.download),('打开采集工作台',self.launch),('数据上传授权（仅上传时使用）',self.login)]:
            button=QPushButton(text);button.clicked.connect(callback);layout.addWidget(button)
        self.status=QLabel('就绪');self.status.setWordWrap(True);layout.addWidget(self.status)
        self.process=QProcess(self);self.process.setWorkingDirectory(str(ROOT))
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self.output)
        self.process.finished.connect(lambda code,status:self.status.setText('采集程序已退出。' if code==0 else '采集程序已退出，请查看 reports/launcher.log。'))
    def login(self):
        try:connect_smb(self)
        except Exception as e:QMessageBox.warning(self,'NAS',str(e))
    def download(self):
        if self.process.state()!=QProcess.ProcessState.NotRunning:
            QMessageBox.information(self,'采集程序正在运行','请关闭采集程序后再更新图片。');return
        TransferDialog('下载并校验图片',lambda progress:download_assets(progress=progress),self).exec()
    def launch(self):
        if self.process.state()!=QProcess.ProcessState.NotRunning:return
        transfer=TransferDialog('自动准备 / 校验实验图片（无需登录）',lambda progress:download_assets(progress=progress),self,auto_close=True)
        transfer.exec()
        if transfer.worker.error:return
        self.status.setText('正在校验素材并打开工作台…')
        self.process.start(sys.executable,['-X','utf8',str(ROOT/'main.py'),'--config',str(ROOT/'config/brainco.toml'),'--gui'])
    def output(self):
        text=bytes(self.process.readAllStandardOutput()).decode('utf-8',errors='replace')
        log=ROOT/'reports/launcher.log';log.parent.mkdir(exist_ok=True)
        with log.open('a',encoding='utf-8') as f:f.write(text)
        if text.strip():self.status.setText(text.strip()[-600:])
    def reject(self):
        if self.process.state()==QProcess.ProcessState.NotRunning:super().reject()
    def closeEvent(self,event):
        if self.process.state()!=QProcess.ProcessState.NotRunning:
            QMessageBox.information(self,'采集程序正在运行','请先在采集窗口中结束并保存，再关闭启动器。');event.ignore()
        else:super().closeEvent(event)


def main():
    app=QApplication.instance() or QApplication(sys.argv);dialog=Launcher();dialog.show()
    QTimer.singleShot(0,dialog.launch)
    return app.exec()

if __name__=='__main__':raise SystemExit(main())
