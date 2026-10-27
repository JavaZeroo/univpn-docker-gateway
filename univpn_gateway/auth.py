import time
from dataclasses import dataclass


class AuthError(RuntimeError):
    pass


@dataclass
class Challenge:
    deadline: float
    used: bool = False

    def consume(self, now):
        if self.used or now >= self.deadline:
            raise AuthError("SMS challenge expired or was already submitted; do not restart login automatically")
        self.used = True


def login(ui, mapping, username, password, read_otp, ready, clock=time.monotonic,
          sleep=time.sleep, timeout=180):
    """One user-initiated login, at most one OTP submission, no automatic SMS resend.

    Selectors are version-specific and must be captured from a real client.
    No guessed coordinates or blind certificate acceptance.
    """
    start = clock()
    if ready():
        return "already_ready"
    for selector in mapping.get("open_login", []):
        ui.press(selector)
        sleep(0.3)
    ui.fill(mapping["username"], username)
    ui.fill(mapping["password"], password)
    ui.press(mapping["submit"])
    challenge = None
    while clock() - start < timeout:
        if ready():
            return "ready"
        if mapping.get("error") and ui.matches(mapping["error"]):
            raise AuthError("client reported an authentication error; automatic retry is disabled")
        if mapping.get("certificate") and ui.matches(mapping["certificate"]):
            raise AuthError("certificate confirmation required; no automatic acceptance is implemented")
        otp = mapping.get("otp")
        if otp and challenge is None and ui.matches(otp["input"]):
            challenge = Challenge(clock() + otp.get("timeout_seconds", 120))
            value = read_otp()
            challenge.consume(clock())
            if not value.isascii() or not value.isdigit() or not 4 <= len(value) <= 10:
                raise AuthError("OTP must contain 4 to 10 ASCII digits")
            # Resolve the exact dialog again; never re-login to submit a code.
            ui.fill(otp["input"], value)
            ui.press(otp["submit"])
            # Allow async widgets to close, but never click the button twice.
            sleep(1)
        sleep(0.5)
    raise AuthError("login/probe timed out; existing client session was left untouched")
