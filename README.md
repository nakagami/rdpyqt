# rdpyqt

Microsoft RDP client built with Python, [Twisted](https://twisted.org/) and [PyQt6](https://www.riverbankcomputing.com/software/pyqt/).

It is forked from https://github.com/citronneur/rdpy.
While rdpy has various commands, this project only includes the RDP client.

## Installation

```sh
pip install rdpyqt
```

H.264 デコード機能を使用する場合は、以下のようにインストールしてください:

```sh
pip install "rdpyqt[h264]"
```

## Usage

```sh
rdpyqt6 [options] ip[:port]
```

### Options

| Option  | Description                                   | Default              |
|---------|-----------------------------------------------|----------------------|
| `-u`    | Username                                      | (empty)              |
| `-p`    | Password                                      | (empty)              |
| `-d`    | Domain                                        | (empty)              |
| `-w`    | Width of screen                               | `1280`               |
| `-l`    | Height of screen                              | `1024`               |
| `-kt`   | Keyboard type (see values below)              | `IBM_101_102_KEYS`   |
| `-kl`   | Keyboard layout (see values below)            | `US`                 |
| `--swap-alt-meta` | Swap Alt and Meta (Windows/Super/Command) keys | (disabled)     |

#### `-kt` Keyboard Type values

| Value             | Description              |
|-------------------|--------------------------|
| `IBM_PC_XT_83_KEY`  | IBM PC/XT 83-key keyboard  |
| `OLIVETTI`          | Olivetti keyboard          |
| `IBM_PC_AT_84_KEY`  | IBM PC/AT 84-key keyboard  |
| `IBM_101_102_KEYS`  | IBM 101/102-key keyboard (most common) |
| `NOKIA_1050`        | Nokia 1050 keyboard        |
| `NOKIA_9140`        | Nokia 9140 keyboard        |
| `JAPANESE`          | Japanese keyboard          |

#### `-kl` Keyboard Layout values

| Value                | Language / Region        |
|----------------------|--------------------------|
| `ARABIC`             | Arabic                   |
| `BULGARIAN`          | Bulgarian                |
| `CHINESE_US_KEYBOARD`| Chinese (US keyboard)    |
| `CZECH`              | Czech                    |
| `DANISH`             | Danish                   |
| `GERMAN`             | German                   |
| `GREEK`              | Greek                    |
| `US`                 | English (United States)  |
| `SPANISH`            | Spanish                  |
| `FINNISH`            | Finnish                  |
| `FRENCH`             | French                   |
| `HEBREW`             | Hebrew                   |
| `HUNGARIAN`          | Hungarian                |
| `ICELANDIC`          | Icelandic                |
| `ITALIAN`            | Italian                  |
| `JAPANESE`           | Japanese                 |
| `KOREAN`             | Korean                   |
| `DUTCH`              | Dutch                    |
| `NORWEGIAN`          | Norwegian                |

Example:

```sh
rdpyqt6 -u user -p password -w 1920 -l 1080 rdp_server:3389
```
