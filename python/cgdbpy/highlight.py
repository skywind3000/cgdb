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
#
# The cursor line and the status bar are deliberately different hues
# (blue-grey vs violet-grey): sharing one colour made the selected line
# read as "another status bar" at a glance.
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
    # chrome. The interface groups mirror highlight_groups.cpp's defaults:
    # enabled/disabled breakpoints and the executing line number colour the
    # line NUMBER (bold red / bold yellow / bold green); arrows are bold
    # green (executing) and bold white (selected); blocks are reverse video.
    "line-number": "#6272a4",
    "line-number.current": "bold #f8f8f2",
    "line-number.executing": "bold #50fa7b",
    "breakpoint": "bold #ff5555",
    "breakpoint.disabled": "bold #ffcb6b",
    "exec-arrow": "bold #50fa7b",
    "sel-arrow": "bold #f8f8f2",
    "exec-block": "reverse #50fa7b",
    "sel-block": "reverse #f8f8f2",
    "exec-line-highlight": "bold bg:#50fa7b #282a36",
    "mark": "bold #f8f8f2",
    "vline": "#6272a4",
    "vline-focus": "bold #6272a4",
    "selected-line": "bg:#2e4a62",
    "status": "bg:#44475a #f8f8f2",
    "command": "bold #f8f8f2",
    "console": "",
    "console.prompt": "bold #50fa7b",
    "search-match": "bg:#ffb86c #282a36",
    "scroll-status": "bold",
    "filedlg": "",
    "filedlg.label": "bold",
    "filedlg.selnum": "bold",
    "filedlg.arrow": "bold #50fa7b",
    "filedlg.filler": "#6272a4",
    "border": "#6272a4",
}

MONO_STYLE = {k: "" for k in COLOR_STYLE}
# keep structural emphasis even without colour -- the same groups C marks
# SWIN_A_BOLD (arrows, line numbers, breakpoints, marks) stay bold, the
# reverse-video groups (status bar, blocks) stay reverse
MONO_STYLE.update({
    "line-number.current": "bold",
    "line-number.executing": "bold",
    "breakpoint": "bold",
    "breakpoint.disabled": "bold",
    "exec-arrow": "bold",
    "sel-arrow": "bold",
    "exec-block": "reverse",
    "sel-block": "reverse",
    "exec-line-highlight": "bold",
    "mark": "bold",
    "vline-focus": "bold",
    "selected-line": "bold",
    "status": "reverse",
    "command": "bold",
    "search-match": "reverse",
    "scroll-status": "bold",
    "filedlg.label": "bold",
    "filedlg.selnum": "bold",
    "filedlg.arrow": "bold",
    "filedlg.selected": "reverse",
})


def build_style(color=True):
    from prompt_toolkit.styles import Style
    return Style.from_dict(dict(COLOR_STYLE if color else MONO_STYLE))
