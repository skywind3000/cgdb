/* Includes {{{ */
#if HAVE_CONFIG_H
#include "config.h"
#endif

#if HAVE_STDIO_H
#include <stdio.h>
#endif /* HAVE_STDIO_H */

#if HAVE_UNISTD_H
#include <unistd.h>
#endif /* HAVE_UNISTD_H */

#if HAVE_SIGNAL_H
#include <signal.h> /* sig_atomic_t */
#endif

#if HAVE_STRING_H
#include <string.h>
#endif /* HAVE_STRING_H */

#if HAVE_SYS_WAIT_H
#include <sys/wait.h>
#endif

#define __STDC_FORMAT_MACROS
#include <inttypes.h>

#include <list>
#include <sstream>

#include "tgdb.h"
#include "io.h"
#include "sys_util.h"
#include "cgdb_clog.h"
#include "gdbwire.h"

#ifndef _WIN32
#include "fork_util.h"
#include "terminal.h"
#include "pseudo.h" /* SLAVE_SIZE constant */
#else
#include "process_win.h"
#endif

/* }}} */

/* struct tgdb {{{ */

#ifdef _WIN32
/* State of the synthesized GDB console line editor. The editor receives
 * the raw byte stream that cgdb would have written into the console pty
 * on Linux (printable bytes plus xterm-style escape sequences from
 * kui's hardcoded bindings), edits locally, echoes through the console
 * output callback, and submits finished lines as
 * -interpreter-exec console commands. */
struct win_edit_state {
    /* Current input line (UTF-8 bytes, edited bytewise) */
    std::string line;

    /* Byte offset of the cursor within line */
    size_t pos;

    /* Number of line bytes currently drawn on screen (the echo erases
     * this many when the line shrinks) */
    size_t last_shown;

    /* Command history, oldest first */
    std::list<std::string> history;

    /* 0 when not browsing history, otherwise the distance from the
     * newest entry (1 = newest) */
    int walking;

    /* Partial escape sequence being accumulated */
    std::string pending;
};
#endif

typedef struct tgdb_request *tgdb_request_ptr;
typedef std::list<tgdb_request_ptr> tgdb_request_ptr_list;

/**
 * The TGDB context data structure.
 */
struct tgdb {
    /** Reading from this will read from the debugger's output */
    int debugger_stdout;

    /** Writing to this will write to the debugger's stdin */
    int debugger_stdin;

    /**
     * The gdb new-ui mi console file descriptor.
     *
     * Writing to this will write to the gdb/mi interpreter.
     * Reading from it will read the output of the gdb/mi interpreter.
     */
    int gdb_mi_ui_fd;

    /** The master, slave and slavename of the new-ui pty device */
#ifndef _WIN32
    pty_pair_ptr new_ui_pty_pair;
#endif

    /** The pid of child process */
    pid_t debugger_pid;

#ifdef _WIN32
    /* MI-over-pipes transport (see process_win.h) */
    struct win_process win_gdb;

    /* The inferior's tty named pipe + reader thread */
    struct win_inferior_tty win_tty;

    /* True once the inferior tty request was issued */
    int win_inf_used;

    /* True when gdb is at the MI prompt (between commands) */
    int win_at_prompt;

    /* Synthesized console line editor state */
    struct win_edit_state win_edit;
#endif

    /** The list of command requests to process */
    tgdb_request_ptr_list *command_requests;

    /**
     * If set to 1, libtgdb thinks the lower level subsystem is capable of
     * receiving another command. It needs this so that it doesn't send 2
     * commands to the lower level before it can say it can't receive a command.
     * At some point, maybe this can be removed?
     * When its set to 0, libtgdb thinks it can not send the lower level another
     * command.
     *
     * Basically whether gdb is at prompt or not. */
    int is_gdb_ready_for_next_command;

    /** If ^c was hit by user */
    sig_atomic_t control_c;

    tgdb_callbacks callbacks;

    // commands structure
    // The current command request type that is executing. NULL to start.
    enum tgdb_request_type current_request_type;

    // The disassemble command output.
    std::list<std::string> disasm;
    uint64_t address_start, address_end;

    // The gdbwire context to talk to GDB with.
    struct gdbwire *wire;

    // True if the disassemble command supports /s, otherwise false.
    int disassemble_supports_s_mode;

    // True if GDB supports the 'new-ui' command, otherwise false.
    // If gdb prints out,
    //   Undefined command: "new-ui".  Try "help".
    // on the console output, then it does not support the new-ui command.
    // Otherwise it is assumed that it does support the command.
    bool gdb_supports_new_ui_command;

    // Temporary buffer used to store the line by line console output
    // in order to search for the unsupported new ui string above.
    std::string *undefined_new_ui_command;
};

// This is the type of request
struct tgdb_request {
    enum tgdb_request_type header;

    union {
        struct {
            // The null terminated console command to pass to GDB
            const char *command;
        } console_command;

        struct {
            const char *slavename;
        } tty_command;

        struct {
            // This is the command that libtgdb should run through the debugger
            enum tgdb_command_type c;
        } debugger_command;

        struct {
            // The filename to set the breakpoint in
            const char *file;
            // The corresponding line number
            int line;
            // The address to set breakpoint in (if file is null)
            uint64_t addr;
            // The action to take
            enum tgdb_breakpoint_action b;
        } modify_breakpoint;

        struct {
            int lines;
        } disassemble;

        struct {
            int source;
            int raw;
        } disassemble_func;

        struct {
            // The filename to set the breakpoint in
            const char *file;
            // The corresponding line number
            int line;
            // The address to set breakpoint in (if file is null)
            uint64_t addr;
        } until_line;
    } choice;
};

/* }}} */

/* Temporary prototypes {{{ */
struct tgdb_response *tgdb_create_response(enum tgdb_response_type header);
void tgdb_send_response(struct tgdb *tgdb, struct tgdb_response *response);
static void tgdb_run_request(struct tgdb *tgdb, struct tgdb_request *request);
static void tgdb_unqueue_and_deliver_command(struct tgdb *tgdb);
void tgdb_run_or_queue_request(struct tgdb *tgdb,
        struct tgdb_request *request, bool priority);

/* }}} */

#ifdef _WIN32
/* Windows synthesized console {{{ */

/* The prompt gdb would normally print through readline. gdbwire consumes
 * the literal prompt bytes, so the display is driven from here. */
#define WIN_PROMPT "(gdb) "
#define WIN_HISTORY_MAX 100

static void win_edit_init(struct win_edit_state *e)
{
    e->pos = 0;
    e->last_shown = 0;
    e->walking = 0;
}

static void win_edit_destroy(struct win_edit_state *e)
{
    e->line.clear();
    e->pending.clear();
    e->history.clear();
    e->pos = 0;
    e->last_shown = 0;
    e->walking = 0;
}

/* Send bytes to the display (same funnel as the Linux console pty). */
static void win_emit(struct tgdb *tgdb, const std::string &text)
{
    tgdb->callbacks.console_output_callback(tgdb->callbacks.context, text);
}

/* Redraw the whole input line: CR, prompt, line, erase the leftover tail
 * of the previously drawn line, then back up to the cursor. The
 * synthesized scroller understands CR (column 0), printable overwrite
 * and BS (column left) - a classic dumb terminal. */
static void win_edit_redraw(struct tgdb *tgdb)
{
    struct win_edit_state *e = &tgdb->win_edit;
    size_t pad = e->last_shown > e->line.size() ?
            e->last_shown - e->line.size() : 0;
    size_t back = e->line.size() + pad - e->pos;
    std::string out("\r");

    out += WIN_PROMPT;
    out += e->line;
    out.append(pad, ' ');
    out.append(back, '\b');
    e->last_shown = e->line.size();

    win_emit(tgdb, out);
}

/* Display text coming from gdb (console stream records, ^error
 * messages, inferior output). If the user is mid-line, or a prompt is
 * showing, push the line up, print the text and redraw below - the way
 * readline interleaves async output. */
static void tgdb_win_output(struct tgdb *tgdb, const std::string &text)
{
    struct win_edit_state *e = &tgdb->win_edit;

    if (!tgdb->win_at_prompt && e->line.empty() && e->pos == 0) {
        win_emit(tgdb, text);
        return;
    }

    std::string out("\n");
    out += text;
    win_emit(tgdb, out);
    win_edit_redraw(tgdb);
}

/* gdb reached its prompt: (re)display the prompt, restoring any partial
 * input the user typed while the previous command was running. */
