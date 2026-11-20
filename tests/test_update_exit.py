"""Exercise Qt's real quit event; mocking QApplication.quit hides close vetoes."""
import os
from pathlib import Path
import subprocess
import sys


def test_ready_updater_can_close_real_qt_window_and_event_loop(tmp_path):
    code = r'''
import sys
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
import umeyc_steamback.app as module
from umeyc_steamback.updater import PreparedUpdate

app = QApplication([])
module.Window.refresh = lambda self: None
module.launch_update = lambda *args: None  # Independent updater signalled ready.
window = module.Window(Path(sys.argv[1]) / "data")
window.monitor.stop()
window.prepared_update = PreparedUpdate("0.2.0", Path(sys.argv[1]), Path(sys.argv[1]))
window.show()
app.setQuitOnLastWindowClosed(True)
timed_out = []
def fail():
    timed_out.append(True)
    app.exit(9)
QTimer.singleShot(3000, fail)
QTimer.singleShot(0, window.install_prepared_update)
result = app.exec()
window.update_pool.waitForDone(5000)
assert not timed_out, "Update handoff was blocked by the window close handler"
assert window.quitting and not window.isVisible()
assert result == 0
'''
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)],
                            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                            capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
