# region imports
import curses
import importlib
import logging
import multiprocessing
import os
import threading
import warnings

logging.disable(logging.CRITICAL)

import click
import music_tag
import numpy as np

from datetime import date
from shutil import copy, move
# from time import sleep

from just_playback import Playback
from numba import jit  # NOTE: I think I'm in love with this decorator
from numba.core.errors import NumbaWarning

warnings.simplefilter("ignore", category=NumbaWarning)

try:
    LIBROSA = importlib.import_module("librosa")
    if not (
        "load" in dir(LIBROSA) and
        "amplitude_to_db" in dir(LIBROSA) and
        "stft" in dir(LIBROSA)
    ):
        raise ImportError
except ImportError:
    LIBROSA = None

# endregion

# region constants

DISCORD_ID = 1039038199881810040

CUR_YEAR = date.today().year
EXTS = (".mp3", ".wav", ".flac", ".ogg")
PROMPT_MODES = {
    "insert": 0,
    "add": 1,
    "tag": 2,
}
LOOP_MODES = {
    "none": 0,
    "one": 1,
    "inf": 2,
}

METADATA_KEYS = (
    "album",
    "albumartist",
    "artist",
    "artwork",
    "comment",
    "compilation",
    "composer",
    "discnumber",
    "genre",
    "lyrics",
    "totaldiscs",
    "totaltracks",
    "tracknumber",
    "tracktitle",
    "year",
    "isrc",
    "#bitrate",
    "#codec",
    "#length",
    "#channels",
    "#bitspersample",
    "#samplerate",
)

# region paths
MAESTRO_DIR = os.path.join(os.path.expanduser("~"), ".maestro-files/")

SONGS_DIR = os.path.join(MAESTRO_DIR, "songs/")

SONGS_INFO_PATH = os.path.join(MAESTRO_DIR, "songs.txt")

STATS_DIR = os.path.join(MAESTRO_DIR, "stats/")
CUR_YEAR_STATS_PATH = os.path.join(STATS_DIR, f"{CUR_YEAR}.txt")
TOTAL_STATS_PATH = os.path.join(STATS_DIR, "total.txt")
# endregion

# region player
HORIZONTAL_BLOCKS = {
    1: "▏",
    2: "▎",
    3: "▍",
    4: "▌",
    5: "▋",
    6: "▊",
    7: "▉",
    8: "█",
}
SCRUB_TIME = 5  # in seconds
VOLUME_STEP = 0.01  # self.volume is 0-1
MIN_PROGRESS_BAR_WIDTH = 20
MIN_VOLUME_BAR_WIDTH, MAX_VOLUME_BAR_WIDTH = 10, 40
# endregion

# region visualizer
FPS = 60

STEP_SIZE = 512  # librosa default
SAMPLE_RATE = STEP_SIZE * FPS

VERTICAL_BLOCKS = {
    0: " ",
    1: "▁",
    2: "▂",
    3: "▃",
    4: "▄",
    5: "▅",
    6: "▆",
    7: "▇",
    8: "█",
}
VISUALIZER_HEIGHT = 8  # should divide 80
WAVEFORM_HEIGHT = 6  # should also divide 80

VIS_FLATTEN_FACTOR = 3  # higher = more flattening; 1 = no flattening
WAVEFORM_FLATTEN_FACTOR = 20
# endregion

# endregion


