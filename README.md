CarryOn - Pack your Python script with its dependencies

# Installation

```bash
pip install carryon
```

Or better, since this is a command-line tool:
```bash
pipx install carryon
```

# Usage

```bash
carryon script.py
```

This creates a self-contained executable that includes all non-stdlib dependencies.

Options:
- `-o, --output` - Specify output file
- `-p, --packages` - Include complete packages
- `-f, --file FILE ARCNAME` - Add extra files to the bundle

# How it works

Carryon appends a ZIP archive with all dependencies to your script, making it
self-contained while still being a valid Python script.

Not a heavyweight solution like PyInstaller and other "checked luggage" tools.
It still requires a python interpreter.

The script portion can still be edited after packaging - just ensure your editor
preserves binary data and doesn't add a newline at EOF. For vim, use:
    vim -b script_packaged.py   # binary mode