static void tgdb_win_prompt(struct tgdb *tgdb)
{
    struct win_edit_state *e = &tgdb->win_edit;

    if (!e->line.empty() || e->pos > 0)
        win_emit(tgdb, std::string("\n"));

    e->last_shown = 0;
    win_edit_redraw(tgdb);
}

/* Escape a string for use inside an MI double-quoted string. */
static std::string win_mi_quote(const std::string &s)
{
    std::string out;

    for (size_t i = 0; i < s.size(); i++) {
        if (s[i] == '"' || s[i] == '\\')
            out += '\\';
        out += s[i];
    }

    return out;
}

/* Queue a finished user line as a console command request. */
static void tgdb_win_submit_command(struct tgdb *tgdb, const std::string &line)
{
    tgdb_request_ptr request_ptr;

    request_ptr = (tgdb_request_ptr)
            cgdb_malloc(sizeof(struct tgdb_request));
    request_ptr->header = TGDB_REQUEST_CONSOLE_COMMAND;
    request_ptr->choice.console_command.command = cgdb_strdup(line.c_str());
    tgdb_run_or_queue_request(tgdb, request_ptr, false);
}

static void win_edit_submit(struct tgdb *tgdb)
{
    struct win_edit_state *e = &tgdb->win_edit;
    std::string line = e->line;

    win_emit(tgdb, std::string("\n"));
    e->line.clear();
    e->pos = 0;
    e->last_shown = 0;
    e->walking = 0;
    e->pending.clear();

    if (line.empty()) {
        win_edit_redraw(tgdb);
        return;
    }

    if (e->history.empty() || e->history.back() != line) {
        e->history.push_back(line);
        if (e->history.size() > WIN_HISTORY_MAX)
            e->history.pop_front();
    }

    tgdb_win_submit_command(tgdb, line);
}

static void win_edit_replace_line(struct tgdb *tgdb, const std::string &line)
{
    struct win_edit_state *e = &tgdb->win_edit;

    e->line = line;
    e->pos = e->line.size();
    win_edit_redraw(tgdb);
}

static void win_edit_history(struct tgdb *tgdb, int direction)
{
    struct win_edit_state *e = &tgdb->win_edit;
    int count = (int) e->history.size();

    if (count == 0)
        return;

    e->walking += direction;
    if (e->walking <= 0) {
        e->walking = 0;
        win_edit_replace_line(tgdb, std::string());
        return;
    }
    if (e->walking > count)
        e->walking = count;

    std::list<std::string>::iterator it = e->history.end();
    for (int i = 0; i < e->walking; i++)
        --it;
    win_edit_replace_line(tgdb, *it);
}

static void win_edit_insert(struct tgdb *tgdb, char c)
{
    struct win_edit_state *e = &tgdb->win_edit;

    e->line.insert(e->pos, 1, c);
    e->pos++;
    if (e->pos == e->line.size()) {
        /* common case: append at the end, no redraw needed */
        std::string out(1, c);
        e->last_shown = e->line.size();
        win_emit(tgdb, out);
    } else {
        win_edit_redraw(tgdb);
    }
}

static void win_edit_backspace(struct tgdb *tgdb)
{
    struct win_edit_state *e = &tgdb->win_edit;

    if (e->pos == 0)
        return;

    e->pos--;
    e->line.erase(e->pos, 1);
    win_edit_redraw(tgdb);
}

static void win_edit_delete(struct tgdb *tgdb)
{
    struct win_edit_state *e = &tgdb->win_edit;

    if (e->pos >= e->line.size())
        return;

    e->line.erase(e->pos, 1);
    win_edit_redraw(tgdb);
}

static void win_edit_home(struct tgdb *tgdb)
{
    struct win_edit_state *e = &tgdb->win_edit;

    e->pos = 0;
    win_edit_redraw(tgdb);
}

static void win_edit_end(struct tgdb *tgdb)
{
    struct win_edit_state *e = &tgdb->win_edit;

    e->pos = e->line.size();
    win_edit_redraw(tgdb);
}

static void win_edit_left(struct tgdb *tgdb)
{
    struct win_edit_state *e = &tgdb->win_edit;

    if (e->pos > 0) {
        e->pos--;
        win_emit(tgdb, std::string("\b"));
    }
}

static void win_edit_right(struct tgdb *tgdb)
{
    struct win_edit_state *e = &tgdb->win_edit;

    if (e->pos < e->line.size()) {
        e->pos++;
        std::string out(1, e->line[e->pos - 1]);
        win_emit(tgdb, out);
    }
}

static void win_edit_kill_line(struct tgdb *tgdb)
{
    struct win_edit_state *e = &tgdb->win_edit;

    e->line.clear();
    e->pos = 0;
    win_edit_redraw(tgdb);
}

static void win_edit_kill_to_eol(struct tgdb *tgdb)
{
    struct win_edit_state *e = &tgdb->win_edit;

    if (e->pos < e->line.size()) {
        e->line.erase(e->pos);
        win_edit_redraw(tgdb);
    }
}

static void win_edit_kill_word_back(struct tgdb *tgdb)
{
    struct win_edit_state *e = &tgdb->win_edit;

    while (e->pos > 0 && e->line[e->pos - 1] == ' ')
        e->pos--;
    while (e->pos > 0 && e->line[e->pos - 1] != ' ')
        e->pos--;
    e->line.erase(e->pos);
    win_edit_redraw(tgdb);
}

/* Feed one byte of an escape sequence into the pending buffer. Returns
 * true when the buffer no longer needs more bytes (either matched or
 * rejected). */
static bool win_esc_step(struct tgdb *tgdb)
{
    struct win_edit_state *e = &tgdb->win_edit;
    const std::string &p = e->pending;

    if (p.size() == 1)
        return false;               /* lone ESC: need one more byte */

    if (p.size() == 2 && (p[1] == '[' || p[1] == 'O'))
        return false;               /* need the final byte */

    if (p.size() == 3 && p[1] == '[' && p[2] == '0')
        return false;               /* MSDOS-style, one more byte */

    if (p.size() >= 2 && (p[1] == '[' || p[1] == 'O')) {
        char final_byte = p[p.size() - 1];
        e->pending.clear();
        switch (final_byte) {
            case 'A': win_edit_history(tgdb, 1); break;
            case 'B': win_edit_history(tgdb, -1); break;
            case 'C': win_edit_right(tgdb); break;
            case 'D': win_edit_left(tgdb); break;
            case 'H': win_edit_home(tgdb); break;
            case 'F': win_edit_end(tgdb); break;
            default: break;         /* unknown sequence: ignore */
        }
        return true;
    }

    if (p.size() >= 2) {
        char second = p[1];
        e->pending.clear();
        switch (second) {
            case 'b':               /* backward-word */
                win_edit_left(tgdb);
                break;
            case 'f':               /* forward-word */
                win_edit_right(tgdb);
                break;
            case 'd':               /* kill-word: delete to word end */
                win_edit_delete(tgdb);
                break;
            case '\b':              /* backward-kill-word */
                win_edit_kill_word_back(tgdb);
                break;
            default:                /* lone ESC / unknown meta: ignore */
                break;
        }
        return true;
    }

    e->pending.clear();
    return true;
}

/* Feed one byte of user input to the editor. The bytes are the same
 * stream Linux cgdb writes into the console pty: printable characters,
 * control characters and the xterm-style escape sequences kui's
 * hardcoded bindings produce. */