class Scroller:
    def __init__(self, num_lines, win_size):
        self.num_lines = num_lines
        self.win_size = win_size
        self.pos = 0
        self.top = 0

    def scroll_forward(self):
        if self.pos < self.num_lines - 1:
            if (
                self.pos == self.halfway
                and self.top < self.num_lines - self.win_size
            ):
                self.top += 1
            self.pos += 1

    def scroll_backward(self):
        if self.pos > 0:
            if self.pos == self.halfway and self.top > 0:
                self.top -= 1
            self.pos -= 1

    @property
    def halfway(self):
        return self.top + self.win_size // 2

    def resize(self, win_size=None):
        if win_size is not None:
            self.win_size = win_size
        self.top = max(0, self.pos - self.win_size // 2)
        self.top = max(0, min(self.num_lines - self.win_size, self.top))


def fit_string_to_width(string, width, length_so_far):
    if length_so_far + len(string) > width:
        remaining_width = width - length_so_far
        if remaining_width >= 3:
            string = string[: (remaining_width - 3)] + "..."
        else:
            string = "." * remaining_width
    length_so_far += len(string)
    return string, length_so_far


def addstr_fit_to_width(stdscr, string, width, length_so_far, *args, **kwargs):
    string, length_so_far = fit_string_to_width(string, width, length_so_far)
    if string:
        if length_so_far <= width:
            stdscr.addstr(string, *args, **kwargs)
        else:
            stdscr.addstr(string[:-1], *args, **kwargs)
            stdscr.insstr(string[-1], *args, **kwargs)
    return length_so_far


@jit
def lerp(start, stop, t):
    return start + t * (stop - start)


@jit
def bin_average(arr, n, include_remainder=False, func=np.max):
    remainder = arr.shape[1] % n
    if remainder == 0:
        return func(arr.reshape(arr.shape[0], -1, n), axis=1)

    avg_head = func(arr[:, :-remainder].reshape(arr.shape[0], -1, n), axis=1)
    if include_remainder:
        avg_tail = func(
            arr[:, -remainder:].reshape(arr.shape[0], -1, remainder), axis=1
        )
        return np.concatenate((avg_head, avg_tail), axis=1)

    return avg_head


@jit
def render(
    num_bins,
    freqs,
    t,
    visualizer_height,
    mono=None,
    include_remainder=None,
    func=np.max,
):
    """
    mono:
        True:  forces one-channel visualization
        False: forces two-channel visualization
        None:  if freqs[0] == freqs[1], one-channel, else two
    """
    if mono is None:
        mono = np.array_equal(freqs[0], freqs[1])

    if not mono:
        gap_bins = 1 if num_bins % 2 else 2
        num_bins = (num_bins - 1) // 2
    else:
        gap_bins = 0
        freqs[0, :, t] = (freqs[0, :, t] + freqs[1, :, t]) / 2

    num_vertical_block_sizes = len(VERTICAL_BLOCKS) - 1
    freqs = np.round(
        bin_average(
            freqs[:, :, t],
            num_bins,
            (freqs.shape[-2] % num_bins) > num_bins / 2
            if include_remainder is None
            else include_remainder,
            func=func,
        )
        / 80
        * visualizer_height
        * num_vertical_block_sizes
    )

    arr = np.zeros((int(not mono) + 1, visualizer_height, num_bins))
    for b in range(num_bins):
        # NOTE: only l for now
        bin_height = freqs[0, b]
        h = 0
        while bin_height > num_vertical_block_sizes:
            arr[0, h, b] = num_vertical_block_sizes
            bin_height -= num_vertical_block_sizes
            h += 1
        arr[0, h, b] = bin_height
        if not mono:
            bin_height = freqs[1, b]
            h = 0
            while bin_height > num_vertical_block_sizes:
                arr[1, h, b] = num_vertical_block_sizes
                bin_height -= num_vertical_block_sizes
                h += 1
            arr[1, h, b] = bin_height

    res = []
    for h in range(visualizer_height - 1, -1, -1):
        s = ""
        for b in range(num_bins):
            s += VERTICAL_BLOCKS[arr[0, h, b]]
        if not mono:
            s += " " * gap_bins
            for b in range(num_bins):
                s += VERTICAL_BLOCKS[arr[1, h, b]]
        res.append(s)

    return res


class PlayerOutput:
    def __init__(
        self, stdscr, playlist, volume, clip_mode, update_discord, visualize
    ):
        self.stdscr = stdscr
        self.scroller = Scroller(
            len(playlist), stdscr.getmaxyx()[0] - 2  # -2 for status bar
        )
        self.playlist = playlist
        self.i = 0
        self.volume = volume
        self.clip_mode = clip_mode
        self.update_discord = update_discord
        self.visualize = visualize  # want to visualize
        self.can_visualize = LIBROSA is not None  # can generate visualization
        # space to show visualization
        self.can_show_visualization = (
            self.can_visualize and
            self.stdscr.getmaxyx()[0] > VISUALIZER_HEIGHT + 5
        )
        if self.can_visualize:
            t = threading.Thread(
                target=self.load_visualizer_data,
                daemon=True,
            )
            t.start()

        self.looping_current_song = LOOP_MODES["none"]
        self.duration = 0
        self.paused = False
        self.ending = False
        self.prompting: None | tuple = None
        self.clip = (0, 0)
        self.discord_connected = multiprocessing.Value("i", 2)

    def load_visualizer_data(self):
        i = self.i
        while True:
            song_path = os.path.join(SONGS_DIR, self.playlist[i][1])
            cur_song_data = LIBROSA.load(song_path, mono=False, sr=SAMPLE_RATE)[0]

            if len(cur_song_data.shape) == 1:  # mono -> stereo
                cur_song_data = np.repeat([cur_song_data], 2, axis=0)
            elif cur_song_data.shape[0] == 1:  # mono -> stereo
                cur_song_data = np.repeat(cur_song_data, 2, axis=0)
            elif cur_song_data.shape[0] == 6:  # 5.1 surround -> stereo
                cur_song_data = np.delete(cur_song_data, (1, 3, 4, 5), axis=0)

            self.playlist[i][4] = (
                LIBROSA.amplitude_to_db(
                    np.abs(LIBROSA.stft(cur_song_data)),
                    ref=np.max
                ) + 80
            )

            if self.playlist[self.i][4] is None:
                i = self.i
            else:
                original = i
                while self.playlist[i][4] is not None:
                    i += 1
                    if i >= len(self.playlist):
                        i = 0
                    elif i == original:
                        break

    @property
    def song_path(self):
        return os.path.join(SONGS_DIR, self.playlist[self.i][1])

    def output(self, pos):
        self.can_show_visualization = (
            self.can_visualize and
            self.stdscr.getmaxyx()[0] > VISUALIZER_HEIGHT + 5
        )
        self.scroller.resize(
            self.stdscr.getmaxyx()[0]
            - 3  # -3 for status bar
            - 1  # -1 for header
            - (self.prompting != None)  # - add mode
          # - visualizer
            - (VISUALIZER_HEIGHT if self.can_show_visualization else 0)
        )

        if self.clip_mode:
            pos -= self.clip[0]

        self.stdscr.erase()

        screen_width = self.stdscr.getmaxyx()[1]

        length_so_far = 0
        if self.update_discord:
            if self.discord_connected.value == 2:
                length_so_far = addstr_fit_to_width(
                    self.stdscr,
                    "Connecting to Discord ... ",
                    screen_width,
                    length_so_far,
                    curses.color_pair(12),
                )
            elif self.discord_connected.value == 1:
                length_so_far = addstr_fit_to_width(
                    self.stdscr,
                    "Discord connected! ",
                    screen_width,
                    length_so_far,
                    curses.color_pair(17),
                )
            else:
                length_so_far = addstr_fit_to_width(
                    self.stdscr,
                    "Failed to connect to Discord. ",
                    screen_width,
                    length_so_far,
                    curses.color_pair(14),
                )

        visualize_message = ""
        visualize_color = 12
        if self.visualize:
            if not self.can_visualize:
                visualize_message = "Librosa is required for visualization."
                visualize_color = 14
            elif not self.can_show_visualization:
                visualize_message = "Window too small for visualization."
                visualize_color = 14
            elif self.playlist[self.i][4] is None:
                visualize_message = "Loading visualization..."
                visualize_color = 12
        length_so_far = addstr_fit_to_width(
            self.stdscr,
            " " * (screen_width - length_so_far - len(visualize_message))
            + visualize_message,
            screen_width,
            length_so_far,
            curses.color_pair(visualize_color),
        )
        self.stdscr.move(1, 0)

        song_display_color = 5 if self.looping_current_song else 3
        progress_bar_display_color = (
            17 if (self.clip_mode and self.clip != (0, self.duration)) else 15
        )

        # for aligning song names
        longest_song_id_length = max(len(song[0]) for song in self.playlist)

        for j in range(
            self.scroller.top, self.scroller.top + self.scroller.win_size
        ):
            if j <= len(self.playlist) - 1:
                length_so_far = 0

                length_so_far = addstr_fit_to_width(
                    self.stdscr,
                    " " * (longest_song_id_length - len(self.playlist[j][0]))
                    + f"{self.playlist[j][0]} ",
                    screen_width,
                    length_so_far,
                    curses.color_pair(2),
                )
                if j == self.i:
                    length_so_far = addstr_fit_to_width(
                        self.stdscr,
                        f"{self.playlist[j][1]} ",
                        screen_width,
                        length_so_far,
                        curses.color_pair(song_display_color) | curses.A_BOLD,
                    )
                else:
                    length_so_far = addstr_fit_to_width(
                        self.stdscr,
                        f"{self.playlist[j][1]} ",
                        screen_width,
                        length_so_far,
                        (
                            curses.color_pair(4)
                            if (j == self.scroller.pos)
                            else curses.color_pair(1)
                        ),
                    )
                length_so_far = addstr_fit_to_width(
                    self.stdscr,
                    f"{', '.join(self.playlist[j][2].split(','))}",
                    screen_width,
                    length_so_far,
                    curses.color_pair(2),
                )
            self.stdscr.move((j - self.scroller.top) + 2, 0)

        if self.prompting is not None:
            # pylint: disable=unsubscriptable-object
            if (
                self.prompting[2] == PROMPT_MODES["add"]
                or self.prompting[2] == PROMPT_MODES["insert"]
            ):
                adding_song_length = addstr_fit_to_width(
                    self.stdscr,
                    (
                        "Insert"
                        if self.prompting[2] == PROMPT_MODES["insert"]
                        else "Append"
                    )
                    + " song (by ID): "
                    + self.prompting[0],
                    screen_width,
                    0,
                    curses.color_pair(1),
                )
            else:
                adding_song_length = addstr_fit_to_width(
                    self.stdscr,
                    "Add tag to songs: " + self.prompting[0],
                    screen_width,
                    0,
                    curses.color_pair(1),
                )
            self.stdscr.move(self.stdscr.getyx()[0] + 1, 0)

        length_so_far = 0

        length_so_far = addstr_fit_to_width(
            self.stdscr,
            ("| " if self.paused else "> ") + f"({self.playlist[self.i][0]}) ",
            screen_width,
            length_so_far,
            curses.color_pair(song_display_color + 10),
        )
        length_so_far = addstr_fit_to_width(
            self.stdscr,
            f"{self.playlist[self.i][1]} ",
            screen_width,
            length_so_far,
            curses.color_pair(song_display_color + 10) | curses.A_BOLD,
        )
        length_so_far = addstr_fit_to_width(
            self.stdscr,
            "%d/%d  " % (self.i + 1, len(self.playlist)),
            screen_width,
            length_so_far,
            curses.color_pair(12),
        )
        length_so_far = addstr_fit_to_width(
            self.stdscr,
            f"{'c' if self.clip_mode else ' '}",
            screen_width,
            length_so_far,
            curses.color_pair(17) | curses.A_BOLD,
        )
        loop_char = " "
        if self.looping_current_song == LOOP_MODES["one"]:
            loop_char = "l"
        elif self.looping_current_song == LOOP_MODES["inf"]:
            loop_char = "L"
        # print_to_logfile(self.looping_current_song)
        length_so_far = addstr_fit_to_width(
            self.stdscr,
            loop_char,
            screen_width,
            length_so_far,
            curses.color_pair(15) | curses.A_BOLD,
        )
        volume_line_length_so_far = addstr_fit_to_width(
            self.stdscr,
            f"{'e' if self.ending else ' '}  ",
            screen_width,
            length_so_far,
            curses.color_pair(14) | curses.A_BOLD,
        )
        addstr_fit_to_width(
            self.stdscr,
            " " * (screen_width - volume_line_length_so_far - 1),
            screen_width,
            volume_line_length_so_far,
            curses.color_pair(16),
        )
        self.stdscr.insstr(  # hacky fix for curses bug
            " ",
            curses.color_pair(16),
        )
        self.stdscr.move(
            self.stdscr.getmaxyx()[0]
            - 2
            - (VISUALIZER_HEIGHT if self.can_show_visualization else 0),
            0,
        )

        addstr_fit_to_width(
            self.stdscr,
            " " * (screen_width - 1),
            screen_width,
            0,
            curses.color_pair(16),
        )
        self.stdscr.insstr(  # hacky fix for curses bug
            " ",
            curses.color_pair(16),
        )
        self.stdscr.move(
            self.stdscr.getyx()[0],
            0,
        )

        song_data_length_so_far = addstr_fit_to_width(
            self.stdscr,
            self.playlist[self.i][-3] + " - ",
            screen_width,
            0,
            curses.color_pair(12),
        )

        try:
            song_data_length_so_far = addstr_fit_to_width(
                self.stdscr,
                self.playlist[self.i][-2],
                screen_width,
                song_data_length_so_far,
                curses.color_pair(12) | curses.A_ITALIC,
            )
        except:  # pylint: disable=bare-except
            song_data_length_so_far = addstr_fit_to_width(
                self.stdscr,
                self.playlist[self.i][-2],
                screen_width,
                song_data_length_so_far,
                curses.color_pair(12),
            )

        addstr_fit_to_width(
            self.stdscr,
            f" ({self.playlist[self.i][-1]})",
            screen_width,
            song_data_length_so_far,
            curses.color_pair(12),
        )

        self.stdscr.move(
            self.stdscr.getmaxyx()[0]
            - (VISUALIZER_HEIGHT if self.can_show_visualization else 0)
            - 1,
            0,
        )

        length_so_far = 0
        secs = int(pos)
        length_so_far = addstr_fit_to_width(
            self.stdscr,
            f"{format_seconds(secs)} / {format_seconds(self.duration)}  ",
            screen_width,
            length_so_far,
            curses.color_pair(progress_bar_display_color),
        )
        if not length_so_far >= screen_width:
            if screen_width - length_so_far >= MIN_PROGRESS_BAR_WIDTH + 2:
                progress_bar_width = screen_width - length_so_far - 2
                bar = "|"
                progress_block_width = (
                    progress_bar_width * 8 * pos
                ) // self.duration
                for _ in range(progress_bar_width):
                    if progress_block_width > 8:
                        bar += HORIZONTAL_BLOCKS[8]
                        progress_block_width -= 8
                    elif progress_block_width > 0:
                        bar += HORIZONTAL_BLOCKS[progress_block_width]
                        progress_block_width = 0
                    else:
                        bar += " "

                self.stdscr.addstr(
                    bar, curses.color_pair(progress_bar_display_color)
                )
                self.stdscr.insstr(  # hacky fix for curses bug
                    "|", curses.color_pair(progress_bar_display_color)
                )
            else:
                self.stdscr.addstr(
                    " " * (screen_width - length_so_far - 1),
                    curses.color_pair(16),
                )
                self.stdscr.insstr(  # hacky fix for curses bug
                    " ", curses.color_pair(16)
                )

        # right align volume bar
        if not volume_line_length_so_far >= screen_width:
            self.stdscr.move(
                self.stdscr.getmaxyx()[0]
                - 3
                - (VISUALIZER_HEIGHT if self.can_show_visualization else 0),
                volume_line_length_so_far,
            )
            if (
                screen_width - volume_line_length_so_far
                >= MIN_VOLUME_BAR_WIDTH + 10
            ):
                bar = f"{str(int(self.volume*100)).rjust(3)}/100 |"
                volume_bar_width = min(
                    screen_width - volume_line_length_so_far - (len(bar) + 1),
                    MAX_VOLUME_BAR_WIDTH,
                )
                block_width = int(volume_bar_width * 8 * self.volume)
                for _ in range(volume_bar_width):
                    if block_width > 8:
                        bar += HORIZONTAL_BLOCKS[8]
                        block_width -= 8
                    elif block_width > 0:
                        bar += HORIZONTAL_BLOCKS[block_width]
                        block_width = 0
                    else:
                        bar += " "
                bar += "|"
                bar = bar.rjust(screen_width - volume_line_length_so_far)

                self.stdscr.addstr(bar, curses.color_pair(16))
            elif screen_width - volume_line_length_so_far >= 7:
                self.stdscr.addstr(
                    f"{str(int(self.volume*100)).rjust(3)}/100".rjust(
                        screen_width - volume_line_length_so_far
                    ),
                    curses.color_pair(16),
                )

        if self.can_show_visualization:
            if self.clip_mode:
                pos += self.clip[0]

            self.stdscr.move(
                self.stdscr.getmaxyx()[0]
                - (VISUALIZER_HEIGHT if self.can_show_visualization else 0),
                0,
            )
            if self.playlist[self.i][4] is None:
                self.stdscr.addstr(
                    (
                        (" " * (self.stdscr.getmaxyx()[1] - 1) + "\n")
                        * VISUALIZER_HEIGHT
                    ).rstrip()
                )
            else:
                rendered_lines = render(
                    self.stdscr.getmaxyx()[1],
                    self.playlist[self.i][4],
                    min(
                        round(pos * FPS),
                        self.playlist[self.i][4].shape[2] - 1,
                    ),
                    VISUALIZER_HEIGHT,
                )
                for i in range(len(rendered_lines)):
                    self.stdscr.addstr(rendered_lines[i][:-1])
                    self.stdscr.insstr(rendered_lines[i][-1])
                    if i < len(rendered_lines) - 1:
                        self.stdscr.move(self.stdscr.getyx()[0] + 1, 0)

        if self.prompting is not None:
            # pylint: disable=unsubscriptable-object
            self.stdscr.move(
                self.stdscr.getmaxyx()[0]
                - (VISUALIZER_HEIGHT if self.can_show_visualization else 0)
                - 4,  # 4 lines for status bar + adding entry
                adding_song_length
                + (self.prompting[1] - len(self.prompting[0])),
            )

        self.stdscr.refresh()


def init_curses(stdscr):
    curses.start_color()
    curses.use_default_colors()

    # region colors

    curses.init_pair(1, curses.COLOR_WHITE, -1)
    curses.init_pair(2, curses.COLOR_BLACK + 8, -1)  # bright black
    curses.init_pair(3, curses.COLOR_BLUE, -1)
    curses.init_pair(4, curses.COLOR_RED, -1)
    curses.init_pair(5, curses.COLOR_YELLOW, -1)
    curses.init_pair(6, curses.COLOR_GREEN, -1)
    curses.init_pair(7, curses.COLOR_MAGENTA, -1)

    curses.init_pair(12, curses.COLOR_BLACK + 8, curses.COLOR_BLACK)
    curses.init_pair(13, curses.COLOR_BLUE, curses.COLOR_BLACK)
    curses.init_pair(14, curses.COLOR_RED, curses.COLOR_BLACK)
    curses.init_pair(15, curses.COLOR_YELLOW, curses.COLOR_BLACK)
    curses.init_pair(16, curses.COLOR_GREEN, curses.COLOR_BLACK)
    curses.init_pair(17, curses.COLOR_MAGENTA, curses.COLOR_BLACK)
    # endregion

    curses.curs_set(False)
    stdscr.nodelay(True)
    try:
        curses.set_escdelay(25)  # 25 ms
    except:  # pylint: disable=bare-except
        pass


def add_song(
    path,
    tags,
    move_,
    songs_file,
    lines,
    song_id,
    prepend_newline,
    clip_start,
    clip_end,
):
    song_name = os.path.split(path)[1]
    if "|" in song_name:
        song_name = song_name.replace("|", "-")
        click.secho(
            f"The song \"{song_name}\" contains one or more '|' character(s), which is not allowed—all ocurrences have been replaced with '-'.",
            fg="yellow",
        )

    for line in lines:
        details = line.split("|")
        if details[1] == song_name:
            click.secho(
                f"Song with name '{song_name}' already exists, 'copy' will be appended to the song name.",
                fg="yellow",
            )
            song_basename, song_ext = os.path.splitext(song_name)
            song_name = song_basename + " copy" + song_ext
            break
    dest_path = os.path.join(SONGS_DIR, song_name)

    if move_:
        move(path, dest_path)
    else:
        copy(path, dest_path)

    tags = list(set(tags))

    if prepend_newline:
        songs_file.write("\n")
    songs_file.write(f"{song_id}|{song_name}|{','.join(tags)}|")
    if clip_start is not None:
        songs_file.write(f"{clip_start} {clip_end}")
    songs_file.write("\n")

    for stats_file in os.listdir(STATS_DIR):
        if not stats_file.endswith(".txt"):
            continue

        with open(
            os.path.join(STATS_DIR, stats_file), "r+", encoding="utf-8"
        ) as stats_file:
            stats_file_contents = stats_file.read()
            if stats_file_contents and not stats_file_contents.endswith("\n"):
                stats_file.write("\n")
            stats_file.write(f"{song_id}|0\n")

    if not tags:
        tags_string = ""
    elif len(tags) == 1:
        tags_string = f" and tag '{tags[0]}'"
    else:
        tags_string = f" and tags {', '.join([repr(tag) for tag in tags])}"

    if clip_start is not None:
        clip_string = f" and clip [{format_seconds(clip_start, show_decimal=True)}, {format_seconds(clip_end, show_decimal=True)}]"
    else:
        clip_string = ""

    click.secho(
        f"Added song '{song_name}' with ID {song_id}"
        + tags_string
        + clip_string
        + ".",
        fg="green",
    )


def clip_editor(stdscr, details):
    song_name = details[1]
    song_path = os.path.join(SONGS_DIR, song_name)

    show_waveform = True
    if LIBROSA is None:
        show_waveform = False
    else:
        audio_data = LIBROSA.load(song_path, sr=SAMPLE_RATE)[0]

        if len(audio_data.shape) == 1:  # mono -> stereo
            audio_data = np.repeat([audio_data], 2, axis=0)
        elif audio_data.shape[0] == 1:  # mono -> stereo
            audio_data = np.repeat(audio_data, 2, axis=0)
        elif audio_data.shape[0] == 6:  # 5.1 surround -> stereo
            audio_data = np.delete(audio_data, (1, 3, 4, 5), axis=0)


        audio_data.data /= np.max(np.abs(audio_data.data))
        audio_data.data = (
            80
            * (
                (np.reshape(audio_data.data, audio_data.data.shape + (1,)) + 1)
                / 2
            )
            ** WAVEFORM_FLATTEN_FACTOR
        )

    playback = Playback()
    playback.load_file(song_path)

    init_curses(stdscr)

    if details[3]:
        clip_start, clip_end = [float(x) for x in details[3].split()]
    else:
        clip_start, clip_end = 0, playback.duration
    editing_start = True
    change_output = True
    playback.play()
    playback.pause()
    last_timestamp = playback.curr_pos
    while True:
        if playback.curr_pos >= clip_end:
            playback.pause()

        change_output = change_output or (
            (playback.curr_pos - last_timestamp)
            >= (playback.duration / (8 * (stdscr.getmaxyx()[1] - 2)))
        )

        if change_output:
            clip_editor_output(
                stdscr,
                audio_data,
                details,
                playback.curr_pos,
                playback.paused,
                playback.duration,
                clip_start,
                clip_end,
                editing_start,
                show_waveform,
            )

        c = stdscr.getch()
        next_c = stdscr.getch()
        while next_c != -1:
            c, next_c = next_c, stdscr.getch()

        if c == -1:
            continue

        change_output = False
        if editing_start:
            if c == curses.KEY_LEFT:
                change_output = True
                playback.pause()
                clip_start = max(0, clip_start - 0.1)
                playback.seek(clip_start)
            elif c == curses.KEY_SLEFT:
                change_output = True
                playback.pause()
                clip_start = max(0, clip_start - 1)
                playback.seek(clip_start)
            elif c == curses.KEY_RIGHT:
                change_output = True
                playback.pause()
                clip_start = min(clip_start + 0.1, clip_end)
                playback.seek(clip_start)
            elif c == curses.KEY_SRIGHT:
                change_output = True
                playback.pause()
                clip_start = min(clip_start + 1, clip_end)
                playback.seek(clip_start)
            elif c == curses.KEY_ENTER:
                break
            else:
                c = chr(c)
                if c == " ":  # space
                    change_output = True
                    if playback.playing:
                        playback.pause()
                    else:
                        playback.resume()
                elif c in "tT":
                    change_output = True
                    playback.pause()
                    playback.seek(clip_end - 1)
                    editing_start = False
                elif c in "qQ":
                    return (None, None)
                elif c in "\r\n":
                    break
        else:
            if c == curses.KEY_LEFT:
                change_output = True
                playback.pause()
                clip_end = max(clip_end - 0.1, clip_start)
                playback.seek(clip_end - 1)
            elif c == curses.KEY_SLEFT:
                change_output = True
                playback.pause()
                clip_end = max(clip_end - 1, clip_start)
                playback.seek(clip_end - 1)
            elif c == curses.KEY_RIGHT:
                change_output = True
                playback.pause()
                clip_end = min(clip_end + 0.1, playback.duration)
                playback.seek(clip_end - 0.1)
            elif c == curses.KEY_SRIGHT:
                change_output = True
                playback.pause()
                clip_end = min(clip_end + 1, playback.duration)
                playback.seek(clip_end - 1)
            elif c == curses.KEY_ENTER:
                break
            else:
                c = chr(c)
                if c == " ":
                    change_output = True
                    if playback.playing:
                        playback.pause()
                    else:
                        playback.resume()
                elif c in "tT":
                    change_output = True
                    playback.pause()
                    playback.seek(clip_start)
                    editing_start = True
                elif c in "qQ":
                    return (None, None)
                elif c in "\r\n":
                    break

    return clip_start, clip_end


def clip_editor_output(
    stdscr,
    audio_data,
    details,
    pos,
    paused,
    duration,
    clip_start,
    clip_end,
    editing_start,
    show_waveform,
):
    stdscr.erase()

    if stdscr.getmaxyx()[0] < 3:
        stdscr.addstr("Window too small.", curses.color_pair(4))
        stdscr.refresh()
        return

    screen_width = stdscr.getmaxyx()[1]

    show_waveform = (
        show_waveform and stdscr.getmaxyx()[0] >= 4 + WAVEFORM_HEIGHT
    )
    if show_waveform:
        rendered_lines = render(
            screen_width - 2,
            audio_data.data,
            0,
            WAVEFORM_HEIGHT,
            mono=True,
            include_remainder=True,
            func=np.max,
        )

    stdscr.insstr(
        f"{format_seconds(clip_start, show_decimal=True)}"
        + (" <" if editing_start else ""),
        curses.color_pair(7),
    )

    end_str = (
        "> " if not editing_start else ""
    ) + f"{format_seconds(clip_end, show_decimal=True)}"
    stdscr.move(0, screen_width - len(end_str))
    stdscr.insstr(end_str, curses.color_pair(7))

    stdscr.move(1, 0)
    if show_waveform:
        for i in range(len(rendered_lines)):
            stdscr.addstr(" " + rendered_lines[i])
            stdscr.move(stdscr.getyx()[0] + 1, 0)

    clip_bar_width = screen_width - 2
    if clip_bar_width > 0:
        bar = "|"
        before_clip_block_width = round(
            (clip_bar_width * 8 * clip_start) / duration
        )
        clip_block_width = round(
            clip_bar_width * 8 * (clip_end - clip_start) / duration
        )
        num_chars_added = 0
        stdscr.addstr("|", curses.color_pair(7))
        while before_clip_block_width:
            if before_clip_block_width >= 8:
                stdscr.addstr(" ", curses.color_pair(7))
                before_clip_block_width -= 8
            else:
                stdscr.addstr(
                    HORIZONTAL_BLOCKS[before_clip_block_width],
                    curses.color_pair(7) | curses.A_REVERSE,
                )
                clip_block_width -= 8 - before_clip_block_width
                before_clip_block_width = 0
            num_chars_added += 1

        while num_chars_added < clip_bar_width:
            if clip_block_width >= 8:
                stdscr.addstr(HORIZONTAL_BLOCKS[8], curses.color_pair(7))
                clip_block_width -= 8
            elif clip_block_width > 0:
                stdscr.addstr(
                    HORIZONTAL_BLOCKS[clip_block_width], curses.color_pair(7)
                )
                clip_block_width = 0
            else:
                stdscr.addstr(" ", curses.color_pair(7))
            num_chars_added += 1
        stdscr.insstr("|", curses.color_pair(7))
        stdscr.move(stdscr.getyx()[0] + 1, 0)

    progress_bar_width = screen_width - 2
    if progress_bar_width > 0:
        bar = "|"
        progress_block_width = (progress_bar_width * 8 * pos) // duration
        for _ in range(progress_bar_width):
            if progress_block_width > 8:
                bar += HORIZONTAL_BLOCKS[8]
                progress_block_width -= 8
            elif progress_block_width > 0:
                bar += HORIZONTAL_BLOCKS[progress_block_width]
                progress_block_width = 0
            else:
                bar += " "

        stdscr.addstr(bar, curses.color_pair(5))
        stdscr.insstr("|", curses.color_pair(5))  # hacky fix for curses bug
        stdscr.move(stdscr.getyx()[0] + 1, 0)

    length_so_far = 0
    length_so_far = addstr_fit_to_width(
        stdscr,
        ("| " if paused else "> ") + f"({details[0]}) ",
        screen_width,
        length_so_far,
        curses.color_pair(3),
    )
    length_so_far = addstr_fit_to_width(
        stdscr,
        f"{details[1]} ",
        screen_width,
        length_so_far,
        curses.color_pair(3) | curses.A_BOLD,
    )
    length_so_far = addstr_fit_to_width(
        stdscr,
        f"{', '.join(details[2].split(','))} ",
        screen_width,
        length_so_far,
        curses.color_pair(2),
    )

    stdscr.refresh()


def format_seconds(secs, show_decimal=False):
    """Format seconds into a string."""
    return f"{int(secs//60):02}:{int(secs%60):02}" + (
        f".{secs%1:0.2f}"[2:] if show_decimal else ""
    )


def print_entry(entry_list, highlight=None, show_song_info=None):
    """
    tuple or iterable of strings

    0: song ID
    1: song name
    2: tags
    3: clip

    optional:
    4: seconds listened
    5: total duration (must be passed if 4 is passed)

    Pretty prints
        <song ID> <song name> [<total duration> <seconds listened> <times listened>] <clip> <tags>
        [<artist> - <album> (<album artist>)]
    """
    click.secho(entry_list[0] + " ", fg="bright_black", nl=False)
    if highlight is None:
        click.secho(entry_list[1] + " ", fg="blue", nl=False, bold=True)
    else:
        highlight_loc = entry_list[1].lower().find(highlight.lower())
        click.secho(
            entry_list[1][:highlight_loc],
            fg="white",
            nl=False,
        )
        click.secho(
            entry_list[1][highlight_loc : highlight_loc + len(highlight)],
            fg="blue",
            nl=False,
            bold=True,
        )
        click.secho(
            entry_list[1][highlight_loc + len(highlight) :] + " ",
            fg="white",
            nl=False,
        )

    if len(entry_list) > 4:  # len should == 6
        secs_listened = float(entry_list[4])
        total_duration = float(entry_list[5])
        click.secho(
            format_seconds(total_duration, show_decimal=True) + " ",
            nl=False,
        )
        click.secho(
            format_seconds(secs_listened, show_decimal=True) + " ",
            fg="yellow",
            nl=False,
        )
        click.secho(
            f"{secs_listened / total_duration:0.2f} ", fg="green", nl=False
        )

    if entry_list[3]:
        decimal_format_seconds = lambda x: format_seconds(
            float(x), show_decimal=True
        )
        start, end = map(decimal_format_seconds, entry_list[3].split())
        click.secho(
            f"[{start}, {end}] ",
            fg="magenta",
            nl=False,
        )

    if entry_list[2]:
        click.secho(", ".join(entry_list[2].split(",")), fg="bright_black")
    else:
        click.echo()  # newline

    if show_song_info:
        song_data = music_tag.load_file(os.path.join(SONGS_DIR, entry_list[1]))
        artist, album, album_artist = (
            song_data["artist"].value,
            song_data["album"].value,
            song_data["albumartist"].value,
        )
        click.secho(
            f"{(len(entry_list[0])+1)*' '}{artist if artist else 'Unknown Artist'} - ",
            fg="bright_black",
            nl=False,
        )
        click.secho(
            (album if album else "Unknown Album"),
            italic=True,
            fg="bright_black",
            nl=False,
        )
        click.secho(
            f" ({album_artist if album_artist else 'Unknown Album Artist'})",
            fg="bright_black",
        )


def print_to_logfile(*args, **kwargs):
    if "file" in kwargs:
        raise ValueError("file kwargs not allowed for 'print_to_logfile'")
    print(*args, **kwargs, file=open("log.txt", "a", encoding="utf-8"))
