"""A bounded interpreter for a small Python subset, never Python exec/eval.

This deliberately limited reference-demo runtime has no filesystem, network,
process, reflection, arbitrary import or bytecode interface. It is not a VM or
a general-purpose production sandbox. Unsupported model output is rejected.
"""
from __future__ import annotations

import ast
import copy
import csv
import io
import math
import operator
import re
import time
from decimal import Decimal, InvalidOperation, localcontext

MAX_SOURCE = 32 * 1024
MAX_ITEMS = 2000
MAX_STRING = 64 * 1024


class RestrictedError(ValueError):
    pass


class _Return(BaseException):
    def __init__(self, value):
        self.value = value


class _Break(BaseException):
    pass


class _Continue(BaseException):
    pass


class _Call:
    def __init__(self, function):
        self.function = function


class _Module:
    def __init__(self, values):
        self.values = values


class _CSVRows(list):
    def __init__(self, text):
        if not isinstance(text, str) or len(text) > MAX_STRING:
            raise ValueError("CSV input too large")
        reader = csv.DictReader(io.StringIO(text))
        self.fieldnames = reader.fieldnames
        if self.fieldnames is not None and len(self.fieldnames) > 64:
            raise RestrictedError("CSV column limit exceeded")
        rows = []
        cells = 0
        for row in reader:
            if len(rows) >= MAX_ITEMS:
                raise ValueError("Too many CSV rows")
            cells += len(row) + len(row.get(None) or [])
            if cells > 8192:
                raise RestrictedError("CSV cell limit exceeded")
            rows.append(row)
        super().__init__(rows)


def _decimal(value):
    if not isinstance(value, (str, int)) or isinstance(value, bool) or len(str(value)) > 64:
        raise ValueError("Unsupported decimal")
    d = Decimal(value)
    if not d.is_finite() or len(d.as_tuple().digits) > 24 or abs(d.as_tuple().exponent) > 12:
        raise ValueError("Decimal outside bounded reference runtime")
    return d


def _range(*args):
    r = range(*args)
    if len(r) > MAX_ITEMS:
        raise RestrictedError("Range limit exceeded")
    return r


def _isinstance(value, types):
    valid = (int, str, bool, float, list, dict, tuple)
    choices = types if isinstance(types, tuple) else (types,)
    if any(t not in valid for t in choices):
        raise RestrictedError("Unsupported type test")
    return isinstance(value, types)


def _type(value):
    """Plain data types only, so a module can write type(x) is int."""
    if type(value) not in (int, str, bool, float, list, dict, tuple, type(None)):
        raise RestrictedError("type() is limited to plain data")
    return type(value)


