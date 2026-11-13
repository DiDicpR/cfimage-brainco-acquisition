import os,unittest
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtWidgets import QApplication,QDialog
from PySide6.QtCore import QTimer
from src.transfer_ui import TransferDialog


class AutomaticPreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def test_success_closes_download_step_without_operator_click(self):
        dialog=TransferDialog('test',lambda _:dict(verified=1),auto_close=True)
        watchdog=QTimer(dialog);watchdog.setSingleShot(True);watchdog.timeout.connect(dialog.reject);watchdog.start(3000)
        self.assertEqual(dialog.exec(),QDialog.DialogCode.Accepted)
        self.assertFalse(dialog.worker.error)

    def test_failed_download_stays_visible_instead_of_advancing(self):
        def fail(_):raise IOError('NAS disconnected')
        dialog=TransferDialog('test',fail,auto_close=True)
        observed=[]
        def inspect():
            if dialog.worker.isRunning():QTimer.singleShot(20,inspect);return
            observed.append(dialog.isVisible());dialog.reject()
        QTimer.singleShot(50,inspect)
        self.assertEqual(dialog.exec(),QDialog.DialogCode.Rejected)
        self.assertEqual(observed,[True])
        self.assertIn('NAS disconnected',dialog.worker.error)
