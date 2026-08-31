from app.pine import ast_nodes as ast
from app.pine.errors import PineParseError, PineUnsupportedError
from app.pine.lexer import Token, preprocess, tokenize_line

_TYPE_KEYWORDS = {"float", "int", "bool", "string", "color", "line", "label", "box", "table", "array", "matrix"}
_COMPARISON_OPS = {"==", "!=", ">", "<", ">=", "<="}
_ADDITIVE_OPS = {"+", "-"}
_MULT_OPS = {"*", "/", "%"}


class _ExprParser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> Token:
        return self.tokens[self.pos]

    def peek2(self) -> Token | None:
        return self.tokens[self.pos + 1] if self.pos + 1 < len(self.tokens) else None

    def advance(self) -> Token:
        t = self.tokens[self.pos]
        self.pos += 1
        return t

    def expect(self, value: str) -> Token:
        t = self.peek()
        if t.value != value:
            raise PineParseError(f"atteso '{value}', trovato '{t.value}'", t.line_no)
        return self.advance()

    def at_end(self) -> bool:
        return self.peek().kind == "EOF"

    def parse(self) -> ast.Expr:
        e = self.parse_ternary()
        if not self.at_end():
            raise PineParseError(f"token inatteso '{self.peek().value}'", self.peek().line_no)
        return e

    def parse_ternary(self) -> ast.Expr:
        cond = self.parse_or()
        if self.peek().value == "?":
            self.advance()
            if_true = self.parse_ternary()
            self.expect(":")
            if_false = self.parse_ternary()
            return ast.Ternary(cond, if_true, if_false)
        return cond

    def parse_or(self) -> ast.Expr:
        left = self.parse_and()
        while self.peek().kind == "or":
            self.advance()
            left = ast.Binary("or", left, self.parse_and())
        return left

    def parse_and(self) -> ast.Expr:
        left = self.parse_not()
        while self.peek().kind == "and":
            self.advance()
            left = ast.Binary("and", left, self.parse_not())
        return left

    def parse_not(self) -> ast.Expr:
        if self.peek().kind == "not":
            self.advance()
            return ast.Unary("not", self.parse_not())
        return self.parse_comparison()

    def parse_comparison(self) -> ast.Expr:
        left = self.parse_additive()
        while self.peek().value in _COMPARISON_OPS:
            op = self.advance().value
            left = ast.Binary(op, left, self.parse_additive())
        return left

    def parse_additive(self) -> ast.Expr:
        left = self.parse_mult()
        while self.peek().value in _ADDITIVE_OPS:
            op = self.advance().value
            left = ast.Binary(op, left, self.parse_mult())
        return left

    def parse_mult(self) -> ast.Expr:
        left = self.parse_unary()
        while self.peek().value in _MULT_OPS:
            op = self.advance().value
            left = ast.Binary(op, left, self.parse_unary())
        return left

    def parse_unary(self) -> ast.Expr:
        if self.peek().value in ("+", "-"):
            op = self.advance().value
            return ast.Unary(op, self.parse_unary())
        return self.parse_postfix()

    def parse_postfix(self) -> ast.Expr:
        expr = self.parse_primary()
        while True:
            t = self.peek()
            if t.value == "(":
                if not isinstance(expr, ast.Identifier):
                    raise PineUnsupportedError("chiamata su espressione non identificatore", t.line_no)
                args, kwargs = self._parse_args()
                expr = ast.Call(expr.name, args, kwargs, t.line_no)
            elif t.value == "[":
                self.advance()
                idx = self.parse_ternary()
                self.expect("]")
                expr = ast.HistoryRef(expr, idx)
            else:
                break
        return expr

    def _parse_args(self) -> tuple[list[ast.Expr], dict[str, ast.Expr]]:
        self.expect("(")
        args: list[ast.Expr] = []
        kwargs: dict[str, ast.Expr] = {}
        if self.peek().value == ")":
            self.advance()
            return args, kwargs
        while True:
            t = self.peek()
            t2 = self.peek2()
            if t.kind == "IDENT" and t2 is not None and t2.value == "=":
                name = self.advance().value
                self.advance()  # '='
                kwargs[name] = self.parse_ternary()
            else:
                args.append(self.parse_ternary())
            if self.peek().value == ",":
                self.advance()
                continue
            break
        self.expect(")")
        return args, kwargs

    def parse_primary(self) -> ast.Expr:
        t = self.peek()
        if t.kind == "NUMBER":
            self.advance()
            return ast.NumberLit(float(t.value))
        if t.kind == "STRING":
            self.advance()
            return ast.StringLit(t.value[1:-1])
        if t.kind == "true":
            self.advance()
            return ast.BoolLit(True)
        if t.kind == "false":
            self.advance()
            return ast.BoolLit(False)
        if t.kind == "na":
            self.advance()
            return ast.NaLit()
        if t.value == "(":
            self.advance()
            e = self.parse_ternary()
            self.expect(")")
            return e
        if t.kind == "IDENT":
            name = self.advance().value
            while self.peek().value == "." and self.peek2() is not None and self.peek2().kind == "IDENT":
                self.advance()
                name += "." + self.advance().value
            return ast.Identifier(name)
        raise PineParseError(f"token inatteso '{t.value}'", t.line_no)