class RestrictedModule:
    def __init__(self, source: str, function: str, max_steps=50000, timeout=1.0):
        if not isinstance(source, str) or len(source.encode()) > MAX_SOURCE:
            raise RestrictedError("Module source exceeds limit")
        try:
            self.tree = ast.parse(source)
        except (SyntaxError, RecursionError) as exc:
            raise RestrictedError("Invalid module syntax") from exc
        nodes = list(ast.walk(self.tree))
        if len(nodes) > 2500:
            raise RestrictedError("Module syntax too large")
        for node in nodes:
            if isinstance(node, (ast.ClassDef, ast.Lambda, ast.With, ast.AsyncFunctionDef, ast.Await, ast.Global, ast.Nonlocal, ast.Delete, ast.Yield, ast.YieldFrom, ast.Match, ast.NamedExpr)):
                raise RestrictedError(f"Unsupported syntax: {type(node).__name__}")
            if isinstance(node, ast.Name) and node.id.startswith("_") and node.id != "_":
                raise RestrictedError("Private identifiers are forbidden")
            if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
                raise RestrictedError("Private attributes are forbidden")
        definitions = [n for n in self.tree.body if isinstance(n, ast.FunctionDef)]
        if len(definitions) != 1 or definitions[0].name != function:
            raise RestrictedError("Expected exactly the contracted function")
        self.function = definitions[0]
        args = self.function.args
        if self.function.decorator_list or args.vararg or args.kwarg or args.kwonlyargs or args.defaults or args.posonlyargs or len(args.args) != 1:
            raise RestrictedError("Contract function must have one simple argument")
        if any(not isinstance(n, (ast.FunctionDef, ast.Import, ast.ImportFrom)) and not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)) for n in self.tree.body):
            raise RestrictedError("Only imports and the contracted function are allowed")
        self.max_steps = max_steps
        self.timeout = timeout

    def run(self, argument):
        self.steps = 0
        self.until = time.monotonic() + self.timeout
        self.env = {
            "len": _Call(len), "sum": _Call(sum), "min": _Call(min), "max": _Call(max),
            "abs": _Call(abs), "round": _Call(round), "any": _Call(any), "all": _Call(all),
            "sorted": _Call(sorted), "enumerate": _Call(lambda a, start=0: list(enumerate(a, start))),
            "zip": _Call(lambda *a: list(zip(*a))), "set": _Call(frozenset),
            "range": _Call(_range), "isinstance": _Call(_isinstance), "type": _Call(_type),
            "int": int, "str": str, "bool": bool, "float": float, "list": list, "dict": dict, "tuple": tuple,
            "ValueError": ValueError, "TypeError": TypeError, "KeyError": KeyError, "IndexError": IndexError,
            "ArithmeticError": ArithmeticError, "Exception": Exception,
        }
        self.modules = {
            "csv": _Module({"DictReader": _Call(_CSVRows)}),
            "io": _Module({"StringIO": _Call(lambda text: text)}),
            "decimal": _Module({"Decimal": _Call(_decimal), "InvalidOperation": InvalidOperation}),
        }
        self._check(argument)
        self.last_argument = copy.deepcopy(argument)
        self.env[self.function.args.args[0].arg] = self.last_argument
        self._check(argument)
        for node in self.tree.body:
            if isinstance(node, ast.Import):
                for name in node.names:
                    if name.name not in self.modules:
                        raise RestrictedError("Import not allowed")
                    self.env[name.asname or name.name] = self.modules[name.name]
            elif isinstance(node, ast.ImportFrom):
                if node.level or node.module not in self.modules:
                    raise RestrictedError("Import not allowed")
                for name in node.names:
                    if name.name not in self.modules[node.module].values:
                        raise RestrictedError("Import member not allowed")
                    self.env[name.asname or name.name] = self.modules[node.module].values[name.name]
        try:
            with localcontext() as ctx:
                ctx.prec = 32
                self.block(self.function.body)
        except _Return as result:
            self._check(result.value)
            return result.value
        except (_Break, _Continue):
            raise RestrictedError("Loop control used outside an active loop") from None
        return None

    def tick(self):
        self.steps += 1
        if self.steps > self.max_steps or time.monotonic() > self.until:
            raise RestrictedError("Execution budget exceeded")

    def _check(self, value, depth=0, volume=None):
        if volume is None:
            volume = [0]
        volume[0] += len(value) * 4 if isinstance(value, str) else 16
        if volume[0] > 128 * 1024:
            raise RestrictedError("Aggregate value size limit exceeded")
        if isinstance(value, str):
            self.steps += len(value) // 64
        self.tick()
        if depth > 20:
            raise RestrictedError("Value nesting limit exceeded")
        if isinstance(value, str) and len(value) > MAX_STRING:
            raise RestrictedError("String limit exceeded")
        if isinstance(value, int) and value.bit_length() > 128:
            raise RestrictedError("Integer limit exceeded")
        if isinstance(value, float) and not math.isfinite(value):
            raise RestrictedError("Nonfinite value")
        if isinstance(value, Decimal) and (not value.is_finite() or len(value.as_tuple().digits) > 32 or abs(value.as_tuple().exponent) > 32 or abs(value.adjusted()) > 32):
            raise RestrictedError("Decimal result outside bounded reference runtime")
        if isinstance(value, (list, tuple, dict, frozenset)):
            if len(value) > MAX_ITEMS:
                raise RestrictedError("Collection limit exceeded")
            for item in (list(value.items()) if isinstance(value, dict) else value):
                self._check(item, depth + 1, volume)
        return value

    def block(self, nodes):
        for node in nodes:
            self.statement(node)

    def assign(self, target, value):
        self._check(value)
        if isinstance(target, ast.Name):
            if target.id in self.modules or target.id in ("int", "str", "bool", "list", "dict", "tuple", "float", "isinstance"):
                raise RestrictedError("Cannot overwrite runtime names")
            self.env[target.id] = value
        elif isinstance(target, (ast.Tuple, ast.List)):
            if len(target.elts) != len(value):
                raise ValueError("Unpacking mismatch")
            for t, v in zip(target.elts, value):
                self.assign(t, v)
        elif isinstance(target, ast.Subscript):
            obj, key = self.expression(target.value), self.expression(target.slice)
            if type(obj) not in (list, dict):
                raise RestrictedError("Assignment target forbidden")
            obj[key] = value
            self._check(obj)
        else:
            raise RestrictedError("Assignment target forbidden")

    def statement(self, node):
        self.tick()
        if isinstance(node, ast.Return):
            raise _Return(self.expression(node.value) if node.value else None)
        if isinstance(node, ast.Assign):
            value = self.expression(node.value)
            for target in node.targets:
                self.assign(target, value)
        elif isinstance(node, ast.AnnAssign):
            self.assign(node.target, self.expression(node.value))
        elif isinstance(node, ast.AugAssign):
            self.assign(node.target, self.binary(node.op, self.expression(node.target), self.expression(node.value)))
        elif isinstance(node, ast.Expr):
            self.expression(node.value)
        elif isinstance(node, ast.If):
            self.block(node.body if self.expression(node.test) else node.orelse)
        elif isinstance(node, (ast.For, ast.While)):
            broke = False
            iterator = iter(self.expression(node.iter)) if isinstance(node, ast.For) else None
            while True:
                self.tick()
                if iterator is not None:
                    try:
                        item = next(iterator)
                    except StopIteration:
                        break
                    self.assign(node.target, item)
                elif not self.expression(node.test):
                    break
                try:
                    self.block(node.body)
                except _Break:
                    broke = True
                    break
                except _Continue:
                    continue
            if not broke:
                self.block(node.orelse)
        elif isinstance(node, ast.Raise):
            error = self.expression(node.exc)
            if error in (ValueError, TypeError):
                error = error()
            if type(error) not in (ValueError, TypeError, InvalidOperation):
                raise RestrictedError("Exception type forbidden")
            raise error
        elif isinstance(node, ast.Try):
            try:
                self.block(node.body)
            except (ValueError, TypeError, InvalidOperation, KeyError, IndexError) as exc:
                if isinstance(exc, RestrictedError):
                    raise
                for handler in node.handlers:
                    if handler.type is None:
                        raise RestrictedError("Bare exception handlers forbidden")
                    types = self.expression(handler.type)
                    types = types if isinstance(types, tuple) else (types,)
                    if any(t not in (ValueError, TypeError, InvalidOperation, KeyError, IndexError, ArithmeticError, Exception) for t in types):
                        raise RestrictedError("Exception handler forbidden")
                    if isinstance(exc, types):
                        if handler.name:
                            self.env[handler.name] = str(exc)
                        self.block(handler.body)
                        break
                else:
                    raise
            else:
                self.block(node.orelse)
            finally:
                self.block(node.finalbody)
        elif isinstance(node, ast.Break):
            raise _Break()
        elif isinstance(node, ast.Continue):
            raise _Continue()
        elif isinstance(node, ast.Pass):
            pass
        else:
            raise RestrictedError(f"Unsupported statement: {type(node).__name__}")

    def binary(self, op, left, right):
        if isinstance(op, ast.Mult):
            if isinstance(left, (str, list, tuple)) and isinstance(right, int) and len(left) * max(right, 0) > MAX_STRING:
                raise RestrictedError("Multiplication limit exceeded")
            if isinstance(right, (str, list, tuple)) and isinstance(left, int) and len(right) * max(left, 0) > MAX_STRING:
                raise RestrictedError("Multiplication limit exceeded")
        operations = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod}
        if type(op) not in operations:
            raise RestrictedError("Operator forbidden")
        if isinstance(op, ast.Mod) and isinstance(left, str):
            raise RestrictedError("String interpolation forbidden")
        return self._check(operations[type(op)](left, right))

    def attribute(self, obj, name):
        if name.startswith("_"):
            raise RestrictedError("Private attribute forbidden")
        if isinstance(obj, _Module):
            if name not in obj.values:
                raise RestrictedError("Module attribute forbidden")
            return obj.values[name]
        if isinstance(obj, _CSVRows) and name == "fieldnames":
            return obj.fieldnames
        if isinstance(obj, Decimal) and name in ("is_finite", "as_tuple", "quantize", "to_integral_value", "is_nan", "is_infinite"):
            return _Call(getattr(obj, name))
        if type(obj).__name__ == "DecimalTuple" and name in ("exponent", "digits", "sign"):
            return getattr(obj, name)
        methods = {
            str: {"strip", "lstrip", "rstrip", "lower", "upper", "casefold", "split", "rsplit", "replace", "startswith", "endswith", "isdigit", "isdecimal", "isascii", "isalpha", "isalnum", "isspace", "join", "count"},
            list: {"append", "extend", "copy", "count", "index"},
            dict: {"get", "items", "keys", "values", "copy", "setdefault", "update", "pop"},
        }
        for typ, allowed in methods.items():
            if type(obj) is typ and name in allowed:
                def method(*args, **kwargs):
                    for value in args:
                        self._check(value)
                    if isinstance(obj, str) and name == "join":
                        if len(args) != 1 or not isinstance(args[0], (list, tuple)) or any(not isinstance(v, str) for v in args[0]):
                            raise RestrictedError("Join requires bounded strings")
                        if sum(map(len, args[0])) + len(obj) * len(args[0]) > MAX_STRING:
                            raise RestrictedError("Join limit exceeded")
                    if isinstance(obj, str) and name == "replace":
                        if len(args) >= 2 and isinstance(args[1], str) and (len(obj) + 1) * (len(args[1]) + 1) > MAX_STRING:
                            raise RestrictedError("Replacement expansion limit exceeded")
                    result = getattr(obj, name)(*args, **kwargs)
                    self._check(obj)
                    return result
                return _Call(method)
        raise RestrictedError(f"Attribute {name} is not available on {type(obj).__name__} in the reference runtime")

    def expression(self, node):
        self.tick()
        if isinstance(node, ast.Constant):
            if type(node.value) not in (str, int, float, bool, type(None)):
                raise RestrictedError("Constant forbidden")
            return self._check(node.value)
        if isinstance(node, ast.Name):
            if node.id not in self.env:
                raise RestrictedError("Name is not exposed by the reference runtime: " + node.id)
            return self.env[node.id]
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            values = [self.expression(x) for x in node.elts]
            if isinstance(node, ast.Set):
                return self._check(frozenset(values))  # Immutable: membership tests only.
            return self._check(tuple(values) if isinstance(node, ast.Tuple) else values)
        if isinstance(node, ast.Dict):
            if any(k is None for k in node.keys):
                raise RestrictedError("Dictionary unpacking forbidden")
            return self._check({self.expression(k): self.expression(v) for k, v in zip(node.keys, node.values)})
        if isinstance(node, ast.JoinedStr):
            return self._check("".join(self.expression(x) for x in node.values))
        if isinstance(node, ast.FormattedValue):
            value = self.expression(node.value)
            if not isinstance(value, (str, int, float, bool, Decimal, list, dict, tuple, type(None))):
                raise RestrictedError("Only plain data can be formatted")
            spec = self.expression(node.format_spec) if node.format_spec else ""
            # Bounded width and precision: a huge width would bypass the string limit.
            if not re.fullmatch(r"[<>^]?\d{0,3}(\.\d{1,2})?[dfs]?", spec):
                raise RestrictedError("Format specification forbidden")
            if node.conversion == 114:
                value = repr(value)
            elif node.conversion == 115:
                value = str(value)
            elif node.conversion != -1:
                raise RestrictedError("Format conversion forbidden")
            return self._check(format(value, spec) if spec else str(value))
        if isinstance(node, ast.Attribute):
            return self.attribute(self.expression(node.value), node.attr)
        if isinstance(node, ast.Subscript):
            obj, key = self.expression(node.value), self.expression(node.slice)
            if not isinstance(obj, (str, list, tuple, dict)):
                raise RestrictedError("Subscript target forbidden")
            return self._check(obj[key])
        if isinstance(node, ast.Slice):
            return slice(*(self.expression(x) if x is not None else None for x in (node.lower, node.upper, node.step)))
        if isinstance(node, ast.BinOp):
            return self.binary(node.op, self.expression(node.left), self.expression(node.right))
        if isinstance(node, ast.UnaryOp):
            value = self.expression(node.operand)
            if isinstance(node.op, ast.Not):
                return not value
            if isinstance(node.op, ast.USub):
                return self._check(-value)
            if isinstance(node.op, ast.UAdd):
                return self._check(+value)
            raise RestrictedError("Unary operator forbidden")
        if isinstance(node, ast.BoolOp):
            value = None
            for operand in node.values:
                value = self.expression(operand)
                if isinstance(node.op, ast.And) and not value or isinstance(node.op, ast.Or) and value:
                    break
            return value
        if isinstance(node, ast.IfExp):
            return self.expression(node.body if self.expression(node.test) else node.orelse)
        if isinstance(node, ast.Compare):
            operations = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge, ast.In: lambda a,b:a in b, ast.NotIn: lambda a,b:a not in b, ast.Is: operator.is_, ast.IsNot: operator.is_not}
            left = self.expression(node.left)
            for op, right_node in zip(node.ops, node.comparators):
                right = self.expression(right_node)
                if type(op) not in operations or not operations[type(op)](left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.Call):
            function = self.expression(node.func)
            args = [self.expression(x) for x in node.args]
            if any(k.arg is None for k in node.keywords):
                raise RestrictedError("Keyword expansion forbidden")
            kwargs = {k.arg: self.expression(k.value) for k in node.keywords}
            if "key" in kwargs:
                raise RestrictedError("Native callback arguments are not supported")
            for value in args:
                self._check(value)
            if isinstance(function, _Call):
                result = function.function(*args, **kwargs)
            elif function in (int, str, bool, float, list, dict, tuple, ValueError, TypeError, InvalidOperation):
                if function is str and args and not isinstance(args[0], (str, int, bool, float, Decimal, type(None))):
                    raise RestrictedError("Only scalar string conversion is allowed")
                result = function(*args, **kwargs)
            else:
                raise RestrictedError("Call target forbidden")
            if isinstance(result, (type({}.items()), type({}.keys()), type({}.values()))):
                result = list(result)
            return self._check(result)
        if isinstance(node, (ast.ListComp, ast.GeneratorExp, ast.DictComp)):
            output = {} if isinstance(node, ast.DictComp) else []
            previous = self.env.copy()
            def visit(index):
                self.tick()
                if index == len(node.generators):
                    if isinstance(output, dict):
                        output[self.expression(node.key)] = self.expression(node.value)
                    else:
                        output.append(self.expression(node.elt))
                    self._check(output)
                    return
                gen = node.generators[index]
                if gen.is_async:
                    raise RestrictedError("Async comprehensions forbidden")
                for item in self.expression(gen.iter):
                    self.assign(gen.target, item)
                    if all(self.expression(test) for test in gen.ifs):
                        visit(index + 1)
            try:
                visit(0)
            finally:
                self.env = previous
            return output
        raise RestrictedError(f"Unsupported expression: {type(node).__name__}")
