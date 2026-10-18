"""vim-style key mapping engine -- a port of lib/kui.

Architecture mirrors the C version (doc/reference.md §6):

    KeyTree    prefix tree, stateful matcher, no clock
    MapSet     registry of `map lhs rhs` entries + the tree built from them
    KeyContext read loop: drives a MapSet, handles pushback and recursive
               expansion, owns the timeout
    KeyManager two stacked KeyContexts -- terminal_keys (escape sequences ->
               special keys) feeding normal_keys (user :map/:imap)

Two deliberate deviations from the C version, both to make the engine
deterministic under test:

  * The read callback returns None for "no input available right now" instead
    of blocking. KeyContext treats None as end-of-input and finalises the
    match, which is what the C version does on timeout. A real blocking reader
    can be layered on top via `blocking_reader()`.
  * Keys are Python ints (0..255 for bytes, >=10000 for special keys) rather
    than a NUL-terminated C array.
"""

from collections import deque

from . import keycodes as kc

# Match states, matching kui_tree_state_type
MATCHING = "matching"
FOUND = "found"
NOT_FOUND = "not_found"

# Returned by KeyContext.getkey() when a partial match is still possible and
# defer_on_empty is set: the caller should wait for more input (or for
# `timeoutlen` to expire) instead of emitting the keys literally.
PENDING = "pending"


class KeyTree:
    """Prefix tree over key ints. Port of lib/kui/kui_tree.cpp.

    Longest-match semantics: when a node both terminates a map and has
    children, the match is remembered but matching continues, so `map abc` and
    `map abcde` can coexist and typing `abcd` then stopping yields `abc`.
    """

    class Node:
        __slots__ = ("key", "value", "children")

        def __init__(self, key):
            self.key = key
            self.value = None       # list[int] when a map terminates here
            self.children = {}

    def __init__(self):
        self.root = KeyTree.Node(None)
        # state
        self._state = MATCHING
        self._node = self.root
        self._found = None          # remembered intermediate match
        self._found_len = 0         # how many pushed keys that match consumed

    def reset(self):
        self._state = MATCHING
        self._node = self.root
        self._found = None
        self._found_len = 0

    @property
    def state(self):
        return self._state

    def add(self, keys, value):
        node = self.root
        for k in keys:
            node = node.children.setdefault(k, KeyTree.Node(k))
        node.value = list(value)

    def remove(self, keys):
        """Delete a map. Prunes nodes that neither terminate a map nor have
        children (kui_tree.cpp:95-122). Returns True if something was removed.
        """
        path = [self.root]
        node = self.root
        for k in keys:
            nxt = node.children.get(k)
            if nxt is None:
                return False
            path.append(nxt)
            node = nxt
        if node.value is None:
            return False
        node.value = None
        # prune upwards
        for i in range(len(path) - 1, 0, -1):
            child, parent = path[i], path[i - 1]
            if child.value is None and not child.children:
                del parent.children[child.key]
            else:
                break
        return True

    def push_key(self, key, depth=None):
        """Advance the matcher by one key. Returns the new state.

        `depth` is the number of keys consumed so far *including* this one; it
        is recorded alongside any intermediate hit so the caller can give back
        the keys that turned out to be past the end of the match.
        """
        if self._state != MATCHING:
            return self._state
        child = self._node.children.get(key)
        if child is None:
            self._state = NOT_FOUND
            return self._state
        self._node = child
        if child.value is not None:
            self._found = child.value
            self._found_len = depth
        if not child.children:
            # no way to extend further: this is the longest match
            self._state = FOUND
            self._found = child.value
            self._found_len = depth
        return self._state

    def finalize(self):
        """Input stopped, either by timeout or by a mismatch.

        Both promote a remembered intermediate hit to FOUND
        (doc/reference.md §6.2: "输入停止（超时/失配）时若有中途命中则提升为
        FOUND"). This is what makes `\\033o` yield ESC followed by 'o' rather
        than swallowing the ESC.
        """
        if self._state in (MATCHING, NOT_FOUND) and self._found is not None:
            self._state = FOUND
        return self._state

    @property
    def found_value(self):
        return self._found

    @property
    def found_len(self):
        return self._found_len


