"""Exercise real Qt/DBus/AT-SPI, without vendor software or network credentials."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def fixture(flag):
    from PyQt5.QtWidgets import QApplication, QDialog, QLineEdit, QPushButton, QVBoxLayout
    app = QApplication([])
    app.setApplicationName("UniVPN Fixture")
    window = QDialog()
    window.setWindowTitle("UniVPN Test Login")
    layout = QVBoxLayout(window)
    username = QLineEdit()
    username.setAccessibleName("Test Username")
    password = QLineEdit()
    password.setAccessibleName("Test Password")
    password.setEchoMode(QLineEdit.Password)
    submit = QPushButton("Test Login")
    for widget in (username, password, submit):
        layout.addWidget(widget)
    otp = QDialog(window)
    otp.setWindowTitle("UniVPN Test OTP")
    otp_layout = QVBoxLayout(otp)
    code = QLineEdit()
    code.setAccessibleName("Test OTP")
    confirm = QPushButton("Test Confirm")
    otp_layout.addWidget(code)
    otp_layout.addWidget(confirm)

    def start():
        if username.text() == "fixture-user" and password.text() == "fixture-password":
            otp.show()

    def complete():
        if code.text() == "123456":
            Path(flag).write_text("ready")
            otp.hide()

    submit.clicked.connect(start)
    confirm.clicked.connect(complete)
    window.show()
    app.exec()


if "--fixture" in sys.argv:
    fixture(sys.argv[-1])
else:
    from univpn_gateway.accessibility import AccessibleUI
    from univpn_gateway.auth import login
    mapping = {
        "username": {"role": "text", "name": "Test Username"},
        "password": {"role": "password text", "name": "Test Password"},
        "submit": {"role": "push button", "name": "Test Login"},
        "otp": {"input": {"role": "text", "name": "Test OTP"},
                "submit": {"role": "push button", "name": "Test Confirm"}},
    }
    with tempfile.TemporaryDirectory() as directory:
        flag = Path(directory) / "authenticated"
        process = subprocess.Popen([sys.executable, __file__, "--fixture", str(flag)])
        try:
            ui = AccessibleUI()
            for attempt in range(30):
                if ui.matches(mapping["username"]):
                    break
                time.sleep(0.5)
            else:
                raise AssertionError("fixture unavailable via AT-SPI: " + str(ui.inspect()))
            assert login(ui, mapping, "fixture-user", "fixture-password", lambda: "123456",
                         flag.exists, timeout=15) == "ready"
            print("Real Qt password editing and terminal OTP bridge: PASS (fixture only)")
        finally:
            process.terminate()
            process.wait(timeout=5)

