from butler.tools.validation import validate_arguments

SCHEMA = {
    "type": "object",
    "properties": {
        "device_id": {"type": "string"},
        "value": {"type": "integer", "minimum": 1, "maximum": 100},
        "speed": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["device_id"],
    "additionalProperties": False,
}


def test_valid_arguments_pass():
    assert validate_arguments({"device_id": "light.living_room"}, SCHEMA) == []


def test_missing_required():
    errors = validate_arguments({}, SCHEMA)
    assert any("device_id" in e for e in errors)


def test_wrong_type():
    errors = validate_arguments({"device_id": 123}, SCHEMA)
    assert any("string" in e for e in errors)


def test_bool_is_not_integer():
    errors = validate_arguments({"device_id": "x", "value": True}, SCHEMA)
    assert errors


def test_enum_violation():
    errors = validate_arguments({"device_id": "x", "speed": "turbo"}, SCHEMA)
    assert any("turbo" in e for e in errors)


def test_range_violation():
    errors = validate_arguments({"device_id": "x", "value": 0}, SCHEMA)
    assert any("小于" in e for e in errors)
    errors = validate_arguments({"device_id": "x", "value": 101}, SCHEMA)
    assert any("大于" in e for e in errors)


def test_unknown_property_rejected():
    errors = validate_arguments({"device_id": "x", "hacker": True}, SCHEMA)
    assert any("hacker" in e for e in errors)


def test_non_object_arguments():
    assert validate_arguments([1, 2], SCHEMA)
    assert validate_arguments("{}", SCHEMA)