class MapSet:
    """Registry of maps plus the KeyTree built from them.

    Port of lib/kui/kui_map_set.cpp. Keys are the *original* notation strings
    so that `map abc def` and `map <C-a>b def` are distinct entries and
    `unmap` can find them again.
    """

    def __init__(self):
        self._maps = {}             # lhs string -> (lhs keys, rhs keys)
        self.tree = KeyTree()

    def __len__(self):
        return len(self._maps)

    def __contains__(self, lhs):
        return lhs in self._maps

    def entries(self):
        return sorted(self._maps.items())

    def add(self, lhs, rhs):
        """Register `map lhs rhs`. Re-adding the same lhs replaces it."""
        lhs_keys = kc.string_to_keys(lhs)
        rhs_keys = kc.string_to_keys(rhs)
        if not lhs_keys:
            raise ValueError("empty map lhs")
        if lhs in self._maps:
            self.tree.remove(lhs_keys)
        self._maps[lhs] = (lhs_keys, rhs_keys)
        self.tree.add(lhs_keys, rhs_keys)
        return True

    def remove(self, lhs):
        if lhs not in self._maps:
            return False
        lhs_keys, _ = self._maps.pop(lhs)
        self.tree.remove(lhs_keys)
        return True

    def clear(self):
        self._maps.clear()
        self.tree = KeyTree()


def terminal_map_set(extra_sequences=()):
    """The fixed map set that folds terminal escape sequences into keys.

    This is the C version's terminal_keys layer (kui_term's
    hard_coded_bindings + terminfo sequences). Each sequence becomes a map
    from its bytes to the single special key.
    """
    ms = MapSet()
    seqs = list(kc.HARD_CODED_BINDINGS) + list(kc.TERM_SEQUENCE_DEFAULTS)
    seqs += list(extra_sequences)
    for key, seq in seqs:
        lhs_keys = [ord(c) for c in seq]
        # Register by a synthetic lhs; the tree is what matters here.
        lhs = "\0seq:%s" % seq
        ms._maps[lhs] = (lhs_keys, [key])
        ms.tree.add(lhs_keys, [key])
    return ms


class KeyContext:
    """Read loop over one MapSet. Port of lib/kui/kui_ctx.cpp.

    `read()` must return an int key, or None when no input is available.
    """

    def __init__(self, map_set, read, timeout=None, name="",
                 defer_on_empty=False):
        self.map_set = map_set
        self.read = read
        self.timeout = timeout
        self.name = name
        self.buffer = deque()       # pushback queue, consumed front-first
        self.tree = map_set.tree
        # When True, running out of input mid-match returns PENDING instead of
        # finalising. An event-loop-driven caller sets this and finalises on a
        # timer; a blocking caller leaves it False and gets timeout semantics.
        self.defer_on_empty = defer_on_empty

    def set_map_set(self, map_set):
        self.map_set = map_set
        self.tree = map_set.tree
        self.buffer.clear()

    def can_get_key(self):
        return bool(self.buffer)

    def getkey(self):
        """Return the next key after mapping, None if input is exhausted, or
        PENDING if a partial match should be given more time.

        Recursive expansion: a matched map's replacement is pushed to the FRONT
        of the buffer and re-enters matching, so `map first second` plus
        `map second third` turns `first` into `third` (kui_ctx.cpp:241-256).
        """
        while True:
            key = self._next_key()
            if key is None:
                return None

            self.tree.reset()
            consumed = [key]
            state = self.tree.push_key(key, depth=1)

            while state == MATCHING:
                nxt = self._next_key()
                if nxt is None:
                    if self.defer_on_empty:
                        # give the keys back and report that we are undecided
                        self.tree.reset()
                        self.buffer.extendleft(reversed(consumed))
                        return PENDING
                    # input stopped -> timeout path
                    state = self.tree.finalize()
                    break
                consumed.append(nxt)
                state = self.tree.push_key(nxt, depth=len(consumed))

            if state == NOT_FOUND:
                # a mismatch also promotes an intermediate hit
                state = self.tree.finalize()

            if state == FOUND and self.tree.found_value is not None:
                value = list(self.tree.found_value)
                used = self.tree.found_len or len(consumed)
                leftover = consumed[used:]
                self.tree.reset()
                # leftover first, then the replacement, so the replacement is
                # consumed next and itself re-enters matching (recursive maps)
                self.buffer.extendleft(reversed(leftover))
                self.buffer.extendleft(reversed(value))
                continue

            # nothing matched: emit the first key and push the rest back
            # (kui_ctx.cpp:46-67 update_buffer)
            self.tree.reset()
            self.buffer.extendleft(reversed(consumed[1:]))
            return consumed[0]

    def flush(self):
        """Force a pending partial match to resolve now (timeout expired)."""
        saved = self.defer_on_empty
        self.defer_on_empty = False
        try:
            return self.getkey()
        finally:
            self.defer_on_empty = saved

    def _next_key(self):
        if self.buffer:
            return self.buffer.popleft()
        return self.read()


