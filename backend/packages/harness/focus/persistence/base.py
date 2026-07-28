"""
本文件对外提供 Base 声明式基类，所有业务表模型均继承自该类。

对外提供:
    Base — 继承自 sqlalchemy.orm.DeclarativeBase 的声明式基类，扩展了 to_dict() 和 __repr__()

输入: 无 — 本文件为纯定义文件
输出: Base 类

具体工作流:
    to_dict():
    (1) 遍历 self.__table__.columns 获取所有数据库列
    (2) 对每个列，通过 getattr(self, column_name) 获取当前实例的值
    (3) 返回 {列名: 列值} 的 dict

    __repr__():
    (1) 遍历 self.__table__.columns 获取所有数据库列
    (2) 对每个列，格式化为 "col=value" 形式
    (3) 返回 "<ClassName(col1=val1, col2=val2, ...)>" 格式字符串

示例:
    from focus.persistence.base import Base
    from sqlalchemy.orm import Mapped, mapped_column

    class User(Base):
        __tablename__ = "users"
        id: Mapped[int] = mapped_column(primary_key=True)
        name: Mapped[str] = mapped_column()

    user = User(id=1, name="foo")
    user.to_dict()  # → {"id": 1, "name": "foo"}
    repr(user)      # → "<User(id=1, name='foo')>"
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """声明式基类，所有业务表模型 SHALL 继承此类。"""

    def to_dict(self) -> dict:
        """将模型实例转换为普通 dict，仅包含数据库列，不包含 relationship 等非列属性。

        输入: 无（通过 self 读取）
        输出: dict — {列名: 列值}

        工作流:
            (1) 遍历 self.__table__.columns 获取所有 Column 对象
            (2) 对每个 Column，用 getattr(self, column.name) 取值
            (3) 组装为 dict 返回
        """
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}

    def __repr__(self) -> str:
        """返回可读的字符串表示，显示列名和值，不含内存地址。

        输入: 无（通过 self 读取）
        输出: str — "<ClassName(col1=val1, col2=val2, ...)>"
        """
        fields = ", ".join(
            f"{c.name}={getattr(self, c.name)!r}"
            for c in self.__table__.columns
        )
        return f"<{type(self).__name__}({fields})>"
