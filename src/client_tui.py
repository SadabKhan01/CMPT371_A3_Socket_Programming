"""Enhanced terminal dashboard for the SocketShare client.

The networking logic still lives in ``client.py``. This module only provides a
full-screen terminal UI on top of that existing behavior.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING, Any

from config import DOWNLOADS_PATH, PROJECT_ROOT, STORAGE_PATH
from utils import format_file_size

if TYPE_CHECKING:
    from client import SocketShareClient

MIN_WIDTH = 88
MIN_HEIGHT = 28
MAX_LOG_LINES = 250


class TerminalUiUnavailableError(RuntimeError):
    """Raised when the terminal dashboard cannot run in the current terminal."""


class SocketShareTerminalUI:
    """Curses-powered dashboard for browsing, uploading, and downloading files."""

    def __init__(self, client: "SocketShareClient", curses_module: Any) -> None:
        self.client = client
        self.curses = curses_module
        self.screen: Any | None = None
        self.log_lines: list[str] = []
        self.files: list[dict[str, Any]] = []
        self.selected_index = 0
        self.prompt_mode: str | None = None
        self.prompt_label = ""
        self.prompt_buffer = ""
        self.show_help_overlay = False
        self.should_exit = False

    def run(self) -> None:
        """Starts the full-screen terminal UI."""

        previous_handler = self.client.message_handler
        self.client.set_message_handler(self.log)
        try:
            self.curses.wrapper(self._main)
        finally:
            self.client.set_message_handler(previous_handler)

    def _main(self, stdscr: Any) -> None:
        self.screen = stdscr
        self.configure_terminal()
        self.log("Launching SocketShare terminal dashboard.")
        self.log("Tip: upload sample_files/demo.txt for a quick demo.")
        self.attempt_connection()

        while not self.should_exit:
            self.draw()

            try:
                key = stdscr.get_wch()
            except self.curses.error:
                continue

            if self.prompt_mode is not None:
                self.handle_prompt_key(key)
            else:
                self.handle_normal_key(key)

        if self.client.client_socket is not None:
            self.client.quit()

    def configure_terminal(self) -> None:
        """Configures curses defaults and color pairs when available."""

        assert self.screen is not None

        self.curses.cbreak()
        self.screen.keypad(True)
        self.screen.timeout(-1)

        try:
            self.curses.curs_set(0)
        except self.curses.error:
            pass

        if self.curses.has_colors():
            self.curses.start_color()
            try:
                self.curses.use_default_colors()
            except self.curses.error:
                pass

            self.curses.init_pair(1, self.curses.COLOR_CYAN, -1)
            self.curses.init_pair(2, self.curses.COLOR_GREEN, -1)
            self.curses.init_pair(3, self.curses.COLOR_RED, -1)
            self.curses.init_pair(4, self.curses.COLOR_YELLOW, -1)
            self.curses.init_pair(5, self.curses.COLOR_BLACK, self.curses.COLOR_CYAN)
            self.curses.init_pair(6, self.curses.COLOR_MAGENTA, -1)

    def log(self, message: str = "") -> None:
        """Appends one or more lines to the activity log."""

        lines = message.splitlines() or [""]
        self.log_lines.extend(lines)
        self.log_lines = self.log_lines[-MAX_LOG_LINES:]

    def attempt_connection(self) -> None:
        """Connects to the server if not already connected."""

        if self.client.client_socket is not None:
            return

        connected = self.client.connect(show_help=False)
        if connected:
            self.refresh_files(announce=True)
        else:
            self.log("Press R after the server is running to reconnect.")

    def refresh_files(self, announce: bool = False) -> None:
        """Refreshes the server file list and updates the highlighted selection."""

        response = self.client.fetch_file_listing()
        if response is None:
            self.files = []
            self.selected_index = 0
            return

        files = response.get("files", [])
        self.files = files if isinstance(files, list) else []

        if self.files:
            self.selected_index = min(self.selected_index, len(self.files) - 1)
        else:
            self.selected_index = 0

        if announce:
            if self.files:
                self.log(f"Server file list refreshed: {len(self.files)} file(s) available.")
            else:
                self.log("Server file list refreshed: no files are available yet.")

    def begin_prompt(self, mode: str, label: str) -> None:
        """Switches the dashboard into text-input mode."""

        self.prompt_mode = mode
        self.prompt_label = label
        self.prompt_buffer = ""

        if self.screen is not None:
            try:
                self.curses.curs_set(1)
            except self.curses.error:
                pass

    def close_prompt(self) -> None:
        """Returns from text-input mode back to the normal dashboard."""

        self.prompt_mode = None
        self.prompt_label = ""
        self.prompt_buffer = ""

        if self.screen is not None:
            try:
                self.curses.curs_set(0)
            except self.curses.error:
                pass

    def download_selected_file(self) -> None:
        """Downloads the currently highlighted file in the server file list."""

        if not self.files:
            self.refresh_files(announce=True)
            if not self.files:
                self.log("There is no server file selected to download.")
                return

        filename = str(self.files[self.selected_index].get("filename", ""))
        if not filename:
            self.log("The selected file entry is invalid.")
            return

        self.log(f"Downloading selected file: {filename}")
        self.client.download_file(filename)
        self.refresh_files(announce=False)

    def handle_normal_key(self, key: str | int) -> None:
        """Handles navigation and action shortcuts in normal mode."""

        normalized = key.lower() if isinstance(key, str) else key

        if self.show_help_overlay:
            if normalized in {"h", "\x1b", "\n", "\r", " "}:
                self.show_help_overlay = False
            elif normalized == "q":
                self.should_exit = True
            else:
                self.show_help_overlay = False
            return

        if normalized in {"q"}:
            self.should_exit = True
            return

        if normalized in {"l"}:
            self.refresh_files(announce=True)
            return

        if normalized in {"u"}:
            self.begin_prompt("upload", "Upload path")
            return

        if normalized in {"d", "\n", "\r"}:
            self.download_selected_file()
            return

        if normalized in {"/"}:
            self.begin_prompt("download", "Download filename or number")
            return

        if normalized in {"h"}:
            self.show_help_overlay = True
            return

        if normalized in {"r"}:
            self.client.close_local_socket()
            self.files = []
            self.selected_index = 0
            self.attempt_connection()
            return

        if normalized in {"k"} or key == self.curses.KEY_UP:
            if self.files:
                self.selected_index = max(0, self.selected_index - 1)
            return

        if normalized in {"j"} or key == self.curses.KEY_DOWN:
            if self.files:
                self.selected_index = min(len(self.files) - 1, self.selected_index + 1)
            return

    def handle_prompt_key(self, key: str | int) -> None:
        """Handles inline text entry for upload and download prompts."""

        if key in {self.curses.KEY_ENTER, "\n", "\r"}:
            submitted_text = self.prompt_buffer.strip()
            active_mode = self.prompt_mode
            self.close_prompt()

            if not submitted_text:
                self.log("Input cancelled because no text was entered.")
                return

            if active_mode == "upload":
                self.log(f"Uploading local file: {submitted_text}")
                self.client.upload_file(submitted_text)
                self.refresh_files(announce=False)
            elif active_mode == "download":
                self.log(f"Downloading requested file: {submitted_text}")
                self.client.download_file(submitted_text)
                self.refresh_files(announce=False)
            return

        if key == "\x1b":
            self.log("Input cancelled.")
            self.close_prompt()
            return

        if key in {self.curses.KEY_BACKSPACE, "\x08", "\x7f"}:
            self.prompt_buffer = self.prompt_buffer[:-1]
            return

        if isinstance(key, str) and key.isprintable():
            self.prompt_buffer += key

    def draw(self) -> None:
        """Renders the full dashboard."""

        assert self.screen is not None

        self.screen.erase()
        height, width = self.screen.getmaxyx()

        if height < MIN_HEIGHT or width < MIN_WIDTH:
            warning = (
                f"Resize the terminal to at least {MIN_WIDTH}x{MIN_HEIGHT} "
                "for the SocketShare dashboard."
            )
            self.add_line(self.screen, 1, 2, warning, width=width - 4)
            self.add_line(
                self.screen,
                3,
                2,
                "Press Q to quit or enlarge the window and continue.",
                width=width - 4,
            )
            self.screen.refresh()
            return

        header_height = 4
        body_top = header_height
        body_height = height - header_height - 9
        log_height = 9
        left_width = 36
        right_width = width - left_width - 3

        self.draw_header(width)
        self.draw_status_panel(body_top, 0, body_height, left_width)
        self.draw_files_panel(body_top, left_width + 1, body_height, right_width)
        self.draw_log_panel(body_top + body_height, 0, log_height, width)
        self.draw_footer(height - 1, width)

        if self.show_help_overlay:
            self.draw_help_overlay(height, width)

        self.screen.refresh()

    def draw_header(self, width: int) -> None:
        """Renders the title and summary strip."""

        assert self.screen is not None

        title_attr = self.color(1) | self.curses.A_BOLD
        self.add_line(self.screen, 0, 2, "SocketShare Terminal Dashboard", title_attr)

        connection_label = "Connected" if self.client.client_socket else "Disconnected"
        connection_color = self.color(2) if self.client.client_socket else self.color(3)
        summary = (
            f"{connection_label}  |  Server {self.client.host}:{self.client.port}  |  "
            f"Files {len(self.files)}  |  Downloads {self.rel_path(DOWNLOADS_PATH)}"
        )
        self.add_line(self.screen, 1, 2, summary, connection_color | self.curses.A_BOLD)

        shortcut_line = (
            "L refresh   U upload   D/Enter download selected   / typed download   "
            "J/K or arrows move   R reconnect   H help   Q quit"
        )
        self.add_line(self.screen, 2, 2, shortcut_line, width=width - 4)

    def draw_status_panel(self, top: int, left: int, height: int, width: int) -> None:
        """Renders the left sidebar with project and control information."""

        window = self.make_box(top, left, height, width, "Overview")

        rows = [
            ("Connection", "Online" if self.client.client_socket else "Offline"),
            ("Selected", self.selected_file_label()),
            ("Uploads", self.rel_path(STORAGE_PATH)),
            ("Downloads", self.rel_path(DOWNLOADS_PATH)),
            ("Sample file", self.rel_path(PROJECT_ROOT / "sample_files" / "demo.txt")),
        ]

        row = 1
        for label, value in rows:
            self.add_line(window, row, 2, f"{label}:", self.curses.A_BOLD)
            for wrapped in self.wrap_text(value, width - 4):
                row += 1
                self.add_line(window, row, 2, wrapped)
            row += 1

        controls_title = "Controls"
        self.add_line(window, row, 2, controls_title, self.color(6) | self.curses.A_BOLD)
        row += 1

        controls = [
            "Refresh the server file list with L.",
            "Move through files with J/K or the arrow keys.",
            "Press Enter or D to download the highlighted file.",
            "Press / to type a filename or file number to download.",
            "Press U to enter a local upload path.",
            "Press R to reconnect if the server restarts.",
            "Press H for the full help overlay.",
        ]
        for line in controls:
            for wrapped in self.wrap_text(f"- {line}", width - 4):
                self.add_line(window, row, 2, wrapped)
                row += 1

    def draw_files_panel(self, top: int, left: int, height: int, width: int) -> None:
        """Renders the live server file list with the highlighted selection."""

        window = self.make_box(top, left, height, width, "Server Files")

        if not self.files:
            empty_state = [
                "No files are currently listed on the server.",
                "Press L to refresh or upload a file with U.",
            ]
            row = 2
            for line in empty_state:
                self.add_line(window, row, 2, line, width=width - 4)
                row += 2
            return

        content_width = max(12, width - 4)
        visible_rows = max(1, height - 4)
        offset = 0

        if self.selected_index >= visible_rows:
            offset = self.selected_index - visible_rows + 1

        for visible_row in range(visible_rows):
            file_index = offset + visible_row
            if file_index >= len(self.files):
                break

            file_info = self.files[file_index]
            filename = str(file_info.get("filename", "unknown"))
            size = format_file_size(int(file_info.get("size", 0)))
            prefix = ">" if file_index == self.selected_index else " "
            line = f"{prefix} {file_index + 1:>2}. {filename} [{size}]"
            attr = self.color(5) | self.curses.A_BOLD if file_index == self.selected_index else 0
            self.add_line(window, visible_row + 1, 2, line, width=content_width, attr=attr)

        overflow_count = len(self.files) - visible_rows - offset
        if overflow_count > 0:
            more_line = f"... {overflow_count} more file(s) below"
            self.add_line(
                window,
                height - 2,
                2,
                more_line,
                width=content_width,
                attr=self.color(4),
            )

    def draw_log_panel(self, top: int, left: int, height: int, width: int) -> None:
        """Renders the recent activity log."""

        window = self.make_box(top, left, height, width, "Activity Log")
        content_width = max(12, width - 4)
        content_height = max(1, height - 2)
        lines = self.log_lines[-content_height:]

        row = 1
        for line in lines:
            self.add_line(window, row, 2, line, width=content_width)
            row += 1

    def draw_footer(self, row: int, width: int) -> None:
        """Renders the bottom prompt or status bar."""

        assert self.screen is not None

        if self.prompt_mode is None:
            footer = "Ready. Press H for help."
        else:
            footer = (
                f"{self.prompt_label}: {self.prompt_buffer}"
                "  (Enter to submit, Esc to cancel)"
            )

        self.add_line(
            self.screen,
            row,
            0,
            " " * max(0, width - 1),
            attr=self.color(4) | self.curses.A_REVERSE,
        )
        self.add_line(
            self.screen,
            row,
            2,
            footer,
            width=width - 4,
            attr=self.color(4) | self.curses.A_REVERSE,
        )

    def draw_help_overlay(self, height: int, width: int) -> None:
        """Renders a centered help overlay above the dashboard."""

        overlay_width = min(72, width - 8)
        overlay_height = 14
        top = max(1, (height - overlay_height) // 2)
        left = max(2, (width - overlay_width) // 2)
        window = self.make_box(top, left, overlay_height, overlay_width, "Help")

        help_lines = [
            "This dashboard keeps the original SocketShare networking flow intact.",
            "L refreshes the server file list.",
            "J/K or the arrow keys move the highlighted selection.",
            "Enter or D downloads the selected file.",
            "/ opens an inline prompt for a filename or file number.",
            "U opens an inline prompt for a local upload path.",
            "R reconnects after a server restart.",
            "Q cleanly disconnects and exits the client.",
            "Press H, Enter, Space, or Esc to close this help box.",
        ]

        row = 1
        for line in help_lines:
            for wrapped in self.wrap_text(line, overlay_width - 4):
                self.add_line(window, row, 2, wrapped, width=overlay_width - 4)
                row += 1

    def selected_file_label(self) -> str:
        """Returns a human-readable summary of the highlighted file."""

        if not self.files:
            return "No file selected"

        file_info = self.files[self.selected_index]
        filename = str(file_info.get("filename", "unknown"))
        size = format_file_size(int(file_info.get("size", 0)))
        return f"{filename} ({size})"

    def make_box(self, top: int, left: int, height: int, width: int, title: str) -> Any:
        """Creates and decorates a bordered window."""

        assert self.screen is not None

        window = self.screen.derwin(height, width, top, left)
        window.erase()
        window.box()
        self.add_line(window, 0, 2, f" {title} ", attr=self.color(1) | self.curses.A_BOLD)
        return window

    def add_line(
        self,
        window: Any,
        row: int,
        column: int,
        text: str,
        attr: int = 0,
        width: int | None = None,
    ) -> None:
        """Safely writes a cropped line into a curses window."""

        max_y, max_x = window.getmaxyx()
        if row < 0 or row >= max_y or column < 0 or column >= max_x:
            return

        available = max_x - column - 1
        if width is not None:
            available = min(available, width)

        if available <= 0:
            return

        try:
            window.addnstr(row, column, text, available, attr)
        except self.curses.error:
            pass

    def wrap_text(self, text: str, width: int) -> list[str]:
        """Wraps long text to fit within a panel."""

        if width <= 1:
            return [text]

        return textwrap.wrap(text, width=width) or [text]

    def rel_path(self, path: Path) -> str:
        """Formats project paths relative to the assignment root when possible."""

        try:
            return str(path.relative_to(PROJECT_ROOT))
        except ValueError:
            return str(path)

    def color(self, pair_number: int) -> int:
        """Returns a curses color pair if color is available."""

        if self.curses.has_colors():
            return self.curses.color_pair(pair_number)
        return 0


def run_terminal_ui(client: "SocketShareClient") -> None:
    """Launches the richer terminal UI when the terminal supports it."""

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise TerminalUiUnavailableError("an interactive terminal is required")

    try:
        import curses
    except ImportError as exc:
        raise TerminalUiUnavailableError(
            "Python curses support is not available in this environment"
        ) from exc

    dashboard = SocketShareTerminalUI(client, curses)
    try:
        dashboard.run()
    except curses.error as exc:
        raise TerminalUiUnavailableError(
            "the active terminal does not support the full-screen dashboard"
        ) from exc