static int tgdb_win_editor_char(struct tgdb *tgdb, char ch)
{
    struct win_edit_state *e = &tgdb->win_edit;
    unsigned char c = (unsigned char) ch;

    /* While the inferior is running, keystrokes become its stdin (the
     * same thing the Linux pty does). */
    if (!tgdb->win_at_prompt && tgdb->win_inf_used &&
            win_inferior_tty_connected(&tgdb->win_tty)) {
        char buf[1];
        std::string echo;

        buf[0] = ch;
        if (win_inferior_tty_write(&tgdb->win_tty, buf, 1) != 1)
            return -1;

        echo.push_back(ch);
        win_emit(tgdb, echo);
        return 0;
    }

    if (!e->pending.empty() || c == 0x1b) {
        e->pending.push_back(ch);
        if (win_esc_step(tgdb))
            e->pending.clear();
        return 0;
    }

    switch (c) {
        case '\r':
        case '\n':
            win_edit_submit(tgdb);
            break;
        case 0x7f:                  /* DEL */
        case 0x08:                  /* BS */
            win_edit_backspace(tgdb);
            break;
        case 0x01:                  /* C-a: home */
            win_edit_home(tgdb);
            break;
        case 0x05:                  /* C-e: end */
            win_edit_end(tgdb);
            break;
        case 0x02:                  /* C-b: left */
            win_edit_left(tgdb);
            break;
        case 0x06:                  /* C-f: right */
            win_edit_right(tgdb);
            break;
        case 0x15:                  /* C-u: kill line */
            win_edit_kill_line(tgdb);
            break;
        case 0x0b:                  /* C-k: kill to end of line */
            win_edit_kill_to_eol(tgdb);
            break;
        case 0x17:                  /* C-w: kill word backwards */
            win_edit_kill_word_back(tgdb);
            break;
        case 0x04:                  /* C-d: delete char at cursor */
            win_edit_delete(tgdb);
            break;
        case 0x0c:                  /* C-l: redraw */
            win_edit_redraw(tgdb);
            break;
        case 0x09:                  /* TAB: no completion yet */
        case 0x03:                  /* C-c: handled upstream */
        case 0x1c:                  /* C-\ : handled upstream */
            break;
        default:
            if (c >= 0x20)
                win_edit_insert(tgdb, ch);
            break;
    }

    return 0;
}

/* }}} */
#endif /* _WIN32 */

// Command Functions {{{
static void
tgdb_commands_send_breakpoints(struct tgdb *tgdb,
        const std::list<tgdb_breakpoint> &breakpoints)
{
    tgdb->callbacks.tgdb_breakpoints_fn(
            tgdb->callbacks.context, breakpoints);
}

static void tgdb_commands_process_breakpoint(
        std::list<tgdb_breakpoint> &breakpoints,
        struct gdbwire_mi_breakpoint *breakpoint)
{
    bool file_location_avialable =
        (breakpoint->fullname || breakpoint->file) && breakpoint->line != 0;
    bool assembly_location_available = breakpoint->address && 
        !breakpoint->pending && !breakpoint->multi;

    if (file_location_avialable || assembly_location_available) {
        struct tgdb_breakpoint tb;

        if (file_location_avialable) {
            tb.path = (breakpoint->fullname)?
                breakpoint->fullname: breakpoint->file;
            tb.line = breakpoint->line;
        } else {
            tb.line = 0;
        }

        if (assembly_location_available) {
            uint64_t address = 0;
            cgdb_hexstr_to_u64(breakpoint->address, &address);
            tb.addr = address;
        } else {
            tb.addr = 0;
        }

        tb.enabled = breakpoint->enabled;
        breakpoints.push_back(tb);
    }
}

static void tgdb_commands_process_breakpoints(struct tgdb *tgdb,
        struct gdbwire_mi_result_record *result_record)
{
    enum gdbwire_result result;
    struct gdbwire_mi_command *mi_command = 0;
    result = gdbwire_get_mi_command(GDBWIRE_MI_BREAK_INFO,
        result_record, &mi_command);
    if (result == GDBWIRE_OK) {
        std::list<tgdb_breakpoint> breakpoints;
        struct gdbwire_mi_breakpoint *breakpoint =
            mi_command->variant.break_info.breakpoints;
        while (breakpoint) {
            tgdb_commands_process_breakpoint(breakpoints, breakpoint);

            if (breakpoint->multi) {
                struct gdbwire_mi_breakpoint *multi_bkpt =
                    breakpoint->multi_breakpoints;
                while (multi_bkpt) {
                    tgdb_commands_process_breakpoint(breakpoints, multi_bkpt);
                    multi_bkpt = multi_bkpt->next;
                }
            }

            breakpoint = breakpoint->next;
        }

        tgdb_commands_send_breakpoints(tgdb, breakpoints);

        gdbwire_mi_command_free(mi_command);
    }
}

static void tgdb_commands_send_source_files(struct tgdb *tgdb,
        const std::list<std::string> &source_files)
{
    tgdb->callbacks.tgdb_inferiors_soure_files_fn(
            tgdb->callbacks.context, source_files);
}

/* This function is capable of parsing the output of 'info source'.
 * It can get both the absolute and relative path to the source file.
 */
static void
tgdb_commands_process_info_sources(struct tgdb *tgdb,
        struct gdbwire_mi_result_record *result_record)
{
    enum gdbwire_result result;
    struct gdbwire_mi_command *mi_command = 0;
    result = gdbwire_get_mi_command(GDBWIRE_MI_FILE_LIST_EXEC_SOURCE_FILES,
        result_record, &mi_command);
    if (result == GDBWIRE_OK) {
        std::list<std::string> source_files;
        struct gdbwire_mi_source_file *files =
            mi_command->variant.file_list_exec_source_files.files;
        while (files) {
            char *file = (files->fullname)?files->fullname:files->file;
            source_files.push_back(file);
            files = files->next;
        }

        tgdb_commands_send_source_files(tgdb, source_files);

        gdbwire_mi_command_free(mi_command);
    }
}

static void send_disassemble_func_complete_response(struct tgdb *tgdb,
        struct gdbwire_mi_result_record *result_record)
{
    bool error = result_record->result_class == GDBWIRE_MI_ERROR;
    
    tgdb->callbacks.tgdb_disassemble_func_fn(tgdb->callbacks.context,
            tgdb->address_start, tgdb->address_end, error, tgdb->disasm);
}

static void
tgdb_commands_send_source_file(struct tgdb *tgdb, const char *fullname,
        const char *file, uint64_t address, const char *from,
        const char *func, int line)
{
    struct tgdb_file_position tfp;

    if (fullname || file) {
        tfp.path = (fullname)?fullname:file;
    } else {
        tfp.path = 0;
    }
    tfp.addr = address;
    tfp.from = from;
    tfp.func = func;
    tfp.line_number = line;

    tgdb->callbacks.tgdb_update_file_pos_fn(tgdb->callbacks.context, tfp);
}

static void tgdb_commands_process_info_source(struct tgdb *tgdb,
        struct gdbwire_mi_result_record *result_record)
{
    enum gdbwire_result result;
    struct gdbwire_mi_command *mi_command = 0;
    result = gdbwire_get_mi_command(GDBWIRE_MI_FILE_LIST_EXEC_SOURCE_FILE,
        result_record, &mi_command);
    if (result == GDBWIRE_OK) {
        tgdb_commands_send_source_file(tgdb,
                mi_command->variant.file_list_exec_source_file.fullname,
                mi_command->variant.file_list_exec_source_file.file,
                0, 0, 0,
                mi_command->variant.file_list_exec_source_file.line);

        gdbwire_mi_command_free(mi_command);
    }
}

static void tgdb_commands_process_info_frame(struct tgdb *tgdb,
        struct gdbwire_mi_result_record *result_record)
{
    bool require_source = false;
    enum gdbwire_result result;
    struct gdbwire_mi_command *mi_command = 0;
    result = gdbwire_get_mi_command(GDBWIRE_MI_STACK_INFO_FRAME,
        result_record, &mi_command);
    if (result == GDBWIRE_OK) {
        struct gdbwire_mi_stack_frame *frame =
            mi_command->variant.stack_info_frame.frame;
        uint64_t address = 0;
        cgdb_hexstr_to_u64(frame->address, &address);

        if (frame->address || frame->file || frame->fullname) {
            tgdb_commands_send_source_file(tgdb, frame->fullname, frame->file,
                    address, frame->from, frame->func, frame->line);
        } else {
            require_source = true;
        }

        gdbwire_mi_command_free(mi_command);
    } else {
        require_source = true;
    }

    if (require_source) {
        tgdb_request_ptr request;
        request = (tgdb_request_ptr) cgdb_malloc(sizeof (struct tgdb_request));
        request->header = TGDB_REQUEST_INFO_SOURCE_FILE;
        tgdb_run_or_queue_request(tgdb, request, true);
    }
}

static void gdbwire_stream_record_callback(void *context,
    struct gdbwire_mi_stream_record *stream_record)
{
    struct tgdb *tgdb = (struct tgdb*)context;

