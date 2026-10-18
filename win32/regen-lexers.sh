#!/bin/sh
# Regenerate win32/generated/*.cpp from the .lpp sources using flex.
#
# flex does not exist on Windows, so the generated lexers are committed
# to the repository and refreshed with this script when a .lpp changes.
# Run it from the repository root, from Git Bash:
#
#   MSYS_NO_PATHCONV=1 wsl.exe -d Ubuntu-24.04 -- bash /mnt/e/.../win32/regen-lexers.sh
#
# or from any Linux environment with flex 2.6.x installed.
set -e

LEXERS="
lib/tokenizer/adalexer.lpp
lib/tokenizer/asmlexer.lpp
lib/tokenizer/cgdbhelplexer.lpp
lib/tokenizer/clexer.lpp
lib/tokenizer/dlexer.lpp
lib/tokenizer/fortranlexer.lpp
lib/tokenizer/golexer.lpp
lib/tokenizer/rustlexer.lpp
cgdb/command_lexer.lpp
"

mkdir -p win32/generated

for l in $LEXERS; do
    name=$(basename "${l%.lpp}")
    flex -o "win32/generated/${name}.cpp" "$l"
    echo "generated win32/generated/${name}.cpp"
done

echo "done. Review with: git diff --stat win32/generated"
