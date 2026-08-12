"""No repository function can read or write a row without naming a student.

`test_store.py` proves the invariant behaviourally (a second student's rows never
come back, a row can't reference another student's parent row). This file proves it
structurally, so the guarantee does not depend on every function being written
carefully by hand:

- A `get_*`/`list_*` function must take `student_id` as an explicit parameter with no
  default, so it cannot be called without naming a student.
- An `insert_*`/`upsert_*` function takes a `Row` dataclass instead; that dataclass
  must itself declare `student_id` as a field with no default, so the row cannot be
  constructed without one either.

Either way, there is no path through the public API that reads or writes a table
without a student_id supplied by the caller — with one deliberate, named exception:
`students.list_students` enumerates every student, because that is the one screen
(the M2.2 student picker) where listing across students is the point, not a leak.
The exception is asserted here rather than silently excluded, so it stays visible.
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
from collections.abc import Iterator

REPOSITORY_MODULES = [
    "k12ta.store.students",
    "k12ta.store.content",
    "k12ta.store.captures",
    "k12ta.store.sessions",
    "k12ta.store.mastery",
    "k12ta.store.schedule",
]

ROOT_LISTING_EXCEPTIONS = {"k12ta.store.students.list_students"}


def _repository_functions() -> Iterator[tuple[str, object]]:
    for module_name in REPOSITORY_MODULES:
        module = importlib.import_module(module_name)
        for name, obj in vars(module).items():
            if name.startswith("_") or not inspect.isfunction(obj):
                continue
            if obj.__module__ != module_name:
                continue
            if f"{module_name}.{name}" in ROOT_LISTING_EXCEPTIONS:
                continue
            yield f"{module_name}.{name}", obj


def _requires_student_id_directly(func: object) -> bool:
    params = inspect.signature(func).parameters
    if "student_id" not in params:
        return False
    return params["student_id"].default is inspect.Parameter.empty


def _requires_student_id_via_row(func: object) -> bool:
    if "row" not in inspect.signature(func).parameters:
        return False
    # Modules use `from __future__ import annotations`, so annotations are strings;
    # eval_str resolves them back to the real dataclass using the function's own
    # module globals.
    row_type = inspect.get_annotations(func, eval_str=True).get("row")
    if row_type is None or not dataclasses.is_dataclass(row_type):
        return False
    row_fields = {f.name: f for f in dataclasses.fields(row_type)}
    student_id_field = row_fields.get("student_id")
    if student_id_field is None:
        return False
    return student_id_field.default is dataclasses.MISSING


def test_every_repository_function_requires_conn_first() -> None:
    functions = list(_repository_functions())
    assert len(functions) >= 8, "expected repository functions across all five modules"
    for qualified_name, func in functions:
        first_param = next(iter(inspect.signature(func).parameters.values()))
        assert first_param.name == "conn", f"{qualified_name} must take conn as its first parameter"


def test_every_repository_function_requires_a_student_id_somewhere_mandatory() -> None:
    functions = list(_repository_functions())
    assert len(functions) >= 8, "expected repository functions across all five modules"
    for qualified_name, func in functions:
        scoped = _requires_student_id_directly(func) or _requires_student_id_via_row(func)
        assert scoped, (
            f"{qualified_name} has no mandatory student_id, directly or via its row "
            "argument's dataclass fields"
        )


def test_the_root_listing_exception_is_exactly_list_students() -> None:
    """The one function excused from the invariant above is named, not swallowed."""
    assert {"k12ta.store.students.list_students"} == ROOT_LISTING_EXCEPTIONS
    module = importlib.import_module("k12ta.store.students")
    assert inspect.isfunction(module.list_students)
    first_param = next(iter(inspect.signature(module.list_students).parameters.values()))
    assert first_param.name == "conn"
