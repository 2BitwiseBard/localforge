"""Tests for the workflow engine safe expression evaluator."""

import ast
import pytest

from localforge.workflows.engine import _safe_eval


def _eval(expr: str, ns: dict = None) -> object:
    """Helper to evaluate an expression string."""
    tree = ast.parse(expr, mode="eval")
    return _safe_eval(tree, ns or {})


class TestSafeEval:
    def test_constants(self):
        assert _eval("42") == 42
        assert _eval("'hello'") == "hello"
        assert _eval("True") is True

    def test_comparisons(self):
        assert _eval("1 < 2") is True
        assert _eval("3 >= 3") is True
        assert _eval("'a' == 'b'") is False
        assert _eval("1 != 2") is True

    def test_boolean_ops(self):
        assert _eval("True and False") is False
        assert _eval("True or False") is True

    def test_variables(self):
        ns = {"x": 10, "name": "test"}
        assert _eval("x > 5", ns) is True
        assert _eval("name == 'test'", ns) is True

    def test_arithmetic(self):
        assert _eval("3 + 4") == 7
        assert _eval("10 - 3") == 7
        assert _eval("2 * 5") == 10

    def test_subscript(self):
        ns = {"data": {"key": "value"}, "items": [1, 2, 3]}
        assert _eval("data['key']", ns) == "value"
        assert _eval("items[0]", ns) == 1

    def test_safe_functions(self):
        ns = {"items": [1, 2, 3]}
        assert _eval("len(items)", ns) == 3
        assert _eval("max(1, 2, 3)") == 3
        assert _eval("str(42)") == "42"

    def test_attribute_on_dict(self):
        ns = {"outputs": {"node1": "result"}}
        assert _eval("outputs['node1']", ns) == "result"

    def test_ternary(self):
        ns = {"x": 5}
        assert _eval("'yes' if x > 3 else 'no'", ns) == "yes"
        assert _eval("'yes' if x > 10 else 'no'", ns) == "no"

    def test_rejects_function_calls(self):
        with pytest.raises(ValueError, match="Function call not allowed"):
            _eval("exec('import os')")

    def test_rejects_import(self):
        with pytest.raises(ValueError, match="Function call not allowed"):
            tree = ast.parse("__import__('os')", mode="eval")
            _safe_eval(tree, {})

    def test_rejects_lambda(self):
        with pytest.raises(ValueError, match="Function call not allowed"):
            _eval("(lambda: 1)()")

    def test_undefined_variable(self):
        with pytest.raises(NameError, match="Undefined"):
            _eval("undefined_var")

    def test_chained_comparison(self):
        assert _eval("1 < 2 < 3") is True
        assert _eval("1 < 2 > 3") is False

    def test_in_operator(self):
        ns = {"items": [1, 2, 3]}
        assert _eval("2 in items", ns) is True
        assert _eval("5 not in items", ns) is True

    def test_unary_not(self):
        assert _eval("not True") is False
        assert _eval("not False") is True

    def test_list_literal(self):
        result = _eval("[1, 2, 3]")
        assert result == [1, 2, 3]