class KeyManager:
    """The facade cgdb talks to. Port of lib/kui/kui_manager.cpp.

    Two stacked contexts:
      terminal_keys -- folds raw escape bytes into special keys
      normal_keys   -- applies the user's :map/:imap set, reading through
                       terminal_keys
    """

    def __init__(self, read_bytes, keycode_timeout=0.04,
                 mapping_timeout=1.0, extra_sequences=()):
        self.terminal_set = terminal_map_set(extra_sequences)
        self.terminal_keys = KeyContext(self.terminal_set, read_bytes,
                                        keycode_timeout, "terminal")
        self.normal_set = MapSet()
        self.insert_set = MapSet()
        self.normal_keys = KeyContext(self.normal_set,
                                      self.terminal_keys.getkey,
                                      mapping_timeout, "normal")
        self._insert_mode = False

    # --- map management ---------------------------------------------------
    def active_set(self):
        return self.insert_set if self._insert_mode else self.normal_set

    def set_insert_mode(self, on):
        if on == self._insert_mode:
            return
        self._insert_mode = on
        self.normal_keys.set_map_set(self.active_set())

    def add_map(self, lhs, rhs, insert=False):
        target = self.insert_set if insert else self.normal_set
        target.add(lhs, rhs)
        if (insert and self._insert_mode) or (not insert and not self._insert_mode):
            self.normal_keys.set_map_set(self.active_set())

    def remove_map(self, lhs, insert=False):
        target = self.insert_set if insert else self.normal_set
        ok = target.remove(lhs)
        if (insert and self._insert_mode) or (not insert and not self._insert_mode):
            self.normal_keys.set_map_set(self.active_set())
        return ok

    def refresh(self):
        """Re-point the normal layer at the active map set.

        Needed after mutating `normal_set` / `insert_set` directly rather than
        through add_map()/remove_map().
        """
        self.normal_keys.set_map_set(self.active_set())

    # --- key reading ------------------------------------------------------
    def getkey(self):
        return self.normal_keys.getkey()

    def can_get_key(self):
        return (self.normal_keys.can_get_key()
                or self.terminal_keys.can_get_key())

    def drain(self):
        """Yield keys until input is exhausted."""
        while True:
            k = self.getkey()
            if k is None:
                return
            yield k


def feed_keys(manager, keys):
    """Test helper: replace the manager's byte source with a fixed list."""
    pending = deque(keys)

    def read():
        return pending.popleft() if pending else None

    manager.terminal_keys.read = read
    return pending


def blocking_reader(fd, timeout):
    """Build a read() callback that blocks on a file descriptor.

    Used by the raw-byte input path; the prompt_toolkit app does not need it
    because prompt_toolkit decodes the terminal itself.
    """
    import select

    def read():
        r, _, _ = select.select([fd], [], [], timeout)
        if not r:
            return None
        data = b""
        try:
            import os
            data = os.read(fd, 1)
        except OSError:
            return None
        if not data:
            return None
        return data[0]

    return read
