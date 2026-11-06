# check_system

A read-only system check tool for Windows and Linux. It prints a quick status report for your computer: system info, CPU, memory, disks, network, and optionally processes, ports, startup items, and large or duplicate files. It also points out anything that needs attention.

By default it only reads information. It never changes files or system settings. The only time it writes a file is when you pass `--output`, and then it writes just the file you named.

中文版见 [README.md](README.md)。

## Requirements

- Python 3.8 or newer
- No third-party packages. It uses the Python standard library only.

## Quick start

```bash
python check_system.py
```

## Examples

```bash
python check_system.py --all                    # run every system check
python check_system.py --ports --processes      # only ports and memory-heavy programs
python check_system.py --folder-sizes ~/Downloads   # which subfolder takes the most space
python check_system.py --duplicates ~/Downloads     # find files with identical content
python check_system.py --json --output report.json # save a JSON report
python check_system.py --quiet                  # only show things that need attention
```

## What it checks

Shown by default: system info, CPU, memory, disks, and network.

| Option | What it checks |
|---|---|
| `--dns` | DNS servers in use |
| `--firewall` | Whether the firewall is on (needs administrator on Windows) |
| `--reboot` | Whether an update is waiting for a reboot |
| `--temp` | Space used by the temp folder |
| `--processes` | Programs using the most memory |
| `--ports` | Ports that are listening |
| `--connections` | Outbound connections that are established |
| `--startup` | Programs that start automatically |
| `--users` | Accounts that can log in |
| `--all` | All of the system checks above |
| `--large-files DIR` | Largest files in a folder |
| `--recent-files DIR` | Recently changed files in a folder |
| `--folder-sizes DIR` | Space used by each subfolder |
| `--duplicates DIR` | Files with identical content |

Other options:

- `--top N`: how many rows each ranking shows. Default is 10.
- `--json`: print JSON for other programs to read.
- `--quiet`: text output shows only the items that need attention.
- `--output FILE`: also save the report to a file.
- `--version`: print the version.

## Exit codes

- `0`: finished, nothing needs attention
- `1`: finished, but something needs attention
- `2`: the folder you gave does not exist

## What it does not do

- It does not delete anything. Duplicate files are listed so you can decide what to remove.
- It does not change firewall rules, services, startup items, accounts, or passwords.
- It does not connect to the internet or upload anything.
- It does not show file contents. Duplicate detection hashes files, but it never displays their content.

## Known limits

- Some checks need administrator rights, such as the firewall status. If a check can't run, it appears under "checks that could not finish" instead of being reported as fine.
- Without administrator rights, Windows hides the names of processes owned by other accounts. Those ports show only the PID.
- A folder scan stops after 200,000 files and prints a warning.
- Most testing so far has been on Windows. The Linux and macOS code paths have not been fully verified on real machines.

## Tests

```bash
python -m unittest -v test_check_system.py
```

## License

MIT License. See [LICENSE](LICENSE).