    switch (stream_record->kind) {
        case GDBWIRE_MI_CONSOLE:
            if (tgdb->current_request_type == TGDB_REQUEST_DISASSEMBLE_PC ||
                tgdb->current_request_type == TGDB_REQUEST_DISASSEMBLE_FUNC)
            {                uint64_t address;
                int result;
                char *str = stream_record->cstring;
                size_t length = strlen(str);
                char *colon = 0, colon_char = 0;

                if (str[length-1] == '\n') {
                    str[length-1] = 0;
                }

                /* Trim the gdb current location pointer off */
                if (length > 2 && str[0] == '=' && str[1] == '>') {
                    str[0] = ' ';
                    str[1] = ' ';
                }

                tgdb->disasm.push_back(str);

                colon = strchr((char*)str, ':');
                if (colon) {
                    colon_char = *colon;
                    *colon = 0;
                }

                result = cgdb_hexstr_to_u64(str, &address);

                if (colon) {
                    *colon = colon_char;
                }

                if (result == 0 && address) {
                    tgdb->address_start = tgdb->address_start ?
                         MIN(address, tgdb->address_start) : address;
                    tgdb->address_end = MAX(address, tgdb->address_end);
                }
            }
#ifdef _WIN32
            else {
                /* Windows MI-over-pipes: console stream records carry
                 * all user-visible gdb output (user commands run via
                 * -interpreter-exec console). Route them to the display
                 * through the synthesized console. */
                std::string msg(stream_record->cstring);
                tgdb_win_output(tgdb, msg);
            }
#endif
            break;
        case GDBWIRE_MI_TARGET:
        {
            std::string cstring(stream_record->cstring);
            std::string cstring_crnl;
            for (auto c : cstring) {
                if (c == '\n') {
                    cstring_crnl.push_back('\r');
                    cstring_crnl.push_back('\n');
                } else {
                    cstring_crnl.push_back(c);
                }
            }

            tgdb->callbacks.console_output_callback(
                    tgdb->callbacks.context, cstring_crnl.c_str());
            break;
        }
        case GDBWIRE_MI_LOG:
            break;
    }
}

static void
source_position_changed(struct tgdb *tgdb, gdbwire_mi_result *result)
{
    std::string frame("frame");
    std::string addr("addr");
    std::string fullname("fullname");
    std::string line("line");

    while (result) {
        if (frame == result->variable &&
            result->kind == GDBWIRE_MI_TUPLE) {

            uint64_t addr_value = 0;
            bool addr_value_set = false;

            std::string fullname_value;
            bool fullname_value_set = false;

            int line_value = 0;
            bool line_value_set = false;

            struct gdbwire_mi_result *fresult = result->variant.result;

            while (fresult) {
                if (addr == fresult->variable) {
                    addr_value = std::stoull(
                            fresult->variant.cstring, 0, 16);
                    addr_value_set = true;
                } else if (fullname == fresult->variable) {
                    fullname_value = fresult->variant.cstring;
                    fullname_value_set = true;
                } else if (line == fresult->variable) {
                    line_value = std::stoi(fresult->variant.cstring);
                    line_value_set = true;
                }
                fresult = fresult->next;
            }

            if(addr_value_set || (fullname_value_set && line_value_set)) {
                tgdb_commands_send_source_file(tgdb, fullname_value.c_str(),
                        NULL, addr_value, NULL, NULL, line_value);
            }
        }

        result = result->next;
    }
}

void tgdb_breakpoints_changed(void *context);
static void gdbwire_async_record_callback(void *context,
        struct gdbwire_mi_async_record *async_record)
{
    struct tgdb *tgdb = (struct tgdb*)context;

    switch (async_record->async_class) {
        case GDBWIRE_MI_ASYNC_STOPPED:
        case GDBWIRE_MI_ASYNC_THREAD_SELECTED:
            source_position_changed(tgdb, async_record->result);
            break;
        case GDBWIRE_MI_ASYNC_BREAKPOINT_CREATED:
        case GDBWIRE_MI_ASYNC_BREAKPOINT_MODIFIED:
        case GDBWIRE_MI_ASYNC_BREAKPOINT_DELETED:
            tgdb_breakpoints_changed(tgdb);
            break;
        default:
            break;
    }
}

static void gdbwire_result_record_callback(void *context,
        struct gdbwire_mi_result_record *result_record)
{
    struct tgdb *tgdb = (struct tgdb*)context;

    switch (tgdb->current_request_type) {
        case TGDB_REQUEST_BREAKPOINTS:
            tgdb_commands_process_breakpoints(tgdb, result_record);
            break;
        case TGDB_REQUEST_INFO_SOURCES:
            tgdb_commands_process_info_sources(tgdb, result_record);
            break;
        case TGDB_REQUEST_DISASSEMBLE_PC:
        case TGDB_REQUEST_DISASSEMBLE_FUNC:
            send_disassemble_func_complete_response(tgdb, result_record);
            break;
        case TGDB_REQUEST_DATA_DISASSEMBLE_MODE_QUERY:
            /**
             * If the mode was to high, than the result record would be
             * an error, meaning the mode is not supported. Otherwise,
             * the mode is supported.
             */
            if (result_record->result_class == GDBWIRE_MI_DONE) {
                tgdb->disassemble_supports_s_mode = 1;
                clog_info(CLOG_CGDB, "disassemble supports s mode");
            }
            break;
        case TGDB_REQUEST_INFO_SOURCE_FILE:
            tgdb_commands_process_info_source(tgdb, result_record);
            break;
        case TGDB_REQUEST_INFO_FRAME:
            tgdb_commands_process_info_frame(tgdb, result_record);
            break;
        case TGDB_REQUEST_TTY:
        case TGDB_REQUEST_DEBUGGER_COMMAND:
        case TGDB_REQUEST_CONSOLE_COMMAND:
#ifdef _WIN32
            /* User commands were run with -interpreter-exec console: show
             * the error message (the & log record text is dropped, as on
             * Linux, since it duplicates the ^error msg). */
            if ((tgdb->current_request_type ==
                        TGDB_REQUEST_DEBUGGER_COMMAND ||
                    tgdb->current_request_type ==
                        TGDB_REQUEST_CONSOLE_COMMAND) &&
                    result_record->result_class == GDBWIRE_MI_ERROR) {
                struct gdbwire_mi_result *r = result_record->result;
                while (r) {
                    if (strcmp(r->variable, "msg") == 0 &&
                            r->kind == GDBWIRE_MI_CSTRING) {
                        std::string msg(r->variant.cstring);
                        if (!msg.empty() && msg[msg.size()-1] != '\n')
                            msg.push_back('\n');
                        tgdb_win_output(tgdb, msg);
                        break;
                    }
                    r = r->next;
                }
            }
#endif
        case TGDB_REQUEST_MODIFY_BREAKPOINT:
        case TGDB_REQUEST_UNTIL_LINE:
            break;
    }
}

void tgdb_console_at_prompt(void *context);
static void gdbwire_prompt_callback(void *context, const char *prompt)
{
    struct tgdb *tgdb = (struct tgdb*)context;
    tgdb_console_at_prompt(tgdb);
}

static void gdbwire_parse_error_callback(void *context, const char *mi,
            const char *token, struct gdbwire_mi_position position)
{
    clog_error(CLOG_CGDB,
        "gdbwire parse error\n"
        "  mi text=[%s]\n"
        "  token=[%s]\n"
        "  start col=[%d]\n"
        "  end col=[%d]\n",
        mi, token, position.start_column, position.end_column);
}

static struct gdbwire_callbacks wire_callbacks =
    {
        0,
        gdbwire_stream_record_callback,
        gdbwire_async_record_callback,
        gdbwire_result_record_callback,
        gdbwire_prompt_callback,
        gdbwire_parse_error_callback
    };

int free_char_star(void *item)
{
    char *s = (char *) item;

    free(s);
    s = NULL;

    return 0;
}

void tgdb_commands_process(struct tgdb *tgdb, const std::string &str)
{
   gdbwire_push_data(tgdb->wire, str.data(), str.size());
}

void tgdb_commands_set_current_request_type(struct tgdb *tgdb,
        enum tgdb_request_type type)
{
    tgdb->current_request_type = type;
}

int tgdb_commands_disassemble_supports_s_mode(struct tgdb *tgdb)
{
    return tgdb->disassemble_supports_s_mode;
}

// }}}

static struct tgdb *initialize_tgdb_context(tgdb_callbacks callbacks)
{
    tgdb *obj = new tgdb();

    obj->control_c = 0;

    obj->debugger_stdout = -1;
    obj->debugger_stdin = -1;

    obj->gdb_mi_ui_fd = -1;

#ifndef _WIN32
    obj->new_ui_pty_pair = NULL;
#endif

    obj->command_requests = new tgdb_request_ptr_list();

    obj->is_gdb_ready_for_next_command = 0;

    obj->callbacks = callbacks;

    obj->address_start = 0;
    obj->address_end = 0;

    wire_callbacks.context = (void*)obj;
    obj->wire = gdbwire_create(wire_callbacks);

