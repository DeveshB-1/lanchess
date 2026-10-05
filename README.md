# LAN Chess

Terminal chess for two players on two computers on the same network, or two players sharing one
keyboard. It has a start menu, a live board, chess clocks, chat, draw offers, takebacks and
rematches, and it saves every finished game as a PGN file. It needs only Python 3.8 or newer.

```
 8  ♜  .  ♝  ♛  ♚  ♝  .  ♜   ● Black                               [5:08]
 7  .  ♟  ♟  ♟  .  ♟  ♟  ♟
 6  ♟  .  ♞  .  .  ♞  .  .    1. e4      e5        2. Nf3     Nc6
 5  .  .  .  .  ♟  .  .  .    3. Bb5     a6        4. Ba4     Nf6
 4  ♗  .  .  .  ♙  .  .  .    5. O-O
 3  .  .  .  .  .  ♘  .  .
 2  ♙  ♙  ♙  ♙  .  ♙  ♙  ♙
 1  ♖  ♘  ♗  ♕ [.] ♖ [♔] .     White                                5:12
    a  b  c  d  e  f  g  h

LAN Chess 1.0.0
Local game: two players share this keyboard. Time control 5+3 (5 min + 3 s per move); clocks start
  after the first move.
Type a move like e4, Nf3, exd5, O-O or e2e4 and press Enter.
 Black to move                                                                     Local game · 5+3
Black>
```

<sub>A local game with `--no-color` in a 100×30 terminal. With colours on, the squares are shaded
and the board is drawn larger.</sub>

