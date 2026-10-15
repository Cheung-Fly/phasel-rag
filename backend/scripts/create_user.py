"""Create a personal-assistant login without exposing passwords to shell history."""
import getpass
import re
import sys

from app.auth_store import close_auth_store, get_auth_store, password_is_valid


def main() -> int:
    username = input("新建个人助手用户名（3-64 位字母/数字/_.-）：").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", username):
        print("用户名格式不正确。", file=sys.stderr)
        return 2

    password = getpass.getpass("设置本项目登录密码（至少 12 位）：")
    confirmation = getpass.getpass("再次输入本项目登录密码：")
    if password != confirmation:
        print("两次输入的密码不一致。", file=sys.stderr)
        return 2
    if not password_is_valid(password):
        print("项目登录密码长度必须为 12 至 1024 个字符。", file=sys.stderr)
        return 2

    try:
        get_auth_store().create_user(username, password)
    except Exception as exc:
        print(f"创建用户失败：{type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        close_auth_store()
    print("个人助手用户已创建。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
