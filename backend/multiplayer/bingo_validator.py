"""
bingo_validator.py
------------------
Server-side Bingo card generation and win validation.

The frontend NEVER decides who wins. Every claim is validated here against:
  - the exact card numbers stored by the server (bingo_cards collection)
  - the official list of numbers the server already called for that game

Winning patterns supported (same as the original UI):
  - any full row
  - any full column
  - any full diagonal
  - the four corners
The FREE center square ('★') always counts as matched.
"""
import random

COLS = ['B', 'I', 'N', 'G', 'O']
FREE = '★'


def column_range(col_index):
    """Numbers allowed in a column: B=1-15, I=16-30, ... O=61-75."""
    return range(col_index * 15 + 1, col_index * 15 + 16)


def generate_card_numbers():
    """
    Generate one valid 5x5 Bingo card as a flat list of 25 values.
    Index 12 (row 2, col 2) is the FREE center square.
    Column c (indexes c, c+5, c+10, c+15, c+20) contains 5 random numbers
    from the matching 15-number range (B=1-15, I=16-30, ... O=61-75).
    """
    card = [0] * 25
    for c in range(5):
        pool = list(column_range(c))
        random.shuffle(pool)
        for r in range(5):
            card[r * 5 + c] = pool[r]
    card[12] = FREE
    return card


def _build_lines():
    """All winning lines: 5 rows + 5 columns + 2 diagonals + 4 corners."""
    lines = []
    for r in range(5):                                    # rows
        lines.append(tuple(r * 5 + i for i in range(5)))
    for c in range(5):                                    # columns
        lines.append(tuple(c + 5 * i for i in range(5)))
    lines.append((0, 6, 12, 18, 24))                      # main diagonal
    lines.append((4, 8, 12, 16, 20))                      # anti diagonal
    lines.append((0, 4, 20, 24))                          # four corners
    return lines


WIN_LINES = _build_lines()


def _cell_matched(card, index, called):
    """
    A cell is matched when it is the FREE center or its number was called AND
    the call belongs to the SAME Bingo column (B/I/N/G/O). A call is always a
    letter+number pair, so the column of the call must equal the column of the
    cell - a number sitting in the wrong column is NEVER matched.
    """
    value = card[index]
    if value == FREE:
        return True
    if value not in called:
        return False
    call_col = (value - 1) // 15      # B=1-15 -> 0, I=16-30 -> 1, ...
    cell_col = index % 5              # flat list is row-major: column = ix % 5
    return call_col == cell_col


def get_completed_lines(card, called):
    """Return every winning line fully matched on this card."""
    completed = []
    for line in WIN_LINES:
        if all(_cell_matched(card, i, called) for i in line):
            completed.append(line)
    return completed


def check_bingo(card, called):
    """True when the card contains at least one completed winning line."""
    return bool(get_completed_lines(card, called))


def winning_cells(card, called):
    """All cell indexes that belong to a completed winning line (for display)."""
    cells = set()
    for line in get_completed_lines(card, called):
        cells.update(line)
    return cells
