import argparse
import getpass
import logging
import pathlib
import sys

from . import auth, config, server

ROOT = pathlib.Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser(prog="webkvm", description="CH9329 + USB 擷取卡 WebKVM")
    ap.add_argument("-c", "--config", type=pathlib.Path, default=ROOT / "config.toml")
    ap.add_argument("--data-dir", type=pathlib.Path, default=ROOT / "data",
                    help="存放密碼、憑證與狀態的目錄")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="啟動伺服器（預設）")
    sub.add_parser("passwd", help="設定登入密碼")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    if args.cmd == "passwd":
        pw = getpass.getpass("新密碼: ")
        if len(pw) < 8:
            sys.exit("密碼至少 8 個字元")
        if pw != getpass.getpass("再輸入一次: "):
            sys.exit("兩次輸入不一致")
        auth.write_password(args.data_dir, pw)
        print("密碼已更新，請重新啟動伺服器。")
        return

    server.run(config.load(args.config), args.data_dir)


if __name__ == "__main__":
    main()
