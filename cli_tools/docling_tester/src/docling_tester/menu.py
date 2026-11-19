"""Arrow-key menus with numbered input outside a terminal."""

import curses
import sys

TITLE = "neurwerk automated tests - Docling"


def choose(title: str, options: list[str]) -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("\n" + title)
        for index, option in enumerate(options, 1):
            print(f"{index}. {option}")
        while True:
            answer = input("Number, then Enter: ").strip()
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return int(answer) - 1

    def screen(window: curses.window) -> int:
        selected = 0
        curses.curs_set(0)
        window.keypad(True)
        while True:
            window.erase()
            height, width = window.getmaxyx()
            span = max(1, width - 5)
            window.addnstr(0, 0, TITLE, span, curses.A_BOLD)
            if height > 3:
                window.addnstr(2, 0, title, span)
            capacity = max(1, height - 7)
            start = max(0, selected - capacity + 1)
            for offset, option in enumerate(options[start : start + capacity]):
                if offset + 4 >= height - 1:
                    break
                index = start + offset
                window.addnstr(
                    offset + 4,
                    0,
                    (" > " if index == selected else "   ") + option,
                    span,
                    curses.A_REVERSE if index == selected else curses.A_NORMAL,
                )
            if height > 2:
                window.addnstr(height - 2, 0, "↑ ↓ Navigate   Enter Select   q Back", span)
            window.refresh()
            key = window.getch()
            if key in (curses.KEY_UP, ord("k")):
                selected = (selected - 1) % len(options)
            elif key in (curses.KEY_DOWN, ord("j")):
                selected = (selected + 1) % len(options)
            elif key in (10, 13, curses.KEY_ENTER):
                return selected
            elif key in (27, ord("q")):
                return len(options) - 1

    return curses.wrapper(screen)


def select_many(title: str, options: list[str]) -> list[int]:
    selected: set[int] = set()
    while True:
        index = choose(
            title,
            [f"[{'x' if i in selected else ' '}] {option}" for i, option in enumerate(options)]
            + [f"Run selected ({len(selected)})", "Cancel"],
        )
        if index == len(options) + 1:
            return []
        if index == len(options):
            if selected:
                return sorted(selected)
        elif index in selected:
            selected.remove(index)
        else:
            selected.add(index)
