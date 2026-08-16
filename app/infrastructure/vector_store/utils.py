import re


def get_float(v):
    """
    将值转换为浮点数
    Args:
        v: 要转换的值
    Returns:
        float: 转换后的浮点数，转换失败时返回负无穷
    """
    if v is None:
        return float('-inf')
    try:
        return float(v)
    except Exception:
        return float('-inf')

def is_english(texts):
    """
    判断文本是否为英文
    Args:
        texts: 文本，可以是字符串或字符串列表
    Returns:
        bool: 如果80%以上的文本符合英文模式则返回True，否则返回False
    """
    if not texts:
        return False

    pattern = re.compile(r"[`a-zA-Z0-9\s.,':;/\"?<>!\(\)\-]")

    if isinstance(texts, str):
        texts = list(texts)
    elif isinstance(texts, list):
        texts = [t for t in texts if isinstance(t, str) and t.strip()]
    else:
        return False

    if not texts:
        return False

    eng = sum(1 for t in texts if pattern.fullmatch(t.strip()))
    return (eng / len(texts)) > 0.8