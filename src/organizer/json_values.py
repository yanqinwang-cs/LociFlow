def is_json_integer(value: object) -> bool:
    # bool is a subclass of int, so JSON true/false would pass a bare isinstance.
    return isinstance(value, int) and not isinstance(value, bool)


def is_json_version(value: object, expected: int) -> bool:
    return is_json_integer(value) and value == expected
