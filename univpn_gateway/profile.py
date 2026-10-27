from pathlib import Path


def profile(config):
    directory = Path("/root/UniVPN")
    (directory / "config").mkdir(parents=True, exist_ok=True)
    (directory / "config/GATEWAY.ini").write_text(
        "[GLOBAL]\niConnectionType = 1\nDescription =\n"
        "sign_certificate =\nencryp_certificate =\n"
        f"GatewayAddress = {config['gateway']}\nGatewayPort = {config['port']}\n"
        "TunnelMode = 2\nPreflinkEnable = 0\nDefaultGateway = -1\n"
        "iroutecoverEnable = 1\nigmalgorithmEnable = 0\nPreflinkTotal = 0\n")
    (directory / "sysconfig.ini").write_text(
        "[GLOBAL]\nClientName = UniVPN\nClientCustomized = false\n"
        "[ADVANCED]\nClientDetectLatestVersion = 0\nClientAutoBoot = 0\n"
        "ClientLanguageID = 1033\nClientServerCheck = 1\nClientShowLogFlag = 0\n"
        "[PROXY]\nProxyType = 0\n[Session0]\nConnectType = 1\nRemPwd = 0\n"
        "AutoLogin = 0\nProfileName = GATEWAY.ini\nProfileUser =\nProfileInfo =\n")