    obj->disassemble_supports_s_mode = 0;
    obj->gdb_supports_new_ui_command = true;
    obj->undefined_new_ui_command = new std::string();

#ifdef _WIN32
    /* new tgdb() value-initializes the aggregate members; only the
     * descriptors need explicit -1 defaults. */
    obj->win_gdb.stdin_fd = -1;
    obj->win_gdb.stdout_fd = -1;
    obj->win_gdb.pid = 0;
    obj->win_gdb.h_process = NULL;
    obj->win_inf_used = 0;
    obj->win_at_prompt = 0;
    win_edit_init(&obj->win_edit);
#endif

    return obj;
}

/*******************************************************************************
 * This is the basic initialization
 ******************************************************************************/

static void tgdb_issue_request(struct tgdb *tgdb, enum tgdb_request_type type,
        bool priority)
{
    tgdb_request_ptr request_ptr;
    request_ptr = (tgdb_request_ptr)cgdb_malloc(sizeof (struct tgdb_request));
    request_ptr->header = type;
    tgdb_run_or_queue_request(tgdb, request_ptr, priority);
}
 
void tgdb_breakpoints_changed(void *context)
{
    struct tgdb *tgdb = (struct tgdb*)context;
    tgdb_issue_request(tgdb, TGDB_REQUEST_BREAKPOINTS, true);
}

static void tgdb_source_location_changed(void *context)
{
    struct tgdb *tgdb = (struct tgdb*)context;
    tgdb_request_current_location(tgdb);
}

static void tgdb_command_error(void *context, const std::string &msg)
{
    struct tgdb *tgdb = (struct tgdb*)context;

    /* Send cgdb the error message */
    tgdb->callbacks.console_output_callback(tgdb->callbacks.context, msg);
}

void tgdb_console_at_prompt(void *context)
{
    struct tgdb *tgdb = (struct tgdb*)context;

    tgdb->is_gdb_ready_for_next_command = 1;

#ifdef _WIN32
    /* gdbwire consumed the literal "(gdb) " prompt bytes, so the
     * synthesized console must re-emit the prompt text itself. */
    tgdb->win_at_prompt = 1;
    tgdb_win_prompt(tgdb);
#endif

    if (tgdb->command_requests->size() > 0) {
        tgdb_unqueue_and_deliver_command(tgdb);
    }
}

/**
 * Free the tgdb request pointer data.
 *
 * @param request_ptr
 * The request pointer to destroy.
 */
static void tgdb_request_destroy(tgdb_request_ptr request_ptr)
{
    if (!request_ptr)
        return;

    switch (request_ptr->header) {
        case TGDB_REQUEST_INFO_SOURCES:
            break;
        case TGDB_REQUEST_DEBUGGER_COMMAND:
            break;
        case TGDB_REQUEST_CONSOLE_COMMAND:
            free((char *) request_ptr->choice.console_command.command);
            request_ptr->choice.console_command.command = NULL;
            break;
        case TGDB_REQUEST_TTY:
            free((char *) request_ptr->choice.tty_command.slavename);
            request_ptr->choice.tty_command.slavename = NULL;
            break;
        case TGDB_REQUEST_MODIFY_BREAKPOINT:
            free((char *) request_ptr->choice.modify_breakpoint.file);
            request_ptr->choice.modify_breakpoint.file = NULL;
            break;
        case TGDB_REQUEST_UNTIL_LINE:
            free((char *) request_ptr->choice.until_line.file);
            request_ptr->choice.until_line.file = NULL;
            break;
        case TGDB_REQUEST_DISASSEMBLE_PC:
        case TGDB_REQUEST_DISASSEMBLE_FUNC:
            break;
        default:
            break;
    }

    free(request_ptr);
    request_ptr = NULL;
}


/* Creating and Destroying a libtgdb context. {{{*/

struct tgdb *tgdb_initialize(tgdb_callbacks callbacks)
{
    /* Initialize the libtgdb context */
    struct tgdb *tgdb = initialize_tgdb_context(callbacks);

#ifndef _WIN32
    // create the new ui pty pair
    // initialize the size to the gdb window size
    tgdb->new_ui_pty_pair = pty_pair_create();
    if (!tgdb->new_ui_pty_pair) {
        clog_error(CLOG_CGDB, "pty_pair_create failed");
        return NULL;
    }

    tgdb->gdb_mi_ui_fd = pty_pair_get_masterfd(tgdb->new_ui_pty_pair);
    tty_set_echo(tgdb->gdb_mi_ui_fd, 0);
#endif

    /* Need to get source information before breakpoint information otherwise
     * the TGDB_UPDATE_BREAKPOINTS event will be ignored in process_commands()
     * because there are no source files to add the breakpoints to.
     */
    tgdb_request_current_location(tgdb);

    /* gdb may already have some breakpoints when it starts. This could happen
     * if the user puts breakpoints in there .gdbinit.
     * This makes sure that TGDB asks for the breakpoints on start up.
     */
    tgdb_issue_request(tgdb, TGDB_REQUEST_BREAKPOINTS, true);

    /**
     * Query if disassemble supports the /s flag
     */
    tgdb_issue_request(tgdb, TGDB_REQUEST_DATA_DISASSEMBLE_MODE_QUERY, true);

    return tgdb;
}

int tgdb_start_gdb(struct tgdb *tgdb,
        const char *debugger, int argc, char **argv,
        int gdb_win_rows, int gdb_win_cols, int *gdb_console_fd,
        int *gdb_mi_fd)
{
#ifdef _WIN32
    /* MI over anonymous pipes: gdb shares our console and runs in its
     * own process group (CTRL_BREAK interrupt path); its stdio is the
     * pipe pair. */
    char **gdb_argv;
    int i;

    gdb_argv = (char **) cgdb_malloc(sizeof(char *) * (argc + 2));
    gdb_argv[0] = (char *) debugger;
    for (i = 0; i < argc; i++)
        gdb_argv[i + 1] = argv[i];
    gdb_argv[argc + 1] = NULL;

    if (win_spawn_debugger(gdb_argv, &tgdb->win_gdb) == -1) {
        free(gdb_argv);
        return -1;
    }
    free(gdb_argv);

    tgdb->debugger_stdin = tgdb->win_gdb.stdin_fd;
    tgdb->debugger_stdout = tgdb->win_gdb.stdout_fd;
    tgdb->gdb_mi_ui_fd = tgdb->win_gdb.stdout_fd;
    tgdb->debugger_pid = (pid_t) tgdb->win_gdb.pid;

    /* Create the inferior's tty pipe now so gdb can be told about it
     * with the first queued commands (the name only matters once the
     * inferior actually starts). */
    if (win_inferior_tty_create(&tgdb->win_tty, tgdb->win_gdb.pid) == 0) {
        tgdb_request_ptr request_ptr;

        tgdb->win_inf_used = 1;
        request_ptr = (tgdb_request_ptr)
                cgdb_malloc(sizeof(struct tgdb_request));
        request_ptr->header = TGDB_REQUEST_TTY;
        request_ptr->choice.tty_command.slavename =
                cgdb_strdup(tgdb->win_tty.name);
        tgdb_run_or_queue_request(tgdb, request_ptr, true);
    } else {
        clog_error(CLOG_CGDB, "win_inferior_tty_create failed; inferior "
                "output will pollute the MI channel");
    }

    /* On Windows there is a single channel: both descriptors alias the
     * same read pipe; the main loop only watches gdb_mi_fd. */
    *gdb_console_fd = tgdb->debugger_stdout;
    *gdb_mi_fd = tgdb->gdb_mi_ui_fd;

    (void) gdb_win_rows;
    (void) gdb_win_cols;
    return 0;
#else
    tgdb->debugger_pid = invoke_debugger(debugger, argc, argv,
            gdb_win_rows, gdb_win_cols, &tgdb->debugger_stdin,
            &tgdb->debugger_stdout, tgdb->new_ui_pty_pair);

    /* Couldn't invoke process */
    if (tgdb->debugger_pid == -1)
        return -1;

    *gdb_console_fd = tgdb->debugger_stdout;
    *gdb_mi_fd = tgdb->gdb_mi_ui_fd;

    return 0;
#endif
}

