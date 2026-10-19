"""Pygments token -> prompt_toolkit style class mapping.

cgdb's own colour system is highlight_groups.cpp (HLG_* groups). This maps
Pygments token types onto an equivalent set so that `:set color=on/off` and the
overall look stay recognisable to a cgdb user.
"""

from pygments.token import Token

# Ordered most-specific first; a token inherits from its parents so we walk up
# the hierarchy until something matches.
_DIRECT = {
    Token.Keyword: "class:kw",
    Token.Keyword.Type: "class:kwtype",
    Token.Keyword.Constant: "class:kwconst",
    Token.Keyword.Namespace: "class:kw",
    Token.Name.Function: "class:func",
    Token.Name.Function.Magic: "class:func",
    Token.Name.Class: "class:type",
    Token.Name.Namespace: "class:type",
    Token.Name.Builtin: "class:builtin",
    Token.Name.Builtin.Pseudo: "class:builtin",
    Token.Name.Variable: "class:var",
    Token.Name.Constant: "class:const",
    Token.Name.Decorator: "class:decorator",
    Token.Name.Tag: "class:kw",
    Token.Name.Attribute: "class:var",
    Token.Literal.String: "class:string",
    Token.Literal.String.Doc: "class:string",
    Token.Literal.String.Interpol: "class:string",
    Token.Literal.String.Escape: "class:escape",
    Token.Literal.Number: "class:number",
    Token.Literal.Number.Integer: "class:number",
    Token.Literal.Number.Float: "class:number",
    Token.Literal.Number.Hex: "class:number",
    Token.Literal.Number.Oct: "class:number",
    Token.Literal.Number.Bin: "class:number",
    Token.Literal.Number.Integer.Long: "class:number",
    Token.Comment: "class:comment",
    Token.Comment.Single: "class:comment",
    Token.Comment.Multiline: "class:comment",
    Token.Comment.Preproc: "class:preproc",
    Token.Comment.PreprocFile: "class:preproc",
    Token.Comment.Special: "class:comment",
    Token.Operator: "class:operator",
    Token.Operator.Word: "class:kw",
    Token.Punctuation: "class:punct",
    Token.Generic.Heading: "class:bold",
    Token.Generic.Subheading: "class:bold",
    Token.Generic.Deleted: "class:diffdel",
    Token.Generic.Inserted: "class:diffins",
    Token.Generic.Emph: "class:italic",
    Token.Generic.Strong: "class:bold",
    Token.Text: "",
    Token.Text.Whitespace: "",
    Token.Error: "class:error",
}


def style_for_token(tok):
    """Return the prompt_toolkit style class for a Pygments token type."""
    t = tok
    while t:
        s = _DIRECT.get(t)
        if s is not None:
            return s
        if not t.subtypes:
            break
        t = t.parent
    return ""


# prompt_toolkit Style.from_dict entries. Colours chosen to read on both light
# and dark terminals; `:set color=off` swaps in the monochrome style.
COLOR_STYLE = {
    "kw": "#ff79c6",
    "kwtype": "#8be9fd",
    "kwconst": "#bd93f9",
    "func": "#50fa7b",
    "type": "#8be9fd",
    "builtin": "#8be9fd",
    "var": "",
    "const": "#bd93f9",
    "decorator": "#ffb86c",
    "string": "#f1fa8c",
    "escape": "#ffb86c",
    "number": "#bd93f9",
    "comment": "italic #6272a4",
    "preproc": "#ffb86c",
    "operator": "#ff79c6",
    "punct": "",
    "bold": "bold",
    "italic": "italic",
    "error": "bg:#ff5555 #ffffff",
    "diffdel": "#ff5555",
    "diffins": "#50fa7b",
    # chrome
    "line-number": "#6272a4",
    "line-number.current": "bold #f8f8f2",
    "breakpoint": "bold #ff5555",
    "exec-arrow": "bold #50fa7b",
    "selected-line": "bg:#44475a",
    "status": "bg:#44475a #f8f8f2",
    "status.focus": "bg:#50fa7b #282a36",
    "command": "bold #f8f8f2",
    "console": "",
    "console.prompt": "bold #50fa7b",
    "search-match": "bg:#ffb86c #282a36",
    "filedlg": "",
    "filedlg.label": "bold",
    "filedlg.selected": "bg:#44475a #f8f8f2",
    "filedlg.filler": "#6272a4",
    "border": "#6272a4",
}

MONO_STYLE = {k: "" for k in COLOR_STYLE}
# keep structural emphasis even without colour
MONO_STYLE.update({
    "line-number.current": "bold",
    "breakpoint": "bold",
    "exec-arrow": "bold",
    "selected-line": "reverse",
    "status": "reverse",
    "status.focus": "bold reverse",
    "command": "bold",
    "search-match": "reverse",
    "filedlg.label": "bold",
    "filedlg.selected": "reverse",
})


def build_style(color=True):
    from prompt_toolkit.styles import Style
    return Style.from_dict(dict(COLOR_STYLE if color else MONO_STYLE))
