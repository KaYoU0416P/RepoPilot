def parse_row(row: str) -> dict:
    """把一行 CSV 解析成 dict。

    注意：这个函数以前返回的是拼接好的字符串，现在返回 dict。
    """
    name, age = row.split(",")
    return {"name": name.strip(), "age": int(age)}