int tgdb_shutdown(struct tgdb *tgdb)
{
    delete tgdb->undefined_new_ui_command;

    tgdb_request_ptr_list::iterator iter = tgdb->command_requests->begin();
    for (; iter != tgdb->command_requests->end(); ++iter) {
        tgdb_request_destroy(*iter);
    }

    delete tgdb->command_requests;
    tgdb->command_requests = 0;

#ifdef _WIN32
    /* Closes the pipe fds (debugger_stdin/stdout alias them) and the
     * process handle; stops the inferior reader thread. */
    if (tgdb->win_inf_used) {
        win_inferior_tty_destroy(&tgdb->win_tty);
        tgdb->win_inf_used = 0;
    }
    win_process_destroy(&tgdb->win_gdb);
    tgdb->debugger_stdin = -1;
    tgdb->debugger_stdout = -1;
    tgdb->gdb_mi_ui_fd = -1;
    win_edit_destroy(&tgdb->win_edit);
#else
    if (tgdb->debugger_stdin != -1) {
        cgdb_close(tgdb->debugger_stdin);
        tgdb->debugger_stdin = -1;
    }
#endif

    gdbwire_destroy(tgdb->wire);

    delete tgdb;

    return 0;
}

void tgdb_close_logfiles()
{
    clog_info(CLOG_CGDB, "Closing logfile.");
    clog_free(CLOG_CGDB_ID);

    clog_info(CLOG_GDBIO, "Closing logfile.");
    clog_free(CLOG_GDBIO_ID);

    clog_info(CLOG_GDBMIIO, "Closing logfile.");
    clog_free(CLOG_GDBMIIO_ID);
}

/* }}}*/

static const char *tgdb_get_client_command(struct tgdb *tgdb,
        enum tgdb_command_type c)
{
    switch (c) {
        case TGDB_CONTINUE:
            return "continue";
        case TGDB_FINISH:
            return "finish";
        case TGDB_NEXT:
            return "next";
        case TGDB_NEXTI:
            return "nexti";
        case TGDB_START:
            return "start";
        case TGDB_RUN:
            return "run";
        case TGDB_KILL:
            return "kill";
        case TGDB_STEP:
            return "step";
        case TGDB_STEPI:
            return "stepi";
        case TGDB_UNTIL:
            return "until";
        case TGDB_UP:
            return "up";
        case TGDB_DOWN:
            return "down";
    }

    return NULL;
}

static char *tgdb_break_call(const char *action,
    const char *file, int line, uint64_t addr)
{
    if (file)
        return sys_aprintf("%s \"%s\":%d", action, file, line);

    return sys_aprintf("%s *0x%" PRIx64, action, addr);
}


static char *tgdb_client_modify_breakpoint_call(struct tgdb *tgdb,
    const char *file, int line, uint64_t addr, enum tgdb_breakpoint_action b)
{
    const char *action;

    switch (b)
    {
    default:
    case TGDB_BREAKPOINT_ADD:
        action = "break";
        break;
    case TGDB_BREAKPOINT_DELETE:
        action ="clear";
        break;
    case TGDB_TBREAKPOINT_ADD:
        action = "tbreak";
        break;
    }

    return tgdb_break_call(action, file, line, addr);
}

/*******************************************************************************
 * This is the main_loop stuff for tgdb-base
 ******************************************************************************/

/**
 * Run a command request if gdb is idle, otherwise queue it.
 *
 * @param tgdb
 * The TGDB context to use.
 *
 * @param request
 * The command request
 *
 * @param priority
 * True if this is a priority request, false otherwise.
 */
void tgdb_run_or_queue_request(struct tgdb *tgdb,
        struct tgdb_request *request, bool priority)
{
    int can_issue;

#ifdef _WIN32
    /* Windows: debugger commands run through the MI channel too
     * (-interpreter-exec console) and produce MI records, so they must
     * serialize like every other request; otherwise response
     * attribution (current_request_type) breaks. */
    can_issue = tgdb->is_gdb_ready_for_next_command;
#else
    // Debugger commands currently get executed in the gdb console
    // rather than the gdb mi channel. The gdb console is no longer
    // queued by CGDB, rather CGDB passes everything along to it that the
    // user types. So always issue debugger commands for now.
    if (request->header == TGDB_REQUEST_DEBUGGER_COMMAND) {
        can_issue = 1;
    } else {
        can_issue = tgdb->is_gdb_ready_for_next_command;
    }
#endif

    if (can_issue) {
        tgdb_run_request(tgdb, request);
    } else {
        if (priority) {
            tgdb->command_requests->push_front(request);
        } else {
            tgdb->command_requests->push_back(request);
        }
    }
}

int tgdb_get_gdb_command(struct tgdb *tgdb, tgdb_request_ptr request,
        std::string &command);

/**
 * Send a command to gdb.
 *
 * @param tgdb
 * An instance of tgdb
 *
 * @param request 
 * The command request to issue the command for
 */
static void tgdb_run_request(struct tgdb *tgdb, struct tgdb_request *request)
{
    std::string command;

    tgdb->is_gdb_ready_for_next_command = 0;

#ifdef _WIN32
    tgdb->win_at_prompt = 0;
#endif

    tgdb_get_gdb_command(tgdb, request, command);

    /* Add a newline to the end of the command if it doesn't exist */
    if (*command.rbegin() != '\n') {
        command.push_back('\n');
    }

    /* Send what we're doing to log file */
    std::string str = sys_quote_nonprintables(command.c_str(), -1);
    clog_debug(CLOG_GDBMIIO, "%s", str.c_str());

    /* A command for the debugger */
    tgdb_commands_set_current_request_type(tgdb, request->header);

    if (request->header == TGDB_REQUEST_DEBUGGER_COMMAND ||
            request->header == TGDB_REQUEST_CONSOLE_COMMAND) {
#ifdef _WIN32
        /* Translate the console command into an MI interpreter-exec;
         * the response comes back as console stream records plus ^done
         * (M0 E1/E8), so this request must serialize like the rest. */
        std::string payload = command;
        if (!payload.empty() && payload[payload.size()-1] == '\n')
            payload.erase(payload.size()-1);
        std::string mi = "-interpreter-exec console \"";
        mi += win_mi_quote(payload);
        mi += "\"\n";
        io_writen(tgdb->debugger_stdin, mi.c_str(), mi.size());
#else
        // since debugger commands are sent to the debugger's stdin
        // and not to the new-ui mi window, then we don't have to wait
        // for gdb to respond with an mi prompt. CGDB can send as many
        // commands as it likes, just as if the user typed it at the console
        tgdb->is_gdb_ready_for_next_command = 1;
        io_writen(tgdb->debugger_stdin, command.c_str(), command.size());
#endif
    } else {
#ifdef _WIN32
        /* The MI write side of the pipe pair (gdb_mi_ui_fd is the read
         * side on Windows). */
        io_writen(tgdb->debugger_stdin, command.c_str(), command.size());
#else
        io_writen(tgdb->gdb_mi_ui_fd, command.c_str(), command.size());
#endif
    }

    tgdb_request_destroy(request);
}

/**
 * TGDB will search it's command queue's and determine what the next command
 * to deliever to GDB should be.
 *
 * \return
 * 0 on success, -1 on error
 */
static void tgdb_unqueue_and_deliver_command(struct tgdb *tgdb)
{
    if (tgdb->command_requests->size() > 0) {
        struct tgdb_request *request = tgdb->command_requests->front();
        tgdb->command_requests->pop_front();
        tgdb_run_request(tgdb, request);
        // TODO: free request?
    }
}

int tgdb_send_char(struct tgdb *tgdb, char c)
{
#ifdef _WIN32
    /* The user's keystrokes feed the synthesized console (line editor,
     * or raw forwarding to the inferior while it runs). */
    return tgdb_win_editor_char(tgdb, c);
#else
    if (io_write_byte(tgdb->debugger_stdin, c) == -1) {
        clog_error(CLOG_CGDB, "io_write_byte failed");
        return -1;
    }

    return 0;
#endif
}

/**
 * TGDB is going to quit.
 *
 * \param tgdb
 * The tgdb context
 *
 * \return
 * 0 on success or -1 on error
 */
static int tgdb_add_quit_command(struct tgdb *tgdb, bool new_ui_unsupported)
{
    struct tgdb_response *response;
    response = tgdb_create_response(TGDB_QUIT);
    response->choice.quit.new_ui_unsupported = new_ui_unsupported;
    tgdb_send_response(tgdb, response);
    return 0;
}

/**
 * Called to process the sigchld signal, and clean up zombies!
 *
 * @param tgdb
 * The tgdb instance
 *
 * @return
 * 0 on success or -1 on error
 */
