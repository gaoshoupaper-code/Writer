#!/usr/bin/env python3
"""Writer 管理 CLI（D4/S21）。

替代砍掉的 admin 后台。在服务器上直接跑（SSH 登录后），操作 executor 的元数据库。

用法示例：
    python scripts/manage.py create-invite --count 5
    python scripts/manage.py create-invite --admin          # 管理员邀请码
    python scripts/manage.py list-users
    python scripts/manage.py list-users --search zhang
    python scripts/manage.py disable-user <user_id>
    python scripts/manage.py enable-user <user_id>
    python scripts/manage.py reset-password <user_id> <new_password>
    python scripts/manage.py set-quota <user_id> <new_quota>
    python scripts/manage.py list-invites                    # 查看邀请码使用情况
    python scripts/manage.py revoke-invite <code>

约束：
    - 必须在 executor 容器内、WORKDIR=/app/executor 下运行（读 executor/.env 的 DB 路径）。
    - 镜像里没 COPY scripts/，需先 docker cp 进容器：
        docker cp scripts/manage.py writer-executor:/app/manage.py
        docker exec -w /app/executor writer-executor python /app/manage.py <command>
    - create-invite 的 created_by 自动取数据库中第一个启用管理员的真实 user_id
      （invite_codes.created_by 有外键约束，不能用 "cli" 这种虚拟标记）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _resolve_db():
    """初始化并返回 database + repositories。

    复用 executor 现有的 Database / Repository（不重复实现业务逻辑）。
    假设 cwd 是 executor/（含 .env 和 app/ 包）。
    """
    # 确保能 import app.*
    executor_root = Path.cwd()
    if not (executor_root / "app" / "platform" / "core" / "db").exists():
        print(
            "ERROR: 必须在 executor/ 目录下运行此脚本（找不到 app/platform/core/db）。\n"
            "用法：cd executor && python ../scripts/manage.py <command>",
            file=sys.stderr,
        )
        sys.exit(2)

    if str(executor_root) not in sys.path:
        sys.path.insert(0, str(executor_root))

    from app.platform.core.db import (  # type: ignore
        Database,
        InviteCodeRepository,
        UserRepository,
        WorkspaceRepository,
    )
    from app.platform.core.settings import get_settings  # type: ignore
    from app.platform.core.security import load_master_key  # type: ignore

    settings = get_settings()
    master_key = load_master_key(settings.master_key)
    db = Database(settings.db_path, master_key)
    return db, InviteCodeRepository(db), UserRepository(db), WorkspaceRepository(db)


def _resolve_creator(db) -> str:
    """查一个真实 user_id 作为 invite_codes.created_by。

    invite_codes.created_by 有外键约束 REFERENCES users(user_id)，
    不能用 "cli" 这种虚拟标记（会触发 FOREIGN KEY constraint failed）。
    优先取第一个启用中的管理员；无管理员则取第一个用户。
    """
    row = db.conn.execute(
        "SELECT user_id FROM users WHERE is_admin = 1 AND disabled = 0 "
        "ORDER BY created_at LIMIT 1"
    ).fetchone()
    if row is None:
        row = db.conn.execute(
            "SELECT user_id FROM users ORDER BY created_at LIMIT 1"
        ).fetchone()
    if row is None:
        print(
            "ERROR: 数据库中没有任何用户。"
            "至少需要 bootstrap 创建的管理员账号才能发码。",
            file=sys.stderr,
        )
        sys.exit(2)
    return row["user_id"]


def cmd_create_invite(args: argparse.Namespace) -> None:
    db, invites, *_ = _resolve_db()
    creator = _resolve_creator(db)
    codes = invites.create(
        created_by=creator,
        count=args.count,
        is_admin_code=args.admin,
    )
    label = "管理员邀请码" if args.admin else "普通邀请码"
    print(f"已生成 {len(codes)} 个{label}：")
    for c in codes:
        print(f"  {c}")


def cmd_list_invites(args: argparse.Namespace) -> None:
    db, *_ = _resolve_db()
    rows = db.conn.execute(
        "SELECT code, is_admin_code, created_by, created_at, used_by, revoked_at "
        "FROM invite_codes ORDER BY created_at DESC LIMIT ?",
        (args.limit,),
    ).fetchall()
    if not rows:
        print("（无邀请码）")
        return
    print(f"{'CODE':<24} {'类型':<6} {'状态':<8} {'创建者':<12} {'创建时间'}")
    print("-" * 80)
    for r in rows:
        kind = "管理员" if r["is_admin_code"] else "普通"
        if r["revoked_at"]:
            status = "已吊销"
        elif r["used_by"]:
            status = "已使用"
        else:
            status = "可用"
        print(
            f"{r['code']:<24} {kind:<6} {status:<8} {r['created_by']:<12} {r['created_at']}"
        )


def cmd_revoke_invite(args: argparse.Namespace) -> None:
    db, *_ = _resolve_db()
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE invite_codes SET revoked_at = ? WHERE code = ? AND revoked_at IS NULL",
            (_now(), args.code),
        )
    if cur.rowcount == 0:
        print(f"邀请码 {args.code} 不存在或已吊销。")
    else:
        print(f"已吊销邀请码：{args.code}")


def cmd_list_users(args: argparse.Namespace) -> None:
    db, *_ = _resolve_db()
    if args.search:
        rows = db.conn.execute(
            "SELECT user_id, username, is_admin, disabled, workspace_quota, created_at "
            "FROM users WHERE username LIKE ? ORDER BY created_at DESC",
            (f"%{args.search}%",),
        ).fetchall()
    else:
        rows = db.conn.execute(
            "SELECT user_id, username, is_admin, disabled, workspace_quota, created_at "
            "FROM users ORDER BY created_at DESC LIMIT ?",
            (args.limit,),
        ).fetchall()
    if not rows:
        print("（无用户）")
        return
    print(f"{'USER_ID':<36} {'用户名':<16} {'角色':<6} {'状态':<6} {'配额':<6} {'创建时间'}")
    print("-" * 100)
    for r in rows:
        role = "管理员" if r["is_admin"] else "普通"
        status = "禁用" if r["disabled"] else "正常"
        print(
            f"{r['user_id']:<36} {r['username']:<16} {role:<6} {status:<6} "
            f"{r['workspace_quota']:<6} {r['created_at']}"
        )


def cmd_disable_user(args: argparse.Namespace) -> None:
    _, _, users, _ = _resolve_db()
    users.set_disabled(args.user_id, True)
    print(f"已禁用用户：{args.user_id}")


def cmd_enable_user(args: argparse.Namespace) -> None:
    _, _, users, _ = _resolve_db()
    users.set_disabled(args.user_id, False)
    print(f"已启用用户：{args.user_id}")


def cmd_reset_password(args: argparse.Namespace) -> None:
    _, _, users, _ = _resolve_db()
    users.set_password(args.user_id, args.new_password)
    print(f"已重置用户密码：{args.user_id}")


def cmd_set_quota(args: argparse.Namespace) -> None:
    db, *_ = _resolve_db()
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE users SET workspace_quota = ? WHERE user_id = ?",
            (args.quota, args.user_id),
        )
    if cur.rowcount == 0:
        print(f"用户不存在：{args.user_id}")
    else:
        print(f"已设置用户 {args.user_id} 的配额为 {args.quota}")


def _now() -> str:
    from datetime import datetime, UTC

    return datetime.now(UTC).isoformat()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="manage.py",
        description="Writer 管理 CLI（替代 admin 后台，D4/S21）。须在 executor/ 目录下运行。",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # create-invite
    p_ci = sub.add_parser("create-invite", help="生成邀请码")
    p_ci.add_argument("--count", type=int, default=1, help="生成数量（默认 1）")
    p_ci.add_argument("--admin", action="store_true", help="生成管理员邀请码")
    p_ci.set_defaults(func=cmd_create_invite)

    # list-invites
    p_li = sub.add_parser("list-invites", help="列出邀请码及使用状态")
    p_li.add_argument("--limit", type=int, default=50, help="显示条数（默认 50）")
    p_li.set_defaults(func=cmd_list_invites)

    # revoke-invite
    p_ri = sub.add_parser("revoke-invite", help="吊销邀请码")
    p_ri.add_argument("code", help="邀请码")
    p_ri.set_defaults(func=cmd_revoke_invite)

    # list-users
    p_lu = sub.add_parser("list-users", help="列出用户")
    p_lu.add_argument("--search", help="按用户名模糊搜索")
    p_lu.add_argument("--limit", type=int, default=50, help="显示条数（默认 50）")
    p_lu.set_defaults(func=cmd_list_users)

    # disable-user
    p_du = sub.add_parser("disable-user", help="禁用用户")
    p_du.add_argument("user_id", help="用户 ID")
    p_du.set_defaults(func=cmd_disable_user)

    # enable-user
    p_eu = sub.add_parser("enable-user", help="启用用户")
    p_eu.add_argument("user_id", help="用户 ID")
    p_eu.set_defaults(func=cmd_enable_user)

    # reset-password
    p_rp = sub.add_parser("reset-password", help="重置用户密码")
    p_rp.add_argument("user_id", help="用户 ID")
    p_rp.add_argument("new_password", help="新密码")
    p_rp.set_defaults(func=cmd_reset_password)

    # set-quota
    p_sq = sub.add_parser("set-quota", help="调整用户作品配额")
    p_sq.add_argument("user_id", help="用户 ID")
    p_sq.add_argument("quota", type=int, help="新配额数值")
    p_sq.set_defaults(func=cmd_set_quota)

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
