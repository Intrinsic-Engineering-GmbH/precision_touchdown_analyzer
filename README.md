# touchdown_analyzer

Python tooling for analyzing touchdown data.

## Requirements

- Python 3.10 or newer
- Git
- VS Code with the Python extension (recommended)

## Setup (Windows / PowerShell)

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements-dev.txt
pip install -e .
```

On Linux/macOS use `python3 -m venv .venv` and `source .venv/bin/activate`.

## Usage

```powershell
touchdown-analyzer --help
# or, without installing the console script:
python -m touchdown_analyzer --help
```

## Development

```powershell
pytest              # run the test suite
ruff check .        # lint
ruff format .       # format
mypy                # type check
```

## Layout

```
src/touchdown_analyzer/   package source (src layout)
tests/                    pytest test suite
tests/data/               small tracked test fixtures
data/                     local input data - not tracked by git
output/                   generated results - not tracked by git
```

Raw measurement data and generated output stay out of the repository (see
`.gitignore`). Only small fixtures needed by the tests belong in `tests/data/`.
