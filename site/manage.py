# -*- coding: utf-8 -*-
"""
Аварийная консоль управления аккаунтами (на случай «забыл пароль / нет почты»).

Работает даже при запущенном сервере (SQLite WAL).

  python manage.py users                              — список пользователей
  python manage.py set-password <email> <новый_пароль> — задать пароль напрямую
  python manage.py make-admin <email>                 — выдать права админа
  python manage.py reset-link <email>                 — напечатать ссылку сброса
  python manage.py create-admin <email> <пароль>      — создать сразу админа

  python manage.py backup [reason]                    — сделать бэкап БД сейчас
  python manage.py backups                            — список всех бэкапов
  python manage.py restore <имя_файла>                — восстановить БД из бэкапа
"""
import sys
import os
from datetime import datetime

# UTF-8 на Windows-консоли
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from accounts import init_db, service, db, backup as backup_mod
from accounts.security import hash_password, normalize_email, is_valid_email


def _print_users():
    rows = db.query_all(
        "SELECT id, email, balance_cards, is_admin, created_at FROM users ORDER BY id")
    if not rows:
        print("Пользователей нет.")
        print("→ Просто зарегистрируйтесь на сайте с email из config.admin_emails —")
        print("  и вы сразу станете админом (или: python manage.py create-admin <email> <пароль>).")
        return
    print(f"{'ID':>4}  {'EMAIL':<34} {'КАРТОЧКИ':>9}  РОЛЬ")
    for r in rows:
        role = "ADMIN" if r["is_admin"] else "user"
        print(f"{r['id']:>4}  {r['email']:<34} {r['balance_cards']:>9}  {role}")


def _fmt_size(n: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "help"

    # backup/restore commands не должны падать на init_db (наоборот, restore
    # хочет выполниться ДО любого подключения к БД, чтобы заменить файл целиком).
    if cmd == "restore" and len(args) >= 2:
        try:
            path = backup_mod.restore(args[1])
            print(f"✓ БД восстановлена из бэкапа в {path}")
        except FileNotFoundError as e:
            print(f"✗ {e}")
        return

    init_db()

    if cmd == "users":
        _print_users()

    elif cmd == "backup":
        reason = args[1] if len(args) >= 2 else "manual"
        paths = backup_mod.backup_now(reason)
        if not paths:
            print("✗ Бэкап не сделан (БД ещё не создана?).")
            return
        for p in paths:
            print(f"✓ {p}")

    elif cmd == "backups":
        rows = backup_mod.list_backups()
        if not rows:
            print("Бэкапов пока нет. Сделайте: python manage.py backup")
            return
        print(f"{'ИМЯ':<46} {'РАЗМЕР':>10}  {'КОГДА':<20}  КОПИЙ")
        for r in rows:
            when = datetime.fromtimestamp(r["mtime"]).strftime("%Y-%m-%d %H:%M:%S")
            print(f"{r['name']:<46} {_fmt_size(r['size']):>10}  {when:<20}  {len(r['paths'])}")

    elif cmd == "set-password" and len(args) >= 3:
        email = normalize_email(args[1]); pw = args[2]
        row = db.query_one("SELECT id FROM users WHERE email=?", (email,))
        if not row:
            print(f"Нет пользователя {email}. Создайте: python manage.py create-admin {email} <пароль>")
            return
        with db.tx() as cur:
            cur.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(pw), row["id"]))
            cur.execute("DELETE FROM sessions WHERE user_id=?", (row["id"],))  # сбросить старые сессии
        print(f"✓ Пароль для {email} обновлён. Войдите с новым паролем.")

    elif cmd == "make-admin" and len(args) >= 2:
        email = normalize_email(args[1])
        row = db.query_one("SELECT id FROM users WHERE email=?", (email,))
        if not row:
            print(f"Нет пользователя {email}.")
            return
        service.set_admin_flag(row["id"], True)
        print(f"✓ {email} теперь администратор.")

    elif cmd == "reset-link" and len(args) >= 2:
        out = service.request_password_reset(args[1], None, service.site_base_url() or "http://127.0.0.1:8000")
        print("Ссылка сброса пароля:", out.get("reset_link") or "(пользователь не найден)")

    elif cmd == "create-admin" and len(args) >= 3:
        email = normalize_email(args[1]); pw = args[2]
        if not is_valid_email(email):
            print("Некорректный email"); return
        if len(pw) < 6:
            print("Пароль минимум 6 символов"); return
        existing = db.query_one("SELECT id FROM users WHERE email=?", (email,))
        if existing:
            with db.tx() as cur:
                cur.execute("UPDATE users SET password_hash=?, is_admin=1 WHERE id=?",
                            (hash_password(pw), existing["id"]))
                cur.execute("DELETE FROM sessions WHERE user_id=?", (existing["id"],))
            print(f"✓ {email} — пароль обновлён и выданы права админа.")
        else:
            out = service.register(email, pw, None, True, True, None, None)
            service.set_admin_flag(out["user"]["id"], True)
            print(f"✓ Создан админ {email}. Войдите этим паролем и откройте /admin.")

    else:
        print(__doc__)


if __name__ == "__main__":
    main()
