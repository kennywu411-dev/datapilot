"""行列级权限（v1.4，ADR 005）：按角色限制可见的列和行，在 SQL 执行前由代码强制，不靠提示词。

两层，叠加在 sql_guard 的三道只读防线之上：
1. 语法树改写（主防线）：sqlglot 解析后，把每一处对受限表的引用，别名、子查询、JOIN、
   UNION、`main.表名`、自己写的 CTE 里，替换成"只含可见列、带行过滤条件"的子查询；
   显式引用无权访问的列直接拒绝并给出清楚的报错；元数据表、其他 schema、无法解析的 SQL
   一律拒绝（无法改写就无法保证）。
2. 授权回调（兜底）：SQLite 每读一列都会询问，受限列直接 DENY。即使改写有漏洞，
   敏感列也读不出来。行过滤无法在这一层做，只能靠第 1 层。

不选同名 CTE 遮蔽：`main.orders` 会绕过同名 CTE 直接读原表，用户自己写的 WITH 也要合并。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError


class AccessDenied(Exception):
    """违反行列级权限。sql_guard 会转成 SQLRejected，消息原样给模型和用户。"""


class Unparseable(AccessDenied):
    """受限角色下 SQL 无法解析（无法改写，所以拒绝）。和"无权访问"分开，评测判分要区分两者。"""


@dataclass(frozen=True)
class Policy:
    label: str
    hidden: dict[str, set[str]] = field(default_factory=dict)  # 表 → 不可见列
    rows: dict[str, str] = field(default_factory=dict)  # 表 → 行过滤条件（写在该表的列上）
    note: str = ""  # 行权限的自然语言说明，告诉模型和用户"你只能看到哪些行"

    @property
    def unrestricted(self) -> bool:
        return not self.hidden and not self.rows

    def hidden_in(self, table: str) -> set[str]:
        return {c.lower() for c in self.hidden.get(table.lower(), ())}

    def __hash__(self) -> int:  # 作为缓存 key：按内容哈希
        return hash(
            (
                self.label,
                tuple(sorted((k, tuple(sorted(v))) for k, v in self.hidden.items())),
                tuple(sorted(self.rows.items())),
                self.note,
            )
        )

    def prompt_note(self) -> str:
        """给模型的权限说明。只是让模型能如实告知用户；真正的限制由改写和授权回调强制。"""
        cols = ", ".join(f"{t}.{c}" for t, cs in sorted(self.hidden.items()) for c in sorted(cs))
        parts = [f"Current user role: {self.label}."]
        if cols:
            parts.append(f"Columns this role cannot access: {cols}.")
        if self.note:
            parts.append(f"Row-level filter (applied automatically): {self.note}.")
        parts.append(
            "If answering requires inaccessible columns, do NOT substitute other columns or guess; "
            "return exactly: SELECT 'NO_PERMISSION: <column>' AS message"
        )
        return " ".join(parts)


_SENSITIVE = {"products": {"cost"}, "users": {"name"}}
_SHANGHAI = "(SELECT user_id FROM users WHERE city = '上海')"

# 各库的角色。没有列出的库不能指定角色（指定了就报错，不默默放行）。
ROLES: dict[str, dict[str, Policy]] = {
    "shop_ops": {
        "analyst": Policy("分析师"),
        "ops": Policy("运营", hidden=_SENSITIVE),
        "sh_manager": Policy(
            "上海区域经理",
            hidden=_SENSITIVE,
            note="only users in 上海 and their orders and tickets are visible",
            rows={
                "users": "city = '上海'",
                "orders": f"user_id IN {_SHANGHAI}",
                "tickets": "order_id IN (SELECT order_id FROM orders "
                f"WHERE user_id IN {_SHANGHAI})",
            },
        ),
    },
}

# 是否把权限说明告诉模型（只影响模型能否如实告知；强制靠改写和授权回调，与此无关）。
# 评测消融时关掉，用来单独测代码层的拦截效果。
PROMPT_NOTE = True

META_TABLES = {"sqlite_master", "sqlite_schema", "sqlite_temp_master", "sqlite_temp_schema"}


def roles_of(db_id: str) -> dict[str, str]:
    return {k: v.label for k, v in ROLES.get(db_id, {}).items()}


def policy_for(db_id: str, role: str | None) -> Policy | None:
    """None 表示不受限（SQL 原样执行）。

    未知角色、或给没定义角色的库指定角色：报错（fail closed），不默默放行。"""
    if role is None:
        return None
    roles = ROLES.get(db_id)
    if not roles or role not in roles:
        raise ValueError(f"库 {db_id} 没有角色 {role}，可选：{sorted(roles or {})}")
    p = roles[role]
    return None if p.unrestricted else p


def source_sql(table: str, columns: list[str], policy: Policy | None) -> str:
    """表在该角色下的"视图"：只含可见列、带行过滤。不受限时就是表名本身。"""
    q = '"' + table.replace('"', '""') + '"'
    if policy is None or (table.lower() not in policy.hidden and table.lower() not in policy.rows):
        return q
    hidden = policy.hidden_in(table)
    cols = ", ".join('"' + c.replace('"', '""') + '"' for c in columns if c.lower() not in hidden)
    where = policy.rows.get(table.lower())
    return f"(SELECT {cols} FROM main.{q}" + (f" WHERE {_qualify(where)})" if where else ")")


def _qualify(predicate: str) -> str:
    """把过滤条件里的表都写成 main.表名：插进用户的查询后，未限定的 users 会被用户自己
    定义的同名 CTE 遮蔽（代码审查实测能绕过行过滤），带 main. 的则不会。"""
    tree = sqlglot.parse_one(f"SELECT 1 WHERE {predicate}", read="sqlite")
    for t in tree.find_all(exp.Table):
        if t.args.get("db") is None:
            t.set("db", exp.to_identifier("main"))
    return tree.args["where"].this.sql(dialect="sqlite")


def _scope_tables(node: exp.Expression) -> list[exp.Table]:
    scope = node.find_ancestor(exp.Select)
    if scope is None:
        return []
    return [t for t in scope.find_all(exp.Table) if t.find_ancestor(exp.Select) is scope]


def _check_hidden_columns(
    tree: exp.Expression,
    policy: Policy,
    real: dict[str, str],
    columns: dict[str, list[str]],
    ctes: set[str],
) -> None:
    """显式引用无权访问的列：给出清楚的报错。

    否则只会得到 no such column，模型会当成 SQL 写错去乱改，而不是告诉用户无权限。"""
    for col in tree.find_all(exp.Column):
        name = col.name.lower()
        tables = [
            t
            for t in _scope_tables(col)
            if not (t.args.get("db") is None and t.name.lower() in ctes)
        ]
        by_alias = {(t.alias or t.name).lower(): real.get(t.name.lower()) for t in tables}
        if col.table:
            hit = by_alias.get(col.table.lower())
            if hit and name in policy.hidden_in(hit):
                raise AccessDenied(f"当前角色（{policy.label}）无权访问 {hit}.{col.name}")
            continue
        owners = [real.get(t.name.lower()) for t in tables]
        owners = [o for o in owners if o]
        hidden_owner = [o for o in owners if name in policy.hidden_in(o)]
        visible_elsewhere = any(
            name in {c.lower() for c in columns.get(o, [])} and name not in policy.hidden_in(o)
            for o in owners
        )
        if hidden_owner and not visible_elsewhere:
            raise AccessDenied(f"当前角色（{policy.label}）无权访问 {hidden_owner[0]}.{col.name}")


def rewrite(sql: str, policy: Policy, columns: dict[str, list[str]]) -> str:
    """把受限表的每一处引用替换成按角色过滤后的子查询。columns：真实表名 → 列名列表。"""
    try:
        trees = [t for t in sqlglot.parse(sql, read="sqlite") if t is not None]
    except SqlglotError as e:
        raise Unparseable(
            f"当前角色（{policy.label}）下 SQL 必须能被解析才能做权限检查：{e}"
        ) from e
    if len(trees) != 1:
        raise AccessDenied("只允许单条语句")
    tree = trees[0]
    real = {t.lower(): t for t in columns}
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    # CTE 有作用域：在某个子查询里定义的同名 CTE 会让别处对真实表的引用被误当成 CTE 而跳过改写
    # （代码审查实测能绕过行过滤）。受限角色下直接禁止 CTE 与真实表同名。
    clash = sorted(ctes & set(real))
    if clash:
        raise AccessDenied(f"当前角色（{policy.label}）下 CTE 不能与数据表同名：{', '.join(clash)}")

    for node in tree.walk():
        name = getattr(node, "name", "") or ""
        if isinstance(node, (exp.Table, exp.Anonymous, exp.Func)) and (
            name.lower() in META_TABLES or name.lower().startswith("pragma_")
        ):
            raise AccessDenied(f"当前角色（{policy.label}）不能查询元数据（{name}）")

    _check_hidden_columns(tree, policy, real, columns, ctes)

    for t in list(tree.find_all(exp.Table)):
        db = t.args.get("db")
        if db is not None and db.name.lower() != "main":
            raise AccessDenied(f"只能查询 main 库，收到 {db.name}.{t.name}")
        if db is None and t.name.lower() in ctes:
            continue  # 引用的是查询自己定义的 CTE；CTE 里读真实表的地方会被单独改写
        actual = real.get(t.name.lower())
        if actual is None:
            continue  # 不存在的表交给 SQLite 报错
        src = source_sql(actual, columns[actual], policy)
        if not src.startswith("("):
            continue
        sub = sqlglot.parse_one(src[1:-1], read="sqlite")  # 去掉外层括号
        alias = t.alias or actual
        t.replace(exp.Subquery(this=sub, alias=exp.TableAlias(this=exp.to_identifier(alias))))
    return tree.sql(dialect="sqlite")


def make_authorizer(policy: Policy, base):
    """在 sql_guard 原有授权回调之上，拒绝读受限列和元数据表。"""
    import sqlite3

    hidden = {t.lower(): policy.hidden_in(t) for t in policy.hidden}  # 编译时每读一列都会调用

    def auth(action, arg1, arg2, db_name, source):
        if action == sqlite3.SQLITE_READ and arg1:
            if arg1.lower() in META_TABLES:
                return sqlite3.SQLITE_DENY
            if arg2 and arg2.lower() in hidden.get(arg1.lower(), ()):
                return sqlite3.SQLITE_DENY
        return base(action, arg1, arg2, db_name, source)

    return auth
