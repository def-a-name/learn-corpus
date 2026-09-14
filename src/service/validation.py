"""保存 transport 配置共用的基础值校验。"""


def positive_integer(value: object) -> bool:
    return type(value) is int and value > 0