#ifndef _WIN32
static int tgdb_handle_sigchld(struct tgdb *tgdb)
{
    int result = 0;
    int status;
    int waitpid_result;

    do {
        waitpid_result = waitpid(tgdb->debugger_pid, &status, WNOHANG);
        if (waitpid_result == -1) {
            result = -1;
            clog_error(CLOG_CGDB, "waitpid error %d %s",
                    errno, strerror(errno));
        }
    } while (waitpid_result == 0);

    return result;
}
#endif /* !_WIN32 */

/**
 * If the user typed control_c at the prompt, clear the queues.
 *
 * @param tgdb
 * The tgdb instance to work on
 */
static void tgdb_handle_control_c(struct tgdb *tgdb)
{
    if (tgdb->control_c) {
        tgdb_request_ptr_list::iterator iter = tgdb->command_requests->begin();
        for (; iter != tgdb->command_requests->end(); ++iter) {
            tgdb_request_destroy(*iter);
        }
        tgdb->command_requests->clear();

        tgdb->control_c = 0;
    }
}

// Search for the string
//   Undefined command: "new-ui"
// to determine if this gdb supports the new-ui command or not.
// If the string is found, set tgdb->gdb_supports_new_ui_command to false
#ifndef _WIN32
static void tgdb_search_for_unsupported_new_ui_message(
        struct tgdb *tgdb, const std::string &console_output)
{
    static const char *new_ui_text = "Undefined command: \"new-ui\".";
    
    (*tgdb->undefined_new_ui_command) += console_output;

    std::istringstream ss(*tgdb->undefined_new_ui_command);
    std::string line;
    while (getline(ss, line)) {
        if (line.find(new_ui_text) == 0) {
            tgdb->gdb_supports_new_ui_command = false;
            break;
        }
    }

    // Remove everything up to the last newline character so that
    // only newly added new lines are searched
    size_t pos = tgdb->undefined_new_ui_command->find_last_of("\n");
    if (pos != std::string::npos) {
        *tgdb->undefined_new_ui_command =
                tgdb->undefined_new_ui_command->substr(pos);
    }
}
#endif /* !_WIN32 */

int tgdb_process(struct tgdb * tgdb, int fd)
{
    const int n = 4096;
    static char buf[n];
    ssize_t size;
    int result = 0;

    // If ^c has been typed at the prompt, clear the queues
    tgdb_handle_control_c(tgdb);

#ifdef TGDB_WIN_TRACE
    fprintf(stderr, "@tp: enter fd=%d\n", fd);
#endif
    size = io_read(fd, buf, n);
#ifdef TGDB_WIN_TRACE
    fprintf(stderr, "@tp: io_read=%d\n", (int) size);
#endif
    if (size < 0) {
        // Error reading from GDB
        clog_error(CLOG_CGDB, "Error reading from gdb's stdout, closing down");
        result = -1;
        tgdb_add_quit_command(tgdb, false);
    } else if (size == 0) {
        // Read EOF from GDB
        clog_info(CLOG_GDBIO, "read EOF from GDB, closing down");
        tgdb_add_quit_command(tgdb, false);
    } else {
#ifdef _WIN32
        /* Single MI channel: everything (including user command output
         * via console stream records) arrives here and is parsed by
         * gdbwire. There is no separate console fd. */
        {
            std::string msg(buf, size);
            clog_debug(CLOG_GDBMIIO, "%s", msg.c_str());
#ifdef TGDB_WIN_TRACE
            fprintf(stderr, "@tp: pushing %d bytes to gdbwire\n",
                    (int) size);
#endif
            tgdb_commands_process(tgdb, msg);
#ifdef TGDB_WIN_TRACE
            fprintf(stderr, "@tp: gdbwire done\n");
#endif
        }
#else
        if (fd == tgdb->debugger_stdout) {
            // Read some GDB console output, process it
            std::string str = sys_quote_nonprintables(buf, size);
            clog_debug(CLOG_GDBIO, "%s", str.c_str());
            std::string msg(buf, size);

            // Determine if this gdb supports the new-ui command.
            // If it does not, send the quit command to alert the user
            // that they need a newer gdb.
            tgdb_search_for_unsupported_new_ui_message(tgdb, msg);
            if (!tgdb->gdb_supports_new_ui_command) {
                tgdb_add_quit_command(tgdb, true);
            }

            tgdb->callbacks.console_output_callback(
                    tgdb->callbacks.context, msg);
        } else if (fd == tgdb->gdb_mi_ui_fd){
            // Read some GDB console output, process it
            std::string msg(buf, size);
            clog_debug(CLOG_GDBMIIO, "%s", msg.c_str());
            tgdb_commands_process(tgdb, msg);
        } else {
        }
#endif
    }

    return result;
}

/* Getting Data out of TGDB {{{*/

struct tgdb_response *tgdb_create_response(enum tgdb_response_type header)
{
    struct tgdb_response *response;

    response = (struct tgdb_response *)cgdb_calloc(1, sizeof(struct tgdb_response));
    response->header = header;

    return response;
}

static int tgdb_delete_response(struct tgdb_response *com)
{
    if (!com)
        return -1;

    switch (com->header) {
        case TGDB_QUIT:
            break;
    }

    free(com);
    com = NULL;
    return 0;
}

void tgdb_send_response(struct tgdb *tgdb, struct tgdb_response *response)
{
    tgdb->callbacks.command_response_callback(
            tgdb->callbacks.context, response);
    tgdb_delete_response(response);
}

int tgdb_resize_console(struct tgdb *tgdb, int rows, int cols)
{
#ifdef _WIN32
    /* The MI channel is a pipe; gdb has no terminal to resize. The
     * synthesized console wraps at the cgdb window width instead. */
    (void) rows;
    (void) cols;
    return 0;
#else
    struct winsize size;
    int result;

    if (tgdb->debugger_stdin == -1) {
        return 0;
    }

    size.ws_row = rows;
    size.ws_col = cols;
    size.ws_xpixel = 0;
    size.ws_ypixel = 0;

    result = ioctl(tgdb->debugger_stdin, TIOCSWINSZ, &size);
    if (result == -1) {
        clog_error(CLOG_CGDB, "ioctl failed");
    }

    return 0;
#endif
}

/* }}}*/

/* Functional commands {{{ */

/* Request {{{*/

void tgdb_request_inferiors_source_files(struct tgdb * tgdb)
{
    tgdb_request_ptr request_ptr;

    request_ptr = (tgdb_request_ptr)cgdb_malloc(sizeof (struct tgdb_request));

    request_ptr->header = TGDB_REQUEST_INFO_SOURCES;

    tgdb_run_or_queue_request(tgdb, request_ptr, false);
}

void tgdb_request_current_location(struct tgdb * tgdb)
{
    tgdb_request_ptr request_ptr;

    request_ptr = (tgdb_request_ptr)cgdb_malloc(sizeof (struct tgdb_request));

    request_ptr->header = TGDB_REQUEST_INFO_FRAME;

    tgdb_run_or_queue_request(tgdb, request_ptr, true);
}

void tgdb_request_breakpoints(struct tgdb * tgdb)
{
    tgdb_request_ptr request_ptr;

    request_ptr = (tgdb_request_ptr)cgdb_malloc(sizeof (struct tgdb_request));

    request_ptr->header = TGDB_REQUEST_BREAKPOINTS;

    tgdb_run_or_queue_request(tgdb, request_ptr, false);
}

void
tgdb_request_run_debugger_command(struct tgdb * tgdb, enum tgdb_command_type c)
{
    tgdb_request_ptr request_ptr;

    request_ptr = (tgdb_request_ptr)cgdb_malloc(sizeof (struct tgdb_request));

    request_ptr->header = TGDB_REQUEST_DEBUGGER_COMMAND;
    request_ptr->choice.debugger_command.c = c;

    tgdb_run_or_queue_request(tgdb, request_ptr, false);
}

void
tgdb_request_modify_breakpoint(struct tgdb *tgdb, const char *file, int line,
    uint64_t addr, enum tgdb_breakpoint_action b)
{
    tgdb_request_ptr request_ptr;

    request_ptr = (tgdb_request_ptr)cgdb_malloc(sizeof (struct tgdb_request));

    request_ptr->header = TGDB_REQUEST_MODIFY_BREAKPOINT;
    request_ptr->choice.modify_breakpoint.file = file ? cgdb_strdup(file) : NULL;
    request_ptr->choice.modify_breakpoint.line = line;
    request_ptr->choice.modify_breakpoint.addr = addr;
    request_ptr->choice.modify_breakpoint.b = b;

    tgdb_run_or_queue_request(tgdb, request_ptr, false);
}