**Contents:** [Install](#install) · [Play on two computers](#play-on-two-computers) ·
[Firewall](#firewall) · [Entering moves](#entering-moves) · [In-game commands](#in-game-commands) ·
[Keys](#keys) · [Menu and settings](#menu-and-settings) · [Time controls](#time-controls) ·
[Local game](#local-hot-seat-game) · [Saved games](#saved-games-pgn) ·
[Troubleshooting](#troubleshooting) · [Command-line reference](#command-line-reference) ·
[Development](#development)

## Install

Install LAN Chess on both computers, preferably the same version: the host turns away a player
whose game protocol differs.

### Ubuntu (20.04 or newer) and other Debian-based systems

Download `lanchess_1.0.0-1_all.deb` from the
[latest release](https://github.com/DeveshB-1/lanchess/releases/latest), then:

```sh
sudo apt install ./lanchess_1.0.0-1_all.deb
```

To remove it later, run `sudo apt remove lanchess`.

### Arch Linux and Arch-based systems

Download `lanchess-1.0.0-1-any.pkg.tar.zst` from the
[latest release](https://github.com/DeveshB-1/lanchess/releases/latest), then:

```sh
sudo pacman -U lanchess-1.0.0-1-any.pkg.tar.zst
```

Once LAN Chess is published on the AUR, you can install it with `yay -S lanchess` (or another AUR
helper) instead. To remove it, run `sudo pacman -R lanchess`.

### One-line installer (Ubuntu or Arch)

```sh
curl -fsSL https://raw.githubusercontent.com/DeveshB-1/lanchess/main/install.sh | sh
```

The script finds the right package for your system in the latest release, checks it against the
release's `SHA256SUMS` and installs it with `apt` or `pacman -U`, using sudo (or doas). To pick
the package type yourself, end the command with `| sh -s -- --method deb` (or `arch`); to install a
particular release, end it with `| LANCHESS_VERSION=1.0.0 sh`. On any other system the script stops
and prints the run-from-source command.

The packages install the `lanchess` command, a man page (`man lanchess`), a desktop menu entry, and
firewall profiles for ufw and firewalld (see [Firewall](#firewall)).

### From source (Linux, macOS, Windows)

You need Python 3.8 or newer and nothing else; LAN Chess uses only the standard library.

```sh
git clone https://github.com/DeveshB-1/lanchess
cd lanchess
python3 play.py
```

On Windows, type `py play.py`. Wherever this README says `lanchess`, type `python3 play.py`
(Windows: `py play.py`) instead, for example `python3 play.py host`. On Linux you can also put a
`lanchess` command in `~/.local/bin` with `make install PREFIX="$HOME/.local"` (undo it with
`make uninstall PREFIX="$HOME/.local"`).

## Play on two computers

Both computers must be on the same network, for example the same Wi-Fi.

1. **First computer:** run `lanchess`, choose **Host a game**, pick your colour and a time control,
   and choose **Start hosting**. The waiting screen shows this computer's IP addresses and, if it
   detects ufw or firewalld, the command that opens the game's ports.
2. **Second computer:** run `lanchess` and choose **Join a game**. Games on the network appear in
   the list after a moment. Select the host and press Enter. If it doesn't appear, choose
   **Enter address manually…** and type the IP address shown on the host's screen.
3. Play. The host's colour and time control apply to both players. When the game ends, `/rematch`
   offers another game with colours swapped. `/quit` returns to the menu, which shows the result.

The same from the command line:

```sh
lanchess host                       # first computer: prints its addresses and waits
lanchess host --time 5+3 --color random
lanchess join                       # second computer: searches the network and lists the games
lanchess join 192.168.1.23          # or connect straight to the host's IP address
lanchess join 192.168.1.23:6000     # a host that uses another port
```

`lanchess host` prints the exact `join` command for each of its addresses. Press Ctrl-C to stop
waiting.

## Firewall

The **host** must accept incoming **TCP 5555** (the game) and **UDP 5556** (network search). The
joining computer usually needs no changes. Joining by IP address needs only TCP 5555. If you host on
another port, open that TCP port instead. Network search always uses UDP 5556.

**Ubuntu (ufw).** Ubuntu's firewall is off by default; `sudo ufw status` shows whether it's active.
If it is, allow the profile the package installs:

```sh
sudo ufw allow lanchess
```

Without the package (or for another port), open the ports directly:

```sh
sudo ufw allow 5555/tcp && sudo ufw allow 5556/udp
```

**Arch Linux (firewalld).** Enable the service the package installs, permanently:

```sh
sudo firewall-cmd --permanent --add-service=lanchess && sudo firewall-cmd --reload
```

Without the package (or for another port), open the ports directly:

```sh
sudo firewall-cmd --permanent --add-port=5555/tcp --add-port=5556/udp && sudo firewall-cmd --reload
```

The command shown on the menu's waiting screen leaves out `--permanent`, so it only lasts until
firewalld reloads or the computer restarts. If you use ufw on Arch, the ufw commands above work
there too.

**macOS and Windows (from source):** allow Python when the system firewall asks the first time you
host. On Windows, also set the Wi-Fi network's profile to *Private*.

**Guest, hotel and public Wi-Fi** networks often isolate devices from each other, so LAN games
can't connect there whatever the firewall says. Use a home network or a phone hotspot instead.

**VPNs** can send local traffic into the tunnel or block it. Disconnect the VPN, or turn on its
"allow local network access" option.

## Entering moves

Type a move and press Enter. Standard notation (SAN) and coordinates both work, and capitals are
optional.

| Move | SAN | Coordinates |
|---|---|---|
| Pawn | `e4`, `d5` | `e2e4`, `e2-e4`, `e2 e4` |
| Piece | `Nf3`, `nf3`, `Bb5` | `g1f3` |
| Capture | `exd5`, `Nxe5`, or without the x: `ed5` | `e4d5`, `e4xd5` |
| Two pieces can reach the square | `Nbd2`, `R1e2` | `b1d2` |
| Castling | `O-O`, `O-O-O`, `0-0`, `o-o`, `OO` | the king's move: `e1g1`, `e1c1`, `e8g8`, `e8c8` |
| Promotion | `e8=Q`, `e8Q`, `exd8=R`, `e8=N` | `e7e8q`, `e7e8n` |

- A pawn that reaches the last rank becomes a queen unless you name another piece.
- Check marks are optional: `Qh4` and `Qh4#` both work.
- A lowercase `b` means a pawn first: `bxc3` is a pawn capture if one is possible, otherwise a
  bishop move. Type `B` to be sure.
- An ambiguous move such as `Nd2` (when both knights can go there) is rejected with the choices.
- `/moves` lists every legal move. Anything that doesn't start with `/` is read as a move, so chat
  with `/c hello`.

## In-game commands

| Command | What it does |
|---|---|
| `/c <msg>` (`/chat`, `/say`) | Chat with your opponent (up to 500 characters) |
| `/draw` | Offer a draw, or accept your opponent's offer |
| `/accept`, `/decline` | Answer a pending draw, takeback or rematch offer |
| `/takeback` (`/undo`) | Ask to take back your last move |
| `/resign` | Resign the game |
| `/rematch` | After the game ends, offer a rematch (colours swap, same time control) |
| `/flip` | Turn the board around |
| `/moves` | List the legal moves |
| `/fen` | Show the position as FEN |
| `/pgn` | Show the game so far as PGN |
| `/save [path]` | Save the game as a PGN file now (default: the games folder) |
| `/clear` | Clear the message log |
| `/quit` (`/exit`, `/q`) | Leave the game |
| `/help` (`?`) | Show the command list |

- **Draws and takebacks** need your opponent's `/accept`. Any move cancels pending offers. If your
  opponent has already replied to the move you take back, both moves are undone.
- **Automatic endings:** checkmate, stalemate, insufficient material, threefold repetition and the
  fifty-move rule end the game without anyone claiming them.
- **Leaving a game in progress:** `/quit` asks you to confirm; a second `/quit` (or Ctrl-C) within
  10 seconds resigns and leaves. After the game ends, `/quit` leaves at once.
- **Disconnects:** if the other player leaves or the connection drops, the game ends as abandoned
  (result `*`) and is still saved if any moves were played.

## Keys

In a game (full-screen view):

| Key | Action |
|---|---|
| Enter | Send the move or command |
| ↑ / ↓ | Recall earlier input |
| ← / →, Home / End, Ctrl-A / Ctrl-E | Move the cursor |
| Backspace / Delete | Delete a character |
| Ctrl-U / Ctrl-W | Clear the line / delete the previous word |
| PgUp / PgDn | Scroll the message log |
| Ctrl-L | Redraw the screen |
| Ctrl-C, or Ctrl-D on an empty line | Same as `/quit` |

In the menu:

| Key | Action |
|---|---|
| ↑ / ↓ (or j / k) | Move; on the help and waiting screens, scroll |
| Enter | Select, or go to the next field in a form |
| 1–6 | Jump to a main-menu item |
| ← / → (or Space) | Change a choice in a form |
| Tab | Next field |
| r | Search again (Join screen) |
| Esc or Ctrl-C (or q, outside text boxes) | Go back; on the main menu, quit |

## Menu and settings

Running `lanchess` with no command opens the start menu:

| Item | What it does |
|---|---|
| Host a game | Choose your colour, time control and port, then wait for the other player |
| Join a game | A live list of games on the network, your recent hosts, and manual address entry |
| Local game (same keyboard) | Choose a time control and whether the board turns, then play |
| Settings | Your defaults (below) |
| How to play | Moves, commands, keys and firewall help |
| Quit | Leave |

A game started from the menu returns to the menu when you `/quit`. With `--plain`, or when input
isn't a terminal, the menu is a numbered text menu instead: type `1`–`6` and press Enter.

**Settings:** your name (default: your login name), the default time control, the colour you play
when hosting, the port, chess symbols or letters for the pieces, colours on or off, whether the
board turns in local games, whether finished games are saved, and the games folder. The Join
screen also lists the last five hosts you joined.

Settings are saved in `config.json` here:

| System | Folder |
|---|---|
| Linux | `~/.config/lanchess/` (or `$XDG_CONFIG_HOME/lanchess/`) |
| macOS | `~/Library/Application Support/lanchess/` |
| Windows | `%APPDATA%\lanchess\` |

Set `LANCHESS_CONFIG_DIR` to use another folder. Command-line options override the saved settings
for one run, but the display options can only switch things on: if Settings say "Letters" or
colours "Off", change them back in Settings.

## Time controls

The time control is minutes per player plus seconds added after each move.

| Value | Meaning |
|---|---|
| `5+3` | 5 minutes each, plus 3 seconds per move (`5\|3` also works) |
| `10` | 10 minutes each, no increment |
| `0.5+0` | 30 seconds each |
| `none`, `0`, `off` | Untimed (the default) |

The menu offers Untimed, 1+0, 3+2, 5+3, 10+5, 15+10, 30+0 and Custom. On the command line, use
`--time` with `host` or `local`. The limits are 24 hours per player and a 10-minute increment.

- No clock runs until White's first move; then Black's clock starts.
- Clocks show `m:ss` (or `h:mm:ss`). Below 20 seconds they show tenths and the running clock turns
  red.
- When a clock reaches zero, that player loses, unless the opponent can't possibly checkmate (for
  example a lone king). Then the game is a draw.

## Local (hot-seat) game

Choose **Local game** in the menu, or run:

```sh
lanchess local --time 5+3
```

Two players share the keyboard, and whoever is to move types the move. The board turns to face the
side to move. Use `--no-flip`, the Local form's *Board* choice or the *Flip local board* setting
to keep White at the bottom, or `/flip` to turn it by hand. Here `/draw` ends the game as a draw at
once, `/takeback` undoes the last move at once, and `/resign` resigns for the side to move.

## Saved games (PGN)

Finished games are saved automatically to `~/lanchess_games/` (on Windows,
`C:\Users\<you>\lanchess_games\`), with names like `20261001-201530_alice-vs-bob.pgn`. Any chess
program or website can open them.

- A game that ends before any move is played isn't saved.
- Change the folder with the *Games folder* setting or `--pgn-dir DIR`. Turn saving off with the
  *Save games* setting or `--no-save`.
- `/save [path]` saves at any time, and `/pgn` shows the PGN in the message log.

## Troubleshooting

| Problem | What to try |
|---|---|
| Join doesn't list the host | Choose **Enter address manually…** (or run `lanchess join 192.168.1.23`) and type an IP the host shows. Network search can fail when the host's firewall blocks UDP 5556, when the joining computer's firewall drops the replies, when the router blocks broadcasts, or when the host used `--no-discovery`. Joining by IP needs only TCP 5555. |
| The host shows several IP addresses | Use the one on the same network as the other computer, usually the Wi-Fi address (often `192.168.x.x` or `10.x.x.x`). Others may belong to Docker, virtual machines or a VPN. |
| "Connection refused" | Nobody is hosting at that address yet, or the port is wrong. Start hosting first, and use `IP:PORT` if the host uses another port. |
| Connection times out, or the host is "unreachable" | The host's firewall is blocking TCP 5555 (see [Firewall](#firewall)), or the computers are on different networks, a guest network or a VPN. |
| "Port … is already in use" | Another LAN Chess game or another program uses that port. Pick another port on the Host form, in Settings, or with `lanchess host --port 6000`; the other player then joins `IP:6000`. |
| Colours or chess symbols look wrong | Use `--ascii` (letters instead of symbols), `--no-color`, or `--plain` (line-by-line output), or change *Pieces* and *Colours* in Settings. The `NO_COLOR` environment variable also turns colours off. Chess symbols need a font that has them, such as DejaVu Sans Mono. |
| "Terminal too small" | Make the window bigger; the menu needs at least 40×14. |
| The screen is garbled after resizing | Press Ctrl-L. |
| A move is rejected | Check whose turn it is. `/moves` lists the legal moves, and chat needs `/c`. |

## Command-line reference

```
lanchess [OPTIONS]                       open the menu
lanchess host  [--port N] [--name NAME] [--color white|black|random] [--time TC] [--fen FEN]
               [--no-discovery] [OPTIONS]
lanchess join  [HOST[:PORT]] [--port N] [--name NAME] [--scan-time SECONDS] [OPTIONS]
lanchess local [--time TC] [--fen FEN] [--no-flip] [OPTIONS]
```

| Option | Command | Meaning |
|---|---|---|
| `--port N` | host | TCP port to listen on (default 5555, or the port in Settings) |
| `--port N` | join | Port to use when `HOST` has no `:PORT` (default 5555, or the port in Settings) |
| `--name NAME` | host, join | The name your opponent sees (default: the name in Settings, else your login name) |
| `--color white\|black\|random` | host | The colour you play (default white, or the colour in Settings); also `--colour` |
| `--time TC` | host, local | Time control, such as `5+3` (default: untimed, or the one in Settings) |
| `--fen FEN` | host, local | Start from this position |
| `--no-discovery` | host | Don't answer network searches; the other player must type your IP |
| `HOST[:PORT]` | join | The host's IP address or name, optionally with a port. Leave it out to search the network. |
| `--scan-time SECONDS` | join | How long to search the network (default 2.5) |
| `--no-flip` | local | Keep White at the bottom instead of turning the board to the side to move |

These `OPTIONS` work with every command, before or after it:

| Option | Meaning |
|---|---|
| `--ascii` | Draw pieces as letters instead of chess symbols |
| `--no-color` (`--no-colour`) | No colours or other terminal styling |
| `--plain` | Simple line-by-line output instead of the full-screen view (also used automatically when input or output isn't a terminal) |
| `--pgn-dir DIR` | Folder for saved games (default `~/lanchess_games`) |
| `--no-save` | Don't save finished games |
| `--version` | Print the version and exit |
| `-h`, `--help` | Show help (`lanchess host --help` for a command's options) |

## Development

```sh
python3 -m unittest discover -s tests       # unit tests (or: make test)
make dist                                   # dist/: the .deb, the Arch package and SHA256SUMS
packaging/test_in_containers.sh             # install and test the packages in podman or docker
```

`make dist` builds the .deb in pure Python on any Linux system (no dpkg needed). The Arch package
needs `makepkg`, so it is built only on Arch (or in an `archlinux` container) as a regular user.
Releases are cut by pushing a `v` tag; [packaging/README-packaging.md](packaging/README-packaging.md)
covers the release workflow, the container tests and publishing to the AUR.
[docs/DESIGN.md](docs/DESIGN.md) is the original design spec.

| File | Contents |
|---|---|
| `lanchess/engine.py` | Chess rules, SAN and FEN |
| `lanchess/net.py` | Networking (JSON lines over TCP) and network search (UDP broadcast) |
| `lanchess/game.py` | Game session, clocks, commands and PGN |
| `lanchess/ui.py`, `lanchess/term.py` | Screen drawing; keyboard and terminal handling |
| `lanchess/menu.py`, `lanchess/config.py` | Start menu; saved settings |
| `lanchess/cli.py` | Command-line entry point |
| `play.py` | Runs LAN Chess from a source checkout |

## License

MIT. See [LICENSE](LICENSE).
