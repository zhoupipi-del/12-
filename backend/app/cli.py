"""运维命令行：创建管理员、重置密码。

  python -m app.cli create-admin --username boss          （交互输入密码）
  python -m app.cli reset-password --username boss
"""
from __future__ import annotations

import argparse
import getpass
import sys

from sqlalchemy import select

from .core.security import hash_password
from .db import SessionLocal
from .models import Role, User

WEAK = {"admin123", "123456", "password", "12345678", "admin", "qwerty123"}


def _password(args) -> str:
    pw = args.password or getpass.getpass("新密码（至少 10 位，含字母和数字）: ")
    if not args.password and getpass.getpass("再输入一次: ") != pw:
        sys.exit("两次输入不一致")
    if len(pw) < 10 or pw.lower() in WEAK or pw.isalpha() or pw.isdigit():
        sys.exit("密码太弱：至少 10 位，且同时包含字母和数字，不能是常见弱口令")
    return pw


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="app.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("create-admin", "reset-password"):
        sp = sub.add_parser(name)
        sp.add_argument("--username", required=True)
        sp.add_argument("--password", help="不建议在命令行里直接写密码，留空则交互输入")
        if name == "create-admin":
            sp.add_argument("--name", default="系统管理员")
    args = ap.parse_args(argv)
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.username == args.username))
        if args.cmd == "create-admin":
            if u:
                sys.exit("用户已存在；如需改密请用 reset-password")
            db.add(User(username=args.username, name=args.name, password_hash=hash_password(_password(args)),
                        roles=Role.ADMIN))
            print("管理员已创建")
        else:
            if not u:
                sys.exit("用户不存在")
            u.password_hash = hash_password(_password(args))
            u.is_active = True
            print("密码已重置")
        db.commit()


if __name__ == "__main__":
    main()