void tgdb_request_disassemble_pc(struct tgdb *tgdb, int lines)
{
    tgdb_request_ptr request_ptr;

    request_ptr = (tgdb_request_ptr)cgdb_malloc(sizeof (struct tgdb_request));
    request_ptr->header = TGDB_REQUEST_DISASSEMBLE_PC;

    request_ptr->choice.disassemble.lines = lines;

    tgdb_run_or_queue_request(tgdb, request_ptr, false);
}

void tgdb_request_disassemble_func(struct tgdb *tgdb,
        enum disassemble_func_type type)
{
    tgdb_request_ptr request_ptr;

    request_ptr = (tgdb_request_ptr)cgdb_malloc(sizeof (struct tgdb_request));
    request_ptr->header = TGDB_REQUEST_DISASSEMBLE_FUNC;

    request_ptr->choice.disassemble_func.raw = (type == DISASSEMBLE_FUNC_RAW_INSTRUCTIONS);
    request_ptr->choice.disassemble_func.source = (type == DISASSEMBLE_FUNC_SOURCE_LINES);

    tgdb_run_or_queue_request(tgdb, request_ptr, false);
}

void tgdb_request_until_line(struct tgdb *tgdb,
        const char *file, int line, uint64_t addr)
{
    tgdb_request_ptr request_ptr;

    request_ptr = (tgdb_request_ptr)cgdb_malloc(sizeof (struct tgdb_request));
    request_ptr->header = TGDB_REQUEST_UNTIL_LINE;

    request_ptr->choice.until_line.file = file ? cgdb_strdup(file) : NULL;
    request_ptr->choice.until_line.line = line;
    request_ptr->choice.until_line.addr = addr;

    tgdb_run_or_queue_request(tgdb, request_ptr, false);
}

/* }}}*/

/* Process {{{*/

int tgdb_get_gdb_command(struct tgdb *tgdb, tgdb_request_ptr request,
        std::string &command)
{
    char *str;

    if (!tgdb || !request) {
        return -1;
    }

    switch (request->header) {
        case TGDB_REQUEST_INFO_SOURCES:
            command = "-file-list-exec-source-files\n";
            break;
        case TGDB_REQUEST_INFO_SOURCE_FILE:
            command = "-file-list-exec-source-file\n";
            break;
        case TGDB_REQUEST_BREAKPOINTS:
            command = "-break-info\n";
            break;
        case TGDB_REQUEST_TTY:
#ifdef _WIN32
            /* Raw backslashes survive the MI argument parser (M0 E2b). */
            str = sys_aprintf("-gdb-set inferior-tty %s\n",
                    request->choice.tty_command.slavename);
#else
            str = sys_aprintf("-inferior-tty-set %s\n",
                    request->choice.tty_command.slavename);
#endif
            command = str;
            free(str);
            str = NULL;
            break;
        case TGDB_REQUEST_INFO_FRAME:
            command = "-stack-info-frame\n";
            break;
        case TGDB_REQUEST_DATA_DISASSEMBLE_MODE_QUERY:
            command = "-data-disassemble -s 0 -e 0 -- 4\n";
            break;
        case TGDB_REQUEST_DEBUGGER_COMMAND:
            command = tgdb_get_client_command(tgdb,
                    request->choice.debugger_command.c);

            break;
        case TGDB_REQUEST_CONSOLE_COMMAND:
            /* An arbitrary user-typed console command (Windows
             * synthesized console). */
            command = request->choice.console_command.command;
            break;
        case TGDB_REQUEST_MODIFY_BREAKPOINT:
            str = tgdb_client_modify_breakpoint_call(tgdb,
                    request->choice.modify_breakpoint.file,
                    request->choice.modify_breakpoint.line,
                    request->choice.modify_breakpoint.addr,
                    request->choice.modify_breakpoint.b);
            command = str;
            free(str);
            str = NULL;
            break;
        case TGDB_REQUEST_UNTIL_LINE:
            str = tgdb_break_call("until",
                request->choice.until_line.file,
                request->choice.until_line.line,
                request->choice.until_line.addr);
            command = str;
            free(str);
            str = NULL;
            break;
        case TGDB_REQUEST_DISASSEMBLE_PC:
            str = sys_aprintf("x/%di $pc\n", request->choice.disassemble.lines);
            command = str;
            free(str);
            str = NULL;
            break;
        case TGDB_REQUEST_DISASSEMBLE_FUNC: {
            /* GDB 7.11 adds /s command to disassemble

            https://sourceware.org/git/gitweb.cgi?p=binutils-gdb.git;a=commit;h=6ff0ba5f7b8a2b10642bbb233a32043595c55670
                The "source centric" /m option to the disassemble command is
                often unhelpful, e.g., in the presence of optimized code.
                This patch adds a /s modifier that is better.
                For one, /m only prints instructions from the originating
                source file, leaving out instructions from
                e.g., inlined functions from other files.

            disassemble
                 /m: source lines included
                 /s: source lines included, output in pc order (7.10 and higher)
                 /r: raw instructions included in hex
                 single argument: function surrounding is dumped
                 two arguments: start,end or start,+length
                 disassemble 'driver.cpp'::main
                 interp mi "disassemble /s 'driver.cpp'::main,+10"
                 interp mi "disassemble /r 'driver.cpp'::main,+10"
             */
            const char *data = NULL;
            if (request->choice.disassemble_func.raw) {
                data = "/r";
            } else if (request->choice.disassemble_func.source &&
                    tgdb_commands_disassemble_supports_s_mode(tgdb)) {
                data = "/s";
            }
            str = sys_aprintf("disassemble%s%s\n",
                    data ? " " : "", data ? data : "");
            command = str;
            free(str);
            str = NULL;
            break;
        }
    }

    return 0;
}

/* }}}*/

/* }}} */

#ifdef _WIN32
/* Windows main-loop helpers {{{ */

/* Drain whatever the inferior wrote to its tty pipe and display it. */
int tgdb_process_inferior(struct tgdb *tgdb)
{
    std::string out;
    int n;

    if (!tgdb->win_inf_used)
        return 0;

    n = win_inferior_tty_drain(&tgdb->win_tty, &out);
    if (n > 0)
        tgdb_win_output(tgdb, out);

    return n;
}

/* Waitable event signaled when the inferior produced output. */
void *tgdb_inferior_event_handle(struct tgdb *tgdb)
{
    if (!tgdb->win_inf_used)
        return NULL;

    return win_inferior_tty_event(&tgdb->win_tty);
}

/* 1 while gdb runs, 0 once it exited. */
int tgdb_debugger_alive(struct tgdb *tgdb)
{
    return win_process_alive(&tgdb->win_gdb);
}

/* }}} */
#else
int tgdb_process_inferior(struct tgdb *tgdb)
{
    (void) tgdb;
    return 0;
}

void *tgdb_inferior_event_handle(struct tgdb *tgdb)
{
    (void) tgdb;
    return NULL;
}

int tgdb_debugger_alive(struct tgdb *tgdb)
{
    (void) tgdb;
    return 1;
}
#endif

/* Signal Handling Support {{{*/

int tgdb_signal_notification(struct tgdb *tgdb, int signum)
{
#ifdef _WIN32
    if (signum == SIGINT) {
        /* ^c: interrupt the inferior via gdb's process group. The
         * command queue is flushed by tgdb_handle_control_c on the
         * next tgdb_process() call. SIGCHLD never arrives on Windows;
         * the main loop polls gdb liveness instead. SIGQUIT arrives as
         * the raw 0x1c byte and is intercepted upstream like ^c. */
        tgdb->control_c = 1;
        return win_interrupt_debugger(&tgdb->win_gdb);
    }

    return 0;
#else
    struct termios t;
    cc_t *sig_char = NULL;

    tcgetattr(tgdb->debugger_stdin, &t);

    if (signum == SIGINT) {     /* ^c */
        tgdb->control_c = 1;
        sig_char = &t.c_cc[VINTR];
        if (write(tgdb->debugger_stdin, sig_char, 1) < 1)
            return -1;
    } else if (signum == SIGQUIT) { /* ^\ */
        sig_char = &t.c_cc[VQUIT];
        if (write(tgdb->debugger_stdin, sig_char, 1) < 1)
            return -1;
    } else if (signum == SIGCHLD) {
        // GDB has died, clean up the zombie and send the quit command
        tgdb_handle_sigchld(tgdb);
        tgdb_add_quit_command(tgdb, false);
    }

    return 0;
#endif
}

/* }}}*/