def parse_expr_tokens(tokens: list[Token]) -> ast.Expr:
    return _ExprParser(tokens).parse()


def _parse_statement_line(text: str, line_no: int) -> ast.Stmt:
    tokens = tokenize_line(text, line_no)

    if tokens[0].kind == "var":
        i = 1
        if tokens[i].kind == "IDENT" and tokens[i].value in _TYPE_KEYWORDS and tokens[i + 1].kind == "IDENT":
            i += 1
        if tokens[i].kind != "IDENT" or tokens[i + 1].value != "=":
            raise PineParseError("dichiarazione 'var' non valida", line_no)
        name = tokens[i].value
        expr = parse_expr_tokens(tokens[i + 2 :] + [Token("EOF", "", line_no)])
        return ast.Assign(name, expr, is_var=True, line_no=line_no)

    if tokens[0].value == "[":
        names = []
        i = 1
        while tokens[i].value != "]":
            if tokens[i].kind == "IDENT":
                names.append(tokens[i].value)
                i += 1
                if tokens[i].value == ",":
                    i += 1
            else:
                raise PineParseError("assegnazione multipla non valida", line_no)
        i += 1  # skip ']'
        if tokens[i].value != "=":
            raise PineParseError("atteso '=' dopo assegnazione multipla", line_no)
        expr = parse_expr_tokens(tokens[i + 1 :] + [Token("EOF", "", line_no)])
        return ast.TupleAssign(names, expr, line_no=line_no)

    if tokens[0].kind == "IDENT" and tokens[1].kind == "WALRUS":
        name = tokens[0].value
        expr = parse_expr_tokens(tokens[2:] + [Token("EOF", "", line_no)])
        return ast.Reassign(name, expr, line_no=line_no)

    # plain assignment, with an optional leading type keyword: `float x = ...`
    start = 0
    if (
        tokens[0].kind == "IDENT"
        and tokens[0].value in _TYPE_KEYWORDS
        and tokens[1].kind == "IDENT"
        and tokens[2].value == "="
    ):
        start = 1
    if tokens[start].kind == "IDENT" and tokens[start + 1].value == "=":
        name = tokens[start].value
        expr = parse_expr_tokens(tokens[start + 2 :] + [Token("EOF", "", line_no)])
        return ast.Assign(name, expr, is_var=False, line_no=line_no)

    expr = parse_expr_tokens(tokens)
    return ast.ExprStmt(expr, line_no=line_no)


def _parse_block(lines: list[tuple[int, str, int]], start: int, indent: int) -> tuple[list[ast.Stmt], int]:
    stmts: list[ast.Stmt] = []
    i = start
    while i < len(lines):
        line_indent, text, line_no = lines[i]
        if line_indent < indent:
            break
        if line_indent > indent:
            raise PineParseError("indentazione inattesa", line_no)

        if text == "if" or text.startswith("if ") or text.startswith("if("):
            cond_text = text[2:].strip()
            cond_tokens = tokenize_line(cond_text, line_no)
            cond = parse_expr_tokens(cond_tokens)
            i += 1
            if i >= len(lines) or lines[i][0] <= indent:
                raise PineParseError("blocco 'if' vuoto", line_no)
            body_indent = lines[i][0]
            body, i = _parse_block(lines, i, body_indent)
            elifs: list[tuple[ast.Expr, list[ast.Stmt]]] = []
            else_body: list[ast.Stmt] | None = None
            while i < len(lines) and lines[i][0] == indent and (lines[i][1] == "else" or lines[i][1].startswith("else ")):
                etext = lines[i][1]
                eline_no = lines[i][2]
                if etext.startswith("else if"):
                    econd_tokens = tokenize_line(etext[7:].strip(), eline_no)
                    econd = parse_expr_tokens(econd_tokens)
                    i += 1
                    if i >= len(lines) or lines[i][0] <= indent:
                        raise PineParseError("blocco 'else if' vuoto", eline_no)
                    ebody_indent = lines[i][0]
                    ebody, i = _parse_block(lines, i, ebody_indent)
                    elifs.append((econd, ebody))
                else:
                    i += 1
                    if i >= len(lines) or lines[i][0] <= indent:
                        raise PineParseError("blocco 'else' vuoto", eline_no)
                    ebody_indent = lines[i][0]
                    else_body, i = _parse_block(lines, i, ebody_indent)
                    break
            stmts.append(ast.If(cond, body, elifs, else_body, line_no=line_no))
        else:
            stmts.append(_parse_statement_line(text, line_no))
            i += 1
    return stmts, i


def parse_program(source: str) -> list[ast.Stmt]:
    lines = preprocess(source)
    filtered = [
        (indent, text, no)
        for indent, text, no in lines
        if not (text.startswith("strategy(") or text.startswith("indicator(") or text.startswith("study("))
    ]
    if not filtered:
        raise PineParseError("script vuoto dopo la rimozione delle dichiarazioni di intestazione")
    base_indent = filtered[0][0]
    stmts, i = _parse_block(filtered, 0, base_indent)
    if i < len(filtered):
        raise PineParseError("indentazione non consistente", filtered[i][2])
    return stmts
